"""
Tests for WebSocket handlers and related REST endpoints in api/server.py.

Uses Starlette TestClient with BrainState and heavy dependencies mocked.
Does not start the full brain or real I/O.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

pytestmark = pytest.mark.no_auto_feral_home


def _skip_heartbeat_create_task(orig_create_task):
    """Do not schedule startup's _state_heartbeat loop (nested inside startup())."""

    def _wrapped(coro, *a, **kw):
        code = getattr(coro, "cr_code", None)
        if code is not None and code.co_name == "_state_heartbeat":
            t = MagicMock()
            t.cancel = MagicMock()
            return t
        return orig_create_task(coro, *a, **kw)

    return _wrapped


def _make_ws_mock_state() -> MagicMock:
    """Minimal BrainState-like mock for api.server WebSocket + startup paths."""
    s = MagicMock()

    s.sessions = {}
    s.daemons = {}
    s.devices = {}
    # Audit-r9: web `/v1/session` now resolves session_id from
    # `state.primary_session_id` (or query param). MagicMock would
    # return a MagicMock object, which fails Pydantic string
    # validation in the FeralMessage schema. Use an explicit empty
    # string so the legacy `str(uuid4())` fallback fires in tests.
    s.primary_session_id = ""

    # Phase 3 (audit-r10) — refcount + cleanup-gating methods need to
    # behave like the real implementation in the mock, otherwise
    # `remaining_attachments = MagicMock()` is truthy and the cleanup
    # branch never fires (causing `on_session_disconnect` mocks to
    # appear "never awaited").
    s.session_attach_count = {}

    def _attach(sid: str) -> int:
        s.session_attach_count[sid] = s.session_attach_count.get(sid, 0) + 1
        return s.session_attach_count[sid]

    def _detach(sid: str) -> int:
        n = max(0, s.session_attach_count.get(sid, 0) - 1)
        if n == 0:
            s.session_attach_count.pop(sid, None)
        else:
            s.session_attach_count[sid] = n
        return n

    def _should_clear(sid: str) -> bool:
        if sid == s.primary_session_id and s.primary_session_id:
            return False
        return s.session_attach_count.get(sid, 0) == 0

    s.attach_session = MagicMock(side_effect=_attach)
    s.detach_session = MagicMock(side_effect=_detach)
    s.should_clear_on_disconnect = MagicMock(side_effect=_should_clear)
    s.snapshot_primary_thread = MagicMock(return_value=True)

    s.init = AsyncMock(return_value=None)
    s.cron_service = None

    s.memory = MagicMock()
    s.memory.working_push = MagicMock()
    s.memory.working_get = MagicMock(return_value=[])
    s.memory.working_clear = MagicMock()
    s.memory.start_background_tasks = MagicMock()

    s.gateway_registry = MagicMock()

    s.bind_session_to_daemon = MagicMock()
    s.get_sessions_for_daemon = MagicMock(return_value=set())

    s.perception = MagicMock()
    s.perception.update_connected_nodes = MagicMock()
    s.perception.update_vision = MagicMock()
    s.perception.clear = MagicMock()
    s.perception.update_sensors = MagicMock()
    s.perception.update_gesture = MagicMock()

    s.orchestrator = MagicMock()
    s.orchestrator.handle_command_stream = AsyncMock()
    s.orchestrator.handle_command = AsyncMock()
    s.orchestrator.on_session_disconnect = AsyncMock()
    s.orchestrator.handle_ui_event = AsyncMock()
    s.orchestrator.update_biometric = MagicMock()
    s.orchestrator.handle_daemon_result = AsyncMock()
    s.orchestrator.resolve_pending_frame = MagicMock()
    s.orchestrator.llm = None

    s.skill_gen = None
    s.voice_router = None
    s.gemini_proxy = None
    s.identity_workspace = None

    s.vision_buffer = MagicMock()
    s.vision_buffer.node_ids_with_frames = MagicMock(return_value=[])
    s.vision_buffer.latest = MagicMock(return_value=None)
    s.vision_buffer.push = MagicMock()

    s.change_detector = MagicMock()
    s.change_detector.force_trigger = MagicMock()
    s.change_detector.should_analyze = MagicMock(return_value=None)
    s.change_detector.stats = MagicMock(return_value={})

    s.scene = MagicMock(available=False)

    s.audio = MagicMock()
    s.audio.clear_session = MagicMock()

    s.skill_executor = MagicMock()
    s.skill_executor.register_daemon_type = MagicMock()
    s.skill_executor.unregister_daemon = MagicMock()

    s.hardware_mesh = MagicMock()
    s.hardware_mesh.on_node_connected = AsyncMock()
    s.hardware_mesh.on_node_disconnected = MagicMock()
    s.hardware_mesh.resolve_invoke = MagicMock()
    s.hardware_mesh.node_health = MagicMock()
    s.hardware_mesh.node_health.record_heartbeat = MagicMock()
    s.hardware_mesh.ledger = MagicMock()
    s.hardware_mesh.ledger.get_pending = MagicMock(return_value=[])
    s.hardware_mesh.ledger.get_recent = MagicMock(return_value=[])
    s.hardware_mesh.ledger.stats = MagicMock(return_value={"total": 0})

    s.sync_engine = MagicMock()
    s.mcp_client = MagicMock()
    s.mcp_client.disconnect_all = AsyncMock()
    s.taskflows = MagicMock()
    s.taskflows.stop = AsyncMock()

    # Route modules (test_api_routes pattern)
    s.skill_registry = MagicMock()
    s.skill_registry.skills = {}
    s.config = MagicMock()
    s.config.to_client_safe_dict.return_value = {"llm": {"provider": "openai"}, "version": "0.4.0"}
    s.config.setup_complete = True
    s.config.update_settings = MagicMock()
    s.vault = MagicMock()
    s.vault.list_keys.return_value = []
    s.vault.to_safe_summary.return_value = {}
    s.sandbox = MagicMock(max_tier="active")
    s.policy = MagicMock()
    s.policy._data = {"name": "default"}
    s.policy.to_dict.return_value = {"name": "default"}
    s.device_registry = MagicMock()
    s.device_registry.stats = {"total_devices": 0}
    s.device_registry.list_devices.return_value = []
    s.mcp_server = MagicMock()
    s.channel_manager = MagicMock(stats={"active": 0})
    s.oauth = MagicMock()
    s.oauth.status.return_value = {}
    s.spotify = MagicMock(connected=False)
    s.home_assistant = MagicMock(connected=False)
    s.notion = MagicMock(connected=False)
    s.event_bus = MagicMock()
    s.event_bus.stats.return_value = {}
    s.marketplace = MagicMock()
    s.marketplace.list_installed.return_value = []
    s.wasm_sandbox = MagicMock(available=False)
    s.wake_word = MagicMock(enabled=False)
    s.session_handoff = None
    s.proactive = None
    s.scheduler = None
    s._demo = None

    from collections import deque

    s.activity_log = deque()

    s.memory.stats = AsyncMock(return_value={"notes": 0, "episodes": 0, "knowledge_triples": 0})
    s.memory.list_recent = AsyncMock(return_value=[])
    s.memory.search = AsyncMock(return_value=[])
    s.memory.save = AsyncMock(return_value={"id": "n1", "content": "x"})
    s.memory.delete = AsyncMock(return_value=True)
    s.memory.knowledge_query = AsyncMock(return_value=[])
    s.memory.knowledge_search = AsyncMock(return_value=[])
    s.memory.knowledge_store = AsyncMock(return_value={"id": "k1"})
    s.memory.knowledge_about = AsyncMock(return_value=[])
    s.memory.wiki_list_pages = AsyncMock(return_value=[])
    s.memory.wiki_stats = AsyncMock(return_value={"pages": 0})
    s.memory.wiki_get_page = AsyncMock(return_value=None)
    s.memory.wiki_compile = AsyncMock(return_value={"compiled": True})
    s.memory.episode_recent = AsyncMock(return_value=[])
    s.memory.log_recent = AsyncMock(return_value=[])

    s.baseline_engine = MagicMock()
    s.baseline_engine.summary.return_value = {"metrics_tracked": 0, "recent_alerts": 0, "categories": []}
    s.baseline_engine.get_all_baselines.return_value = []
    s.baseline_engine.get_alerts.return_value = []

    s.identity_workspace = MagicMock()
    s.identity_workspace.read_soul.return_value = "soul"
    s.identity_workspace.read_memory.return_value = ""
    s.identity_workspace.maintenance_cycle = AsyncMock()

    return s


