"""Registered socket/RPC ingress, real SQLite/runner; synthetic media providers."""

import asyncio
import base64
import threading
from unittest.mock import Mock
from uuid import uuid4

import pytest

from bridges.client_voice_attempt import VoiceAttemptError
from bridges.client_voice_control import handle_client_voice_audio
from memory.runtime_session_checkpoint import CheckpointStatus
from tests.test_chained_managed_utterance import BufferedSTT, Speech
from tests.test_runtime_context_ingress import context_client as _context_client
from voice.chained_pipeline import ChainedVoicePipeline
from voice.router import VoiceRouter

context_client = _context_client
pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def managed_client(context_client, monkeypatch):
    client, state, orch, store, requests = context_client
    router = VoiceRouter(orchestrator=orch, memory=store)
    router.set_chained_pipeline(ChainedVoicePipeline(vad_enabled=False))
    orch.skills.get_skill.return_value = (
        None  # No background-job skill in this fixture.
    )
    router._resolve_chained_config = Mock(
        return_value={
            "stt_provider": "faster_whisper",
            "tts_provider": "macos_say",
            "stt_model": "",
            "tts_model": "",
            "tts_voice": "",
            "tts_voice_id": "",
        }
    )
    recognizers, speakers = [], []

    def stt_factory(*args, **kwargs):
        value = BufferedSTT("exact synthetic voice request")
        recognizers.append(value)
        return value

    def tts_factory(*args, **kwargs):
        value = Speech()
        speakers.append(value)
        return value

    monkeypatch.setattr("voice.stt_providers.get_stt_provider", stt_factory)
    monkeypatch.setattr("voice.tts_providers.get_tts_provider", tts_factory)
    state.voice_router, state.gemini_proxy = router, None
    yield client, state, orch, store, requests, recognizers, speakers
    client.portal.call(router._chained.shutdown)


def until(ws, predicate):
    observed = []
    for _ in range(64):
        value = ws.receive_json()
        observed.append(value)
        if predicate(value):
            return value, observed
    raise AssertionError("bounded expected frame not observed")


def rpc(ws, method, params=None):
    request = str(uuid4())
    ws.send_json(
        {"type": "req", "id": request, "method": method, "params": params or {}}
    )
    return until(
        ws, lambda value: value.get("type") == "res" and value.get("id") == request
    )[0]


def terms(cap, *, request=None, attempt=None):
    value = {
        "managed_chained_voice_version": 1,
        "voice_attempt_version": 1,
        "voice_attempt_id": attempt or str(uuid4()),
        "context_generation": cap["context_checkpoint"]["generation"],
        "context_revision": cap["context_checkpoint"]["revision"],
    }
    if request is not None:
        value["request_id"] = request
    return value


def configure(ws):
    cap = rpc(ws, "chat.capabilities")["payload"]
    assert cap["managed_chained_voice_versions"] == [1]
    identity = terms(cap)
    reply = rpc(
        ws, "voice.config", {**identity, "mode": "chained", "provider": "configured"}
    )
    assert reply["ok"] is True and reply["payload"]["status"] == "configured"
    return {**identity, "request_id": str(uuid4())}


def capture(ws, identity):
    reply = rpc(ws, "voice.utterance.begin", identity)
    assert reply["ok"] is True and reply["payload"]["status"] == "collecting"
    audio = {
        **identity,
        "encoding": "pcm16",
        "sample_rate": 24000,
        "channels": 1,
        "chunk_index": 0,
        "is_final": False,
        "data_b64": base64.b64encode(b"\0\0" * 100).decode(),
    }
    assert rpc(ws, "voice.audio", audio)["payload"]["received"] is True
    return audio


