"""Per-turn attribution: which model answered, and what the turn cost.

Two user-visible problems shared one root cause: the provider reported
usage and the resolved model name on its terminal event, and every layer
above it threw both away.

  1. **The chat showed no model and no token count.** The settings pane
     knows the *configured* model, but a turn can be answered by a later
     hop of the failover chain, so the configured name is not a reliable
     answer to "what am I talking to right now".

  2. **Streamed turns billed at zero.** ``_budget_record`` was only
     reached from the non-streaming paths, so the cost cap never moved
     for the path that actually serves chat.

This module pins the wire contract for both: the terminal ``stream_delta``
frame carries ``model`` and ``usage`` when the provider reported them, and
carries neither when it did not. Absence matters as much as presence: a
fabricated ``0 tokens`` reads as a measurement and would make the meter
untrustworthy in exactly the case where it is already blind.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.orchestrator import Orchestrator
from agents.turn_attribution import (
    accumulate_turn_usage as _accumulate_turn_usage,
    merge_turn_usage,
    model_of_llm_response as _model_of_llm_response,
)
from models.protocol import StreamDeltaPayload, TextResponsePayload
from models.skill_manifest import BrandProfile, SkillManifest

USAGE = {"input_tokens": 10341, "output_tokens": 275, "total_tokens": 10616}
MODEL = "gpt-5.6-sol"


def _skill(skill_id: str) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id, version="1.0.0", author="test",
        brand=BrandProfile(
            name=skill_id, primary_color="#000", logo_url="", icon_set="sf_symbols",
        ),
        description=f"{skill_id} skill",
        trigger_phrases=["hello"],
    )


def _make_orchestrator() -> Orchestrator:
    reg = MagicMock()
    reg.skills = {"notes_memory": _skill("notes_memory")}
    reg.find_skills_for_query = lambda q, top_k=5: []
    reg.get_tools_for_skills = lambda skills: []
    return Orchestrator(
        skill_registry=reg, send_to_client=AsyncMock(), daemons={},
        memory=None, vision_buffer=None, perception=None, learner=None,
    )


def _capture_sends(orch: Orchestrator) -> list[dict]:
    captured: list[dict] = []

    async def _send(session_id: str, msg: Any) -> None:
        dumped = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
        captured.append({
            "type": dumped.get("type"),
            "payload": dumped.get("payload") or {},
        })

    orch.send = _send
    return captured


async def _run_stream_turn(done_event: dict) -> list[dict]:
    """Drive one streaming turn whose provider emits ``done_event``."""
    orch = _make_orchestrator()
    orch.llm = MagicMock()
    orch.llm.available = True
    orch.llm.model_name = "configured-model-not-the-answer"

    async def _stream(messages, tools=None, **kwargs):
        yield {"type": "text_delta", "content": "Booked for 10am PST."}
        yield done_event

    orch.llm.chat_stream = _stream
    orch._route_prompt = AsyncMock(return_value=[])
    orch._ensure_core_skills = lambda x: x
    memory = MagicMock()
    memory.working_push = MagicMock()
    memory.episode_save = AsyncMock(return_value={})
    memory.log_execution = AsyncMock()
    orch.memory = memory

    sends = _capture_sends(orch)
    await orch.handle_command_stream(session_id="s-attr", text="book a meeting")
    return sends


def _terminal_frame(sends: list[dict]) -> dict:
    finals = [
        f["payload"] for f in sends
        if f["type"] == "stream_delta" and f["payload"].get("is_final")
    ]
    assert len(finals) == 1, f"expected exactly one terminal frame, got {len(finals)}"
    return finals[0]


class TestPayloadShape:
    def test_attribution_fields_default_to_absent(self):
        """A plain delta carries no attribution; nothing to attribute yet."""
        p = StreamDeltaPayload(delta="Hi", stream_id="x")
        assert p.model == ""
        assert p.usage == {}

    def test_attribution_survives_model_dump(self):
        """The fields must cross the wire, not just exist on the object."""
        dumped = StreamDeltaPayload(
            delta="", stream_id="x", is_final=True, model=MODEL, usage=USAGE,
        ).model_dump()
        assert dumped["model"] == MODEL
        assert dumped["usage"] == USAGE

    def test_usage_default_is_not_shared_between_instances(self):
        """``dict`` default via ``default_factory``. A shared mutable default
        would leak one turn's token count onto every later turn."""
        a = StreamDeltaPayload(delta="a")
        b = StreamDeltaPayload(delta="b")
        a.usage["input_tokens"] = 999
        assert b.usage == {}