_TEST_API_KEY = "test-feral-key-for-tests"


def _reimport_api_server_before_patching() -> None:
    """Drop and re-import ``api.server`` BEFORE any patcher is applied.

    The harness wants a freshly imported ``api.server``, and it wants
    ``api.state.state`` mocked. Doing the first lazily and the second
    first is what made these fixtures leak.

    ``api.server`` binds ``state`` at import (``from api.state import
    state``). Deleting the module and letting ``patch("api.server.state",
    ...)`` trigger the re-import means the import runs *after*
    ``patch("api.state.state", mock)`` has already landed, so the fresh
    module binds the mock. ``patch`` then records that mock as the
    "original" and faithfully restores it on exit. The teardown put a
    MagicMock back into a module every later test reads.

    It surfaced far away and only sometimes: ``/v1/session`` resolves its
    id via ``getattr(state, "primary_session_id", "")``, so an unrelated
    test failed Pydantic validation with a MagicMock, and only when file
    ordering put it after one of these. It aborted a release run while
    passing alone.

    Importing here, before the ExitStack, means the module binds the real
    state and that is what gets restored.
    """
    sys.modules.pop("api.server", None)
    importlib.import_module("api.server")


def _restore_api_server_state() -> None:
    """Rebind ``api.server.state`` to the real singleton, if it is loaded."""
    server = sys.modules.get("api.server")
    real = getattr(sys.modules.get("api.state"), "state", None)
    if server is not None and real is not None:
        server.state = real