def test_registered_managed_voice_commits_before_speech_and_status_never_replays(
    managed_client,
):
    client, state, orch, store, requests, stt, speakers = managed_client
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        identity = configure(ws)
        capture(ws, identity)
        assert requests == [] and stt[0].flushes == 0
        ack = rpc(ws, "voice.utterance.finish", identity)
        assert (
            ack["payload"]["status"] == "submitted_processing"
            and ack["payload"]["task_accepted"] is False
        )
        terminal, _ = until(ws, lambda frame: frame.get("type") == "chat_turn_terminal")
        result = terminal["payload"]
        assert (
            result["request_id"] == identity["request_id"]
            and result["processing_outcome"] == "completed"
        )
        assert result["action_outcome"] == "not_asserted"
        session = state.voice_router._chained.get_session("voice-A")

        async def drained():
            await asyncio.wait_for(asyncio.shield(session._turn_task), 3)

        client.portal.call(drained)
        assert speakers[0].texts == ["fixture response"] and len(requests) == 1
        saved = client.portal.call(store.runtime_checkpoint_read, "voice-A")
        assert result["context_checkpoint"]["revision"] == saved.record.fence.revision
        audio, _ = until(ws, lambda frame: frame.get("type") == "audio_chunk")
        assert audio["payload"]["request_id"] == identity["request_id"]
        assert audio["payload"]["turn_id"] == result["turn_id"]
        assert audio["payload"]["context_checkpoint"] == result["context_checkpoint"]
        assert (
            rpc(ws, "chat.status", {"request_id": identity["request_id"]})["payload"][
                "receipt"
            ]
            == result
        )
        assert rpc(ws, "voice.utterance.finish", identity)["ok"] is False
        assert rpc(ws, "voice.utterance.begin", identity)["ok"] is False
        assert len(requests) == 1 and speakers[0].texts == ["fixture response"]


@pytest.mark.parametrize("unsupported", ["deepgram", "missing_pipeline"])
def test_capability_requires_selected_buffered_engine(managed_client, unsupported):
    client, state, _, _, requests, stt, _ = managed_client
    if unsupported == "missing_pipeline":
        state.voice_router._chained = None
    else:
        state.voice_router._resolve_chained_config.return_value["stt_provider"] = (
            unsupported
        )
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        assert (
            rpc(ws, "chat.capabilities")["payload"]["managed_chained_voice_versions"]
            == []
        )
    assert not requests and not stt
    if unsupported == "missing_pipeline":
        state.voice_router._chained = ChainedVoicePipeline(vad_enabled=False)


def test_config_without_negotiation_cannot_construct_provider(managed_client):
    client, _, _, _, requests, stt, _ = managed_client
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        # No readiness RPC: this request must fail before its fence matters.
        identity = terms(
            {"context_checkpoint": {"generation": str(uuid4()), "revision": 1}}
        )
        response = rpc(ws, "voice.config", {**identity, "mode": "chained"})
        assert response["error"]["code"] == "managed_voice_not_negotiated"
        assert not requests and not stt


@pytest.mark.parametrize(
    "field,value",
    [
        ("managed_chained_voice_version", True),
        ("managed_chained_voice_version", 1.0),
        ("context_revision", True),
        ("context_revision", 0),
        ("context_revision", 1.0),
        ("context_generation", "bad"),
        ("voice_attempt_id", "bad"),
    ],
)
def test_strict_review_fields_refused_before_provider(managed_client, field, value):
    client, _, _, _, requests, stt, _ = managed_client
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        cap = rpc(ws, "chat.capabilities")["payload"]
        response = rpc(
            ws, "voice.config", {**terms(cap), "mode": "chained", field: value}
        )
        assert response["ok"] is False and not requests and not stt


def test_wrong_request_audio_finish_and_direct_legacy_ingress_refused(managed_client):
    client, state, _, _, requests, stt, _ = managed_client
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        identity = configure(ws)
        audio = capture(ws, identity)
        bad = {**identity, "request_id": str(uuid4())}
        assert rpc(ws, "voice.utterance.finish", bad)["ok"] is False
        assert rpc(ws, "voice.audio", {**audio, **bad})["ok"] is False

        async def bypass():
            with pytest.raises(VoiceAttemptError, match="Voice configuration"):
                await handle_client_voice_audio(
                    state, "voice-A", state.sessions["voice-A"], audio
                )

        client.portal.call(bypass)
        assert len(stt[0].audio) == 200 and not requests


