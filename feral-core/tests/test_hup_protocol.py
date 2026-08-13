"""HUP protocol drift fixes — end-to-end contract tests.

Covers the canonical HUP wire shape between brain and daemons:
  - node_register → node_ack
  - node_heartbeat (not legacy heartbeat)
  - hup_action_request / hup_action_response
  - node_bye → WS close 1000
  - error frame on protocol violations

Version assertions reference ``models.protocol.HUP_VERSION`` rather
than a literal string so the test cannot drift from the canonical
constant. The unification gate lives in
``feral-core/tests/test_hup_version_unified.py`` — that pins every
shipped surface (spec, Python brain, Python SDK, TS SDK, Swift SDK,
iOS Info.plist) to the same canonical string.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from models.protocol import HUP_VERSION

pytestmark = pytest.mark.no_auto_feral_home

_TEST_NODE_KEY = "expected-node-key"


def _skip_heartbeat_create_task(orig_create_task):
    def _wrapped(coro, *a, **kw):
        code = getattr(coro, "cr_code", None)
        if code is not None and code.co_name == "_state_heartbeat":
            t = MagicMock()
            t.cancel = MagicMock()
            return t
        return orig_create_task(coro, *a, **kw)
    return _wrapped


def _make_mock_state() -> MagicMock:
    from collections import deque
    s = MagicMock()
    s.sessions = {}
    s.daemons = {}
    s.devices = {}
    s.init = AsyncMock(return_value=None)
    s.cron_service = None
    s.memory = MagicMock()
    s.memory.working_push = MagicMock()
    s.memory.working_get = MagicMock(return_value=[])
    s.memory.working_clear = MagicMock()
    s.memory.start_background_tasks = MagicMock()
    s.memory.stats.return_value = {"notes": 0, "episodes": 0, "knowledge_triples": 0}
    s.memory.list_recent.return_value = []
    s.memory.search.return_value = []
    s.memory.save.return_value = {"id": "n1", "content": "x"}
    s.memory.knowledge_query.return_value = []
    s.memory.wiki_list_pages.return_value = []
    s.memory.wiki_stats.return_value = {"pages": 0}
    s.memory.episode_recent.return_value = []
    s.memory.log_recent.return_value = []
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
    s.identity_workspace = MagicMock()
    s.identity_workspace.read_soul.return_value = "soul"
    s.identity_workspace.read_memory.return_value = ""
    s.identity_workspace.maintenance_cycle = AsyncMock()
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
    s.activity_log = deque()
    s.baseline_engine = MagicMock()
    s.baseline_engine.summary.return_value = {"metrics_tracked": 0, "recent_alerts": 0, "categories": []}
    s.baseline_engine.get_all_baselines.return_value = []
    s.baseline_engine.get_alerts.return_value = []
    s.somatic_engine = None
    return s


def _brain_patchers(mock: MagicMock):
    greet = MagicMock(return_value="Test greeting")
    return [
        patch("api.state.state", mock),
        patch("api.server.state", mock),
        patch("api.server._build_greeting", greet),
        patch("api.server.FERAL_API_KEY", "test-key"),
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


@contextmanager
def _node_client(mock: MagicMock):
    pairing_store = MagicMock()
    pairing_store.verify_device = MagicMock(return_value=None)
    from tests.test_server_websocket import _reimport_api_server_before_patching

    _reimport_api_server_before_patching()
    mock.device_pairing_store = pairing_store
    with ExitStack() as stack:
        for p in _brain_patchers(mock):
            stack.enter_context(p)
        from api.server import app
        stack.enter_context(patch("api.server.NODE_API_KEY", _TEST_NODE_KEY))
        yield TestClient(app, raise_server_exceptions=False)


def _register_node(ws, node_id="test-node-hup", node_type="sensor", capabilities=None):
    ws.send_json({
        "type": "node_register",
        "payload": {
            "node_id": node_id,
            "node_type": node_type,
            "platform": "linux",
            "capabilities": capabilities or [],
        },
    })
    return ws.receive_json()


class TestNodeRegisterReturnsNodeAck:
    def test_node_ack_shape(self):
        mock = _make_mock_state()
        with _node_client(mock) as client:
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                ack = _register_node(ws, capabilities=["camera", "heart_rate"])
                assert ack["type"] == "node_ack"
                # v2026.5.32 (audit-r12 D3): both the envelope-level
                # `hup_version` and the payload-level `hup_version` MUST
                # equal the canonical constant. Pre-r12 these hardcoded
                # "1.2.0" while the constant was already "1.3.0".
                assert ack["hup_version"] == HUP_VERSION
                p = ack["payload"]
                assert p["hup_version"] == HUP_VERSION
                assert p["heartbeat_ms"] == 10000
                assert "session_token" in p and len(p["session_token"]) > 0
                assert set(p["capabilities"]) == {"camera", "heart_rate"}

    def test_node_ack_not_text_response(self):
        mock = _make_mock_state()
        with _node_client(mock) as client:
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                ack = _register_node(ws)
                assert ack["type"] != "text_response"


class TestNodeHeartbeat:
    def test_node_heartbeat_accepted(self):
        mock = _make_mock_state()
        with _node_client(mock) as client:
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, node_id="hb-node")
                ws.send_json({"type": "node_heartbeat", "payload": {"ts": 1234567890.0}})
            mock.hardware_mesh.node_health.record_heartbeat.assert_called_with("hb-node")

    def test_old_heartbeat_literal_not_matched(self):
        """Brain handler must NOT have a branch for the bare 'heartbeat' type."""
        from tests.test_server_websocket import _reimport_api_server_before_patching

        _reimport_api_server_before_patching()
        with ExitStack() as stack:
            mock = _make_mock_state()
            for p in _brain_patchers(mock):
                stack.enter_context(p)
            import api.server as srv
            src = inspect.getsource(srv.daemon_session)
            assert 'msg.type == "heartbeat"' not in src


class TestHupActionResponse:
    def test_action_response_resolves_mesh_future(self):
        mock = _make_mock_state()
        with _node_client(mock) as client:
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, node_id="action-node")
                ws.send_json({
                    "type": "hup_action_response",
                    "payload": {
                        "action_id": "req-001",
                        "success": True,
                        "result": {"vibrated_ms": 250},
                        "error": None,
                        "duration_ms": 178,
                    },
                })
            mock.hardware_mesh.resolve_invoke.assert_called_once()
            call_args = mock.hardware_mesh.resolve_invoke.call_args
            assert call_args[0][0] == "req-001"


class TestBrainEmitsHupActionRequest:
    def test_mesh_invoke_sends_hup_action_request(self):
        """hardware/mesh.py must emit type='hup_action_request', not 'command'."""
        import hardware.mesh as mesh_mod
        src = inspect.getsource(mesh_mod)
        assert '"hup_action_request"' in src
        assert '"type": "command"' not in src

    def test_tool_runner_sends_hup_action_request(self):
        """agents/tool_runner.py must emit type='hup_action_request'."""
        import agents.tool_runner as tr_mod
        src = inspect.getsource(tr_mod)
        assert '"hup_action_request"' in src
        assert '"type": "command"' not in src


class TestHupExecuteDeprecated:
    def test_hup_execute_not_emitted_on_wire(self):
        """hardware/protocol.py must not use 'hup_execute' as the on-wire type literal."""
        import hardware.protocol as hp
        src = inspect.getsource(hp.WebSocketDeviceAdapter.execute)
        assert '"hup_execute"' not in src
        assert '"hup_action_request"' in src


class TestNodeBye:
    def test_node_bye_closes_ws(self):
        mock = _make_mock_state()
        with _node_client(mock) as client:
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws, node_id="bye-node")
                ws.send_json({
                    "type": "node_bye",
                    "payload": {"reason": "shutdown", "restart_in_s": 0},
                })
            mock.hardware_mesh.on_node_disconnected.assert_called_with("bye-node")


class TestProtocolError:
    def test_unknown_type_gets_error_frame(self):
        mock = _make_mock_state()
        with _node_client(mock) as client:
            with client.websocket_connect(f"/v1/node?api_key={_TEST_NODE_KEY}") as ws:
                _register_node(ws)
                ws.send_json({"type": "totally_bogus_type", "payload": {}})
                err = ws.receive_json()
                assert err["type"] == "error"
                assert err["payload"]["code"] == 1002
                # v2026.5.32 (audit-r12 D3): the error envelope advertises
                # the canonical HUP version; pre-r12 it hardcoded "1.2.0".
                assert err["hup_version"] == HUP_VERSION


class TestHupVersionCoherence:
    """v2026.5.32 (audit-r12 D3): every ``hup_version`` literal that the
    brain emits over the wire MUST equal ``models.protocol.HUP_VERSION``.
    Pre-r12 three envelopes hardcoded ``"1.2.0"`` while the constant was
    already ``"1.3.0"``; iOS clients saw a brain that announced one
    version on register and another on every other frame.
    """

    def test_no_stale_hup_version_literals_in_server(self):
        """Static scan of ``api/server.py``: every string literal value
        in a ``"hup_version"`` dict entry must equal ``HUP_VERSION``."""
        import ast
        from pathlib import Path

        server_path = Path(__file__).parent.parent / "api" / "server.py"
        tree = ast.parse(server_path.read_text())

        drifted: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not isinstance(key, ast.Constant) or key.value != "hup_version":
                    continue
                # Constant string value → must equal HUP_VERSION.
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    if value.value != HUP_VERSION:
                        drifted.append((value.lineno, value.value))
                # ast.Name (e.g. HUP_VERSION) is fine.

        assert not drifted, (
            "api/server.py contains stale `hup_version` literal(s) that "
            "do not equal `HUP_VERSION` from models.protocol. Either use "
            "the constant directly or update the literal. "
            "Lines: " + ", ".join(f"{ln}={val!r}" for ln, val in drifted)
        )


class TestMessageTypesRegistry:
    def test_canonical_types_in_registry(self):
        from models.protocol import MESSAGE_TYPES
        for canonical in ("node_ack", "node_heartbeat", "hup_action_request",
                          "hup_action_response", "node_bye"):
            assert canonical in MESSAGE_TYPES, f"{canonical} missing from MESSAGE_TYPES"

    def test_deprecated_aliases(self):
        from models.protocol import DEPRECATED_TYPE_ALIASES
        assert DEPRECATED_TYPE_ALIASES["command"] == "hup_action_request"
        assert DEPRECATED_TYPE_ALIASES["hup_execute"] == "hup_action_request"
        assert DEPRECATED_TYPE_ALIASES["heartbeat"] == "node_heartbeat"