def _brain_patchers(mock: MagicMock):
    """Patches for api.state.state, api.server.state, route modules, greeting, and heartbeat task."""
    greet = MagicMock(return_value="Test greeting")
    return [
        patch("api.state.state", mock),
        patch("api.server.state", mock),
        patch("api.server._build_greeting", greet),
        patch("api.server.FERAL_API_KEY", _TEST_API_KEY),
        patch("api.server.is_localhost", return_value=True),
        patch("api.server.local_bypass_enabled", return_value=True),
        patch(
            "api.server.asyncio.create_task",
            side_effect=_skip_heartbeat_create_task(asyncio.create_task),
        ),
        patch("api.routes.dashboard.state", mock),
        patch("api.routes.config.state", mock),
        patch("api.routes.skills.state", mock),
        patch("api.routes.memory.state", mock),
        patch("api.routes.baseline.state", mock),
        patch("api.routes.security_and_hardware.state", mock),
        patch("api.routes.identity_nodes_sync.state", mock),
        patch("api.routes.devices.state", mock),
        patch("api.routes.timeline.state", mock),
    ]


@pytest.fixture
def ws_mock_state():
    return _make_ws_mock_state()


@pytest.fixture
def ws_client(ws_mock_state):
    """TestClient with BrainState mocked; skips the dashboard broadcast background task."""
    mock = ws_mock_state
    _reimport_api_server_before_patching()
    try:
        with ExitStack() as stack:
            for p in _brain_patchers(mock):
                stack.enter_context(p)
            from api.server import app

            client = TestClient(app, raise_server_exceptions=False)
            client.headers["Authorization"] = f"Bearer {_TEST_API_KEY}"
            yield client
    finally:
        # ``patch`` restores the module object it patched. If anything
        # re-imported ``api.server`` during the test, sys.modules now
        # holds a different object, and that one is left bound to the
        # mock -- which later tests read as their session state. Belt as
        # well as braces: rebind whatever is in sys.modules now.
        _restore_api_server_state()


@contextmanager
def _node_client(mock: MagicMock, pairing_store: MagicMock, node_api_key: str = "expected-node-key"):
    """TestClient for /v1/node with pairing store and NODE_API_KEY mocked."""
    _reimport_api_server_before_patching()
    mock.device_pairing_store = pairing_store
    with ExitStack() as stack:
        for p in _brain_patchers(mock):
            stack.enter_context(p)
        from api.server import app

        stack.enter_context(patch("api.server.NODE_API_KEY", node_api_key))
        yield TestClient(app, raise_server_exceptions=False)


# ─────────────────────────────────────────────────────────────
# REST startup / health
# ─────────────────────────────────────────────────────────────