def test_stop_before_finish_never_submits_or_flushes(managed_client):
    client, state, _, _, requests, stt, speakers = managed_client
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        identity = configure(ws)
        audio = capture(ws, identity)
        stop = rpc(ws, "voice.config", {**identity, "mode": "disabled"})
        assert (
            stop["ok"] is True
            and stop["payload"]["task_cancellation"]["cancel_requested"] is False
        )
        assert rpc(ws, "voice.utterance.finish", identity)["ok"] is False
        assert rpc(ws, "voice.audio", {**audio, "chunk_index": 1})["ok"] is False
        assert stt[0].flushes == 0 and not requests and speakers[0].texts == []
        assert state.voice_router._chained.get_session("voice-A") is None


def test_saved_context_advance_before_finish_refuses_without_task(managed_client):
    client, _, orch, _, requests, stt, _ = managed_client
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        identity = configure(ws)
        capture(ws, identity)
        client.portal.call(
            orch.handle_command, "voice-A", "separate synthetic typed task"
        )
        response = rpc(ws, "voice.utterance.finish", identity)
        assert (
            response["ok"] is False
            and response["error"]["code"] == "managed_voice_context_superseded"
        )
        assert len(requests) == 1 and stt[0].flushes == 0


def test_stop_can_be_followed_only_by_a_fresh_reviewed_attempt(managed_client):
    client, _, _, _, requests, stt, _ = managed_client
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        old = configure(ws)
        capture(ws, old)
        assert rpc(ws, "voice.config", {**old, "mode": "disabled"})["ok"]
        assert rpc(ws, "voice.config", {**old, "mode": "chained"})["ok"] is False
        fresh = configure(ws)
        assert fresh["voice_attempt_id"] != old["voice_attempt_id"]
        capture(ws, fresh)
        assert rpc(ws, "voice.utterance.finish", fresh)["ok"]
        final, _ = until(ws, lambda value: value.get("type") == "chat_turn_terminal")
        assert final["payload"]["request_id"] == fresh["request_id"]
        assert len(requests) == 1 and len(stt) == 2


def test_stop_during_accepted_send_fences_runner_before_model_dispatch(managed_client):
    client, state, _, store, requests, _, speakers = managed_client
    stop_receipts = []
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        identity = configure(ws)
        capture(ws, identity)

        async def install_barrier():
            selected = state.voice_router._chained.get_session("voice-A")
            adapter = selected.submit_tracked_utterance.__self__
            original = adapter.owner.send_json

            async def send(value):
                if value.get("type") == "chat_turn_accepted":
                    # This awaits inside actual manager.submit before live.task
                    # exists. Its current not_active result must not grant work.
                    stop_receipts.append(
                        await adapter.stop({**identity, "mode": "disabled"})
                    )
                await original(value)

            adapter.owner.send_json = send

        client.portal.call(install_barrier)
        assert rpc(ws, "voice.utterance.finish", identity)["ok"]
        terminal, _ = until(ws, lambda frame: frame.get("type") == "chat_turn_terminal")
        assert terminal["payload"]["processing_outcome"] == "failed"
        assert stop_receipts[0]["task_cancellation"]["cancel_requested"] is False
        assert not requests and not speakers[0].texts
        saved = client.portal.call(store.runtime_checkpoint_read, "voice-A")
        assert saved.record.fence.revision == identity["context_revision"]
        assert saved.record.context.history() == []


