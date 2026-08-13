"""HUP ``audio_frame`` must actually reach transcription.

The defect these tests were written against
-------------------------------------------
``api/server.py::_handle_audio_frame`` ended in::

    audio = getattr(state, "audio", None)
    ingest = getattr(audio, "ingest_frame", None)
    if callable(ingest):
        ingest(effective_node, frame_payload)
    else:
        logger.debug("... state.audio has no ingest_frame hook; dropping.")

``state.audio`` is a ``perception.audio_pipeline.AudioPipeline`` and that
class has never had an ``ingest_frame`` method. The probe was therefore
never true in production: every ``audio_frame`` a hardware daemon sent
was counted, size-checked, and then dropped at ``debug`` level, which is
off in every normal deployment. The brain answered the daemon with no
error, so the device believed the brain was listening.

The reason it survived is trap 3 in CLAUDE.md: the tests that covered
this path (``tests/test_hup_v1_1_brain.py``,
``tests/test_hup_v1_1_e2e.py``) installed a ``FakeAudio`` double that
*did* define ``ingest_frame``, so a green suite proved the handler could
call a method the shipped object does not have.

The contract now
----------------
``audio_frame`` lands where ``audio_chunk`` lands: on
``VoiceRouter.handle_audio_from_node``, which is the only audio entry
point in this repo whose transcript has a consumer (transcript frame,
working memory, orchestrator turn, TTS reply). A frame that cannot be
routed says so at ``warning`` naming the missing precondition.
"""

from __future__ import annotations

import base64
import importlib
import inspect
import logging

import pytest


# No ``no_auto_feral_home`` marker: these tests must run under the
# autouse FERAL_HOME isolation fixture so nothing here can touch the
# operator's real ~/.feral.


def _b64(size: int) -> str:
    return base64.b64encode(b"\x01" * size).decode("ascii")


class _RecordingRouter:
    """Records what the server hands the voice router."""

    def __init__(self):
        self.calls: list[dict] = []

    async def handle_audio_from_node(self, **kwargs):
        self.calls.append(kwargs)


class _FakeState:
    def __init__(self, router=None, sessions=("sid-1",)):
        self.voice_router = router
        self._sessions = set(sessions)
        # The real BrainState always has this attribute. It is here so a
        # regression that reintroduces the ``state.audio`` probe fails on
        # the assertion rather than on an AttributeError.
        self.audio = object()

    def get_sessions_for_daemon(self, node_id):
        return self._sessions


@pytest.fixture()
def server():
    return importlib.import_module("api.server")


# ---------------------------------------------------------------------------
# 1. The probe itself
# ---------------------------------------------------------------------------


def test_shipped_audio_pipeline_has_no_ingest_frame(server):
    """The two halves of the defect, asserted together.

    Either half alone is survivable. It is the pair that produced a
    silent drop: a handler probing for ``ingest_frame`` and a pipeline
    that does not define it.
    """
    from perception.audio_pipeline import AudioPipeline

    assert not hasattr(AudioPipeline, "ingest_frame"), (
        "AudioPipeline grew an ingest_frame method. If that is deliberate, "
        "the server handler and this test both need updating together."
    )
    # The docstring is allowed to name the dead hook - it explains why
    # the hook is gone. Only executable code is checked.
    source = inspect.getsource(server._handle_audio_frame)
    body = source.replace(server._handle_audio_frame.__doc__ or "", "")
    assert "ingest_frame" not in body, (
        "_handle_audio_frame probes for state.audio.ingest_frame, which "
        "AudioPipeline does not define, so every audio_frame is dropped."
    )


# ---------------------------------------------------------------------------
# 2. The frame reaches the real consumer
# ---------------------------------------------------------------------------


async def test_audio_frame_reaches_the_voice_router(server, monkeypatch):
    router = _RecordingRouter()
    monkeypatch.setattr(server, "state", _FakeState(router))

    payload = {
        "event_type": "audio_frame",
        "codec": "pcm16",
        "sample_rate": 24000,
        "channels": 1,
        "sequence": 5,
        "data_b64": _b64(512),
    }
    reason = await server._handle_audio_frame("feral-band-test", payload)

    assert reason is None
    assert len(router.calls) == 1
    call = router.calls[0]
    assert call["node_id"] == "feral-band-test"
    assert call["session_id"] == "sid-1"
    assert call["audio_b64"] == payload["data_b64"]
    assert call["encoding"] == "pcm16"
    assert call["sample_rate"] == 24000