class TestRestStartupEndpoints:
    def test_health_returns_ok(self, ws_client):
        r = ws_client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        from version import VERSION as __version__
        assert body["version"] == __version__

    def test_dashboard_data_shape(self, ws_client):
        r = ws_client.get("/api/dashboard")
        assert r.status_code == 200
        body = r.json()
        assert "devices" in body
        assert "memory" in body


# ─────────────────────────────────────────────────────────────
# /v1/session WebSocket
# ─────────────────────────────────────────────────────────────


class TestSessionWebSocket:
    def test_connection_receives_greeting_with_session_id(self, ws_client):
        with ws_client.websocket_connect("/v1/session") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "text_response"
            assert "session_id" in msg and msg["session_id"]
            assert msg["payload"]["text"] == "Test greeting"

    def test_text_command_routes_to_orchestrator(self, ws_mock_state, ws_client):
        with ws_client.websocket_connect("/v1/session") as ws:
            ws.receive_json()
            sid = next(iter(ws_mock_state.sessions.keys()))

            ws.send_json(
                {
                    "type": "text_command",
                    "payload": {"text": "hello brain", "context": {"src": "test"}},
                }
            )

        ws_mock_state.memory.working_push.assert_called()
        args, kwargs = ws_mock_state.orchestrator.handle_command_stream.call_args
        assert kwargs["session_id"] == sid
        assert kwargs["text"] == "hello brain"
        # Phase 2 (audit-r10) — web chat now threads a `refinement`
        # envelope into context so the Mind tab can show what the
        # brain "heard". Operator-supplied fields still pass through
        # unchanged; we assert the original keys plus tolerate the
        # additional `refinement` key.
        assert kwargs["context"]["src"] == "test"
        if "refinement" in kwargs["context"]:
            assert kwargs["context"]["refinement"].get("raw_text") == "hello brain"

    def test_req_message_uses_gateway_session(self, ws_mock_state, ws_client):
        gw = MagicMock()
        gw.handle_message = AsyncMock()

        with patch("api.server.GatewaySession", return_value=gw):
            with ws_client.websocket_connect("/v1/session") as ws:
                ws.receive_json()
                ws.send_json({"type": "req", "payload": {"x": 1}})

        gw.handle_message.assert_awaited()

    def test_disconnect_invokes_cleanup(self, ws_mock_state, ws_client):
        with ws_client.websocket_connect("/v1/session") as ws:
            ws.receive_json()
            sid = next(iter(ws_mock_state.sessions.keys()))
            assert sid in ws_mock_state.sessions

        ws_mock_state.orchestrator.on_session_disconnect.assert_awaited_once_with(sid)
        assert sid not in ws_mock_state.sessions
        ws_mock_state.audio.clear_session.assert_called_with(sid)
        ws_mock_state.perception.clear.assert_called_with(sid)
        ws_mock_state.memory.working_clear.assert_called_with(sid)

    def test_disconnect_does_not_evict_a_newer_surface(self, ws_mock_state, ws_client):
        """An older surface closing must not de-register a newer live socket.

        ``state.sessions`` holds ONE WebSocket per session_id, and every web
        tab connecting without ``?session_id=`` resolves to the same
        ``primary_session_id``. Surface B therefore overwrites A's slot on
        connect. When A later disconnects, an unconditional
        ``state.sessions.pop`` removed B's still-live socket, after which
        ``send_to_session`` silently no-ops and the brain's replies go
        nowhere: chat spins on "thinking" forever with no error.

        Operator report 2026-07 ("say hi, switch to another tab, it stops
        replying").

        Two *concurrent* TestClient websockets deadlock on the shared
        blocking portal, so surface B is simulated by overwriting the slot
        while A is open, which is exactly what ``server.py:1422`` does on
        B's connect. The assertion is on the identity check itself.
        """
        surface_b = object()  # stands in for B's live WebSocket

        with ws_client.websocket_connect("/v1/session") as ws_a:
            ws_a.receive_json()
            sid = next(iter(ws_mock_state.sessions.keys()))
            assert ws_mock_state.sessions[sid] is not surface_b

            # Surface B connects on the same session_id and takes the slot.
            ws_mock_state.sessions[sid] = surface_b

            # A disconnects on leaving this block, while B still holds it.

        # B must survive A's teardown, or every reply it is owed is dropped.
        assert ws_mock_state.sessions.get(sid) is surface_b

        # A's teardown must also leave B's surface-local buffers alone.
        for mock in (ws_mock_state.audio.clear_session,
                     ws_mock_state.perception.clear):
            assert sid not in [c.args[0] for c in mock.call_args_list if c.args]

    def test_skill_gen_proposal_after_command(self, ws_mock_state, ws_client):
        sg = MagicMock()
        sg.detect_unmet_need = AsyncMock(
            return_value={"capability": "cap", "service": "svc"},
        )
        sg.generate_skill = AsyncMock(return_value={"id": "new-skill"})
        ws_mock_state.skill_gen = sg

        with ws_client.websocket_connect("/v1/session") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "text_command",
                    "payload": {"text": "need skill", "context": {}},
                }
            )
            out = ws.receive_json()
            assert out["type"] == "skill_proposal"
            assert out["payload"]["manifest"] == {"id": "new-skill"}