@pytest.mark.parametrize("control", ["task_stop", "speech_interrupt"])
def test_actual_running_task_stop_is_distinct_from_speech_interrupt(
    managed_client, control
):
    client, _, orch, store, requests, _, speakers = managed_client
    entered = threading.Event()
    release = asyncio.Event()

    async def stream(messages, **kwargs):
        requests.append(messages)
        entered.set()
        await release.wait()
        yield {"type": "text_delta", "content": "fixture response"}
        yield {"type": "done"}

    orch.llm.chat_stream = stream
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        identity = configure(ws)
        capture(ws, identity)
        assert rpc(ws, "voice.utterance.finish", identity)["ok"]
        accepted, _ = until(ws, lambda frame: frame.get("type") == "chat_turn_accepted")
        assert entered.wait(3)
        if control == "task_stop":
            response = rpc(ws, "voice.config", {**identity, "mode": "disabled"})
            receipt = response["payload"]["task_cancellation"]
            assert (
                receipt["cancel_requested"] is True
                and receipt["turn_id"] == accepted["payload"]["turn_id"]
            )
            outcome = "cancelled"
        else:
            response = rpc(
                ws, "voice.interrupt", {**identity, "voice_request_id": str(uuid4())}
            )
            assert (
                response["ok"] is True
                and response["payload"]["session_preserved"] is True
            )
            client.portal.call(release.set)
            outcome = "completed"
        final, _ = until(ws, lambda frame: frame.get("type") == "chat_turn_terminal")
        assert final["payload"]["processing_outcome"] == outcome
        assert final["payload"]["request_id"] == identity["request_id"]
        assert len(requests) == 1
        if control == "task_stop":
            assert not speakers[0].texts
            assert (
                client.portal.call(store.runtime_checkpoint_read, "voice-A").status
                == CheckpointStatus.IN_PROGRESS
            )


@pytest.mark.parametrize("replacement", ["socket", "store", "coordinator"])
def test_captured_owner_replacement_refuses_before_audio_or_task(
    managed_client, replacement
):
    client, state, orch, _, requests, recognizers, _ = managed_client
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        identity = configure(ws)
        audio = capture(ws, identity)
        original = (
            state.sessions["voice-A"]
            if replacement == "socket"
            else state.memory
            if replacement == "store"
            else orch._context_checkpoints
        )

        async def change():
            if replacement == "socket":
                state.sessions["voice-A"] = object()
            elif replacement == "store":
                state.memory = object()
            else:
                orch._context_checkpoints = object()

        client.portal.call(change)
        try:
            assert rpc(ws, "voice.audio", {**audio, "chunk_index": 1})["ok"] is False
            assert rpc(ws, "voice.utterance.finish", identity)["ok"] is False
            assert (
                recognizers[0].flushes == 0
                and len(recognizers[0].audio) == 200
                and not requests
            )
        finally:

            async def restore():
                if replacement == "socket":
                    state.sessions["voice-A"] = original
                elif replacement == "store":
                    state.memory = original
                else:
                    orch._context_checkpoints = original

            client.portal.call(restore)


def test_config_post_open_context_drift_closes_only_its_owned_providers(managed_client):
    client, state, orch, _, requests, stt, speakers = managed_client
    original = state.voice_router.open_chained_session

    async def drifting(*args, **kwargs):
        result = await original(*args, **kwargs)
        await orch.handle_command(
            "voice-A", "separate actual typed request during open"
        )
        return result

    state.voice_router.open_chained_session = drifting
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        cap = rpc(ws, "chat.capabilities")["payload"]
        result = rpc(ws, "voice.config", {**terms(cap), "mode": "chained"})
        assert (
            result["ok"] is False
            and result["error"]["code"] == "managed_voice_context_superseded"
        )
        assert len(requests) == 1 and stt[0].closed and speakers[0].closed
        assert state.voice_router._chained.get_session("voice-A") is None


