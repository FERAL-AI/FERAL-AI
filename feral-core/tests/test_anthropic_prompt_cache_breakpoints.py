"""Anthropic prompt-cache breakpoints on the Messages API body.

Source (fetched 2026-08-19):

* https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
  "Manage screenshot history for prompt caching":
    "Place one ``cache_control`` breakpoint after the system prompt and
     tool definitions, and up to three more on the most recent
     ``tool_result`` blocks, advancing them each turn."
    "Prune old screenshots in *batches*, not one each turn ... keep the
     last three screenshots and prune every 25 turns, so the prefix
     stays byte-identical between prune events."
* https://platform.claude.com/docs/en/build-with-claude/prompt-caching.md
  for the mechanics: render order ``tools`` -> ``system`` -> ``messages``
  (so a breakpoint on the last system block caches tools+system), a hard
  ceiling of 4 breakpoints per request (a 5th is a 400), and a 20-block
  lookback when matching a prior write.

The batch screenshot pruner in ``agents/multimodal_blocks.py`` exists
solely to keep the prompt prefix byte-stable between prune events. That
is worth nothing without a breakpoint to cache against, which is what
this covers.

Bodies are constructed and inspected in-process. No Anthropic request is
made.
"""

from __future__ import annotations

import json

import pytest

from agents.llm_provider import LLMProvider
from agents.multimodal_blocks import (
    SCREENSHOT_KEEP_LAST_DEFAULT,
    SCREENSHOT_PRUNE_EVERY_DEFAULT,
)


TOOLS = [
    {"type": "function", "function": {
        "name": "screen_capture__grab", "description": "grab",
        "parameters": {"type": "object", "properties": {}},
    }},
]


def _build(messages, tools=TOOLS, model="claude-opus-5"):
    return LLMProvider._build_anthropic_body(
        model, messages, tools, 0.7, 1024,
    )


def _breakpoints(body: dict) -> list[str]:
    """Every ``cache_control`` in the body, labelled by where it sits."""
    found: list[str] = []
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and "cache_control" in tool:
            found.append(f"tool:{tool.get('name')}")
    system = body.get("system")
    if isinstance(system, list):
        for i, block in enumerate(system):
            if isinstance(block, dict) and "cache_control" in block:
                found.append(f"system[{i}]")
    for mi, msg in enumerate(body.get("messages") or []):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if isinstance(block, dict) and "cache_control" in block:
                found.append(f"messages[{mi}].{block.get('type')}[{bi}]")
    return found


def _conversation(rounds: int) -> list[dict]:
    """A screenshot agent loop: N tool_use / tool_result rounds."""
    msgs: list[dict] = [
        {"role": "system", "content": "You are FERAL."},
        {"role": "user", "content": "click through the settings pane"},
    ]
    for i in range(rounds):
        msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": f"call-{i}",
                "type": "function",
                "function": {"name": "screen_capture__grab", "arguments": "{}"},
            }],
        })
        msgs.append({
            "role": "tool",
            "tool_call_id": f"call-{i}",
            "content": json.dumps({"ok": True, "round": i}),
        })
    return msgs


# ── breakpoint 1: after system + tools ───────────────────────────────────────


def test_system_prompt_carries_the_first_breakpoint():
    body = _build([
        {"role": "system", "content": "You are FERAL."},
        {"role": "user", "content": "hi"},
    ])
    assert isinstance(body["system"], list), (
        "system must be promoted to the block-list form to carry cache_control"
    )
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["system"][-1]["text"] == "You are FERAL."
    # Render order is tools -> system -> messages, so the system marker
    # already covers the tools; marking a tool too would waste a slot.
    assert not any(b.startswith("tool:") for b in _breakpoints(body))


def test_tools_carry_the_breakpoint_when_there_is_no_system_prompt():
    body = _build([{"role": "user", "content": "hi"}])
    assert "system" not in body
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_no_system_and_no_tools_places_no_prefix_breakpoint():
    body = _build([{"role": "user", "content": "hi"}], tools=None)
    assert _breakpoints(body) == []


# ── breakpoints 2-4: the three most recent tool_result blocks ────────────────


def test_three_most_recent_tool_results_are_marked():
    body = _build(_conversation(6))
    marks = _breakpoints(body)
    tool_result_marks = [m for m in marks if "tool_result" in m]
    assert len(tool_result_marks) == 3

    # They must be the LAST three, not the first three.
    indices = [
        mi for mi, msg in enumerate(body["messages"])
        if isinstance(msg.get("content"), list)
        and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            and "cache_control" in b
            for b in msg["content"]
        )
    ]
    all_tool_result_msgs = [
        mi for mi, msg in enumerate(body["messages"])
        if isinstance(msg.get("content"), list)
        and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in msg["content"]
        )
    ]
    assert indices == all_tool_result_msgs[-3:]


def test_fewer_than_three_tool_results_marks_what_exists():
    body = _build(_conversation(1))
    assert len([m for m in _breakpoints(body) if "tool_result" in m]) == 1


def test_a_turn_with_no_tool_results_uses_only_the_prefix_breakpoint():
    body = _build([
        {"role": "system", "content": "You are FERAL."},
        {"role": "user", "content": "hi"},
    ])
    assert _breakpoints(body) == ["system[0]"]


# ── the 4-breakpoint ceiling (a 5th is a 400 from the API) ───────────────────


