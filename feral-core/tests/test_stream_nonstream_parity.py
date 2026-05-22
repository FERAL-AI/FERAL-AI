"""Lane 08 WS3 — stream / non-stream parity.

AUDIT-r14 finding 20 fix #3 (and audit-r13 cross-pointer): the two
orchestrator code paths drifted. The streaming branch lacked
multi-agent routing, paused-thought re-thread, and the same memory
write semantics; the non-stream branch dispatched tools differently.
The audit scored this 3/5 on response delivery.

This module pins the parity contract:

  1. **Same final assistant text.** Same prompt + LLM response →
     ``accumulated_text`` (stream) == ``text_content`` (non-stream).

  2. **Same tool dispatch sequence.** Same LLM tool_call response →
     both paths call ``_execute_tool_call_for_llm`` for each tool in
     the same order; both emit ``tool_start`` / ``tool_result`` WS
     frames in tool-call index order.

  3. **Same memory write semantics.** Both paths call
     ``memory.working_push`` with ``role=assistant`` carrying the
     same text body (clipped to 300 chars).

  4. **Same multi-agent precedence.** When ``FERAL_MULTI_AGENT`` is
     on, both paths hand the turn to the multi-agent orchestrator
     before falling through to single-agent — used to only work for
     non-stream.

  5. **Same paused-thoughts re-thread.** When the session has paused
     thought fragments, both paths thread them into
     ``conversation_history`` before the LLM call.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.orchestrator import Orchestrator
from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest


def _skill(skill_id: str, triggers: list[str]) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id, version="1.0.0", author="test",
        brand=BrandProfile(name=skill_id, primary_color="#000", logo_url="", icon_set="sf_symbols"),
        description=f"{skill_id} skill",
        trigger_phrases=triggers,
        endpoints=[
            SkillEndpoint(
                id="default", method="POST", url=f"https://x/{skill_id}",
                description="x", returns_description="x", ui_hint="detail_card",
            )
        ],
    )


SKILLS = {
    "notes_memory": _skill("notes_memory", ["my notes", "save a note"]),
    "calendar_google": _skill("calendar_google", ["calendar", "my agenda"]),
    "weather_current": _skill("weather_current", ["what's the weather"]),
}


def _make_orchestrator() -> Orchestrator:
    reg = MagicMock()
    reg.skills = SKILLS
    reg.find_skills_for_query = lambda q, top_k=5: list(SKILLS.values())
    reg.get_tools_for_skills = lambda skills: [
        {
            "type": "function",
            "function": {"name": f"{s.skill_id}__default", "description": "", "parameters": {}},
        }
        for s in skills
    ]
    orch = Orchestrator(
        skill_registry=reg, send_to_client=AsyncMock(), daemons={},
        memory=None, vision_buffer=None, perception=None, learner=None,
    )
    return orch


def _capture_sends(orch: Orchestrator) -> list[dict]:
    """Replace ``orch.send`` with a recorder that returns the raw
    payloads of every outbound WS frame for byte-level diffing.

    ``msg.model_dump()`` returns the full FeralMessage envelope
    (``msg_id``, ``session_id``, ``type``, ``payload``). We flatten
    so callers can read ``frame["payload"]["text"]`` directly without
    walking through the outer envelope.
    """
    captured: list[dict] = []

    async def _send(session_id: str, msg: Any) -> None:
        if hasattr(msg, "model_dump"):
            dumped = msg.model_dump()
        else:
            dumped = dict(msg)
        captured.append({
            "type": dumped.get("type") or getattr(msg, "type", None),
            "payload": dumped.get("payload") or {},
            "session_id": dumped.get("session_id", session_id),
        })

    orch.send = _send
    return captured


class TestParityChatOnlyTurn:
    """No tool calls; LLM returns a plain text answer in both shapes."""

    @pytest.mark.asyncio
    async def test_same_assistant_text_both_paths(self):
        canonical_text = "Yesterday you wrote 4 notes and had 2 meetings."

        # ── Non-stream orchestrator ─────────────────────────────
        orch_a = _make_orchestrator()
        orch_a.llm = MagicMock()
        orch_a.llm.available = True
        orch_a.llm.model_name = "test-model"
        orch_a.llm.chat_with_failover = AsyncMock(
            return_value={
                "choices": [
                    {"message": {"role": "assistant", "content": canonical_text}}
                ]
            }
        )
        orch_a.llm.extract_response = MagicMock(return_value=(canonical_text, []))
        orch_a._route_prompt = AsyncMock(return_value=[])
        orch_a._ensure_core_skills = lambda x: x
        memory_a = MagicMock()
        memory_a.working_push = MagicMock()
        memory_a.episode_save = AsyncMock(return_value={})
        memory_a.log_execution = AsyncMock()
        orch_a.memory = memory_a

        sends_a = _capture_sends(orch_a)
        await orch_a.handle_command(session_id="s-aaa", text="What did I do yesterday?")

        # ── Stream orchestrator (separate instance, same setup) ─
        orch_b = _make_orchestrator()
        orch_b.llm = MagicMock()
        orch_b.llm.available = True
        orch_b.llm.model_name = "test-model"

        async def stream_canonical(messages, tools=None, **kwargs):
            # Fragment the canonical text into deltas — the stream
            # path's ``accumulated_text`` must end up == canonical.
            for piece in [canonical_text[:10], canonical_text[10:30], canonical_text[30:]]:
                yield {"type": "text_delta", "content": piece}
            yield {"type": "done"}

        orch_b.llm.chat_stream = stream_canonical
        orch_b._route_prompt = AsyncMock(return_value=[])
        orch_b._ensure_core_skills = lambda x: x
        memory_b = MagicMock()
        memory_b.working_push = MagicMock()
        memory_b.episode_save = AsyncMock(return_value={})
        memory_b.log_execution = AsyncMock()
        orch_b.memory = memory_b

        sends_b = _capture_sends(orch_b)
        await orch_b.handle_command_stream(session_id="s-bbb", text="What did I do yesterday?")

        # ── Parity contract 1: same final assistant text ────────
        # Non-stream emits exactly one ``text_response`` carrying the
        # full answer. Stream emits N ``stream_delta`` frames whose
        # concatenation == the same answer.
        non_stream_text = next(
            (f["payload"].get("text", "") for f in sends_a if f["type"] == "text_response"),
            None,
        )
        assert non_stream_text == canonical_text

        stream_deltas = [
            f["payload"].get("delta", "")
            for f in sends_b
            if f["type"] == "stream_delta"
            and not f["payload"].get("is_final", False)
        ]
        stream_text = "".join(stream_deltas)
        assert stream_text == canonical_text
        assert stream_text == non_stream_text

        # ── Parity contract 3: same memory write semantics ──────
        # Both paths working_push the assistant turn with role=assistant
        # and the same text payload (clipped to 300 chars).
        non_stream_assistant_pushes = [
            call.args[1] for call in memory_a.working_push.call_args_list
            if call.args[1].get("role") == "assistant"
        ]
        stream_assistant_pushes = [
            call.args[1] for call in memory_b.working_push.call_args_list
            if call.args[1].get("role") == "assistant"
        ]
        assert non_stream_assistant_pushes == stream_assistant_pushes
        assert non_stream_assistant_pushes == [{"role": "assistant", "text": canonical_text[:300]}]


class TestParityToolDispatch:
    """One LLM turn with two tool_calls → both paths execute them in
    the SAME order with the SAME WS frame sequence.
    """

    @pytest.mark.asyncio
    async def test_tool_dispatch_order_matches(self):
        tool_calls = [
            {"id": "tc-1", "name": "notes_memory__default", "args": {"q": "yesterday"}},
            {"id": "tc-2", "name": "calendar_google__default", "args": {"day": "yesterday"}},
        ]

        async def fake_tool_run(session_id, tc, available_skills):
            return {"success": True, "data": {"tool": tc["name"]}}

        # Non-stream — two iterations: first turn returns tool_calls,
        # second turn (after results threaded back) returns final text.
        orch_a = _make_orchestrator()
        orch_a.llm = MagicMock()
        orch_a.llm.available = True
        orch_a.llm.model_name = "test-model"
        responses = [
            {
                "choices": [{
                    "message": {
                        "role": "assistant", "content": "",
                        "tool_calls": [
                            {"id": tc["id"], "type": "function",
                             "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])}}
                            for tc in tool_calls
                        ],
                    }
                }]
            },
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "Done."}}
                ]
            },
        ]
        orch_a.llm.chat_with_failover = AsyncMock(side_effect=responses)
        # The LLM's extract_response mirrors how the orchestrator
        # interprets the responses — first turn returns tool_calls,
        # second returns the final text.
        extract_results = [
            ("", tool_calls),
            ("Done.", []),
        ]
        orch_a.llm.extract_response = MagicMock(side_effect=extract_results)
        orch_a._route_prompt = AsyncMock(return_value=[])
        orch_a._ensure_core_skills = lambda x: x
        orch_a._execute_tool_call_for_llm = AsyncMock(side_effect=fake_tool_run)
        orch_a._try_genui_for_result = AsyncMock()
        orch_a.memory = MagicMock()
        orch_a.memory.working_push = MagicMock()
        orch_a.memory.log_execution = AsyncMock()
        orch_a.memory.episode_save = AsyncMock(return_value={})

        sends_a = _capture_sends(orch_a)
        await orch_a.handle_command(session_id="s-aaa", text="What did I do yesterday?")

        # Stream — same response shape via chat_stream.
        orch_b = _make_orchestrator()
        orch_b.llm = MagicMock()
        orch_b.llm.available = True
        orch_b.llm.model_name = "test-model"

        stream_calls = iter([1, 2])

        async def stream_response(messages, tools=None, **kwargs):
            call_no = next(stream_calls)
            if call_no == 1:
                for tc in tool_calls:
                    yield {"type": "tool_call_delta", "tool_call": tc}
                yield {"type": "done"}
            else:
                yield {"type": "text_delta", "content": "Done."}
                yield {"type": "done"}

        orch_b.llm.chat_stream = stream_response
        orch_b._route_prompt = AsyncMock(return_value=[])
        orch_b._ensure_core_skills = lambda x: x
        orch_b._execute_tool_call_for_llm = AsyncMock(side_effect=fake_tool_run)
        orch_b._try_genui_for_result = AsyncMock()
        orch_b.memory = MagicMock()
        orch_b.memory.working_push = MagicMock()
        orch_b.memory.log_execution = AsyncMock()
        orch_b.memory.episode_save = AsyncMock(return_value={})

        sends_b = _capture_sends(orch_b)
        await orch_b.handle_command_stream(session_id="s-bbb", text="What did I do yesterday?")

        # ── Parity: same tool calls in same order ───────────────
        non_stream_tool_order = [
            call.args[1]["name"]
            for call in orch_a._execute_tool_call_for_llm.call_args_list
        ]
        stream_tool_order = [
            call.args[1]["name"]
            for call in orch_b._execute_tool_call_for_llm.call_args_list
        ]
        assert non_stream_tool_order == stream_tool_order
        assert non_stream_tool_order == ["notes_memory__default", "calendar_google__default"]

        # ── Parity: tool_start / tool_result frame order ────────
        def _tool_frame_seq(sends):
            return [
                (f["type"], f["payload"].get("tool") or f["payload"].get("call_id"))
                for f in sends
                if f["type"] in ("tool_start", "tool_result")
            ]

        a_seq = _tool_frame_seq(sends_a)
        b_seq = _tool_frame_seq(sends_b)
        assert a_seq == b_seq, (
            f"tool frame sequence mismatch:\n  non-stream: {a_seq}\n  stream:     {b_seq}"
        )


class TestParityPausedThoughts:
    """When a paused thought is registered, both paths re-thread it
    before the LLM call. The stream path used to drop it silently.
    """

    @pytest.mark.asyncio
    async def test_paused_thought_in_history_in_both_paths(self):
        canonical_text = "Continuing from where I left off."

        # Non-stream
        orch_a = _make_orchestrator()
        orch_a.llm = MagicMock()
        orch_a.llm.available = True
        orch_a.llm.model_name = "test-model"
        captured_messages_a: list[list[dict]] = []

        async def chat_recording(messages, tools=None, **_kwargs):
            captured_messages_a.append(list(messages))
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": canonical_text}}
                ]
            }

        orch_a.llm.chat_with_failover = AsyncMock(side_effect=chat_recording)
        orch_a.llm.extract_response = MagicMock(return_value=(canonical_text, []))
        orch_a._route_prompt = AsyncMock(return_value=[])
        orch_a._ensure_core_skills = lambda x: x

        orch_a.register_paused_thought(
            session_id="s-aaa", thought_id="t-1",
            text="I was about to say the project is on track",
        )
        await orch_a.handle_command(session_id="s-aaa", text="continue please")

        assert captured_messages_a, "no LLM call recorded"
        history_a = captured_messages_a[0]
        assert any(
            m.get("role") == "assistant" and "[RESUMED THOUGHT]" in (m.get("content") or "")
            for m in history_a
        ), "non-stream did not re-thread the paused thought"

        # Stream
        orch_b = _make_orchestrator()
        orch_b.llm = MagicMock()
        orch_b.llm.available = True
        orch_b.llm.model_name = "test-model"
        captured_messages_b: list[list[dict]] = []

        async def stream_recording(messages, tools=None, **_kwargs):
            captured_messages_b.append(list(messages))
            yield {"type": "text_delta", "content": canonical_text}
            yield {"type": "done"}

        orch_b.llm.chat_stream = stream_recording
        orch_b._route_prompt = AsyncMock(return_value=[])
        orch_b._ensure_core_skills = lambda x: x

        orch_b.register_paused_thought(
            session_id="s-bbb", thought_id="t-1",
            text="I was about to say the project is on track",
        )
        await orch_b.handle_command_stream(session_id="s-bbb", text="continue please")

        assert captured_messages_b, "no stream LLM call recorded"
        history_b = captured_messages_b[0]
        assert any(
            m.get("role") == "assistant" and "[RESUMED THOUGHT]" in (m.get("content") or "")
            for m in history_b
        ), "stream did not re-thread the paused thought"


class TestParityMultiAgentPrePath:
    """When ``FERAL_MULTI_AGENT`` is enabled the multi-agent
    orchestrator runs FIRST. Used to apply only to the non-stream
    branch."""

    @pytest.mark.asyncio
    async def test_multi_agent_runs_in_both_paths(self):
        multi_agent_text = "(multi-agent result)"

        for handler_name in ("handle_command", "handle_command_stream"):
            orch = _make_orchestrator()
            orch.llm = MagicMock()
            orch.llm.available = True
            orch.llm.model_name = "test-model"
            orch._multi_agent_enabled = True
            ma = MagicMock()
            ma.run = AsyncMock(return_value=multi_agent_text)
            orch._multi_agent = ma
            # Ensure single-agent path would never be reached: if it
            # were, this AsyncMock raises.
            orch.llm.chat_with_failover = AsyncMock(side_effect=AssertionError(
                "single-agent path triggered in multi-agent mode"))

            async def stream_should_not_be_called(*a, **kw):
                raise AssertionError("single-agent stream path triggered in multi-agent mode")
                yield

            orch.llm.chat_stream = stream_should_not_be_called
            orch.memory = MagicMock()
            orch.memory.working_push = MagicMock()
            orch.memory.episode_save = AsyncMock(return_value={})

            _capture_sends(orch)  # records to orch._captured_sends; we don't read it here
            handler = getattr(orch, handler_name)
            await handler(session_id=f"s-{handler_name}", text="anything")

            ma.run.assert_awaited_once()
            # working_push got the assistant turn from the multi-agent
            # output — same shape as the single-agent path.
            assistant_pushes = [
                call.args[1] for call in orch.memory.working_push.call_args_list
                if call.args[1].get("role") == "assistant"
            ]
            assert assistant_pushes == [
                {"role": "assistant", "text": multi_agent_text[:300]}
            ], f"{handler_name} did not write assistant turn from multi-agent output"