def test_raw_wire_ack_is_not_acceptance_and_user_transcript_precedes_task(
    managed_client,
):
    client, _, _, _, requests, _, _ = managed_client
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        cap = rpc(ws, "chat.capabilities")["payload"]
        identity = {**terms(cap), "request_id": str(uuid4())}
        ws.send_json(
            {
                "type": "voice_config",
                "payload": {**identity, "mode": "chained", "provider": "configured"},
            }
        )
        configured, _ = until(ws, lambda frame: frame.get("type") == "voice_config_ack")
        assert configured["payload"]["status"] == "configured" and requests == []
        ws.send_json({"type": "voice_utterance_begin", "payload": identity})
        begun, _ = until(
            ws, lambda frame: frame.get("type") == "voice_utterance_begin_ack"
        )
        assert begun["payload"]["status"] == "collecting" and requests == []
        ws.send_json(
            {
                "type": "audio_chunk",
                "payload": {
                    **identity,
                    "encoding": "pcm16",
                    "sample_rate": 24000,
                    "channels": 1,
                    "chunk_index": 0,
                    "is_final": False,
                    "data_b64": base64.b64encode(b"\0\0" * 100).decode(),
                },
            }
        )
        ws.send_json({"type": "voice_utterance_finish", "payload": identity})
        finished, observed = until(
            ws, lambda frame: frame.get("type") == "chat_turn_terminal"
        )
        assert finished["payload"]["processing_outcome"] == "completed"
        ack = next(
            value
            for value in observed
            if value.get("type") == "voice_utterance_finish_ack"
        )
        assert (
            ack["payload"]["task_accepted"] is False
            and ack["payload"]["status"] == "submitted_processing"
        )
        user = next(
            value
            for value in observed
            if value.get("type") == "transcript"
            and value["payload"].get("role") == "user"
        )
        assert user["payload"]["text"] == "exact synthetic voice request"
        assert user["payload"]["request_id"] == identity["request_id"]
        assert user["payload"]["voice_attempt_id"] == identity["voice_attempt_id"]
        assert observed.index(user) < next(
            i
            for i, value in enumerate(observed)
            if value.get("type") == "chat_turn_accepted"
        )
        assert len(requests) == 1


@pytest.mark.parametrize("defer_terminal_commit", [False, True])
def test_disconnect_status_read_does_not_synthesize_or_replay_request(
    managed_client, monkeypatch, defer_terminal_commit
):
    client, _, orch, store, requests, _, speakers = managed_client
    entered = threading.Event()
    terminal_waiting = threading.Event()
    terminal_committed = threading.Event()
    release_terminal = asyncio.Event()
    original_update = store.chat_turn_update

    async def observe_update(**kwargs):
        terminal = kwargs["session_id"] == "voice-A" and kwargs["status"] == "terminal"
        if terminal and defer_terminal_commit:
            terminal_waiting.set()
            await release_terminal.wait()
        changed = await original_update(**kwargs)
        if terminal and changed:
            # Observe the real SQLite commit, not socket context-manager exit.
            terminal_committed.set()
        return changed

    monkeypatch.setattr(store, "chat_turn_update", observe_update)

    async def stream(messages, **kwargs):
        requests.append(messages)
        entered.set()
        await asyncio.Event().wait()
        yield {"type": "done"}

    orch.llm.chat_stream = stream
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        identity = configure(ws)
        capture(ws, identity)
        assert rpc(ws, "voice.utterance.finish", identity)["ok"]
        accepted, _ = until(ws, lambda frame: frame.get("type") == "chat_turn_accepted")
        assert entered.wait(3)
    # Surface teardown is retained asynchronously and gives running work a
    # two-second grace period. A reconnect is allowed to see its running
    # receipt until cancellation has actually committed.
    if defer_terminal_commit:
        assert terminal_waiting.wait(5)
    else:
        assert terminal_committed.wait(5)
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        if defer_terminal_commit:
            try:
                pending = rpc(ws, "chat.status", {"request_id": identity["request_id"]})
                assert pending["ok"] and pending["payload"]["found"]
                receipt = pending["payload"]["receipt"]
                assert receipt["durable"] is True and receipt["status"] == "running"
                assert receipt["request_id"] == identity["request_id"]
                assert receipt["turn_id"] == accepted["payload"]["turn_id"]
                assert "processing_outcome" not in receipt
                assert not speakers[0].texts and len(requests) == 1
            finally:
                client.portal.call(release_terminal.set)
            assert terminal_committed.wait(5)
        status = rpc(ws, "chat.status", {"request_id": identity["request_id"]})
        receipt = status["payload"]["receipt"]
        assert status["ok"] and status["payload"]["found"]
        assert receipt["durable"] is True
        assert receipt["request_id"] == identity["request_id"]
        assert receipt["turn_id"] == accepted["payload"]["turn_id"]
        assert status["payload"]["receipt"]["processing_outcome"] == "cancelled"
        assert status["payload"]["receipt"]["action_outcome"] == "unknown"
        assert (
            rpc(ws, "chat.capabilities")["payload"]["managed_chained_voice_versions"]
            == []
        )
        assert not speakers[0].texts and len(requests) == 1
        assert (
            client.portal.call(store.runtime_checkpoint_read, "voice-A").status
            == CheckpointStatus.IN_PROGRESS
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"replayed": True},
        {"replayed": 0},
        {"durable": 1},
        {"request_id": str(uuid4())},
        {"session_id": "foreign"},
        {"contract_version": True},
    ],
)
def test_live_adapter_receipt_identity_never_trusts_foreign_or_coerced_acceptance(
    managed_client, changes
):
    client, state, _, _, requests, _, speakers = managed_client
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        identity = configure(ws)
        capture(ws, identity)

        async def install():
            selected = state.voice_router._chained.get_session("voice-A")
            adapter = selected.submit_tracked_utterance.__self__

            async def malformed(request, transcript, emit, fence, guard):
                await emit(
                    "chat_turn_accepted",
                    {
                        "contract_version": 1,
                        "session_id": "voice-A",
                        "request_id": request,
                        "turn_id": str(uuid4()),
                        "durable": True,
                        "replayed": False,
                        **changes,
                    },
                )
                raise AssertionError("malformed receipt unexpectedly accepted")

            adapter.submit = malformed

        client.portal.call(install)
        assert rpc(ws, "voice.utterance.finish", identity)["ok"]
        error, _ = until(
            ws,
            lambda frame: frame.get("type") == "voice_state"
            and frame["payload"].get("state") == "error",
        )
        assert error["payload"]["error"] == "ManagedChainedVoiceError"
        assert not requests and not speakers[0].texts


