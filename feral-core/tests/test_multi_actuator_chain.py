"""Lane 08 WS5 — multi-actuator WS frames must arrive in tool_call order.

THESIS_SCENARIOS S5: the Roomba demo chains
``vision__describe_scene`` → ``home_assistant__vacuum_start`` in a
single LLM turn. The user's chat panel renders each tool as a chip,
and the rendering order must match the LLM's intent (vision FIRST,
vacuum SECOND) regardless of which tool happens to complete first.

Before WS5 the non-stream branch dispatched tools concurrently via
``asyncio.gather`` and emitted ``tool_start`` / ``tool_result`` INSIDE
each task — so a faster vacuum_start would put its frames on the wire
before the slower vision describe. The consumer rendered the chain
backwards.

This module pins the fix:

  1. ``tool_start`` frames are emitted in the LLM's tool_call index
     order, BEFORE any tool actually runs.
  2. ``tool_result`` frames are emitted in the same order, even when
     the underlying executions complete in reverse order.
  3. The ``history`` row sequence (tool messages) honours the same
     order so the next LLM turn sees tool_call_id → result in the
     OpenAI-required sequence.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.orchestrator import Orchestrator
from models.skill_manifest import BrandProfile, SkillEndpoint, SkillManifest


def _skill(skill_id: str) -> SkillManifest:
    return SkillManifest(
        skill_id=skill_id, version="1.0.0", author="test",
        brand=BrandProfile(name=skill_id, primary_color="#000", logo_url="", icon_set="sf_symbols"),
        description=f"{skill_id} skill", trigger_phrases=[],
        endpoints=[SkillEndpoint(
            id="default", method="POST", url=f"https://x/{skill_id}",
            description="", returns_description="", ui_hint="detail_card",
        )],
    )


def _make_orchestrator() -> Orchestrator:
    reg = MagicMock()
    reg.skills = {
        "vision": _skill("vision"),
        "home_assistant": _skill("home_assistant"),
    }
    reg.find_skills_for_query = lambda q, top_k=5: []
    reg.get_tools_for_skills = lambda skills: []
    return Orchestrator(
        skill_registry=reg, send_to_client=AsyncMock(), daemons={},
        memory=None, vision_buffer=None, perception=None, learner=None,
    )


def _capture_sends(orch: Orchestrator) -> list[dict]:
    captured: list[dict] = []

    async def _send(session_id: str, msg):
        dumped = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
        captured.append({
            "type": dumped.get("type"),
            "payload": dumped.get("payload") or {},
        })

    orch.send = _send
    return captured


@pytest.mark.asyncio
async def test_tool_frames_emitted_in_tool_call_order_even_with_reverse_completion():
    """The LLM asks for ``vision__describe_scene`` then
    ``home_assistant__vacuum_start``. The vacuum call finishes in
    10ms; the vision call sleeps 100ms. The WS frame sequence must
    still be ``vision_start, vacuum_start, vision_result, vacuum_result``.
    """
    tool_calls = [
        {"id": "tc-vision", "name": "vision__describe_scene", "args": {}},
        {"id": "tc-vacuum", "name": "home_assistant__vacuum_start",
         "args": {"entity_id": "vacuum.mock_roomba"}},
    ]

    async def fake_tool_run(session_id, tc, available_skills):
        # Vision sleeps 100ms; vacuum returns immediately. If WS
        # frames followed completion order the chain would render
        # backwards.
        if tc["name"].startswith("vision__"):
            await asyncio.sleep(0.1)
        return {"success": True, "data": {"tool": tc["name"]}}

    orch = _make_orchestrator()
    orch.llm = MagicMock()
    orch.llm.available = True
    orch.llm.model_name = "test"

    responses = iter([
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
        {"choices": [{"message": {"role": "assistant", "content": "Started the Roomba."}}]},
    ])

    async def chat_responder(messages, tools=None, **kwargs):
        return next(responses)

    extract_iter = iter([("", tool_calls), ("Started the Roomba.", [])])

    def extract(response):
        return next(extract_iter)

    orch.llm.chat_with_failover = chat_responder
    orch.llm.extract_response = extract
    orch._route_prompt = AsyncMock(return_value=[])
    orch._ensure_core_skills = lambda x: x
    orch._execute_tool_call_for_llm = AsyncMock(side_effect=fake_tool_run)
    orch._try_genui_for_result = AsyncMock()

    sends = _capture_sends(orch)
    await orch.handle_command(
        session_id="s-aaaaaaaa",
        text="the room is messy, start the vacuum",
        context={"voice_mode": True},
    )

    # ── Frame-order contract ────────────────────────────────────
    tool_frames = [
        (f["type"], f["payload"].get("tool") or f["payload"].get("name"))
        for f in sends
        if f["type"] in ("tool_start", "tool_result")
    ]
    # tool_start emitted in tool_call index order, BEFORE any
    # tool_result; then tool_result emitted in the same order.
    expected = [
        ("tool_start", "vision__describe_scene"),
        ("tool_start", "home_assistant__vacuum_start"),
        ("tool_result", "vision__describe_scene"),
        ("tool_result", "home_assistant__vacuum_start"),
    ]
    assert tool_frames == expected, (
        f"WS frame sequence drift:\n  got:      {tool_frames}\n"
        f"  expected: {expected}"
    )


@pytest.mark.asyncio
async def test_tool_start_emitted_before_any_execution():
    """All ``tool_start`` frames must reach the wire BEFORE the
    first underlying tool call begins — so the consumer can render
    the full chain of chips immediately.
    """
    tool_calls = [
        {"id": "a", "name": "vision__describe_scene", "args": {}},
        {"id": "b", "name": "home_assistant__vacuum_start", "args": {}},
    ]

    # Capture global ordering: tool_start frames recorded by
    # send_to_client; tool executions recorded inside the executor
    # mock. We assert both tool_start records arrive before either
    # execute.
    events: list[str] = []

    orch = _make_orchestrator()
    orch.llm = MagicMock()
    orch.llm.available = True
    orch.llm.model_name = "test"

    responses = iter([
        {
            "choices": [{
                "message": {
                    "role": "assistant", "content": "",
                    "tool_calls": [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"], "arguments": "{}"}}
                        for tc in tool_calls
                    ],
                }
            }]
        },
        {"choices": [{"message": {"role": "assistant", "content": "done"}}]},
    ])
    extract_iter = iter([("", tool_calls), ("done", [])])

    async def chat_responder(messages, tools=None, **kwargs):
        return next(responses)

    orch.llm.chat_with_failover = chat_responder
    orch.llm.extract_response = lambda r: next(extract_iter)
    orch._route_prompt = AsyncMock(return_value=[])
    orch._ensure_core_skills = lambda x: x

    async def exec_tool(session_id, tc, available_skills):
        events.append(f"exec:{tc['name']}")
        return {"success": True, "data": {}}

    orch._execute_tool_call_for_llm = exec_tool
    orch._try_genui_for_result = AsyncMock()

    async def recording_send(session_id, msg):
        dumped = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
        if dumped.get("type") == "tool_start":
            events.append(f"start:{dumped['payload']['tool']}")
        elif dumped.get("type") == "tool_result":
            events.append(f"result:{dumped['payload']['tool']}")

    orch.send = recording_send
    await orch.handle_command(session_id="s", text="x", context={"voice_mode": True})

    # First two events must be the two tool_start frames in order;
    # no exec event is allowed between them or before them.
    first_two = events[:2]
    assert first_two == [
        "start:vision__describe_scene",
        "start:home_assistant__vacuum_start",
    ], f"tool_start ordering wrong: {events}"

    # Both starts come BEFORE the first exec.
    first_exec_index = next(
        (i for i, e in enumerate(events) if e.startswith("exec:")), -1
    )
    assert first_exec_index >= 2, (
        f"a tool started executing before all tool_start frames went out: {events}"
    )


@pytest.mark.asyncio
async def test_history_tool_messages_in_tool_call_order():
    """Independent of frame order — the LLM's next turn sees tool
    messages in the same order the LLM asked for them. Pinned here
    so an over-eager refactor doesn't break the OpenAI tool-message
    contract.
    """
    tool_calls = [
        {"id": "first", "name": "vision__describe_scene", "args": {}},
        {"id": "second", "name": "home_assistant__vacuum_start", "args": {}},
    ]

    seen_history_on_second_call: list[dict] = []

    async def fake_tool_run(session_id, tc, available_skills):
        # Reverse-complete: vision sleeps, vacuum is instant.
        if tc["name"].startswith("vision__"):
            await asyncio.sleep(0.05)
        return {"success": True, "data": {"tool": tc["name"]}}

    orch = _make_orchestrator()
    orch.llm = MagicMock()
    orch.llm.available = True
    orch.llm.model_name = "test"

    responses = iter([
        {
            "choices": [{
                "message": {
                    "role": "assistant", "content": "",
                    "tool_calls": [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"], "arguments": "{}"}}
                        for tc in tool_calls
                    ],
                }
            }]
        },
        {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    ])
    extract_iter = iter([("", tool_calls), ("ok", [])])
    call_no = {"n": 0}

    async def chat_responder(messages, tools=None, **kwargs):
        call_no["n"] += 1
        if call_no["n"] == 2:
            seen_history_on_second_call.extend(messages)
        return next(responses)

    orch.llm.chat_with_failover = chat_responder
    orch.llm.extract_response = lambda r: next(extract_iter)
    orch._route_prompt = AsyncMock(return_value=[])
    orch._ensure_core_skills = lambda x: x
    orch._execute_tool_call_for_llm = AsyncMock(side_effect=fake_tool_run)
    orch._try_genui_for_result = AsyncMock()
    orch.send = AsyncMock()

    await orch.handle_command(session_id="s", text="x", context={"voice_mode": True})

    tool_rows = [
        m for m in seen_history_on_second_call if m.get("role") == "tool"
    ]
    assert [r.get("tool_call_id") for r in tool_rows] == ["first", "second"]
    assert [r.get("name") for r in tool_rows] == [
        "vision__describe_scene", "home_assistant__vacuum_start",
    ]