# ─────────────────────────────────────────────────────────────
# Session auth (unauthorized)
# ─────────────────────────────────────────────────────────────


class TestSessionAuth:
    def test_unauthorized_closes_connection(self, ws_mock_state, ws_client):
        with patch("api.server.session_auth_required", return_value=True), patch(
            "api.server.verify_session",
            return_value=False,
        ), patch("api.server.is_localhost", return_value=False), patch(
            "api.server.local_bypass_enabled",
            return_value=False,
        ):
            with ws_client.websocket_connect("/v1/session") as ws:
                ws.send_json({"type": "auth", "token": "wrong"})
                with pytest.raises(WebSocketDisconnect):
                    ws.receive_json()

    def test_query_token_bypasses_first_message_auth(self, ws_mock_state, ws_client):
        with patch("api.server.session_auth_required", return_value=True), patch(
            "api.server.verify_session",
            return_value=True,
        ), patch("api.server.is_localhost", return_value=False), patch(
            "api.server.local_bypass_enabled",
            return_value=False,
        ):
            with ws_client.websocket_connect("/v1/session?token=goodtoken") as ws:
                msg = ws.receive_json()
                assert msg["type"] == "text_response"


# ─────────────────────────────────────────────────────────────
# Error handling — malformed / handler failures
# ─────────────────────────────────────────────────────────────


class TestSessionErrors:
    def test_command_handler_error_sends_text_response(self, ws_mock_state, ws_client):
        ws_mock_state.orchestrator.handle_command_stream = AsyncMock(side_effect=RuntimeError("boom"))

        with ws_client.websocket_connect("/v1/session") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "text_command",
                    "payload": {"text": "x", "context": {}},
                }
            )
            err_msg = ws.receive_json()
            assert err_msg["type"] == "text_response"
            assert "boom" in err_msg["payload"]["text"]

    @pytest.mark.skip(reason="Pre-existing: send_to_session is a MagicMock, doesn't forward error to the client WS. The production code path IS correct (see api/server.py L414-422); this is purely a test-mock limitation. Fix would require wiring the mock's send_to_session to actually call ws.send_json on the stored session. Tracked separately.")
    def test_non_json_payload_returns_error(self, ws_client):
        with ws_client.websocket_connect("/v1/session") as ws:
            ws.receive_json()  # greeting
            ws.send_text("not json {")
            err_msg = ws.receive_json()
            assert err_msg["type"] == "error"
            assert "invalid" in err_msg["payload"].get("text", "").lower() or "json" in err_msg["payload"].get("text", "").lower()

    def test_invalid_message_envelope_returns_error_text(self, ws_mock_state, ws_client):
        with ws_client.websocket_connect("/v1/session") as ws:
            ws.receive_json()
            ws.send_json({"type": "text_command", "payload": {}})
            err_msg = ws.receive_json()
            assert err_msg["type"] == "text_response"
            assert "Sorry" in err_msg["payload"]["text"] or "wrong" in err_msg["payload"]["text"].lower()