@pytest.mark.parametrize("began", [False, True])
def test_runner_failure_never_speaks(managed_client, began):
    client, state, orch, _, requests, _, speakers = managed_client

    async def failed(*args, **kwargs):
        from agents.chat_turns import turn_audit

        audit = turn_audit("voice-A")
        assert audit is not None
        audit.began = began
        raise RuntimeError("controlled fixture runner failure")

    orch.handle_command_stream = failed
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        identity = configure(ws)
        capture(ws, identity)
        assert rpc(ws, "voice.utterance.finish", identity)["ok"]
        terminal, _ = until(ws, lambda frame: frame.get("type") == "chat_turn_terminal")
        result = terminal["payload"]
        assert result["processing_outcome"] == (
            "outcome_unknown" if began else "failed"
        ) and result["action_outcome"] == ("unknown" if began else "not_asserted")
        assert result.get("context_checkpoint") is None
        selected = state.voice_router._chained.get_session("voice-A")

        async def drain():
            await asyncio.wait_for(asyncio.shield(selected._turn_task), 3)

        client.portal.call(drain)
        assert selected._turn_failed and not speakers[0].texts and not requests


def test_terminal_commit_failure_produces_no_speech_certificate(
    managed_client, monkeypatch
):
    client, state, _, store, requests, _, speakers = managed_client
    original = store.chat_turn_update

    async def failed(**kwargs):
        return False if kwargs["status"] == "terminal" else await original(**kwargs)

    monkeypatch.setattr(store, "chat_turn_update", failed)
    monkeypatch.setattr(
        "bridges.client_managed_chained_voice.MANAGED_TERMINAL_TIMEOUT_SECONDS", 0.02
    )
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        identity = configure(ws)
        capture(ws, identity)
        assert rpc(ws, "voice.utterance.finish", identity)["ok"]
        error, _ = until(
            ws,
            lambda frame: frame.get("type") == "voice_state"
            and frame["payload"].get("code") == "managed_voice_terminal_unconfirmed",
        )
        assert error["payload"]["request_id"] == identity["request_id"]
        selected = state.voice_router._chained.get_session("voice-A")

        async def drain():
            await asyncio.wait_for(asyncio.shield(selected._turn_task), 3)

        client.portal.call(drain)
        assert selected._turn_failed and not speakers[0].texts and len(requests) == 1