async def test_audio_frame_nested_device_event_shape_reaches_the_router(
    server, monkeypatch
):
    """The Python node SDK nests the frame under ``payload.data``."""
    router = _RecordingRouter()
    monkeypatch.setattr(server, "state", _FakeState(router))

    payload = {
        "node_id": "feral-band-test",
        "event_type": "audio_frame",
        "data": {
            "codec": "opus",
            "sample_rate": 16000,
            "channels": 1,
            "sequence": 9,
            "data_b64": _b64(512),
        },
    }
    await server._handle_audio_frame(None, payload)

    assert len(router.calls) == 1
    assert router.calls[0]["node_id"] == "feral-band-test"
    assert router.calls[0]["encoding"] == "opus"
    assert router.calls[0]["sample_rate"] == 16000


async def test_oversized_audio_frame_is_refused_and_never_routed(server, monkeypatch):
    router = _RecordingRouter()
    monkeypatch.setattr(server, "state", _FakeState(router))

    payload = {
        "event_type": "audio_frame",
        "codec": "opus",
        "sample_rate": 24000,
        "channels": 1,
        "sequence": 6,
        "data_b64": _b64(server.AUDIO_FRAME_MAX_BYTES + 4),
    }
    reason = await server._handle_audio_frame("feral-band-test", payload)

    assert router.calls == []
    assert reason and "audio_frame" in reason


# ---------------------------------------------------------------------------
# 3. When it cannot be routed, it says so
# ---------------------------------------------------------------------------


async def test_unroutable_audio_frame_warns_actionably(server, monkeypatch, caplog):
    """No bound session is the single most likely live failure.

    It used to be a ``debug`` log. A device streaming a microphone into
    a brain that is discarding every frame must be visible at default
    log level, and the message must name the precondition that is
    missing.
    """
    router = _RecordingRouter()
    monkeypatch.setattr(server, "state", _FakeState(router, sessions=()))

    payload = {
        "event_type": "audio_frame",
        "codec": "pcm16",
        "sample_rate": 24000,
        "channels": 1,
        "sequence": 0,
        "data_b64": _b64(256),
    }
    with caplog.at_level(logging.WARNING, logger="feral.brain"):
        reason = await server._handle_audio_frame("feral-band-test", payload)

    assert reason is None  # not the daemon's fault, so no 4020
    assert router.calls == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "an unroutable audio_frame was dropped below WARNING"
    text = " ".join(r.getMessage() for r in warnings)
    assert "voice_session_start" in text
    assert "feral-band-test" in text


async def test_unroutable_audio_frame_does_not_log_once_per_frame(
    server, monkeypatch, caplog
):
    """Audio arrives at ~50 frames/second. One warning per frame is a
    log flood that hides the very message it is trying to deliver."""
    monkeypatch.setattr(server, "state", _FakeState(_RecordingRouter(), sessions=()))

    with caplog.at_level(logging.WARNING, logger="feral.brain"):
        for seq in range(30):
            await server._handle_audio_frame(
                "feral-band-test",
                {
                    "event_type": "audio_frame",
                    "codec": "pcm16",
                    "sample_rate": 24000,
                    "sequence": seq,
                    "data_b64": _b64(64),
                },
            )

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert 0 < len(warnings) < 30


async def test_audio_frame_without_a_voice_router_does_not_raise(server, monkeypatch):
    """A daemon must not be punished for the brain not being booted yet."""
    monkeypatch.setattr(server, "state", _FakeState(router=None))
    await server._handle_audio_frame(
        "any",
        {
            "event_type": "audio_frame",
            "codec": "opus",
            "sample_rate": 24000,
            "sequence": 0,
            "data_b64": _b64(64),
        },
    )


# ---------------------------------------------------------------------------
# 4. The proof: real bytes, real pipeline, real transcription call
# ---------------------------------------------------------------------------


