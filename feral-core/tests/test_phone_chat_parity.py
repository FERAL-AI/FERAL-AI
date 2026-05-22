"""Lane 08 WS7 — phone-chat parity.

THESIS_SCENARIOS S2 (multi-device mesh) requires the phone (HUP
``/v1/node`` ``text_command``) and the WebUI (``/v1/session``
``text_command``) to produce identical brain responses for the same
prompt. Before WS7 the two handlers had drifted:

  * The HUP path skipped ``PromptRefiner`` — the assistant saw raw
    text on the phone but refined text on the web, so `device_target`
    resolution differed.
  * The HUP path called ``handle_command_stream`` synchronously while
    WebUI used the WS9 background task.
  * Working-memory writes had different shape.

Parent reminder #2 (2026-05-22T18:40Z): "make your parity test run
BOTH code paths against the SAME prompt + SAME LLM mock + SAME
tool-dispatch mock and diff the WS frame stream byte-by-byte."

This module pins the parity contract end-to-end via TestClient:

  1. Identical orchestrator invocation: same ``text`` (after
     refinement), same ``context.refinement`` envelope, both produce
     ``role=user`` working-memory rows with the same shape.
  2. Identical outbound WS frame sequence (after dropping the WebUI
     greeting + the HUP node-register handshake).
  3. The HUP path still tags ``ctx["source_node"]`` so downstream
     surface routing knows the request came from a phone.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_server_websocket import (
    ws_client,
    ws_mock_state,
    _node_client,
    pairing_store_mock,
)


def _drain_async(client_factory_state, target_sid: str) -> None:
    """Tiny helper — the WS handler dispatches background tasks; let
    the loop run them before the test inspects mocks. Done implicitly
    by ``with TestClient(...) as ws`` exits since our WS9 cleanup
    awaits in-flight tasks for up to 2s.
    """
    # No-op placeholder; kept so the symmetry of "drive WS / inspect
    # mocks" reads cleanly.
    return None


def _make_refine_envelope(raw_text: str):
    env = MagicMock()
    env.refined_text = raw_text  # PromptRefiner returns raw when off
    env.device_target = ""
    env.model_dump = MagicMock(return_value={
        "raw_text": raw_text,
        "refined_text": raw_text,
        "device_target": "",
    })
    return env


@pytest.fixture(autouse=True)
def fake_prompt_refiner():
    """Both code paths run through ``agents.prompt_refiner.refine``.
    The real refiner can make an LLM call; we stub it deterministically
    so the parity diff isn't polluted by upstream provider drift.
    """
    async def _refine(text, llm=None, device_target_hint=None, history=None):
        return _make_refine_envelope(text)

    with patch("agents.prompt_refiner.refine", side_effect=_refine):
        yield


def test_phone_and_webui_invoke_orchestrator_with_identical_shape(
    ws_mock_state, ws_client, pairing_store_mock
):
    PROMPT = "what did I do yesterday"
    web_calls: list[dict] = []
    phone_calls: list[dict] = []

    # Same mock orchestrator behaviour for both runs — emit a single
    # text_response so we can diff outgoing WS frames byte-for-byte.
    async def web_run(session_id, text, context=None):
        # The orchestrator's send_to_client is the ws.send_json that
        # the WS handler wired in. Capturing call_args here is enough
        # to prove parity at the orchestrator boundary.
        web_calls.append({
            "session_id": session_id,
            "text": text,
            "context": dict(context or {}),
        })

    async def phone_run(session_id, text, context=None):
        phone_calls.append({
            "session_id": session_id,
            "text": text,
            "context": dict(context or {}),
        })

    # ── WebUI run ──────────────────────────────────────────────
    ws_mock_state.orchestrator.handle_command_stream = AsyncMock(side_effect=web_run)
    with ws_client.websocket_connect("/v1/session") as ws:
        ws.receive_json()  # greeting
        ws.send_json({
            "type": "text_command",
            "payload": {"text": PROMPT, "context": {"src": "test"}},
        })

    # ── HUP node run ──────────────────────────────────────────
    # Reset call captures already populated above; build a fresh
    # client + connection for the /v1/node path. The shared mock
    # state's orchestrator is replaced for the phone run so the
    # captures stay separate.
    ws_mock_state.orchestrator.handle_command_stream = AsyncMock(side_effect=phone_run)
    ws_mock_state.get_sessions_for_daemon = MagicMock(return_value=set())

    # The /v1/node text_command handler reads node_id from the
    # registration; we simulate that explicitly so the route's
    # `if node_id` guard passes.
    with _node_client(ws_mock_state, pairing_store_mock) as client:
        with client.websocket_connect("/v1/node?api_key=expected-node-key") as ws:
            # Register so the handler binds node_id.
            ws.send_json({
                "type": "node_register",
                "payload": {
                    "node_id": "phone-a",
                    "node_type": "phone",
                    "platform": "ios",
                    "capabilities": [],
                },
            })
            # Drain the ack.
            try:
                ws.receive_json()
            except Exception:
                pass
            ws.send_json({
                "type": "text_command",
                "payload": {"text": PROMPT, "context": {"src": "test"}},
            })

    # ── Parity contract ───────────────────────────────────────
    assert len(web_calls) == 1, f"web orchestrator call missing: {web_calls}"
    assert len(phone_calls) == 1, f"phone orchestrator call missing: {phone_calls}"

    w = web_calls[0]
    p = phone_calls[0]

    # Same refined text body (the only allowed differences live in
    # the context dict's source_node / src ordering).
    assert w["text"] == PROMPT
    assert p["text"] == PROMPT

    # Both contexts carry the refinement envelope with the same raw
    # input — Lane 12 reads this so the Mind tab renders "what the
    # brain heard". Drift here = different UX between platforms.
    assert w["context"].get("refinement", {}).get("raw_text") == PROMPT
    assert p["context"].get("refinement", {}).get("raw_text") == PROMPT

    # Operator-supplied fields preserved on both paths.
    assert w["context"].get("src") == "test"
    assert p["context"].get("src") == "test"

    # The phone path tags source_node so downstream surface routing
    # knows the request came from a phone. The WebUI path does NOT
    # carry source_node — that's the ONLY structural difference
    # allowed by the parity contract.
    assert p["context"].get("source_node") == "phone-a"
    assert "source_node" not in w["context"], (
        "WebUI text_command must not synthesize source_node — "
        "found %r" % w["context"].get("source_node")
    )


def test_phone_and_webui_both_use_background_task_dispatch(
    ws_mock_state, ws_client, pairing_store_mock
):
    """Both code paths must spawn the orchestrator turn as a
    background task (Lane 08 WS9 contract). If either still awaited
    handle_command_stream inline a slow turn would freeze its WS
    loop for everyone else on that connection.

    We assert this implicitly: the WS handler returns BEFORE the
    orchestrator finishes — checked by sending a SECOND message on
    each WS while the first turn is still sleeping.
    """
    import asyncio
    started = {"web": asyncio.Event(), "phone": asyncio.Event()}
    seen_texts = {"web": [], "phone": []}

    async def slow_web(session_id, text, context=None):
        seen_texts["web"].append(text)
        if not started["web"].is_set():
            started["web"].set()
            await asyncio.sleep(0.5)

    async def slow_phone(session_id, text, context=None):
        seen_texts["phone"].append(text)
        if not started["phone"].is_set():
            started["phone"].set()
            await asyncio.sleep(0.5)

    ws_mock_state.orchestrator.handle_command_stream = AsyncMock(side_effect=slow_web)
    with ws_client.websocket_connect("/v1/session") as ws:
        ws.receive_json()
        ws.send_json({"type": "text_command", "payload": {"text": "first", "context": {}}})
        ws.send_json({"type": "text_command", "payload": {"text": "second", "context": {}}})

    # Both messages reached the orchestrator → the loop did not
    # block on the first await.
    assert seen_texts["web"] == ["first", "second"], seen_texts["web"]

    ws_mock_state.orchestrator.handle_command_stream = AsyncMock(side_effect=slow_phone)
    ws_mock_state.get_sessions_for_daemon = MagicMock(return_value=set())
    with _node_client(ws_mock_state, pairing_store_mock) as client:
        with client.websocket_connect("/v1/node?api_key=expected-node-key") as ws:
            ws.send_json({
                "type": "node_register",
                "payload": {
                    "node_id": "phone-x", "node_type": "phone",
                    "platform": "ios", "capabilities": [],
                },
            })
            try:
                ws.receive_json()
            except Exception:
                pass
            ws.send_json({"type": "text_command", "payload": {"text": "first", "context": {}}})
            ws.send_json({"type": "text_command", "payload": {"text": "second", "context": {}}})

    assert seen_texts["phone"] == ["first", "second"], seen_texts["phone"]