@pytest.mark.parametrize("rounds", [0, 1, 2, 3, 5, 12, 40])
def test_never_more_than_four_breakpoints(rounds):
    body = _build(_conversation(rounds))
    assert len(_breakpoints(body)) <= 4


def test_a_long_agent_loop_uses_exactly_the_documented_four():
    body = _build(_conversation(30))
    marks = _breakpoints(body)
    assert len(marks) == 4
    assert marks[0] == "system[0]"
    assert sum(1 for m in marks if "tool_result" in m) == 3


# ── the breakpoints advance, which is what makes a growing loop hit ──────────


def test_breakpoints_advance_with_the_conversation():
    turn_a = _breakpoints(_build(_conversation(5)))
    turn_b = _breakpoints(_build(_conversation(6)))
    assert turn_a != turn_b, "breakpoints must advance each turn"
    assert turn_a[0] == turn_b[0] == "system[0]", (
        "the tools+system breakpoint must stay put: it is the stable prefix"
    )


def test_no_ttl_is_ever_emitted_because_the_cost_lane_assumes_the_5m_rate():
    """``cost/pricing.py`` bills every cache write at the 5-minute rate
    (1.25x base input) and says so in its docstring: "FERAL never sends
    ``cache_control.ttl``, so the only cache writes it can incur are the
    5-minute kind; billing them at the 1h rate would over-charge by 60%."
    Emitting ``ttl: "1h"`` (a 2x write) here would silently make that
    billing wrong, so the marker stays bare.
    """
    body = _build(_conversation(6))
    markers = [
        block["cache_control"]
        for block in (body.get("system") or [])
        if isinstance(block, dict) and "cache_control" in block
    ]
    for msg in body["messages"]:
        if isinstance(msg.get("content"), list):
            markers += [
                b["cache_control"] for b in msg["content"]
                if isinstance(b, dict) and "cache_control" in b
            ]
    assert markers
    assert all(m == {"type": "ephemeral"} for m in markers), markers


def test_batch_pruning_defaults_still_match_the_documented_guidance():
    """The pruner and these breakpoints are two halves of one design.

    If the keep-last / prune-every defaults drift away from the doc, the
    prefix stops being byte-stable between prune events and the
    breakpoints below stop paying for themselves.
    """
    assert SCREENSHOT_KEEP_LAST_DEFAULT == 3
    assert SCREENSHOT_PRUNE_EVERY_DEFAULT == 25


# ── kill switch ──────────────────────────────────────────────────────────────


def test_env_kill_switch_removes_every_breakpoint(monkeypatch):
    monkeypatch.setenv("FERAL_ANTHROPIC_PROMPT_CACHE", "0")
    body = _build(_conversation(5))
    assert _breakpoints(body) == []
    # And ``system`` stays the plain-string form it had before.
    assert isinstance(body["system"], str)


@pytest.mark.parametrize(
    "value,enabled",
    [("1", True), ("true", True), ("on", True), ("", True),
     ("0", False), ("false", False), ("no", False), ("off", False)],
)
def test_kill_switch_values(monkeypatch, value, enabled):
    monkeypatch.setenv("FERAL_ANTHROPIC_PROMPT_CACHE", value)
    assert LLMProvider._anthropic_cache_enabled() is enabled


def test_cache_is_on_when_the_env_var_is_absent():
    """Default-on: an install that never heard of the flag still caches."""
    import os
    saved = os.environ.pop("FERAL_ANTHROPIC_PROMPT_CACHE", None)
    try:
        assert LLMProvider._anthropic_cache_enabled() is True
    finally:
        if saved is not None:
            os.environ["FERAL_ANTHROPIC_PROMPT_CACHE"] = saved


# ── idempotence / shape safety ───────────────────────────────────────────────


def test_applying_twice_does_not_add_a_fifth_breakpoint():
    body = _build(_conversation(5))
    before = _breakpoints(body)
    LLMProvider._apply_anthropic_cache_breakpoints(body)
    assert _breakpoints(body) == before


def test_the_body_is_still_valid_json_for_the_wire():
    body = _build(_conversation(4))
    round_tripped = json.loads(json.dumps(body))
    assert round_tripped["system"][0]["cache_control"] == {"type": "ephemeral"}


# ── the direct (non-failover) Anthropic path builds its own body ─────────────


@pytest.mark.asyncio
async def test_direct_chat_anthropic_path_also_carries_breakpoints():
    """``_chat_anthropic`` assembles a second, separate body.

    It reports ``cache_creation_input_tokens`` / ``cache_read_input_tokens``
    back to the cost layer, and both stayed permanently zero while
    nothing on the request asked for a cache.
    """
    from unittest.mock import AsyncMock, MagicMock

    p = LLMProvider.__new__(LLMProvider)
    p.provider = "anthropic"
    p.model = "claude-opus-5"
    posted: dict = {}

    async def fake_post(path, json=None, **_kw):
        posted["path"] = path
        posted["body"] = json
        return MagicMock(
            status_code=200,
            raise_for_status=MagicMock(return_value=None),
            json=MagicMock(return_value={
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }),
        )

    p.client = MagicMock(post=AsyncMock(side_effect=fake_post))

    await p._chat_anthropic(_conversation(4), TOOLS, 0.7, 1024)

    assert posted["path"] == "/messages"
    marks = _breakpoints(posted["body"])
    assert marks[0] == "system[0]"
    assert sum(1 for m in marks if "tool_result" in m) == 3
    assert len(marks) == 4