class TestOrchestratorEmitsAttribution:
    @pytest.mark.asyncio
    async def test_terminal_frame_carries_model_and_usage(self):
        sends = await _run_stream_turn(
            {"type": "done", "model": MODEL, "usage": USAGE},
        )
        final = _terminal_frame(sends)
        assert final["model"] == MODEL
        assert final["usage"] == USAGE

    @pytest.mark.asyncio
    async def test_reports_the_answering_model_not_the_configured_one(self):
        """The failover chain can hop mid-turn. The frame must name the model
        that actually answered, or the display is worse than nothing."""
        sends = await _run_stream_turn(
            {"type": "done", "model": "failover-hop-3", "usage": USAGE},
        )
        assert _terminal_frame(sends)["model"] == "failover-hop-3"

    @pytest.mark.asyncio
    async def test_no_provider_usage_yields_empty_not_zero(self):
        """Providers that report nothing must produce an absent count, never
        a fabricated ``0`` that renders as a real measurement."""
        final = _terminal_frame(await _run_stream_turn({"type": "done"}))
        assert final["usage"] == {}
        assert final["model"] == ""

    @pytest.mark.asyncio
    async def test_streaming_deltas_carry_no_attribution(self):
        """Only the terminal frame is attributed; mid-stream frames would
        make the client render a count per token."""
        sends = await _run_stream_turn(
            {"type": "done", "model": MODEL, "usage": USAGE},
        )
        mid = [
            f["payload"] for f in sends
            if f["type"] == "stream_delta" and not f["payload"].get("is_final")
        ]
        assert mid, "expected at least one non-terminal delta"
        assert all(not f.get("usage") and not f.get("model") for f in mid)