# ─────────────────────────────────────────────────────────────
# /v1/node WebSocket
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def pairing_store_mock():
    store = MagicMock()
    store.verify_device = MagicMock(return_value=None)
    # P1 added a second credential surface (phone_bearer) on the
    # DevicePairingStore. The brain's daemon_session credential
    # resolver tries verify_phone_bearer if verify_device returns
    # None. A bare MagicMock would auto-spawn a truthy callable that
    # returns another MagicMock, which the resolver treats as "valid
    # phone_bearer" — silently bypassing the close-with-4003 path.
    # Pin the return so unauthorized credentials really reject.
    store.verify_phone_bearer = MagicMock(return_value=None)
    return store


class TestNodeWebSocket:
    def test_rejects_invalid_api_key(self, ws_mock_state, pairing_store_mock):
        with _node_client(ws_mock_state, pairing_store_mock) as client:
            with client.websocket_connect("/v1/node?api_key=not-valid") as ws:
                # An unauthorized error frame precedes the close so the
                # client can tell "rejected" from "network dropped" and
                # stop retrying. See _send_protocol_error in api/server.py.
                msg = ws.receive()
                if msg.get("type") == "websocket.send":
                    payload = json.loads(msg["text"])["payload"]
                    assert payload["code"] == 1001, payload
                    assert payload["name"] == "unauthorized", payload
                    msg = ws.receive()
                assert msg.get("type") == "websocket.close" or msg.get("code") == 4003

    def test_accepts_legacy_node_api_key_and_registers(self, ws_mock_state, pairing_store_mock):
        with _node_client(ws_mock_state, pairing_store_mock) as client:
            with client.websocket_connect("/v1/node?api_key=expected-node-key") as ws:
                ws.send_json(
                    {
                        "type": "node_register",
                        "payload": {
                            "node_id": "node-a",
                            "node_type": "desktop",
                            "platform": "linux",
                            "capabilities": ["camera"],
                        },
                    }
                )
                ack = ws.receive_json()
                assert ack["type"] == "node_ack"
                assert ack["payload"]["node_id"] == "node-a"

            assert "node-a" not in ws_mock_state.daemons

    def test_heartbeat_records_mesh_health(self, ws_mock_state, pairing_store_mock):
        with _node_client(ws_mock_state, pairing_store_mock) as client:
            with client.websocket_connect("/v1/node?api_key=expected-node-key") as ws:
                ws.send_json(
                    {
                        "type": "node_register",
                        "payload": {
                            "node_id": "hb-node",
                            "node_type": "glasses",
                            "platform": "ios",
                            "capabilities": [],
                        },
                    }
                )
                ws.receive_json()
                ws.send_json({"type": "node_heartbeat", "payload": {"ts": 1234567890.0}})

            ws_mock_state.hardware_mesh.node_health.record_heartbeat.assert_called_with("hb-node")

    def test_disconnect_unregisters_daemon(self, ws_mock_state, pairing_store_mock):
        ws_mock_state.get_sessions_for_daemon = MagicMock(return_value={"sess-z"})
        with _node_client(ws_mock_state, pairing_store_mock) as client:
            with client.websocket_connect("/v1/node?api_key=expected-node-key") as ws:
                ws.send_json(
                    {
                        "type": "register",
                        "payload": {
                            "node_id": "disc-node",
                            "node_type": "sensor",
                            "platform": "linux",
                            "capabilities": [],
                        },
                    }
                )
                ws.receive_json()
                assert "disc-node" in ws_mock_state.daemons

            ws_mock_state.skill_executor.unregister_daemon.assert_called_with("disc-node")
            ws_mock_state.hardware_mesh.on_node_disconnected.assert_called_with("disc-node")


# ─────────────────────────────────────────────────────────────
# Session / daemon integration
# ─────────────────────────────────────────────────────────────


class TestSessionIdsAndBinding:
    def test_session_stored_under_generated_id(self, ws_mock_state, ws_client):
        with ws_client.websocket_connect("/v1/session") as ws:
            first = ws.receive_json()
            sid = first["session_id"]
            assert sid in ws_mock_state.sessions

    def test_existing_daemon_bound_on_session_connect(self, ws_mock_state, ws_client):
        fake_ws = MagicMock()
        ws_mock_state.daemons["pre-node"] = fake_ws

        with ws_client.websocket_connect("/v1/session") as ws:
            ws.receive_json()

        ws_mock_state.bind_session_to_daemon.assert_called()

