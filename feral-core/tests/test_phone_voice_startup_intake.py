"""Registered paired node intake while an inert voice startup waits."""

import asyncio
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridges.client_voice_attempt import current_voice_attempt, voice_attempt_payload
from memory.store import MemoryStore
from security.device_pairing import DevicePairingStore
from tests.test_daemon_session_phone_branches import _flush_with_known_error
from tests.test_hup_protocol import _node_client, _register_node
from tests.test_phone_chat_responsive_intake import _state

pytestmark = [pytest.mark.no_auto_feral_home, pytest.mark.timeout(15)]


def test_paired_intake_remains_responsive_while_voice_startup_waits(tmp_path):
    brain = _state()
    memory = brain.memory = MemoryStore(db_path=str(tmp_path / "memory.db"))
    pairing = DevicePairingStore(db_path=str(tmp_path / "pairing.db"))
    paired = pairing.pair_device("Synthetic voice fixture", mint_phone_bearer=True)
    entered = threading.Event()
    release = asyncio.Event()

    async def inert_open(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(connected=True)

    brain.voice_router = MagicMock()
    brain.voice_router.open_session = AsyncMock(side_effect=inert_open)
    brain.voice_router.stop_node_voice = AsyncMock()
    brain.orchestrator.handle_daemon_result = AsyncMock()
    with _node_client(brain) as client:
        brain.device_pairing_store = pairing
        with client.websocket_connect(
            "/v1/node", headers={"Authorization": "Bearer " + paired["phone_bearer"]}
        ) as ws:
            assert _register_node(ws, "paired-voice", "phone")["type"] == "node_ack"
            ws.send_json(
                {
                    "type": "voice_session_start",
                    "payload": {
                        "stream_id": "fixture-stream",
                        "session_id": "fixture-primary",
                        "voice_mode": "openai_realtime",
                        "sample_rate": 24000,
                        "channels": 1,
                    },
                }
            )
            assert entered.wait(2)
            # Release only as a watchdog so the broken registered route can
            # produce its ordered sentinel and fail without hanging pytest.
            watchdog = threading.Timer(1, lambda: ws.portal.call(release.set))
            watchdog.start()
            try:
                ws.send_json({"type": "node_heartbeat", "payload": {"ts": 1.0}})
                ws.send_json(
                    {
                        "type": "hup_action_response",
                        "payload": {
                            "action_id": "fixture-action",
                            "success": True,
                            "result": {"read": "ok"},
                        },
                    }
                )
                _flush_with_known_error(ws)
                assert not release.is_set(), (
                    "voice startup blocked paired node receive/control intake"
                )
                brain.hardware_mesh.node_health.record_heartbeat.assert_called_once()
                brain.orchestrator.handle_daemon_result.assert_awaited_once()
            finally:
                watchdog.cancel()
                watchdog.join(2)
                ws.portal.call(release.set)
                ws.portal.call(memory.aclose)




@contextmanager
def _paired_fixture(tmp_path):
    brain = _state()
    brain.memory = MemoryStore(db_path=str(tmp_path / "memory.db"))
    pairing = DevicePairingStore(db_path=str(tmp_path / "pairing.db"))
    paired = pairing.pair_device("Synthetic voice fixture", mint_phone_bearer=True)
    brain.voice_router = MagicMock()
    brain.voice_router._realtime = SimpleNamespace(
        _sessions={},
        _node_to_session={"paired-voice": "fixture-primary"},
        get_session=lambda node: brain.voice_router._realtime._sessions.get(
            "fixture-primary"
        ),
        stop_session=AsyncMock(),
    )
    brain.voice_router._gemini = brain.voice_router._chained = None
    brain.voice_router.is_session_muted.return_value = False
    brain.voice_router._node_declares_mute.return_value = False
    brain.voice_router.stop_node_voice = AsyncMock()
    with _node_client(brain) as client:
        brain.device_pairing_store = pairing
        with client.websocket_connect(
            "/v1/node", headers={"Authorization": "Bearer " + paired["phone_bearer"]}
        ) as ws:
            assert _register_node(ws, "paired-voice", "phone")["type"] == "node_ack"
            try:
                yield brain, pairing, paired, client, ws
            finally:
                ws.portal.call(brain.memory.aclose)


def _start(ws, **extra):
    ws.send_json(
        {
            "type": "voice_session_start",
            "payload": {
                "stream_id": "fixture-stream",
                "session_id": "fixture-primary",
                "voice_mode": "openai_realtime",
                "sample_rate": 24000,
                "channels": 1,
                **extra,
            },
        }
    )


def _audio(ws, index=0, stream="fixture-stream"):
    ws.send_json(
        {
            "type": "audio_chunk",
            "payload": {
                "node_id": "paired-voice",
                "stream_id": stream,
                "data_b64": "AAAA",
                "chunk_index": index,
                "is_final": False,
                "sample_rate": 24000,
                "channels": 1,
            },
        }
    )


def _producer(brain, *, on_audio=None):
    async def send(data):
        if on_audio:
            on_audio(data)

    producer = SimpleNamespace(
        connected=True,
        _voice_attempt=current_voice_attempt("fixture-primary"),
        send_audio=AsyncMock(side_effect=send),
        disconnect=AsyncMock(),
    )
    brain.voice_router._realtime._sessions["fixture-primary"] = producer
    return producer


def test_audio_is_ordered_after_startup_and_stale_stream_is_not_replayed(tmp_path):
    with _paired_fixture(tmp_path) as (brain, _, _, _, ws):
        entered, delivered = threading.Event(), threading.Event()
        release = asyncio.Event()
        actual = []

        async def open_session(**kwargs):
            assert kwargs["provider_opts"]["node_id"] == "paired-voice"
            entered.set()
            await release.wait()
            return _producer(
                brain,
                on_audio=lambda data: (
                    actual.append(data),
                    delivered.set() if len(actual) == 2 else None,
                ),
            )

        brain.voice_router.open_session = AsyncMock(side_effect=open_session)
        _start(ws, provider_opts={"node_id": "foreign-node"})
        assert entered.wait(2)
        _audio(ws, 0)
        _audio(ws, 1)
        _audio(ws, 2, "stale-stream")
        _flush_with_known_error(ws)
        assert actual == []
        record = brain.daemons["paired-voice"]._feral_node_voice
        assert record.lane._task in record.intake.tasks
        ws.portal.call(release.set)
        assert delivered.wait(2)
        assert actual == ["AAAA", "AAAA"]
        ws.portal.call(asyncio.sleep, 0.05)
        assert record.ready and record.pending_base64 == 0
        brain.voice_router.handle_audio_from_node.assert_not_called()


def test_revoked_paired_credential_fences_late_startup_and_audio(tmp_path):
    with _paired_fixture(tmp_path) as (brain, pairing, paired, _, ws):
        entered, finished = threading.Event(), threading.Event()
        release = asyncio.Event()
        produced = []

        async def open_session(**kwargs):
            entered.set()
            await release.wait()
            produced.append(_producer(brain))
            finished.set()
            return produced[-1]

        brain.voice_router.open_session = AsyncMock(side_effect=open_session)
        _start(ws)
        assert entered.wait(2)
        _audio(ws)
        _flush_with_known_error(ws)
        # Use the real paired store's current credential revocation.
        pairing.revoke_device(paired["device_id"])
        ws.portal.call(release.set)
        assert finished.wait(2)

        async def settle():
            record = brain.daemons["paired-voice"]._feral_node_voice
            await asyncio.sleep(0.05)
            return record

        record = ws.portal.call(settle)
        assert not record.ready and not record.current()
        assert not produced[0]._voice_attempt.current()
        produced[0].send_audio.assert_not_awaited()
        produced[0].disconnect.assert_awaited()


def test_same_sid_producer_replacement_never_receives_old_microphone(tmp_path):
    with _paired_fixture(tmp_path) as (brain, _, _, _, ws):
        opened = threading.Event()

        async def open_session(**kwargs):
            producer = _producer(brain)
            opened.set()
            return producer

        brain.voice_router.open_session = AsyncMock(side_effect=open_session)
        _start(ws)
        assert opened.wait(2)
        _flush_with_known_error(ws)
        record = brain.daemons["paired-voice"]._feral_node_voice
        original = record.producer
        replacement = SimpleNamespace(
            connected=True,
            _voice_attempt=None,
            send_audio=AsyncMock(),
            disconnect=AsyncMock(),
        )
        brain.voice_router._realtime._sessions["fixture-primary"] = replacement
        _audio(ws)
        _flush_with_known_error(ws)
        ws.portal.call(asyncio.sleep, 0.05)
        original.send_audio.assert_not_awaited()
        replacement.send_audio.assert_not_awaited()
        replacement.disconnect.assert_not_awaited()
        assert brain.voice_router._realtime._sessions["fixture-primary"] is replacement
        assert record.closed


def test_resistant_startup_disconnect_replacement_and_repeated_starts_are_bounded(
    tmp_path,
):
    with _paired_fixture(tmp_path) as (brain, _, paired, client, ws):
        entered, cancelled, returned = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        release = asyncio.Event()
        produced = []

        async def open_session(**kwargs):
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
            # Callback publication uses the production attempt fence.
            try:
                voice_attempt_payload(
                    {"state": "listening"}, current_voice_attempt("fixture-primary")
                )
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("obsolete producer published a status")
            produced.append(_producer(brain))
            returned.set()
            return produced[-1]

        brain.voice_router.open_session = AsyncMock(side_effect=open_session)
        _start(ws)
        assert entered.wait(2)
        old = brain.daemons["paired-voice"]._feral_node_voice
        for _ in range(3):
            _start(ws)
            frame = ws.receive_json()
            assert frame["payload"]["name"] == "voice_session_failed"
        assert cancelled.wait(2)
        assert brain.voice_router.open_session.await_count == 1
        assert len(old.intake.tasks) == 1 and old.retirement is not None
        assert old.pending_base64 == 0
        with client.websocket_connect(
            "/v1/node", headers={"Authorization": "Bearer " + paired["phone_bearer"]}
        ) as replacement:
            assert (
                _register_node(replacement, "paired-voice", "phone")["type"]
                == "node_ack"
            )
            _start(replacement)
            assert (
                replacement.receive_json()["payload"]["name"] == "voice_session_failed"
            )
            _flush_with_known_error(replacement)
            assert not returned.is_set()
            replacement_ws = brain.daemons["paired-voice"]
            ws.portal.call(release.set)
            assert returned.wait(2)
            ws.portal.call(asyncio.sleep, 0.05)
            assert brain.daemons["paired-voice"] is replacement_ws
            assert not old.binding.current() and not old.ready
            produced[0].disconnect.assert_awaited()
            brain.voice_router.stop_node_voice.assert_not_awaited()


def test_backlog_limit_closes_attempt_without_replaying_queued_audio(
    tmp_path, monkeypatch
):
    with _paired_fixture(tmp_path) as (brain, _, _, _, ws):
        from api.server import _OwnedNodeVoice

        monkeypatch.setattr(_OwnedNodeVoice, "MAX_PENDING_BASE64", 4)
        entered = threading.Event()
        release = asyncio.Event()

        async def open_session(**kwargs):
            entered.set()
            await release.wait()
            return _producer(brain)

        brain.voice_router.open_session = AsyncMock(side_effect=open_session)
        _start(ws)
        assert entered.wait(2)
        _audio(ws)
        _audio(ws, 1)
        assert ws.receive_json()["payload"]["name"] == "voice_session_failed"
        record = brain.daemons["paired-voice"]._feral_node_voice
        assert record.closed and record.pending_base64 == 0
        assert not record.lane._pending
        ws.portal.call(release.set)


def test_settled_closed_attempt_can_restart_and_chained_fallback_is_preserved(tmp_path):
    with _paired_fixture(tmp_path) as (brain, _, _, _, ws):
        brain.voice_router.open_session = AsyncMock(return_value=None)
        _start(ws)
        assert ws.receive_json()["payload"]["name"] == "voice_session_failed"
        ws.portal.call(asyncio.sleep, 0.05)
        assert brain._owned_node_voice == {}
        delivered = threading.Event()
        chained = SimpleNamespace(
            _sessions={},
            get_session=lambda sid: chained._sessions.get(sid),
            close_session=AsyncMock(),
            handle_audio_for_session=AsyncMock(
                side_effect=lambda *args, **kwargs: delivered.set()
            ),
        )
        brain.voice_router._chained = chained

        async def fallback(**kwargs):
            producer = SimpleNamespace(
                _voice_attempt=current_voice_attempt("fixture-primary")
            )
            chained._sessions["fixture-primary"] = producer
            return producer

        brain.voice_router.open_session = AsyncMock(side_effect=fallback)
        _start(ws)
        _audio(ws)
        _flush_with_known_error(ws)
        assert delivered.wait(2)
        record = brain.daemons["paired-voice"]._feral_node_voice
        assert record.ready
        chained.handle_audio_for_session.assert_awaited_once_with(
            record.producer, "AAAA", 0, False, wait_for_turn=True
        )


def test_same_socket_manifest_refresh_retains_owned_startup(tmp_path):
    with _paired_fixture(tmp_path) as (brain, _, _, _, ws):
        entered = threading.Event()
        release = asyncio.Event()

        async def open_session(**kwargs):
            entered.set()
            await release.wait()
            return _producer(brain)

        brain.voice_router.open_session = AsyncMock(side_effect=open_session)
        _start(ws)
        assert entered.wait(2)
        actual_ws = brain.daemons["paired-voice"]
        record = actual_ws._feral_node_voice
        principal = record.intake._review_principal
        ack = _register_node(
            ws, "paired-voice", "phone", capabilities=["microphone", "vitals"]
        )
        assert ack["type"] == "node_ack"
        assert brain.daemons["paired-voice"] is actual_ws
        assert actual_ws._feral_node_voice is record and record.current()
        assert (
            record.intake._review_principal is principal
            and not record.lane._task.cancelled()
        )
        ws.portal.call(release.set)


@pytest.mark.parametrize("replacement", ["router", "orchestrator", "generation"])
def test_runtime_replacement_fences_late_startup_without_success_audit(
    tmp_path, replacement
):
    with _paired_fixture(tmp_path) as (brain, _, _, _, ws):
        entered, returned = threading.Event(), threading.Event()
        release = asyncio.Event()
        produced = []
        old_router = brain.voice_router

        async def open_session(**kwargs):
            entered.set()
            await release.wait()
            producer = SimpleNamespace(
                _voice_attempt=current_voice_attempt("fixture-primary"),
                disconnect=AsyncMock(),
            )
            old_router._realtime._sessions["fixture-primary"] = producer
            produced.append(producer)
            returned.set()
            return producer

        brain.voice_router.open_session = AsyncMock(side_effect=open_session)
        _start(ws)
        assert entered.wait(2)
        if replacement == "router":
            brain.voice_router = MagicMock()
        elif replacement == "orchestrator":
            brain.orchestrator = MagicMock()
        else:
            brain._native_agent_turn_generation = 999
        ws.portal.call(release.set)
        assert returned.wait(2)
        ws.portal.call(asyncio.sleep, 0.05)
        record = brain.daemons["paired-voice"]._feral_node_voice
        assert not record.ready and not record.binding.current()
        produced[0].disconnect.assert_awaited()
        rows = [
            c.kwargs
            for c in brain.supervisor.record.call_args_list
            if c.kwargs.get("kind") == "phone_envelope"
            and c.kwargs.get("detail", {}).get("message_type") == "voice_session_start"
        ]
        assert all(row["decision"] != "allowed" for row in rows)


def test_ready_idle_attempt_allows_next_recording_on_first_start(tmp_path):
    with _paired_fixture(tmp_path) as (brain, _, _, _, ws):

        async def open_session(**kwargs):
            return _producer(brain)

        brain.voice_router.open_session = AsyncMock(side_effect=open_session)
        _start(ws)
        _flush_with_known_error(ws)
        ws.portal.call(asyncio.sleep, 0.05)
        first = brain.daemons["paired-voice"]._feral_node_voice
        assert first.ready and not first.busy()
        _start(ws, stream_id="second-stream")
        _flush_with_known_error(ws)
        ws.portal.call(asyncio.sleep, 0.05)
        second = brain.daemons["paired-voice"]._feral_node_voice
        assert second is not first and second.ready
        assert brain.voice_router.open_session.await_count == 2
        assert not first.binding.current() and not first.busy()
        first.producer.disconnect.assert_awaited_once()


def test_actual_realtime_proxy_registry_and_disconnect_preserve_replacement(
    tmp_path, monkeypatch
):
    from voice.realtime_proxy import RealtimeProxy, RealtimeSession
    from voice.router import VoiceRouter

    monkeypatch.setattr("voice.realtime_proxy._resolve_openai_key", lambda: "")
    with _paired_fixture(tmp_path) as (brain, _, _, _, ws):
        proxy = RealtimeProxy(memory=brain.memory, orchestrator=brain.orchestrator)
        router = VoiceRouter(
            realtime_proxy=proxy, memory=brain.memory, orchestrator=brain.orchestrator
        )
        brain.voice_router = router
        originals = []

        async def open_session(**kwargs):
            actual = RealtimeSession("fixture-primary", "paired-voice")
            actual._connected = True
            actual._ws = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
            proxy._sessions["fixture-primary"] = actual
            proxy._node_to_session["paired-voice"] = "fixture-primary"
            originals.append(actual)
            return actual

        router.open_session = AsyncMock(side_effect=open_session)
        _start(ws)
        _flush_with_known_error(ws)
        ws.portal.call(asyncio.sleep, 0.05)
        record = brain.daemons["paired-voice"]._feral_node_voice
        actual = originals[0]
        assert actual._voice_attempt is record.binding
        # Same SID replacement with a real producer; neither its transport nor
        # maps may receive old input or get pruned by retirement.
        replacement = RealtimeSession("fixture-primary", "paired-voice")
        replacement._connected = True
        replacement._ws = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        proxy._sessions["fixture-primary"] = replacement
        _audio(ws)
        _flush_with_known_error(ws)
        ws.portal.call(asyncio.sleep, 0.05)
        actual._ws.send.assert_not_awaited()
        actual._ws.close.assert_awaited_once()
        replacement._ws.send.assert_not_awaited()
        replacement._ws.close.assert_not_awaited()
        assert proxy._sessions["fixture-primary"] is replacement
        assert proxy._node_to_session["paired-voice"] == "fixture-primary"
        assert brain._owned_node_voice == {}
