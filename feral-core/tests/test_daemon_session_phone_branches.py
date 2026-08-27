"""Daemon /v1/node phone-envelope branch coverage."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.test_hup_protocol import (
    _TEST_NODE_KEY,
    _make_mock_state,
    _node_client,
    _register_node,
)

pytestmark = pytest.mark.no_auto_feral_home


def _configure_bindings(mock_state: MagicMock) -> None:
    bindings: dict[str, set[str]] = {}

    def _bind(session_id: str, node_id: str):
        bindings.setdefault(node_id, set()).add(session_id)

    def _sessions_for(node_id: str) -> set[str]:
        return set(bindings.get(node_id, set()))

    def _clear(node_id: str) -> set[str]:
        return bindings.pop(node_id, set())

    mock_state.bind_session_to_daemon = MagicMock(side_effect=_bind)
    mock_state.get_sessions_for_daemon = MagicMock(side_effect=_sessions_for)
    mock_state.clear_daemon_bindings = MagicMock(side_effect=_clear)


def _flush_with_known_error(ws):
    ws.send_json({"type": "totally_bogus_type", "payload": {}})
    err = ws.receive_json()
    assert err["type"] == "error"
    assert err["payload"]["code"] == 1002


def _phone_recorded(mock_state: MagicMock, message_type: str, decision: str | None = None) -> bool:
    for c in mock_state.supervisor.record.call_args_list:
        detail = c.kwargs.get("detail", {}) if c.kwargs else {}
        if detail.get("message_type") != message_type:
            continue
        if decision is not None and c.kwargs.get("decision") != decision:
            continue
        if c.kwargs.get("kind") == "phone_envelope":
            return True
    return False


def _mock_state_with_supervisor() -> MagicMock:
    mock = _make_mock_state()
    _configure_bindings(mock)
    mock.supervisor = MagicMock()
    mock.supervisor.record = MagicMock()
    return mock


def test_phone_registration_binds_shared_session_before_first_chat_turn():
    mock = _mock_state_with_supervisor()
    mock.primary_session_id = "shared-primary"

    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(
                ws,
                node_id="phone-ambient",
                node_type="phone",
                capabilities=["jw_health_glasses"],
            )
            assert "shared-primary" in mock.get_sessions_for_daemon("phone-ambient")


def test_phone_registration_advertises_primary_session_and_ambient_capability():
    mock = _mock_state_with_supervisor()
    mock.primary_session_id = "shared-primary"

    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            ack = _register_node(
                ws,
                node_id="phone-ambient",
                node_type="phone",
                capabilities=["jw_health_glasses", "ambient_delivery"],
            )

    assert ack["payload"]["primary_session_id"] == "shared-primary"
    assert "ambient_delivery" in ack["payload"]["granted_capabilities"]


def test_new_connection_with_same_node_id_closes_superseded_socket():
    mock = _mock_state_with_supervisor()
    mock.primary_session_id = "shared-primary"

    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as old_ws:
            _register_node(old_ws, node_id="stable-phone", node_type="phone")
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as new_ws:
                _register_node(new_ws, node_id="stable-phone", node_type="phone")

                with pytest.raises(WebSocketDisconnect) as excinfo:
                    old_ws.receive_json()
                assert excinfo.value.code == 4000

                # The replacement remains registered and operational after
                # the old handler observes its close.
                _flush_with_known_error(new_ws)
                assert "stable-phone" in mock.daemons


def test_chat_request_routes_to_orchestrator_and_responds():
    mock = _mock_state_with_supervisor()
    mock.orchestrator.handle_command = AsyncMock(return_value={"text": "Chat acknowledged"})

    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(
                ws,
                node_id="phone-chat",
                node_type="phone",
                capabilities=["voice", "genui"],
            )
            ws.send_json(
                {
                    "type": "chat_request",
                    "hup_version": "1.3.0",
                    "ts": 1734369922.123,
                    "payload": {
                        "session_id": "phone-session-1",
                        "text": "what is this?",
                        "reply_mode": "final",
                        "channel": "chat",
                        "reply_to": None,
                    },
                }
            )
            reply = ws.receive_json()
            assert reply["type"] == "chat_response"
            assert reply["payload"]["session_id"] == "phone-session-1"
            assert reply["payload"]["text"] == "Chat acknowledged"
            # Phase-1 validation pass (Item 2): success branch must
            # carry `error: null` so a future regression that emits
            # a non-null error on a healthy turn is caught here.
            assert reply["payload"].get("error") is None

    kwargs = mock.orchestrator.handle_command.call_args.kwargs
    assert kwargs["session_id"] == "phone-session-1"
    assert kwargs["text"] == "what is this?"
    assert kwargs["context"]["mode"] == "phone_surface"
    assert kwargs["context"]["source"] == "phone_surface"
    assert _phone_recorded(mock, "chat_request", "allowed")


def test_chat_request_orchestrator_failure_surfaces_error_no_silent_empty_reply():
    """Phase-1 validation pass Item 2: the brain must NOT swallow an
    orchestrator exception into an empty `chat_response`. Two-frame
    contract:

      1. HUP `error` frame, code 4001, name `orchestrator_error`,
         carrying the failure text — strict-error-aware clients
         match on this.
      2. Legacy `chat_response` with `error: <str>` populated on
         the payload — chat-only clients still surface the real
         failure.

    Pre-fix, the brain set `response_text = ""` and emitted a
    `chat_response` with no error field, leaving the phone showing
    an empty assistant bubble (audit-r7 brief 1 §8 lie).
    """
    mock = _mock_state_with_supervisor()
    mock.orchestrator.handle_command = AsyncMock(
        side_effect=RuntimeError("LLM hard fail: budget exceeded"),
    )

    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(
                ws,
                node_id="phone-chat-broken",
                node_type="phone",
                capabilities=["voice"],
            )
            ws.send_json(
                {
                    "type": "chat_request",
                    "hup_version": "1.3.0",
                    "ts": 1734369922.123,
                    "payload": {
                        "session_id": "phone-session-broken",
                        "text": "this turn will explode",
                        "reply_mode": "final",
                        "channel": "chat",
                        "reply_to": None,
                    },
                }
            )

            error_frame = ws.receive_json()
            assert error_frame["type"] == "error"
            assert error_frame["payload"]["code"] == 4001
            assert error_frame["payload"]["name"] == "orchestrator_error"
            assert "LLM hard fail" in error_frame["payload"]["message"]

            reply = ws.receive_json()
            assert reply["type"] == "chat_response"
            assert reply["payload"]["session_id"] == "phone-session-broken"
            assert reply["payload"]["text"] == ""
            assert reply["payload"].get("error")
            assert "LLM hard fail" in reply["payload"]["error"]


def test_voice_session_start_registers_voice_session():
    mock = _mock_state_with_supervisor()
    mock.voice_router = MagicMock()
    # PR #61 wire-up: daemon_session now awaits
    # voice_router.open_session(mode=...) to dispatch to the selected
    # voice backend. MagicMock's default return isn't awaitable, so it
    # has to be stubbed explicitly.
    #
    # It used to be stubbed as returning None, which is what the real
    # router returns on EVERY failure. This test therefore asserted
    # that a session that never opened is recorded as "allowed", and
    # stayed green over the defect for as long as it existed. A
    # successful open returns a session handle, so this stub returns
    # one; the failure path is pinned in
    # tests/test_voice_session_start_open_failure.py.
    mock.voice_router.open_session = AsyncMock(return_value=MagicMock())

    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="phone-voice", node_type="phone")
            ws.send_json(
                {
                    "type": "voice_session_start",
                    "hup_version": "1.3.0",
                    "ts": 1734369923.0,
                    "payload": {
                        "stream_id": "voice-stream-1",
                        "sample_rate": 16000,
                        "channels": 1,
                        "language_hint": "en-US",
                        "mode": "push_to_talk",
                        "interrupt_policy": "barge_in",
                        "camera_linked": True,
                    },
                }
            )
            _flush_with_known_error(ws)

    cfg_call = mock.voice_router.register_voice_config.call_args
    assert cfg_call.args[0] == "phone-voice"
    assert cfg_call.args[1]["sample_rate"] == 16000
    assert cfg_call.args[1]["channels"] == 1
    mock.voice_router.bind_node_to_session.assert_called_with("phone-voice", "voice-stream-1")
    assert _phone_recorded(mock, "voice_session_start", "allowed")


def test_voice_interrupt_cancels_inflight_tts():
    mock = _mock_state_with_supervisor()
    realtime_session = MagicMock()
    realtime_session.cancel_response = AsyncMock()
    realtime = MagicMock()
    realtime.get_session = MagicMock(return_value=realtime_session)
    realtime._node_to_session = {}
    voice_router = MagicMock()
    voice_router._realtime = realtime
    voice_router._gemini = None
    mock.voice_router = voice_router

    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="phone-interrupt", node_type="phone")
            ws.send_json(
                {
                    "type": "voice_interrupt",
                    "hup_version": "1.3.0",
                    "ts": 1734369924.0,
                    "payload": {"stream_id": "voice-stream-1", "reason": "barge_in"},
                }
            )
            _flush_with_known_error(ws)

    assert realtime_session.cancel_response.await_count == 1
    assert _phone_recorded(mock, "voice_interrupt", "allowed")


def test_genui_event_routes_to_handle_app_action():
    mock = _mock_state_with_supervisor()
    app_action_mock = AsyncMock()

    with patch("agents.ui_handlers._handle_app_action", app_action_mock):
        with _node_client(mock) as client:
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, node_id="phone-genui", node_type="phone")
                ws.send_json(
                    {
                        "type": "genui_event",
                        "hup_version": "1.3.0",
                        "ts": 1734369925.0,
                        "payload": {
                            "app_id": "feral.notes",
                            "surface_id": "today",
                            "event_type": "tap",
                            "action_id": "approve",
                            "value": {"action": "approve"},
                        },
                    }
                )
                _flush_with_known_error(ws)

    kwargs = app_action_mock.await_args.kwargs
    assert kwargs["app_id"] == "feral.notes"
    assert kwargs["screen_id"].startswith("feral.notes:today:")
    assert kwargs["action_id"] == "approve"
    assert _phone_recorded(mock, "genui_event", "allowed")


def test_peripheral_bridge_register_stores_devices():
    mock = _mock_state_with_supervisor()

    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="phone-bridge", node_type="phone")
            ws.send_json(
                {
                    "type": "peripheral_bridge_register",
                    "hup_version": "1.3.0",
                    "ts": 1734369926.0,
                    "payload": {
                        "bridge_id": "bridge-1",
                        "platform": "android",
                        "devices": [
                            {
                                "device_id": "smart_glasses_01",
                                "kind": "glasses",
                                "protocol": "web_bluetooth",
                                "capabilities": ["imu", "notifications"],
                                "status": "connected",
                                "manifest": {},
                            }
                        ],
                        "expires_at": "2026-04-30T12:00:00Z",
                    },
                }
            )
            _flush_with_known_error(ws)

    assert mock.device_registry.register_device.called
    assert mock.devices["bridge-1"]["devices"] == ["smart_glasses_01"]
    assert _phone_recorded(mock, "peripheral_bridge_register", "allowed")


def test_backchannel_request_persists_sqlite(tmp_path):
    mock = _mock_state_with_supervisor()

    with patch("config.loader.feral_home", return_value=tmp_path):
        with _node_client(mock) as client:
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, node_id="phone-backchannel", node_type="phone")
                ws.send_json(
                    {
                        "type": "backchannel_request",
                        "hup_version": "1.3.0",
                        "ts": 1734369927.0,
                        "payload": {
                            "request_id": "bc-1",
                            "device_id": "phone-backchannel",
                            "kind": "bug",
                            "payload": {"summary": "voice dropped mid-turn"},
                            "status": "pending",
                        },
                    }
                )
                _flush_with_known_error(ws)

    db_path = tmp_path / "backchannel_requests.db"
    assert db_path.exists()
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT id, device_id, kind, status FROM backchannel_requests WHERE id = ?",
            ("bc-1",),
        ).fetchone()
    assert row == ("bc-1", "phone-backchannel", "bug", "pending")
    assert _phone_recorded(mock, "backchannel_request", "allowed")


@pytest.mark.parametrize(
    "message_type,malformed_payload",
    [
        ("chat_request", {"text": "missing session_id"}),
        ("chat_response", {"session_id": "phone-session-1"}),
        ("voice_session_start", {"stream_id": "voice-stream-1"}),
        # voice_interrupt no longer has any required fields — see
        # test_phone_envelopes.py for the same parametrisation update.
        ("genui_event", {"app_id": "feral.notes"}),
        ("peripheral_bridge_register", {"bridge_id": "bridge-1", "platform": "android"}),
        ("backchannel_request", {"kind": "bug"}),
    ],
)
def test_malformed_phone_payload_sends_protocol_error(message_type, malformed_payload):
    mock = _mock_state_with_supervisor()

    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(ws, node_id="phone-malformed", node_type="phone")
            ws.send_json(
                {
                    "type": message_type,
                    "hup_version": "1.3.0",
                    "ts": 1734369928.0,
                    "payload": malformed_payload,
                }
            )
            err = ws.receive_json()
            assert err["type"] == "error"
            assert err["payload"]["code"] == 1003
            assert "payload validation failed" in err["payload"]["message"]


# ── HUP v1.3.0: location_update over the daemon WS ───────────────


def test_location_update_routes_to_location_engine():
    """Phone-streamed location should reach LocationEngine.update_location
    and increment the geofence-event count returned in audit detail."""
    mock = _mock_state_with_supervisor()
    mock.location_engine = MagicMock()
    mock.location_engine.update_location = AsyncMock(
        return_value=[{"fence_id": "home", "event": "enter"}],
    )

    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(
                ws,
                node_id="phone-loc-1",
                node_type="phone",
                capabilities=["location"],
            )
            ws.send_json({
                "type": "location_update",
                "hup_version": "1.3.0",
                "ts": 1734369999.0,
                "payload": {
                    "node_id": "phone-loc-1",
                    "lat": 37.7749,
                    "lon": -122.4194,
                    "accuracy_m": 12.5,
                    "source": "browser_node",
                },
            })

    mock.location_engine.update_location.assert_awaited_once()
    args, kwargs = mock.location_engine.update_location.await_args
    assert args[0] == 37.7749
    assert args[1] == -122.4194
    assert kwargs.get("source") == "browser_node"
    assert _phone_recorded(mock, "location_update", decision="accepted")


def test_location_update_skips_null_island():
    """(0, 0) is browser-geolocation's pre-fix sentinel; we MUST NOT
    pass it through to LocationEngine or every phone enters/exits a
    Null Island geofence at startup."""
    mock = _mock_state_with_supervisor()
    mock.location_engine = MagicMock()
    mock.location_engine.update_location = AsyncMock(return_value=[])

    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(
                ws,
                node_id="phone-loc-2",
                node_type="phone",
                capabilities=["location"],
            )
            ws.send_json({
                "type": "location_update",
                "hup_version": "1.3.0",
                "ts": 1734369999.0,
                "payload": {"node_id": "phone-loc-2", "lat": 0, "lon": 0},
            })

    mock.location_engine.update_location.assert_not_called()
    assert _phone_recorded(mock, "location_update", decision="skipped")


def test_location_update_denies_when_engine_missing():
    """If LocationEngine isn't initialised on the brain, the envelope
    is rejected cleanly (audited as denied), not 500'd."""
    mock = _mock_state_with_supervisor()
    mock.location_engine = None

    with _node_client(mock) as client:
        with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
            _register_node(
                ws,
                node_id="phone-loc-3",
                node_type="phone",
                capabilities=["location"],
            )
            ws.send_json({
                "type": "location_update",
                "hup_version": "1.3.0",
                "ts": 1734369999.0,
                "payload": {
                    "node_id": "phone-loc-3",
                    "lat": 1.0, "lon": 2.0,
                },
            })

    assert _phone_recorded(mock, "location_update", decision="denied")
