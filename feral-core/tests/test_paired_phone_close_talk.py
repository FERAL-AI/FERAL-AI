"""Actual paired HUP ingress and emitted GA configuration; no provider calls."""
import asyncio
import json
import threading
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bridges.client_voice_attempt import VoiceAttemptBinding, voice_attempt_scope
from security.approval_ingress import PairedDevicePrincipal
from security.device_pairing import DevicePairingStore
from voice.realtime_proxy import RealtimeSession

pytest_plugins = ["tests.test_voice_task_handoff"]


class CaptureSocket:
    def __init__(self, signal=None):
        self.events = []
        self.signal = signal

    async def send(self, text):
        self.events.append(json.loads(text))
        if self.signal:
            self.signal.set()

    async def close(self):
        pass


@pytest.fixture
def admitted(tmp_path):
    store = DevicePairingStore(db_path=str(tmp_path / "paired.db"))
    issued = store.pair_device("synthetic phone", kind="browser_node_v2")
    credential = issued["phone_bearer"]
    principal = PairedDevicePrincipal(issued["device_id"], lambda: store.admitted_credential_current(
        device_id=issued["device_id"], credential=credential, bearer_kind="phone_bearer"))
    current = {"value": True}
    binding = VoiceAttemptBinding("synthetic-close-talk", str(uuid4()), object(),
        lambda: current["value"], lambda: True)
    return SimpleNamespace(store=store, issued=issued, principal=principal, binding=binding, current=current)


def session(binding=None):
    with voice_attempt_scope(binding):
        rs = RealtimeSession(session_id="synthetic-close-talk", node_id="synthetic-node", api_key="inert")
    rs._ws = CaptureSocket()
    rs._connected = True
    return rs


@pytest.mark.asyncio
async def test_desktop_and_paired_payload_differ_only_by_input_denoise(admitted):
    desktop = session()
    await desktop.configure()
    admitted.binding._close_talk_principal = admitted.principal
    paired = session(admitted.binding)
    await paired.configure()
    plain, close = desktop._ws.events[0], paired._ws.events[0]
    assert close["session"]["audio"]["input"].pop("noise_reduction") == {"type": "near_field"}
    assert close == plain
    assert plain["session"]["audio"]["input"]["turn_detection"] == {
        "type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 300, "silence_duration_ms": 1000}
    assert "noise_reduction" not in desktop._ws.events[0]["session"]["audio"]["input"]


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["credential", "binding", "producer", "at_wire"])
async def test_profile_never_dispatches_from_retired_authority(admitted, retirement):
    admitted.binding._close_talk_principal = admitted.principal
    rs = session(admitted.binding)
    if retirement == "credential":
        admitted.store.rotate_phone_bearer(admitted.principal.device_id)
    elif retirement == "binding":
        admitted.current["value"] = False
    elif retirement == "producer":
        rs._callback_guard = lambda: False
    else:
        real_send = rs._send

        async def delayed_send(event, **kwargs):
            admitted.store.rotate_phone_bearer(admitted.principal.device_id)
            await real_send(event, **kwargs)
        rs._send = delayed_send
    try:
        await rs.configure()
    except asyncio.CancelledError:
        pass
    assert rs._ws.events == []


