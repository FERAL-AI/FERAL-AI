"""Voice realtime tool pinning + schedule-intent forcing (v2026.6.27).

Regression for the live failure where OpenAI Realtime truncated 214 tools
to 128 via naive ``[:128]``, dropping ``feral_routines__create``, and the
voice path never applied ``_force_tool_for_query`` from the orchestrator.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.tool_list import (
    OPENAI_TOOL_HARD_LIMIT,
    PINNED_OPENAI_TOOL_NAMES,
    cap_tools_with_pins,
    tool_name_from_def,
)
from voice.realtime_proxy import RealtimeProxy, RealtimeSession


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _many_tools(count: int) -> list[dict]:
    return [_tool(f"zz_tail_skill__endpoint_{i}") for i in range(count)]


def test_cap_tools_with_pins_keeps_feral_routines_and_cutebot():
    """Pinned automation tools survive a 128-cap even when alphabetically late."""
    raw = _many_tools(200)
    raw.append(_tool("feral_routines__create"))
    raw.append(_tool("cutebot__drive"))
    raw.append(_tool("cutebot__halt"))

    capped = cap_tools_with_pins(raw, max_tools=OPENAI_TOOL_HARD_LIMIT)
    names = {tool_name_from_def(t) for t in capped}

    assert len(capped) == OPENAI_TOOL_HARD_LIMIT
    assert "feral_routines__create" in names
    assert "cutebot__drive" in names
    assert "cutebot__halt" in names

    # Pinned tools must appear before the alphabetical tail block.
    create_idx = next(
        i for i, t in enumerate(capped)
        if tool_name_from_def(t) == "feral_routines__create"
    )
    tail_idx = next(
        i for i, t in enumerate(capped)
        if tool_name_from_def(t).startswith("zz_tail_skill__")
    )
    assert create_idx < tail_idx


def test_pinned_order_respects_priority_tuple():
    raw = _many_tools(150) + [_tool(n) for n in PINNED_OPENAI_TOOL_NAMES]
    capped = cap_tools_with_pins(raw, max_tools=OPENAI_TOOL_HARD_LIMIT)
    pinned_present = [
        tool_name_from_def(t) for t in capped[: len(PINNED_OPENAI_TOOL_NAMES) + 2]
        if tool_name_from_def(t) in PINNED_OPENAI_TOOL_NAMES
    ]
    assert pinned_present == list(PINNED_OPENAI_TOOL_NAMES)


@pytest.mark.asyncio
async def test_realtime_configure_pins_routines_before_truncation():
    """session.update must expose feral_routines even with 200+ raw tools."""
    rs = RealtimeSession(
        session_id="sess-pin",
        node_id="phone-1",
        api_key="sk-test",
    )
    rs._ws = AsyncMock()
    rs._connected = True

    sent: list[dict] = []
    rs._ws.send = AsyncMock(side_effect=lambda msg: sent.append(json.loads(msg)))

    raw = _many_tools(200)
    raw.extend([
        _tool("feral_routines__create"),
        _tool("cutebot__drive"),
    ])
    await rs.configure(tools=raw)

    su = [e for e in sent if e["type"] == "session.update"][0]
    wire_names = [t["name"] for t in su["session"]["tools"]]
    assert len(wire_names) == OPENAI_TOOL_HARD_LIMIT
    assert "feral_routines__create" in wire_names
    assert "cutebot__drive" in wire_names


@pytest.mark.asyncio
async def test_realtime_force_tool_for_turn_updates_tool_choice():
    # The forced name must be IN the session's tool list — forcing now
    # runs through ``resolve_forced_tool_choice`` like the configure
    # path does. Pre-fix this test passed with an empty tool list,
    # which is exactly the payload OpenAI rejects with an `error`
    # event (see test_realtime_force_tool_absent_degrades_to_auto).
    rs = RealtimeSession(
        session_id="s",
        node_id="n",
        api_key="sk",
        tools=[_tool("feral_routines__create")],
    )
    rs._ws = AsyncMock()
    rs._connected = True
    sent: list[dict] = []
    rs._ws.send = AsyncMock(side_effect=lambda msg: sent.append(json.loads(msg)))

    await rs.force_tool_for_turn("feral_routines__create")

    updates = [e for e in sent if e["type"] == "session.update"]
    assert updates[-1]["session"]["type"] == "realtime"
    assert updates[-1]["session"]["tool_choice"] == {
        "type": "function",
        "name": "feral_routines__create",
    }
    assert any(e["type"] == "response.create" for e in sent)


@pytest.mark.asyncio
async def test_realtime_reset_tool_choice_includes_session_type():
    rs = RealtimeSession(session_id="s", node_id="n", api_key="sk")
    rs._ws = AsyncMock()
    rs._connected = True
    sent: list[dict] = []
    rs._ws.send = AsyncMock(side_effect=lambda msg: sent.append(json.loads(msg)))

    rs._active_force_tool = "feral_routines__create"
    await rs.reset_tool_choice()

    su = [e for e in sent if e["type"] == "session.update"][0]
    assert su["session"]["type"] == "realtime"
    assert su["session"]["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_realtime_force_tool_skips_when_already_executed():
    """Late transcript hook must not re-force after VAD drove the tool."""
    rs = RealtimeSession(session_id="s", node_id="n", api_key="sk")
    rs._ws = AsyncMock()
    rs._connected = True
    sent: list[dict] = []
    rs._ws.send = AsyncMock(side_effect=lambda msg: sent.append(json.loads(msg)))

    rs._turn_tools_executed.add("feral_routines__create")
    await rs.force_tool_for_turn("feral_routines__create")

    assert not [e for e in sent if e["type"] == "session.update"]
    assert not [e for e in sent if e["type"] == "response.create"]


@pytest.mark.asyncio
async def test_note_voice_user_turn_returns_forced_routine_tool():
    from agents.orchestrator import Orchestrator
    from agents.refusal_handler import RefusalHandler

    orch = Orchestrator.__new__(Orchestrator)
    orch.refusal_handler = RefusalHandler(MagicMock())
    orch.conversation_history = {}

    tools = [_tool("feral_routines__create"), _tool("feral_reminders__create")]
    out = await orch.note_voice_user_turn(
        "sess-voice-routine",
        "spin the robot every night at 9pm",
        tools=tools,
    )
    assert out["forced_tool"] == "feral_routines__create"


@pytest.mark.asyncio
async def test_voice_transcript_hook_triggers_force_tool_for_turn():
    """Final user transcript on realtime path must force routines on schedule intent."""
    proxy = RealtimeProxy(
        skill_registry=MagicMock(),
        skill_executor=MagicMock(),
        memory=MagicMock(),
        orchestrator=MagicMock(),
    )
    rs = RealtimeSession(
        session_id="sess-hook",
        node_id="webclient_x",
        api_key="sk",
    )
    rs.force_tool_for_turn = AsyncMock()
    proxy._sessions["sess-hook"] = rs

    proxy._orchestrator.note_voice_user_turn = AsyncMock(return_value={
        "forced_tool": "feral_routines__create",
        "context_hint": "",
        "resolved_text": "spin the robot every night at 9pm",
        "active_subject": "",
    })
    proxy._memory.working_push = MagicMock()

    await proxy._handle_transcript(
        "sess-hook",
        "[user] spin the robot every night at 9pm",
        is_final=True,
    )

    rs.force_tool_for_turn.assert_awaited_once_with("feral_routines__create")
