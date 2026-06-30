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
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from voice.stt_providers import STTProvider, TranscriptFragment
from voice.transcript_filter import should_commit_user_transcript
from voice.tts_providers import TTSProvider

logger = logging.getLogger("feral.voice.chained_pipeline")


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
    _stt_task: asyncio.Task | None = field(default=None, repr=False)
    _last_transcript: str = ""
    _chunk_count: int = 0


class ChainedVoicePipeline:
    """Manages chained STT→LLM→TTS voice sessions.

    The pipeline does NOT reinvent LLM routing — it delegates to the
    brain's existing orchestrator via the ``llm_handle`` passed to
    ``open_session``.
    """

    def __init__(self):
        self._sessions: dict[str, ChainedSession] = {}

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

        When ``is_final=True``, flushes accumulated audio through
        STT → LLM → TTS and streams results back.
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

        if is_final:
            await self._flush_pipeline(session)

    async def _flush_pipeline(self, session: ChainedSession) -> None:
        """Run the full STT → LLM → TTS chain for accumulated audio."""
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

            await self._emit_transcript(session, transcript, is_partial=False)

            response_text = await self._run_llm(session, transcript)

            if response_text:
                await self._set_state(session, VoiceState.SPEAKING)
                await self._run_tts(session, response_text)

            await self._set_state(session, VoiceState.IDLE)

        except Exception as exc:
            logger.exception("Chained pipeline error for session %s", session.session_id[:8])
            await self._set_state(session, VoiceState.ERROR, error=str(exc))
            await self._set_state(session, VoiceState.IDLE)

    async def _collect_transcript(self, session: ChainedSession) -> str:
        """Drain any pending transcript fragments from the STT provider.

        For buffered providers (Whisper, Groq), ``flush()`` populates the
        result queue.  For streaming providers (Deepgram), fragments arrive
        via the open_stream iterator — but since we drive the pipeline
        synchronously after ``is_final``, we collect whatever is queued.
        """
        fragments: list[str] = []

        if hasattr(session.stt_provider, "_result_queue"):
            queue = session.stt_provider._result_queue
            while not queue.empty():
                frag = queue.get_nowait()
                if frag is not None:
                    fragments.append(frag.text)
                    if not frag.is_partial:
                        await self._emit_transcript(session, frag.text, is_partial=False)
                    else:
                        await self._emit_transcript(session, frag.text, is_partial=True)

        if hasattr(session.stt_provider, "_transcript_queue"):
            queue = session.stt_provider._transcript_queue
            while not queue.empty():
                frag = queue.get_nowait()
                if frag is not None:
                    fragments.append(frag.text)
                    if frag.is_final:
                        await self._emit_transcript(session, frag.text, is_partial=False)
                    else:
                        await self._emit_transcript(session, frag.text, is_partial=True)

        return " ".join(fragments) if fragments else session._last_transcript

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

        try:
            await session.stt_provider.close()
        except Exception:
            logger.debug("STT provider close error", exc_info=True)

        try:
            await session.tts_provider.close()
        except Exception:
            logger.debug("TTS provider close error", exc_info=True)

        if session._stt_task and not session._stt_task.done():
            session._stt_task.cancel()

        logger.info("Chained voice session closed: %s", session_id[:8])

    async def shutdown(self) -> None:
        """Shut down all active sessions."""
        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            await self.close_session(sid)