@pytest.mark.asyncio
async def test_untyped_client_profile_and_attempt_substitution_do_not_select_denoise(admitted):
    admitted.binding._close_talk_principal = {"owner_verified": True, "type": "near_field"}
    rs = session(admitted.binding)
    await rs.configure()
    assert "noise_reduction" not in rs._ws.events[0]["session"]["audio"]["input"]
    admitted.binding._close_talk_principal = admitted.principal
    rs = session(admitted.binding)
    real_send = rs._send

    async def replace_attempt(event, **kwargs):
        rs._voice_attempt = VoiceAttemptBinding(rs.session_id, str(uuid4()), object(), lambda: True, lambda: True)
        await real_send(event, **kwargs)
    rs._send = replace_attempt
    await rs.configure()
    assert rs._ws.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("node_kind,credential_kind,selected_mode,expected", [
    ("phone", "phone_bearer", "openai_realtime", True),
    ("desktop", "phone_bearer", "openai_realtime", False),
    ("phone", "pair_token", "openai_realtime", False),
    ("phone", "phone_bearer", "gemini_live", True),
])
async def test_authenticated_hup_route_selects_exact_phone_profile(runtime, monkeypatch,
        node_kind, credential_kind, selected_mode, expected):
    from api import server
    from api.state import BrainState
    from memory.capability_registry import CapabilityRegistry
    r = runtime
    issued = r.store.rotate_phone_bearer(r.peer.device_id)
    credential = issued["phone_bearer"] if credential_kind == "phone_bearer" else r.store.pair_device(
        "synthetic token node", kind="browser_node_v2")["token"]
    signal = threading.Event()
    captured = []

    class InertRouter:
        def __init__(self):
            self._realtime = SimpleNamespace(_sessions={}, _node_to_session={})
            self._gemini = self._chained = None

        def set_node_surface(self, *args):
            pass

        def bind_node_to_session(self, *args):
            pass

        def register_voice_config(self, *args):
            pass

        async def open_session(self, *, session_id, mode, provider_opts):
            assert mode == selected_mode
            from bridges.client_voice_attempt import current_voice_attempt
            binding = current_voice_attempt(session_id)
            principal = getattr(binding, "_close_talk_principal", None)
            if mode == "gemini_live":
                # No OpenAI session or DSP field is emitted for Gemini.
                captured.append({"profile": type(principal) is PairedDevicePrincipal, "mode": mode})
                signal.set()
                return SimpleNamespace(_voice_attempt=binding, disconnect=self.noop)
            rs = RealtimeSession(session_id=session_id, node_id=provider_opts["node_id"], api_key="inert")
            rs._ws, rs._connected = CaptureSocket(), True
            await rs.configure()
            captured.extend(rs._ws.events)
            self._realtime._sessions[session_id] = rs
            self._realtime._node_to_session[provider_opts["node_id"]] = session_id
            signal.set()
            return rs

        async def noop(self):
            pass

        async def close_session(self, *args, **kwargs):
            pass

    r.brain.device_pairing_store = r.store
    r.brain.daemons, r.brain.activity_log = {}, []
    r.brain.supervisor = r.brain.hardware_mesh = None
    r.brain.skill_executor = r.orch.executor
    r.brain.voice_router = InertRouter()
    r.brain.capability_registry = CapabilityRegistry()
    r.brain.perception = r.orch.perception
    r.brain._daemon_session_bindings = {}
    r.brain.bind_session_to_daemon = MethodType(BrainState.bind_session_to_daemon, r.brain)
    r.brain.get_sessions_for_daemon = MethodType(BrainState.get_sessions_for_daemon, r.brain)
    r.brain._background_tasks = set()

    def retain(task):
        r.brain._background_tasks.add(task)
        task.add_done_callback(r.brain._background_tasks.discard)
        return task
    r.brain.register_background_task = retain
    monkeypatch.setattr(server, "state", r.brain)
    app = FastAPI()
    app.websocket("/v1/node")(server.daemon_session)

    def wire():
        with TestClient(app, raise_server_exceptions=True) as client:
            with client.websocket_connect("/v1/node", headers={"Authorization": "Bearer " + credential}) as ws:
                ws.send_json({"type": "node_register", "payload": {"node_id": "synthetic-phone-profile",
                    "node_type": node_kind, "platform": "ios" if node_kind == "phone" else "macos", "capabilities": []}})
                assert ws.receive_json()["type"] == "node_ack"
                ws.send_json({"type": "voice_session_start", "payload": {"stream_id": "synthetic-audio",
                    "session_id": r.binding.session_id, "sample_rate": 24000, "channels": 1,
                    "voice_mode": selected_mode, "provider_opts": {"paired_phone_audio": True,
                        "noise_reduction": {"type": "near_field"}}}})
                assert signal.wait(4.0), "actual ingress did not configure a producer"
                if selected_mode == "gemini_live":
                    assert captured == [{"profile": True, "mode": selected_mode}]
                else:
                    audio = captured[0]["session"]["audio"]["input"]
                    assert (audio.get("noise_reduction") == {"type": "near_field"}) is expected
                    assert audio["turn_detection"]["silence_duration_ms"] == 1000
    await asyncio.wait_for(asyncio.to_thread(wire), 7.0)