async def test_real_audio_bytes_from_a_device_reach_transcription(server, monkeypatch):
    """End to end through the real VoiceRouter and the real AudioPipeline.

    Only the network leg is stubbed: ``AudioPipeline._transcribe`` is the
    last thing before the Whisper HTTP request. Reaching it with the
    exact bytes the device encoded is what "FERAL can hear a connected
    device" means.
    """
    from perception.audio_pipeline import AudioPipeline
    from voice.router import VoiceRouter

    pipeline = AudioPipeline()
    seen: list[bytes] = []

    async def _fake_transcribe(audio_bytes, encoding="opus", sample_rate=16000):
        seen.append(audio_bytes)
        return "hello from the wristband"

    monkeypatch.setattr(pipeline, "_transcribe", _fake_transcribe)

    router = VoiceRouter(audio_pipeline=pipeline)
    monkeypatch.setattr(server, "state", _FakeState(router, sessions=("sid-live",)))

    # 3000 bytes of PCM16. Over AudioBuffer's 2000-byte floor for a
    # silence-gap flush, and over the 1000-byte floor for a transcription.
    spoken = b"\x11\x22" * 1500
    half = len(spoken) // 2
    frames = [spoken[:half], spoken[half:]]

    for seq, chunk in enumerate(frames):
        await server._handle_audio_frame(
            "feral-band-live",
            {
                "event_type": "audio_frame",
                "codec": "pcm16",
                "sample_rate": 24000,
                "channels": 1,
                "sequence": seq,
                "data_b64": base64.b64encode(chunk).decode("ascii"),
            },
        )

    # Nothing has flushed yet: audio_frame carries no is_final, so the
    # utterance ends the way a real stream ends, on a silence gap.
    assert seen == []

    buf = pipeline.get_buffer("sid-live")
    buf._last_chunk_time -= 5.0  # the daemon stopped sending 5s ago

    await server._handle_audio_frame(
        "feral-band-live",
        {
            "event_type": "audio_frame",
            "codec": "pcm16",
            "sample_rate": 24000,
            "channels": 1,
            "sequence": 2,
            "data_b64": base64.b64encode(b"\x00\x00").decode("ascii"),
        },
    )

    assert len(seen) == 1, "device audio never reached AudioPipeline._transcribe"
    assert seen[0].startswith(spoken), (
        "the bytes handed to transcription are not the bytes the device sent"
    )


# ---------------------------------------------------------------------------
# 5. Why the wiring alone was not enough: the silence-gap VAD never fired
# ---------------------------------------------------------------------------


async def test_silence_gap_flushes_the_buffer_without_is_final():
    """``AudioBuffer.append`` stamped ``_last_chunk_time = now`` and the
    boundary check ran *after* it, so ``vad_triggered()`` measured a
    zero-length gap on every call and could never be True.

    Only ``is_final=True`` ever flushed. A browser sends ``is_final``;
    a HUP ``audio_frame`` has no such field, so a device stream
    accumulated forever and was never transcribed. Measured before the
    fix: 30,100 bytes resident, zero transcriptions.
    """
    from perception.audio_pipeline import AudioPipeline

    pipeline = AudioPipeline()
    seen: list[bytes] = []

    async def _fake_transcribe(audio_bytes, encoding="opus", sample_rate=16000):
        seen.append(audio_bytes)
        return "ok"

    pipeline._transcribe = _fake_transcribe

    chunk = base64.b64encode(b"\x07" * 3000).decode("ascii")
    for i in range(10):
        assert await pipeline.process_audio_chunk("s", chunk, i, False, "pcm16", 16000) is None

    buf = pipeline.get_buffer("s")
    assert buf.pending_bytes == 30000
    buf._last_chunk_time -= 5.0  # the device went quiet 5 seconds ago

    transcript = await pipeline.process_audio_chunk(
        "s", base64.b64encode(b"\x08" * 100).decode("ascii"), 10, False, "pcm16", 16000
    )

    assert transcript == "ok"
    assert len(seen) == 1
    assert len(seen[0]) == 30000, "the completed utterance was not what got flushed"
    # The chunk that arrived after the gap opens the next utterance
    # rather than being swallowed by the previous one.
    assert buf.pending_bytes == 100


async def test_is_final_still_flushes_immediately():
    """The web-client contract must not regress."""
    from perception.audio_pipeline import AudioPipeline

    pipeline = AudioPipeline()
    seen: list[bytes] = []

    async def _fake_transcribe(audio_bytes, encoding="opus", sample_rate=16000):
        seen.append(audio_bytes)
        return "ok"

    pipeline._transcribe = _fake_transcribe
    out = await pipeline.process_audio_chunk(
        "s", base64.b64encode(b"\x09" * 4000).decode("ascii"), 0, True, "pcm16", 16000
    )
    assert out == "ok"
    assert seen == [b"\x09" * 4000]


async def test_teardown_of_a_non_empty_buffer_is_reported(caplog):
    """Audio thrown away at teardown was invisible. It is a real hole in
    the whisper path (a stream that ends mid-utterance is never
    transcribed) and it must at least be attributable."""
    from perception.audio_pipeline import AudioPipeline

    pipeline = AudioPipeline()
    await pipeline.process_audio_chunk(
        "s", base64.b64encode(b"\x0a" * 2000).decode("ascii"), 0, False, "pcm16", 16000
    )
    with caplog.at_level(logging.WARNING, logger="feral.audio"):
        pipeline.clear_session("s")

    text = " ".join(r.getMessage() for r in caplog.records)
    assert "2000 bytes" in text and "untranscribed" in text