class TestUsageAccumulator:
    """One user turn can span many LLM calls. The reported cost is the sum.

    Reporting only the final round is the failure mode that matters: a
    tool-heavy turn spends most of its tokens on the rounds that carry the
    tool results, so last-round-only would show a large turn as a small one.
    """

    def test_sums_across_rounds(self):
        total: dict = {}
        _accumulate_turn_usage(total, {"usage": {"input_tokens": 100, "output_tokens": 10}})
        _accumulate_turn_usage(total, {"usage": {"input_tokens": 900, "output_tokens": 40}})
        assert total == {
            "input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050,
        }

    def test_normalises_chat_completions_dialect(self):
        """chat-completions says prompt/completion; Responses says input/output."""
        total: dict = {}
        _accumulate_turn_usage(total, {"usage": {"prompt_tokens": 7, "completion_tokens": 3}})
        assert total == {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}

    def test_mixed_dialects_sum_into_one_total(self):
        """A turn can hop providers mid-flight, so both dialects can appear."""
        total: dict = {}
        _accumulate_turn_usage(total, {"usage": {"prompt_tokens": 10, "completion_tokens": 2}})
        _accumulate_turn_usage(total, {"usage": {"input_tokens": 5, "output_tokens": 1}})
        assert total == {"input_tokens": 15, "output_tokens": 3, "total_tokens": 18}

    def test_recomputes_total_rather_than_trusting_the_provider(self):
        """Some reasoning models fold reasoning tokens into BOTH
        ``output_tokens`` and ``total_tokens``. Summing the parts avoids
        double-counting them."""
        total: dict = {}
        _accumulate_turn_usage(
            total,
            {"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 999}},
        )
        assert total["total_tokens"] == 15

    def test_rounds_without_usage_do_not_create_keys(self):
        """An empty total is how the UI tells "not reported" from "cost zero"."""
        total: dict = {}
        _accumulate_turn_usage(total, {"choices": []})
        _accumulate_turn_usage(total, {"usage": {}})
        _accumulate_turn_usage(total, {"usage": {"input_tokens": 0, "output_tokens": 0}})
        _accumulate_turn_usage(total, None)
        _accumulate_turn_usage(total, "not a dict")
        assert total == {}

    def test_unreported_round_does_not_erase_a_reported_one(self):
        total: dict = {}
        _accumulate_turn_usage(total, {"usage": {"input_tokens": 5, "output_tokens": 5}})
        _accumulate_turn_usage(total, {"choices": []})
        assert total == {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}

    def test_model_of_response_prefers_the_echoed_name(self):
        assert _model_of_llm_response({"model": "gpt-5.6-sol-2026-04-01"}) == "gpt-5.6-sol-2026-04-01"
        assert _model_of_llm_response({}) == ""
        assert _model_of_llm_response(None) == ""


class TestNonStreamingPathEmitsAttribution:
    """``features.streaming`` defaults to False, so this is the path a
    fresh install actually serves chat from. It needs attribution at least
    as much as the streaming path, and it was the half most likely to be
    forgotten precisely because the developer profile has streaming on."""

    def test_payload_carries_attribution(self):
        dumped = TextResponsePayload(
            text="ok", model=MODEL, usage=USAGE,
        ).model_dump()
        assert dumped["model"] == MODEL
        assert dumped["usage"] == USAGE

    def test_payload_defaults_to_absent(self):
        dumped = TextResponsePayload(text="ok").model_dump()
        assert dumped["model"] == ""
        assert dumped["usage"] == {}

    @pytest.mark.asyncio
    async def test_send_text_passes_attribution_through_to_the_frame(self):
        from agents.response_delivery import send_text

        orch = _make_orchestrator()
        sends = _capture_sends(orch)
        await send_text(orch, "s-ns", "Booked.", model=MODEL, usage=USAGE)

        frame = next(f for f in sends if f["type"] == "text_response")
        assert frame["payload"]["model"] == MODEL
        assert frame["payload"]["usage"] == USAGE

    @pytest.mark.asyncio
    async def test_status_sends_stay_unattributed(self):
        """Errors, acks and status lines are not answers; attributing them
        would put a token count under text that cost nothing to produce."""
        from agents.response_delivery import send_text

        orch = _make_orchestrator()
        sends = _capture_sends(orch)
        await send_text(orch, "s-ns", "Cancelled `foo`.")

        frame = next(f for f in sends if f["type"] == "text_response")
        assert frame["payload"]["model"] == ""
        assert frame["payload"]["usage"] == {}

    @pytest.mark.asyncio
    async def test_tool_loop_reports_the_sum_of_all_rounds(self):
        """End-to-end through the real tool loop, not just the helper.

        Round 1 calls a tool and round 2 answers. Both rounds are billed to
        the user, so the frame must show 1,200 tokens, not round 2's 200.
        """
        orch = _make_orchestrator()
        orch.llm = MagicMock()
        orch.llm.available = True
        orch.llm.model_name = "configured-model-not-the-answer"
        orch._route_prompt = AsyncMock(return_value=[])
        orch._ensure_core_skills = lambda x: x
        orch._streaming_enabled = False

        rounds = [
            {
                "model": "cheap-tier",
                "usage": {"input_tokens": 800, "output_tokens": 200},
                "choices": [{"message": {"role": "assistant", "tool_calls": [
                    {"id": "c1", "type": "function", "function": {
                        "name": "notes_memory__default", "arguments": "{}",
                    }},
                ]}}],
            },
            {
                "model": "escalated-tier",
                "usage": {"input_tokens": 150, "output_tokens": 50},
                "choices": [{"message": {"role": "assistant", "content": "Done."}}],
            },
        ]
        calls = {"n": 0}

        async def _call(*_a, **_kw):
            r = rounds[min(calls["n"], len(rounds) - 1)]
            calls["n"] += 1
            return r

        orch._call_llm_chat = _call
        orch.llm.extract_response = MagicMock(side_effect=lambda r: (
            r["choices"][0]["message"].get("content", ""),
            [
                {"id": t["id"], "name": t["function"]["name"], "arguments": {}}
                for t in r["choices"][0]["message"].get("tool_calls", []) or []
            ],
        ))
        orch._execute_tool_call_for_llm = AsyncMock(return_value={"success": True})

        memory = MagicMock()
        memory.working_push = MagicMock()
        memory.episode_save = AsyncMock(return_value={})
        memory.log_execution = AsyncMock()
        orch.memory = memory

        sends = _capture_sends(orch)
        await orch.handle_command(session_id="s-sum", text="save a note")

        frame = next(f for f in sends if f["type"] == "text_response")
        assert calls["n"] == 2, "expected a two-round turn"
        assert frame["payload"]["usage"] == {
            "input_tokens": 950, "output_tokens": 250, "total_tokens": 1200,
        }
        # The model that produced the FINAL answer, not the first round's.
        assert frame["payload"]["model"] == "escalated-tier"


class TestMultiAgentPathEmitsAttribution:
    """``features.multi_agent`` defaults to True, and the multi-agent branch
    runs BEFORE the single-agent loop and returns.

    So on a default profile this is the path that answers, and it reaches
    the client through ``_try_send_sdui`` rather than the tool loop that
    records usage. Wiring only the loop would have left the default install
    showing nothing, which is the failure this class exists to prevent.
    """

    def test_worker_result_defaults_to_absent(self):
        from agents.multi_agent import WorkerResult

        r = WorkerResult(worker_id="general")
        assert r.usage == {}
        assert r.model == ""

    def test_worker_usage_is_not_shared_between_results(self):
        from agents.multi_agent import WorkerResult

        a = WorkerResult(worker_id="a")
        b = WorkerResult(worker_id="b")
        a.usage["input_tokens"] = 5
        assert b.usage == {}

    def test_merge_sums_worker_tallies(self):
        """Parallel strategy bills every worker, not just the one whose
        text survives the merge."""
        total: dict = {}
        merge_turn_usage(total, {"input_tokens": 100, "output_tokens": 20})
        merge_turn_usage(total, {"input_tokens": 300, "output_tokens": 40})
        assert total == {
            "input_tokens": 400, "output_tokens": 60, "total_tokens": 460,
        }

    def test_merge_ignores_empty_and_missing_tallies(self):
        total: dict = {}
        merge_turn_usage(total, {})
        merge_turn_usage(total, None)
        assert total == {}

    def test_router_starts_with_no_usage(self):
        from agents.multi_agent import AgentRouter

        assert AgentRouter(llm=None).last_usage == {}

    @pytest.mark.asyncio
    async def test_router_keyword_fastpath_does_not_bill_a_stale_tally(self):
        """``route`` returns early on the keyword guards without calling the
        classifier. A tally left over from a previous turn must not be
        attributed to this one."""
        from agents.multi_agent import AgentRouter

        router = AgentRouter(llm=None)
        router.last_usage = {"input_tokens": 999, "output_tokens": 999}
        out = await router.route("write me an html file")
        assert out["workers"] == ["general"]
        assert router.last_usage == {}

    def test_pop_attribution_consumes_so_a_later_turn_cannot_reuse_it(self):
        from agents.multi_agent import MultiAgentOrchestrator

        ma = MultiAgentOrchestrator.__new__(MultiAgentOrchestrator)
        ma._turn_attribution = {}
        ma._stash_turn_attribution("s1", MODEL, {"input_tokens": 5, "output_tokens": 5})

        first = ma.pop_turn_attribution("s1")
        assert first["model"] == MODEL
        assert first["usage"] == {"input_tokens": 5, "output_tokens": 5}
        # Second read is empty: a turn that reported nothing must show
        # nothing, not the previous turn's numbers.
        assert ma.pop_turn_attribution("s1") == {}

    def test_pop_attribution_is_session_scoped(self):
        from agents.multi_agent import MultiAgentOrchestrator

        ma = MultiAgentOrchestrator.__new__(MultiAgentOrchestrator)
        ma._turn_attribution = {}
        ma._stash_turn_attribution("s1", "m1", {"input_tokens": 1, "output_tokens": 1})
        assert ma.pop_turn_attribution("s2") == {}
        assert ma.pop_turn_attribution("s1")["model"] == "m1"

    @pytest.mark.asyncio
    async def test_try_send_sdui_passes_attribution_to_the_text_fallback(self):
        from agents.response_delivery import try_send_sdui

        orch = _make_orchestrator()
        sends = _capture_sends(orch)
        await try_send_sdui(orch, "s-ma", "Plain prose, not JSON.", model=MODEL, usage=USAGE)

        frame = next(f for f in sends if f["type"] == "text_response")
        assert frame["payload"]["model"] == MODEL
        assert frame["payload"]["usage"] == USAGE

    @pytest.mark.asyncio
    async def test_multi_agent_turn_reaches_the_client_attributed(self):
        """End-to-end through the orchestrator's multi-agent branch."""
        orch = _make_orchestrator()
        orch.llm = MagicMock()
        orch.llm.available = True
        orch._multi_agent_enabled = True
        orch._streaming_enabled = False

        ma = MagicMock()
        ma.run = AsyncMock(return_value="Booked for 10am PST.")
        ma.pop_turn_attribution = MagicMock(return_value={
            "model": "worker-model", "usage": {"input_tokens": 400, "output_tokens": 60},
        })
        orch._multi_agent = ma

        memory = MagicMock()
        memory.working_push = MagicMock()
        memory.episode_save = AsyncMock(return_value={})
        memory.log_execution = AsyncMock()
        orch.memory = memory

        sends = _capture_sends(orch)
        await orch.handle_command(session_id="s-ma2", text="book a meeting")

        frame = next(f for f in sends if f["type"] == "text_response")
        assert frame["payload"]["model"] == "worker-model"
        assert frame["payload"]["usage"] == {"input_tokens": 400, "output_tokens": 60}

    @pytest.mark.asyncio
    async def test_a_multi_agent_without_attribution_support_still_answers(self):
        """Regression: attribution must never cost the user their reply.

        The multi-agent orchestrator is swappable, so ``pop_turn_attribution``
        may be missing or may return something that is not a
        ``{model, usage}`` dict. Reading it naively raised inside the turn,
        which the caller caught as "Multi-agent failed" and degraded to the
        single-agent path: a cosmetic label silently changing which engine
        answered the user.
        """
        for bad in [
            MagicMock(),                       # returns a MagicMock, not a dict
            MagicMock(return_value=None),
            MagicMock(return_value="nonsense"),
            MagicMock(return_value={"model": 42, "usage": "no"}),
            MagicMock(side_effect=RuntimeError("boom")),
        ]:
            orch = _make_orchestrator()
            orch.llm = MagicMock()
            orch.llm.available = True
            orch._multi_agent_enabled = True
            orch._streaming_enabled = False

            ma = MagicMock()
            ma.run = AsyncMock(return_value="Booked for 10am PST.")
            if isinstance(bad, MagicMock) and bad._mock_name is None:
                ma.pop_turn_attribution = bad
            orch._multi_agent = ma

            memory = MagicMock()
            memory.working_push = MagicMock()
            memory.episode_save = AsyncMock(return_value={})
            memory.log_execution = AsyncMock()
            orch.memory = memory

            sends = _capture_sends(orch)
            await orch.handle_command(session_id="s-bad", text="book a meeting")

            frame = next(f for f in sends if f["type"] == "text_response")
            # The answer still lands, unattributed rather than absent.
            assert frame["payload"]["text"] == "Booked for 10am PST."
            assert frame["payload"]["model"] == ""
            assert frame["payload"]["usage"] == {}

    def test_missing_method_is_tolerated(self):
        orch = _make_orchestrator()
        orch._multi_agent = object()
        assert orch._pop_multi_agent_attribution("s") == ("", {})

    def test_attribution_map_is_bounded(self):
        """A turn that produces no text never reaches the pop, so the map
        must not grow once per session for the life of the process."""
        from agents.multi_agent import MultiAgentOrchestrator

        ma = MultiAgentOrchestrator.__new__(MultiAgentOrchestrator)
        ma._turn_attribution = {}
        cap = MultiAgentOrchestrator._ATTRIBUTION_MAX_SESSIONS
        for i in range(cap * 3):
            ma._stash_turn_attribution(f"s{i}", "m", {"input_tokens": 1, "output_tokens": 1})
        assert len(ma._turn_attribution) <= cap
        # The most recent session is the one still worth answering for.
        assert f"s{cap * 3 - 1}" in ma._turn_attribution

    def test_restashing_the_same_session_does_not_evict(self):
        from agents.multi_agent import MultiAgentOrchestrator

        ma = MultiAgentOrchestrator.__new__(MultiAgentOrchestrator)
        ma._turn_attribution = {}
        for _ in range(MultiAgentOrchestrator._ATTRIBUTION_MAX_SESSIONS * 2):
            ma._stash_turn_attribution("same", "m", {"input_tokens": 1, "output_tokens": 1})
        assert list(ma._turn_attribution) == ["same"]
