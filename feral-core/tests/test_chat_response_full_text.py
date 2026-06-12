"""Bug fix pinning — iOS chat replies truncated to exactly 300 chars.

Chain that produced the bug:
  * Phone sends ``chat_request`` over ``/v1/node``; the handler calls
    ``orchestrator.handle_command(...)`` and uses the RETURN VALUE as
    the ``chat_response`` text.
  * ``_handle_command_impl``'s multi-agent branch delivered text via
    the (suppressed) ``text_response`` broadcast and then did a bare
    ``return`` — ``handle_command`` returned ``None``.
  * The server's fallback then scanned working memory for the last
    assistant entry... which the orchestrator had pushed TRUNCATED to
    300 chars (``working_push(..., text=response_text[:300])``).
  * Net: the phone rendered exactly 300 chars, cut mid-word.

Fix contract pinned here:
  1. ``handle_command`` RETURNS the full final text (multi-agent and
     plain-text paths) so ``chat_response`` carries it directly.
  2. Working memory stores the FULL assistant text — the server's
     fallback is now a safe net, not a truncator. (Prompt context is
     unaffected: ``working_context_string`` slices to [:200] itself.)
  3. End-to-end: a >300-char reply reaches the phone ``chat_response``
     payload untruncated.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.orchestrator import Orchestrator

from tests.test_server_websocket import (  # noqa: F401 — pytest fixtures
    ws_client,
    ws_mock_state,
    _node_client,
    pairing_store_mock,
)

LONG_REPLY = ("A poem that keeps going well past the three-hundred mark — " * 12).strip()
assert len(LONG_REPLY) > 300


# ─────────────────────────────────────────────────────────────
# Orchestrator level — where the text used to get dropped
# ─────────────────────────────────────────────────────────────


def _make_orchestrator(memory=None) -> Orchestrator:
    reg = MagicMock()
    reg.skills = {}
    reg.find_skills_for_query = MagicMock(return_value=[])
    reg.get_tools_for_skills = MagicMock(return_value=[])
    return Orchestrator(
        skill_registry=reg,
        send_to_client=AsyncMock(),
        daemons={},
        memory=memory,
        vision_buffer=None,
        perception=None,
        learner=None,
    )


@pytest.mark.asyncio
async def test_handle_command_multi_agent_returns_full_text():
    """The multi-agent branch must RETURN the full final text — a bare
    ``return`` here is exactly what forced the server onto the
    300-char working-memory fallback."""
    memory = MagicMock()
    memory.working_push = MagicMock()
    memory.save_episode = AsyncMock()

    orch = _make_orchestrator(memory=memory)
    orch.llm = MagicMock(available=True)
    orch._multi_agent_enabled = True
    orch._multi_agent = MagicMock()
    orch._multi_agent.run = AsyncMock(return_value=LONG_REPLY)
    orch._try_send_sdui = AsyncMock()

    result = await orch.handle_command("sess-phone", "write me a long poem", context={})

    assert result == LONG_REPLY, "handle_command must return the FULL multi-agent text"


@pytest.mark.asyncio
async def test_multi_agent_working_push_stores_full_text():
    """Working memory must hold the FULL assistant reply (no [:300])
    so the server's fallback never truncates."""
    memory = MagicMock()
    memory.working_push = MagicMock()
    memory.save_episode = AsyncMock()

    orch = _make_orchestrator(memory=memory)
    orch.llm = MagicMock(available=True)
    orch._multi_agent_enabled = True
    orch._multi_agent = MagicMock()
    orch._multi_agent.run = AsyncMock(return_value=LONG_REPLY)
    orch._try_send_sdui = AsyncMock()

    await orch.handle_command("sess-phone", "write me a long poem", context={})

    assistant_pushes = [
        c.args[1]
        for c in memory.working_push.call_args_list
        if c.args[1].get("role") == "assistant"
    ]
    assert assistant_pushes, "assistant reply must be pushed to working memory"
    assert assistant_pushes[-1]["text"] == LONG_REPLY, (
        "working memory must store the full text, got %d chars"
        % len(assistant_pushes[-1]["text"])
    )


# ─────────────────────────────────────────────────────────────
# Server level — chat_request → chat_response end-to-end
# ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fake_prompt_refiner():
    """Stub PromptRefiner so the WS round-trip doesn't depend on the
    refiner's LLM behaviour (same approach as test_phone_chat_parity)."""
    def _make_env(raw_text: str):
        env = MagicMock()
        env.refined_text = raw_text
        env.device_target = ""
        env.model_dump = MagicMock(return_value={
            "raw_text": raw_text,
            "refined_text": raw_text,
            "device_target": "",
        })
        return env

    async def _refine(text, llm=None, device_target_hint=None, history=None):
        return _make_env(text)

    with patch("agents.prompt_refiner.refine", side_effect=_refine):
        yield


def _drive_chat_request(ws_mock_state, pairing_store_mock):  # noqa: F811
    """Register a phone node and send one final-mode chat_request;
    return the chat_response payload."""
    with _node_client(ws_mock_state, pairing_store_mock) as client:
        with client.websocket_connect("/v1/node?api_key=expected-node-key") as ws:
            ws.send_json({
                "type": "node_register",
                "payload": {
                    "node_id": "phone-a",
                    "node_type": "phone",
                    "platform": "ios",
                    "capabilities": [],
                },
            })
            ws.receive_json()  # node_ack
            ws.send_json({
                "type": "chat_request",
                "payload": {
                    "session_id": "sess-phone",
                    "text": "write me a long poem",
                    "reply_mode": "final",
                    "channel": "chat",
                },
            })
            while True:
                frame = ws.receive_json()
                if frame.get("type") == "chat_response":
                    return frame["payload"]


def test_long_reply_reaches_phone_untruncated(
    ws_mock_state, pairing_store_mock  # noqa: F811 — pytest fixtures
):
    """>300-char reply returned by handle_command must arrive whole in
    the chat_response payload (the actual user-facing regression)."""
    ws_mock_state.orchestrator.handle_command = AsyncMock(return_value=LONG_REPLY)

    payload = _drive_chat_request(ws_mock_state, pairing_store_mock)

    assert payload["text"] == LONG_REPLY
    assert len(payload["text"]) > 300


def test_working_memory_fallback_carries_full_text(
    ws_mock_state, pairing_store_mock  # noqa: F811 — pytest fixtures
):
    """Safety net: when handle_command returns nothing, the fallback
    reads working memory — which now stores FULL text, so the phone
    still gets the whole reply."""
    ws_mock_state.orchestrator.handle_command = AsyncMock(return_value=None)
    ws_mock_state.memory.working_get = MagicMock(return_value=[
        {"role": "user", "text": "write me a long poem"},
        {"role": "assistant", "text": LONG_REPLY},
    ])

    payload = _drive_chat_request(ws_mock_state, pairing_store_mock)

    assert payload["text"] == LONG_REPLY
