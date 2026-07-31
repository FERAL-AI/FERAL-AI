"""
Chained Voice Pipeline — STT → LLM → TTS
==========================================

The third voice mode: an explicit multi-stage pipeline where each
component (speech recognition, language model, speech synthesis) is
independently selectable and debuggable.

State machine::

    idle → listening → processing → speaking → idle
              ↑                         |
              └─────────────────────────┘

Each transition emits a ``voice_state`` frame to the phone so the
UI can drive the orb animation.  Transcript frames (partial + final)
are emitted during ``listening``.  TTS audio chunks are emitted
during ``speaking``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from voice.stt_providers import STTProvider, TranscriptFragment
from voice.transcript_filter import should_commit_user_transcript
from voice.tts_providers import TTSProvider

logger = logging.getLogger("feral.voice.chained_pipeline")

# Seconds of inbound-audio silence that end an utterance when the STT
# provider gives us no end-of-speech signal of its own.
#
# The pipeline used to flush ONLY on ``is_final=True`` from the client.
# No client ever sets it — ``feral-client-v2/src/lib/voiceRealtime.js``
# hardcodes ``is_final: false`` and the brain forwards what it got — so
# after a fallback morph the session accepted audio forever and emitted
# nothing. Two server-side drivers replace that flag now:
#
#   * streaming providers (Deepgram) flush on their own ``speech_final``
#     fragment, consumed by the per-session STT task;
#   * buffered providers (Whisper/Groq) produce nothing until someone
#     calls ``flush()``, so this timer calls it after the phone stops
#     sending audio.
#
# 0.8s matches the Realtime server-VAD ``silence_duration_ms`` band
# (1000ms) closely enough to feel the same to a speaker while leaving
# room for jittery uplinks.
SILENCE_FLUSH_SECONDS = 0.8


class VoiceState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    ERROR = "error"


@dataclass
class ChainedSession:
    """Holds per-session state for the chained pipeline."""
    session_id: str
    stt_provider: STTProvider
    tts_provider: TTSProvider
    llm_handle: Any
    state: VoiceState = VoiceState.IDLE
    send_frame: Callable[[str, dict], Awaitable[None]] | None = None
    _audio_buffer: bytearray = field(default_factory=bytearray)
    # Consumes ``stt_provider.open_stream()``. Was declared but never
    # assigned (only ever cancelled), which meant ``open_stream`` was
    # never called at all — for Deepgram that is the call that opens
    # the WebSocket, so ``send_audio`` no-op'd on a None ``_ws`` and
    # the fallback pipeline could not transcribe a single word.
    _stt_task: asyncio.Task | None = field(default=None, repr=False)
    # Fires the end-of-utterance flush when the client stops sending
    # audio and the provider has no end-of-speech signal of its own.
    _silence_task: asyncio.Task | None = field(default=None, repr=False)
    _last_audio_ts: float = 0.0
    # Final transcript text handed over by the STT consumer task,
    # waiting to be picked up by the next flush.
    _pending_finals: list[str] = field(default_factory=list)
    _flushing: bool = False
    _last_transcript: str = ""
    _chunk_count: int = 0


class ChainedVoicePipeline:
    """Manages chained STT→LLM→TTS voice sessions.

    The pipeline does NOT reinvent LLM routing — it delegates to the
    brain's existing orchestrator via the ``llm_handle`` passed to
    ``open_session``.
    """

    def __init__(self, *, silence_flush_seconds: float = SILENCE_FLUSH_SECONDS):
        self._sessions: dict[str, ChainedSession] = {}
        self._silence_flush_seconds = float(silence_flush_seconds)

    async def open_session(
        self,
        session_id: str,
        stt_provider: STTProvider,
        tts_provider: TTSProvider,
        llm_handle: Any,
        send_frame: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> ChainedSession:
        """Create a new chained voice session.

        Args:
            session_id: Unique session identifier.
            stt_provider: An instantiated STT provider.
            tts_provider: An instantiated TTS provider.
            llm_handle: The brain's orchestrator — must have
                ``handle_command_stream(session_id, text, context)``.
            send_frame: Callback to emit frames to the phone client.
                Signature: ``async def send_frame(session_id, frame_dict)``
        """
        if session_id in self._sessions:
            await self.close_session(session_id)

        session = ChainedSession(
            session_id=session_id,
            stt_provider=stt_provider,
            tts_provider=tts_provider,
            llm_handle=llm_handle,
            send_frame=send_frame,
        )
        self._sessions[session_id] = session
        # Start consuming the provider's recognition stream. For
        # streaming providers this is the call that actually opens the
        # upstream socket (Deepgram connects inside ``open_stream`` and
        # starts its receive loop there) — without it ``send_audio``
        # silently returned on a None ``_ws``. For buffered providers
        # it just parks on the result queue until ``flush()`` fills it.
        session._stt_task = asyncio.create_task(self._consume_stt(session))
        await self._set_state(session, VoiceState.IDLE)
        logger.info("Chained voice session opened: %s", session_id[:8])
        return session

    def get_session(self, session_id: str) -> ChainedSession | None:
        return self._sessions.get(session_id)

    async def handle_audio(
        self,
        session_id: str,
        audio_b64: str,
        chunk_index: int = 0,
        is_final: bool = False,
    ) -> None:
        """Accept an audio chunk from the phone.

        ``is_final`` is treated as a HINT, not the driver: it forces an
        immediate flush when a client does send it, but the utterance
        ends on the STT provider's own end-of-speech signal or on the
        silence timer either way. No shipped client sets the flag, and
        making it load-bearing is what left the fallback pipeline
        listening forever without ever answering.
        """
        session = self._sessions.get(session_id)
        if not session:
            logger.warning("handle_audio: no session %s", session_id[:8])
            return

        audio_bytes = base64.b64decode(audio_b64)
        session._chunk_count += 1

        if session.state == VoiceState.IDLE:
            await self._set_state(session, VoiceState.LISTENING)

        await session.stt_provider.send_audio(audio_bytes)

        session._last_audio_ts = time.monotonic()
        if session._silence_task is None or session._silence_task.done():
            session._silence_task = asyncio.create_task(self._silence_timer(session))

        if is_final:
            await self._flush_pipeline(session)

    async def _consume_stt(self, session: ChainedSession) -> None:
        """Drain the STT provider's recognition stream for one session.

        Sole owner of the provider's fragments: it publishes partials
        to the client for latency feedback, parks final text in
        ``_pending_finals`` for the next flush, and — crucially —
        starts the flush itself when the provider says the speaker
        stopped (Deepgram's ``speech_final``; buffered providers set it
        on their single post-``flush()`` fragment).
        """
        try:
            async for fragment in session.stt_provider.open_stream():
                await self._on_stt_fragment(session, fragment)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Surface it: a dead recognition stream means the session
            # can hear nothing, and silently swallowing that is the
            # exact failure mode this whole change exists to remove.
            logger.exception(
                "STT stream failed for session %s", session.session_id[:8],
            )
            await self._set_state(session, VoiceState.ERROR, error=str(exc))
            await self._set_state(session, VoiceState.IDLE)

    async def _on_stt_fragment(
        self, session: ChainedSession, fragment: TranscriptFragment,
    ) -> None:
        """Handle one transcript fragment from the recognition stream."""
        text = (fragment.text or "").strip()
        if not text:
            return

        is_final = fragment.is_final or not fragment.is_partial
        await self._emit_transcript(session, text, is_partial=not is_final)
        if not is_final:
            return

        session._pending_finals.append(text)
        if fragment.speech_final:
            await self._flush_pipeline(session)

    async def _silence_timer(self, session: ChainedSession) -> None:
        """Flush the utterance once inbound audio has stopped.

        Self-extending: each new chunk pushes ``_last_audio_ts``
        forward and this re-sleeps the remainder, so one task covers a
        whole utterance instead of one per chunk.
        """
        while True:
            remaining = (
                self._silence_flush_seconds
                - (time.monotonic() - session._last_audio_ts)
            )
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)
        logger.debug(
            "chained: silence flush for session %s after %.2fs",
            session.session_id[:8], self._silence_flush_seconds,
        )
        await self._flush_pipeline(session)

    def _cancel_silence_timer(self, session: ChainedSession) -> None:
        """Disarm the silence timer (no-op when it is the caller)."""
        task = session._silence_task
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        session._silence_task = None

    async def _flush_pipeline(self, session: ChainedSession) -> None:
        """Run the full STT → LLM → TTS chain for accumulated audio."""
        if session._flushing:
            # Two drivers (provider end-of-speech + silence timer +
            # an explicit is_final) can land on the same utterance.
            # First one through owns it; the others must not start a
            # second LLM turn on the same words.
            return
        session._flushing = True
        self._cancel_silence_timer(session)
        try:
            await self._set_state(session, VoiceState.PROCESSING)

            await session.stt_provider.flush()

            transcript = await self._collect_transcript(session)

            if not transcript.strip():
                logger.debug("Empty transcript, returning to idle")
                await self._set_state(session, VoiceState.IDLE)
                return

            # Bug 3 (chained pipeline parity): the Deepgram path filters
            # its own phantoms at the WS-receive boundary, but
            # buffered providers (Whisper/Groq) commit a single
            # post-utterance transcript that bypasses Deepgram's per-
            # fragment gate. Apply the shared blocklist here so the
            # full pipeline (regardless of STT choice) never feeds the
            # LLM a stock whisper closer.
            if not should_commit_user_transcript(transcript):
                logger.info(
                    "chained: dropped phantom transcript before LLM: %r",
                    transcript,
                )
                await self._set_state(session, VoiceState.IDLE)
                return

            session._last_transcript = transcript

            response_text = await self._run_llm(session, transcript)

            if response_text:
                await self._set_state(session, VoiceState.SPEAKING)
                await self._run_tts(session, response_text)

            await self._set_state(session, VoiceState.IDLE)

        except Exception as exc:
            logger.exception("Chained pipeline error for session %s", session.session_id[:8])
            await self._set_state(session, VoiceState.ERROR, error=str(exc))
            await self._set_state(session, VoiceState.IDLE)
        finally:
            session._flushing = False

    async def _collect_transcript(self, session: ChainedSession) -> str:
        """Return the finalised text for the utterance being flushed.

        Fragments are owned by ``_consume_stt``; this only picks up
        what that task has already parked in ``_pending_finals``. It
        also drains the provider queues directly, because
        ``stt_provider.flush()`` (buffered providers) may have enqueued
        a fragment microseconds ago that the consumer task has not been
        scheduled to read yet — whichever side gets it first, the text
        is used exactly once.

        Returns ``""`` when there is nothing new. It used to fall back
        to ``session._last_transcript``, which meant a second flush on
        an already-consumed utterance re-ran the PREVIOUS turn through
        the LLM — a duplicate command from a silence timer that fired
        just behind an end-of-speech flush.
        """
        fragments: list[str] = list(session._pending_finals)
        session._pending_finals.clear()

        for attr in ("_result_queue", "_transcript_queue"):
            queue = getattr(session.stt_provider, attr, None)
            if queue is None:
                continue
            while not queue.empty():
                frag = queue.get_nowait()
                if frag is None:
                    continue
                text = (frag.text or "").strip()
                if not text:
                    continue
                fragments.append(text)
                await self._emit_transcript(
                    session, text, is_partial=frag.is_partial and not frag.is_final,
                )

        return " ".join(fragments)

    async def _run_llm(self, session: ChainedSession, transcript: str) -> str:
        """Send transcript to the brain's orchestrator and capture the response."""
        if not session.llm_handle:
            logger.warning("No LLM handle for session %s", session.session_id[:8])
            return ""

        try:
            await session.llm_handle.handle_command_stream(
                session_id=session.session_id,
                text=transcript,
                context={"source": "voice_chained"},
            )

            return self._extract_last_response(session)
        except Exception:
            logger.exception("LLM call failed for session %s", session.session_id[:8])
            raise

    def _extract_last_response(self, session: ChainedSession) -> str:
        """Pull the last assistant response from the orchestrator's history."""
        if not session.llm_handle:
            return ""

        history = getattr(session.llm_handle, "conversation_history", {})
        session_history = history.get(session.session_id, [])

        for msg in reversed(session_history):
            if msg.get("role") == "assistant":
                text = msg.get("text", msg.get("content", ""))
                return text[:2000]

        return ""

    async def _run_tts(self, session: ChainedSession, text: str) -> None:
        """Synthesize speech and emit a complete TTS audio frame.

        v2026.5.28 — pre-fix this emitted one ``audio_chunk`` frame per
        TTS transport slice (4096-byte chunks). Each slice was labelled
        ``encoding: "mp3"`` but a slice is **not** a self-contained MP3
        file — it is one piece of one. The browser called
        ``decodeAudioData`` on each slice; every call failed
        ``EncodingError`` and the .catch on the playback queue
        swallowed the failure → the chat showed the assistant's text
        reply, the speaker stayed silent (operator's 2026-05-15
        report).

        Fix: accumulate the full TTS output into a single buffer and
        emit ONE complete MP3 frame. The browser's ``decodeAudioData``
        decodes it cleanly and the PCM source plays through. This
        loses incremental playback (the first audio arrives after the
        last byte) but matches the working ``perception/audio_pipeline.py``
        path that already buffers full bytes before slicing
        (``_synthesize_cloud`` ~434-447). A future PR can restore
        streaming by switching the TTS provider output to PCM, which
        the client's ``queuePcm16Playback`` path plays incrementally
        without per-chunk decoding.
        """
        try:
            buffer = bytearray()
            async for audio_chunk in session.tts_provider.synthesize(text):
                buffer.extend(audio_chunk)

            if buffer:
                b64_full = base64.b64encode(bytes(buffer)).decode("ascii")
                await self._emit_audio_chunk(
                    session,
                    b64_full,
                    chunk_index=0,
                    is_final=False,
                )

            # Sentinel close frame so the client knows the TTS turn
            # is complete. data_b64="" matches the existing wire
            # protocol (`queueAudioPlayback` no-ops on empty / final).
            await self._emit_audio_chunk(
                session, "", chunk_index=1, is_final=True
            )
        except Exception:
            logger.exception("TTS failed for session %s", session.session_id[:8])
            raise

    async def _set_state(
        self, session: ChainedSession, state: VoiceState, *, error: str = ""
    ) -> None:
        """Transition state and emit a voice_state frame."""
        old = session.state
        session.state = state
        logger.debug(
            "Session %s: %s → %s", session.session_id[:8], old.value, state.value
        )

        frame = {
            "type": "voice_state",
            "payload": {
                "state": state.value,
                "mode": "chained",
            },
        }
        if error:
            frame["payload"]["error"] = error

        if session.send_frame:
            await session.send_frame(session.session_id, frame)

    async def _emit_transcript(
        self, session: ChainedSession, text: str, is_partial: bool
    ) -> None:
        """Emit a transcript frame to the phone."""
        frame = {
            "type": "transcript",
            "payload": {
                "text": text,
                "is_partial": is_partial,
                "role": "user",
            },
        }
        if session.send_frame:
            await session.send_frame(session.session_id, frame)

    async def _emit_audio_chunk(
        self,
        session: ChainedSession,
        data_b64: str,
        chunk_index: int,
        is_final: bool,
    ) -> None:
        """Emit a TTS audio chunk frame to the phone."""
        frame = {
            "type": "audio_chunk",
            "payload": {
                "data_b64": data_b64,
                "chunk_index": chunk_index,
                "is_final": is_final,
                "encoding": "mp3",
            },
        }
        if session.send_frame:
            await session.send_frame(session.session_id, frame)

    async def close_session(self, session_id: str) -> None:
        """Tear down a chained voice session."""
        session = self._sessions.pop(session_id, None)
        if not session:
            return

        # Disarm the timer first so it can't fire a flush against a
        # provider we are about to close.
        self._cancel_silence_timer(session)

        try:
            await session.stt_provider.close()
        except Exception:
            logger.debug("STT provider close error", exc_info=True)

        try:
            await session.tts_provider.close()
        except Exception:
            logger.debug("TTS provider close error", exc_info=True)

        # ``close()`` ends the recognition stream, so the consumer task
        # normally finishes on its own; cancel + await covers providers
        # that block instead, and stops the task leaking past the
        # session it belongs to.
        for task in (session._stt_task, session._silence_task):
            if task is None or task.done() or task is asyncio.current_task():
                continue
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        session._stt_task = None
        session._silence_task = None

        logger.info("Chained voice session closed: %s", session_id[:8])

    async def shutdown(self) -> None:
        """Shut down all active sessions."""
        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            await self.close_session(sid)