def test_socket_loss_after_terminal_commit_keeps_status_only_without_speech(
    managed_client, monkeypatch
):
    client, state, _, store, requests, _, speakers = managed_client
    original = store.chat_turn_update
    old_owner = []

    async def superseded(**kwargs):
        result = await original(**kwargs)
        if kwargs["status"] == "terminal":
            old_owner.append(state.sessions["voice-A"])
            state.sessions["voice-A"] = object()
        return result

    monkeypatch.setattr(store, "chat_turn_update", superseded)
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        identity = configure(ws)
        capture(ws, identity)
        assert rpc(ws, "voice.utterance.finish", identity)["ok"]
        until(ws, lambda frame: frame.get("type") == "chat_turn_accepted")

        # Ownership loss prevents delivery and leaves the waiter unresolved;
        # close_session retires only this local waiter, not the durable receipt.
        async def settled():
            manager = state.chat_turns

            async def wait_terminal():
                while manager.has_active_session("voice-A"):
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_terminal(), 3)
            await state.voice_router._chained.close_session("voice-A")
            state.sessions["voice-A"] = old_owner[0]

        client.portal.call(settled)
        receipt = rpc(ws, "chat.status", {"request_id": identity["request_id"]})[
            "payload"
        ]["receipt"]
        assert (
            receipt["processing_outcome"] == "completed"
            and receipt["context_checkpoint"]["durable"] is True
        )
        assert len(requests) == 1 and not speakers[0].texts


def test_deadline_retires_speech_but_late_live_receipt_remains_truthful(
    managed_client, monkeypatch
):
    client, state, orch, _, requests, _, speakers = managed_client
    release = asyncio.Event()

    async def delayed(messages, **kwargs):
        requests.append(messages)
        await release.wait()
        yield {"type": "text_delta", "content": "fixture response"}
        yield {"type": "done"}

    orch.llm.chat_stream = delayed
    monkeypatch.setattr(
        "bridges.client_managed_chained_voice.MANAGED_TERMINAL_TIMEOUT_SECONDS", 0.02
    )
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        identity = configure(ws)
        capture(ws, identity)
        assert rpc(ws, "voice.utterance.finish", identity)["ok"]
        until(
            ws,
            lambda frame: frame.get("type") == "voice_state"
            and frame["payload"].get("code") == "managed_voice_terminal_unconfirmed",
        )
        selected = state.voice_router._chained.get_session("voice-A")

        async def drained():
            await asyncio.wait_for(asyncio.shield(selected._turn_task), 3)

        client.portal.call(drained)
        assert selected._turn_failed and state.chat_turns.has_active_session("voice-A")
        assert not speakers[0].texts
        client.portal.call(release.set)
        terminal, _ = until(ws, lambda frame: frame.get("type") == "chat_turn_terminal")
        assert terminal["payload"]["request_id"] == identity["request_id"]
        assert terminal["payload"]["processing_outcome"] == "completed"
        assert (
            rpc(ws, "chat.status", {"request_id": identity["request_id"]})["payload"][
                "receipt"
            ]
            == terminal["payload"]
        )
        assert len(requests) == 1 and not speakers[0].texts


def test_existing_durable_request_cannot_be_reused_for_voice(managed_client):
    client, _, _, _, requests, stt, _ = managed_client
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        request = str(uuid4())
        ws.send_json(
            {
                "type": "text_command",
                "msg_id": request,
                "payload": {"text": "prior fixture task", "turn_contract_version": 1},
            }
        )
        until(ws, lambda frame: frame.get("type") == "chat_turn_terminal")
        identity = {**configure(ws), "request_id": request}
        begin = rpc(ws, "voice.utterance.begin", identity)
        assert (
            begin["ok"] is False
            and begin["error"]["code"] == "managed_voice_request_reused"
        )
        assert len(requests) == 1 and stt[0].flushes == 0


@pytest.mark.parametrize("timing", ["between_chunks", "during_send"])
def test_same_generation_commit_between_speech_chunks_revokes_later_media(
    managed_client,
    timing,
):
    client, state, orch, store, requests, _, speakers = managed_client
    release = asyncio.Event()
    advanced = asyncio.Event()
    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        identity = configure(ws)
        selected = state.voice_router._chained.get_session("voice-A")

        if timing == "during_send":

            async def install():
                adapter = selected.submit_tracked_utterance.__self__
                original = adapter.owner.send_json

                async def advancing_send(frame):
                    await original(frame)
                    if frame.get("type") == "audio_chunk":
                        await orch.handle_command(
                            "voice-A", "another actual committed turn during speech"
                        )
                        advanced.set()

                adapter.owner.send_json = advancing_send

            client.portal.call(install)

        async def speech(text):
            speakers[0].texts.append(text)
            yield b"\0\0" * 2400
            await release.wait()
            yield b"\1\1" * 2400

        selected.tts_provider.synthesize = speech
        capture(ws, identity)
        assert rpc(ws, "voice.utterance.finish", identity)["ok"]
        first, observed = until(ws, lambda frame: frame.get("type") == "audio_chunk")
        final = next(
            value for value in observed if value.get("type") == "chat_turn_terminal"
        )
        committed = final["payload"]["context_checkpoint"]
        assert first["payload"]["context_checkpoint"] == committed
        if timing == "during_send":

            async def wait_advanced():
                await asyncio.wait_for(advanced.wait(), 3)

            client.portal.call(wait_advanced)
        else:
            client.portal.call(
                orch.handle_command,
                "voice-A",
                "another actual committed turn during speech",
            )
        newer = client.portal.call(
            store.runtime_checkpoint_read, "voice-A"
        ).record.fence
        assert (
            newer.generation == committed["generation"]
            and newer.revision > committed["revision"]
        )
        client.portal.call(release.set)
        failed, later = until(
            ws,
            lambda frame: frame.get("type") == "voice_state"
            and frame["payload"].get("state") == "error",
        )
        assert failed["payload"]["error"] == "ManagedChainedVoiceError"
        assert not any(value.get("type") == "audio_chunk" for value in later)
        assert len(requests) == 2
        historical = rpc(ws, "chat.status", {"request_id": identity["request_id"]})[
            "payload"
        ]["receipt"]
        assert (
            historical["context_checkpoint"] == committed
            and historical["processing_outcome"] == "completed"
        )


def test_writer_queued_during_readiness_refuses_media_after_await(
    managed_client, monkeypatch
):
    import bridges.client_managed_chained_voice as managed

    client, _, orch, _, requests, stt, _ = managed_client
    writer_tasks = []
    original = managed.attachment_readiness

    async def queued(coordinator, attachment):
        snapshot = await original(coordinator, attachment)
        async with coordinator.lock_for("voice-A"):
            # Real writer registers before the SID lock and cannot begin while
            # this completed READY snapshot is held. No synthetic counter edit.
            work = asyncio.create_task(
                orch.handle_command("voice-A", "actual queued typed turn")
            )
            writer_tasks.append(work)

            async def registered():
                while not coordinator.has_writers("voice-A"):
                    await asyncio.sleep(0)

            await asyncio.wait_for(registered(), 3)
        return snapshot

    with client.websocket_connect(
        "/v1/session?session_id=voice-A&context_checkpoint_version=1"
    ) as ws:
        identity = configure(ws)
        audio = capture(ws, identity)
        monkeypatch.setattr(managed, "attachment_readiness", queued)
        refused = rpc(ws, "voice.audio", {**audio, "chunk_index": 1})
        assert (
            refused["ok"] is False
            and refused["error"]["code"] == "managed_voice_context_busy"
        )

        async def drained():
            await asyncio.wait_for(asyncio.shield(writer_tasks[0]), 3)

        client.portal.call(drained)
        assert len(requests) == 1 and len(stt[0].audio) == 200 and stt[0].flushes == 0
