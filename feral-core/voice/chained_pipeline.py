"""
Chained Voice Pipeline - STT -> LLM -> TTS
==========================================

The third voice mode: an explicit multi-stage pipeline where each
component (speech recognition, language model, speech synthesis) is
independently selectable and debuggable.

State machine::

    idle -> listening -> processing -> speaking -> idle
              ^                          |
              +--------------------------+

Each transition emits a ``voice_state`` frame to the phone so the
UI can drive the orb animation.  Transcript frames (partial + final)
are emitted during ``listening``.  TTS audio chunks are emitted
during ``speaking``.

Latency
-------
Three things used to serialise that nobody needed serialised, and
together they were most of the turn:

1. **Endpointing was two silence timers in series.** The client's
   energy gate held the socket open for 1.5s after the speaker
   stopped, and only once it went quiet did the server's own 0.8s
   packet-absence timer start. 2.3s of dead air per turn before the
   STT request was issued. :mod:`voice.vad` replaces both with Silero
   VAD reading the audio itself; the timer stays as the fallback for
   hosts that cannot run the model.
2. **TTS waited for the whole LLM answer.** The pipeline awaited
   ``handle_command_stream`` to completion and then scraped the last
   assistant message out of history. Now :mod:`voice.llm_stream_tap`
   observes the tokens as they are published and
   :mod:`voice.sentence_stream` cuts them into sentences, so sentence
   1 is being synthesised while the model is still writing sentence 2.
3. **Audio was emitted in one frame at the end.** Time-to-first-audio
   equalled total synthesis time. Providers that can emit PCM now
   stream it out incrementally; see :meth:`_speak_chunk`.

Barge-in
--------
:meth:`cancel` stops the turn in flight: the LLM task, the sentence
queue, the in-flight synthesis and the audio already queued on the
client. The chained path had no such thing, so a user who started
talking over a wrong answer had to listen to all of it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from voice.stt_providers import STTProvider, TranscriptFragment
from voice.transcript_filter import should_commit_user_transcript
from voice.tts_providers import TTSProvider

logger = logging.getLogger("feral.voice.chained_pipeline")

# Seconds of inbound-audio silence that end an utterance when neither
# the STT provider nor the server VAD gives us an end-of-speech signal.
#
# The pipeline used to flush ONLY on ``is_final=True`` from the client.
# No client ever sets it - ``feral-client-v2/src/lib/voiceRealtime.js``
# hardcodes ``is_final: false`` and the brain forwards what it got - so
# after a fallback morph the session accepted audio forever and emitted
# nothing. Three server-side drivers replace that flag now:
#
#   * server VAD (:mod:`voice.vad`) endpoints on the audio itself and
#     is the fast path, roughly 300ms after the speaker stops;
#   * streaming providers (Deepgram) flush on their own ``speech_final``
#     fragment, consumed by the per-session STT task;
#   * this timer, for buffered providers on a host with no VAD, which
#     produce nothing until someone calls ``flush()``.
#
# The timer is packet-absence based: it can only fire once the client
# stops sending, which is why it is the fallback and not the default.
SILENCE_FLUSH_SECONDS = 0.8

# Bytes of PCM16 accumulated before an audio frame goes out. 4800 is
# 100ms at 24kHz mono, matching the client's own capture frame size.
# Smaller frames mean lower time-to-first-audio but more WebSocket
# overhead; 100ms is the point where the overhead stops being free.
PCM_CHUNK_BYTES = 4800

# What the client is told to do with the bytes. ``pcm16`` routes to
# ``queuePcm16Playback``, which schedules raw samples on the shared
# AudioContext with no per-chunk decode, so partial buffers play.
PCM_ENCODING = "pcm16"
DEFAULT_TTS_SAMPLE_RATE = 24000


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
    sample_rate: int = 24000
    _audio_buffer: bytearray = field(default_factory=bytearray)
    # Consumes ``stt_provider.open_stream()``. Was declared but never
    # assigned (only ever cancelled), which meant ``open_stream`` was
    # never called at all - for Deepgram that is the call that opens
    # the WebSocket, so ``send_audio`` no-op'd on a None ``_ws`` and
    # the fallback pipeline could not transcribe a single word.
    _stt_task: asyncio.Task | None = field(default=None, repr=False)
    # Fires the end-of-utterance flush when the client stops sending
    # audio and nothing faster has already ended the utterance.
    _silence_task: asyncio.Task | None = field(default=None, repr=False)
    # The turn itself, as a cancellable unit. Barge-in cancels this.
    _turn_task: asyncio.Task | None = field(default=None, repr=False)
    _last_audio_ts: float = 0.0
    # Final transcript text handed over by the STT consumer task,
    # waiting to be picked up by the next flush.
    _pending_finals: list[str] = field(default_factory=list)
    _flushing: bool = False
    _last_transcript: str = ""
    _chunk_count: int = 0
    # Server VAD. ``None`` means this host could not load it and the
    # silence timer is doing the endpointing instead.
    _endpointer: Any = field(default=None, repr=False)
    _vad_speaking: bool = False
    # Monotonic index across every audio frame of one turn, so the
    # client can order them and spot a gap.
    _audio_seq: int = 0
    _cancelled: bool = False
    # Instrumentation for the latency bench and the degraded banner.
    last_endpoint_source: str = ""
    last_turn_started: float = 0.0


class ChainedVoicePipeline:
    """Manages chained STT->LLM->TTS voice sessions.

    The pipeline does NOT reinvent LLM routing - it delegates to the
    brain's existing orchestrator via the ``llm_handle`` passed to
    ``open_session``.
    """

    def __init__(
        self,
        *,
        silence_flush_seconds: float = SILENCE_FLUSH_SECONDS,
        vad_config: Any = None,
        vad_enabled: bool | None = None,
        barge_in: bool | None = None,
    ):
        self._sessions: dict[str, ChainedSession] = {}
        self._silence_flush_seconds = float(silence_flush_seconds)
        # ``None`` means "read settings on first use". The pipeline is
        # constructed by ``api/state.py`` with no arguments, so it has
        # to be able to configure itself; explicit arguments still win
        # so tests can pin behaviour without touching settings.
        self._vad_config = vad_config
        self._vad_enabled = vad_enabled
        self._barge_in = barge_in
        self._settings_loaded = False
        # One tap per LLM handle, shared across sessions.
        self._taps: dict[int, Any] = {}

    def _load_voice_settings(self) -> None:
        """Fill in whatever the constructor was not told, once.

        Reads ``voice.vad`` (see ``config/loader.py``). Failure is not
        fatal: the shipped defaults are the ones in :mod:`voice.vad`.
        """
        if self._settings_loaded:
            return
        self._settings_loaded = True
        block: dict = {}
        try:
            from config.loader import load_settings

            block = dict(((load_settings() or {}).get("voice") or {}).get("vad") or {})
        except Exception:
            logger.debug("could not read voice.vad settings", exc_info=True)
        if self._vad_enabled is None:
            self._vad_enabled = bool(block.get("enabled", True))
        if self._barge_in is None:
            self._barge_in = bool(block.get("barge_in", True))
        if self._vad_config is None and block:
            try:
                from voice.vad import VadConfig

                self._vad_config = VadConfig.from_settings(block)
            except Exception:
                logger.debug("could not build VadConfig", exc_info=True)

    # -- lifecycle ---------------------------------------------------

    async def open_session(
        self,
        session_id: str,
        stt_provider: STTProvider,
        tts_provider: TTSProvider,
        llm_handle: Any,
        send_frame: Callable[[str, dict], Awaitable[None]] | None = None,
        sample_rate: int = 24000,
    ) -> ChainedSession:
        """Create a new chained voice session.

        Args:
            session_id: Unique session identifier.
            stt_provider: An instantiated STT provider.
            tts_provider: An instantiated TTS provider.
            llm_handle: The brain's orchestrator - must have
                ``handle_command_stream(session_id, text, context)``.
            send_frame: Callback to emit frames to the phone client.
                Signature: ``async def send_frame(session_id, frame_dict)``
            sample_rate: Sample rate of the inbound PCM16. The VAD
                resamples from this, so a wrong value makes every
                endpointing decision wrong.
        """
        if session_id in self._sessions:
            await self.close_session(session_id)

        session = ChainedSession(
            session_id=session_id,
            stt_provider=stt_provider,
            tts_provider=tts_provider,
            llm_handle=llm_handle,
            send_frame=send_frame,
            sample_rate=int(sample_rate or 24000),
        )
        session._endpointer = self._build_endpointer(session)
        self._sessions[session_id] = session
        # Start consuming the provider's recognition stream. For
        # streaming providers this is the call that actually opens the
        # upstream socket (Deepgram connects inside ``open_stream`` and
        # starts its receive loop there) - without it ``send_audio``
        # silently returned on a None ``_ws``. For buffered providers
        # it just parks on the result queue until ``flush()`` fills it.
        session._stt_task = asyncio.create_task(self._consume_stt(session))
        await self._set_state(session, VoiceState.IDLE)
        logger.info(
            "Chained voice session opened: %s (endpointing: %s)",
            session_id[:8],
            "server VAD" if session._endpointer else "silence timer",
        )
        return session

    def _build_endpointer(self, session: ChainedSession):
        """Load a server VAD for this session, or ``None``.

        Never raises and never downloads. A host without onnxruntime
        or without the weights keeps the legacy timer; VAD is a
        latency optimisation, not a dependency.
        """
        self._load_voice_settings()
        if not self._vad_enabled:
            return None
        try:
            from voice.vad import load_endpointer

            return load_endpointer(
                sample_rate=session.sample_rate, config=self._vad_config
            )
        except Exception:
            logger.debug("VAD endpointer unavailable", exc_info=True)
            return None

    def get_session(self, session_id: str) -> ChainedSession | None:
        return self._sessions.get(session_id)

    def endpoint_mode(self, session_id: str) -> str:
        """``"vad"`` or ``"timer"``. Used by tests and the status frame."""
        session = self._sessions.get(session_id)
        if session is None:
            return ""
        return "vad" if session._endpointer is not None else "timer"

    # -- inbound audio -----------------------------------------------

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
        ends on the server VAD, the STT provider's own end-of-speech
        signal, or the silence timer either way. No shipped client sets
        the flag, and making it load-bearing is what left the fallback
        pipeline listening forever without ever answering.
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
        vad_ended = await self._feed_vad(session, audio_bytes)

        # The silence timer stays armed even when the VAD is live, as a
        # backstop rather than the primary endpointer. It has to: the
        # VAD can only see frames that arrive, so a client that simply
        # stops sending (every shipped client with an energy gate, and
        # any client whose socket stalls) would never produce an
        # end-of-speech event and the turn would hang forever. It was
        # disarmed under VAD in the first cut of this change and that
        # is exactly what happened - the latency bench's ``gated``
        # client never got an answer at all.
        #
        # It does not race the VAD in practice: a still-streaming
        # client gets its end-of-speech at ~300ms of detected silence,
        # long before ``SILENCE_FLUSH_SECONDS`` of packet absence can
        # elapse, and ``_drive_turn`` is idempotent for whichever
        # arrives second.
        if session._silence_task is None or session._silence_task.done():
            session._silence_task = asyncio.create_task(self._silence_timer(session))

        if is_final:
            await self._drive_turn(session, wait=True, source="client_is_final")
        elif vad_ended:
            # Must not block the audio pump: the client keeps sending
            # while we answer, and awaiting the turn here would stall
            # ingest for the whole reply.
            await self._drive_turn(session, wait=False, source="vad")

    async def _feed_vad(self, session: ChainedSession, audio_bytes: bytes) -> bool:
        """Run the VAD over one chunk. Returns True on end-of-speech.

        Also owns barge-in: speech detected while the assistant is
        talking cancels the turn.
        """
        endpointer = session._endpointer
        if endpointer is None or not audio_bytes:
            return False
        try:
            from voice.vad import VadEvent

            events = endpointer.feed(audio_bytes)
        except Exception:
            # A VAD that starts throwing must not take the session
            # with it. Drop to the timer for the rest of the session.
            logger.warning(
                "Server VAD failed mid-session %s - reverting to silence timer",
                session.session_id[:8],
                exc_info=True,
            )
            session._endpointer = None
            return False

        ended = False
        for event in events:
            if event == VadEvent.SPEECH_START:
                session._vad_speaking = True
                if self._barge_in and session.state in (
                    VoiceState.SPEAKING,
                    VoiceState.PROCESSING,
                ):
                    logger.info(
                        "Barge-in on session %s (%s)",
                        session.session_id[:8], session.state.value,
                    )
                    await self.cancel(session.session_id, reason="barge_in")
            elif event == VadEvent.SPEECH_END:
                session._vad_speaking = False
                ended = True
        return ended

    async def _consume_stt(self, session: ChainedSession) -> None:
        """Drain the STT provider's recognition stream for one session.

        Sole owner of the provider's fragments: it publishes partials
        to the client for latency feedback, parks final text in
        ``_pending_finals`` for the next flush, and - crucially -
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
            await self._drive_turn(
                session, wait=True, source="provider_speech_final"
            )

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
        await self._drive_turn(session, wait=True, source="silence_timer")

    def _cancel_silence_timer(self, session: ChainedSession) -> None:
        """Disarm the silence timer (no-op when it is the caller)."""
        task = session._silence_task
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        session._silence_task = None

    # -- the turn ----------------------------------------------------

    async def _drive_turn(
        self, session: ChainedSession, *, wait: bool, source: str = ""
    ) -> None:
        """Run one STT->LLM->TTS turn as a cancellable task.

        The turn has to be a task rather than a plain await so
        :meth:`cancel` has something to cancel; before this there was
        no handle on an in-flight turn at all, which is why the chained
        path had no barge-in. ``wait`` preserves the old synchronous
        contract for the callers that had it (``is_final``, the STT
        provider's own end-of-speech, the silence timer): those all run
        on their own tasks already, so blocking them costs nothing.
        The VAD path passes False because it runs on the audio ingest
        path, which must not stall for the length of a reply.

        ``asyncio.wait`` rather than ``await task`` on purpose: a
        barge-in that cancels the turn must not raise ``CancelledError``
        out of whichever coroutine happened to be waiting on it.
        """
        existing = session._turn_task
        if existing is not None and not existing.done():
            if wait:
                await asyncio.wait({existing})
            return
        # Recorded only by the driver that actually opens the turn.
        # Setting it at the call sites let a later loser (usually the
        # silence-timer backstop) overwrite the winner and the bench
        # then credited the wrong endpointer.
        session.last_endpoint_source = source
        task = asyncio.create_task(self._flush_pipeline(session))
        session._turn_task = task
        if wait:
            await asyncio.wait({task})

    async def _flush_pipeline(self, session: ChainedSession) -> None:
        """Run the full STT -> LLM -> TTS chain for accumulated audio."""
        if session._flushing:
            # Several drivers (VAD end-of-speech + provider
            # end-of-speech + silence timer + an explicit is_final) can
            # land on the same utterance. First one through owns it;
            # the others must not start a second LLM turn on the same
            # words.
            return
        session._flushing = True
        session._cancelled = False
        session._audio_seq = 0
        session.last_turn_started = time.monotonic()
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

            await self._run_turn(session, transcript)

            await self._set_state(session, VoiceState.IDLE)

        except asyncio.CancelledError:
            # Barge-in. Not an error: the user interrupted on purpose,
            # and the state was already moved by ``cancel``.
            logger.debug(
                "Chained turn cancelled for session %s", session.session_id[:8]
            )
            return
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
        scheduled to read yet - whichever side gets it first, the text
        is used exactly once.

        Returns ``""`` when there is nothing new. It used to fall back
        to ``session._last_transcript``, which meant a second flush on
        an already-consumed utterance re-ran the PREVIOUS turn through
        the LLM - a duplicate command from a silence timer that fired
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

    # -- LLM + TTS, overlapped ---------------------------------------

    async def _run_turn(self, session: ChainedSession, transcript: str) -> str:
        """Stream the LLM into TTS, sentence by sentence.

        Returns the full response text (for tests and logging).

        Before this the pipeline awaited ``handle_command_stream`` to
        completion and then scraped the last assistant message out of
        ``conversation_history``. That put the entire generation ahead
        of the entire synthesis: nothing was heard until the model had
        finished writing. Here the two overlap, and the speaker starts
        on sentence 1 while the model is still on sentence 2.

        There is a buffered fallback for handles the tap cannot reach
        (a stub orchestrator, or one whose ``send`` is not wrappable):
        it produces the same audio, just no sooner than it used to.
        """
        if not session.llm_handle:
            logger.warning("No LLM handle for session %s", session.session_id[:8])
            return ""

        from voice.llm_stream_tap import DeltaCollector
        from voice.sentence_stream import SentenceAccumulator, split_sentences

        tap = self._tap_for(session.llm_handle)
        collector = DeltaCollector() if tap is not None else None

        speech_queue: asyncio.Queue = asyncio.Queue()
        speaker = asyncio.create_task(self._speech_consumer(session, speech_queue))

        # How much text is worth one synthesis request is a property of
        # the engine, not of the language. macOS ``say`` pays roughly a
        # second of process start per invocation, so a chunk under
        # ~80 characters takes longer to synthesise than it takes to
        # play and the speaker underruns; a resident model like Piper
        # or a streaming cloud API has no such floor. Providers declare
        # their own floor and the default stays sentence-sized.
        min_chars = getattr(session.tts_provider, "min_chunk_chars", 0)
        accumulator = (
            SentenceAccumulator(min_chars=int(min_chars))
            if min_chars
            else SentenceAccumulator()
        )
        response_text = ""
        try:
            if tap is not None and collector is not None:
                tap.subscribe(session.session_id, collector.on_delta)
                llm_task = asyncio.create_task(
                    session.llm_handle.handle_command_stream(
                        session_id=session.session_id,
                        text=transcript,
                        context={"source": "voice_chained"},
                    )
                )
                llm_task.add_done_callback(lambda _t: collector.close())
                try:
                    async for delta in collector.stream():
                        for chunk in accumulator.push(delta):
                            await speech_queue.put(chunk)
                    # Surface an LLM failure rather than answering with
                    # silence: the caller turns it into an error state.
                    await llm_task
                finally:
                    tap.unsubscribe(session.session_id)
                    if not llm_task.done():
                        llm_task.cancel()
                        with contextlib.suppress(
                            asyncio.CancelledError, Exception
                        ):
                            await llm_task
                tail = accumulator.flush()
                if tail:
                    await speech_queue.put(tail)
                response_text = accumulator.full_text()

            if not response_text:
                # No tap, or a turn that produced no stream_delta (a
                # tool-only turn, or a non-streaming handle). Fall back
                # to the history scrape, then still split it so TTS
                # gets sentence-sized work.
                if tap is None:
                    await session.llm_handle.handle_command_stream(
                        session_id=session.session_id,
                        text=transcript,
                        context={"source": "voice_chained"},
                    )
                response_text = self._extract_last_response(session)
                split_kwargs = {"min_chars": int(min_chars)} if min_chars else {}
                for chunk in split_sentences(response_text, **split_kwargs):
                    await speech_queue.put(chunk)
        except asyncio.CancelledError:
            speaker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await speaker
            raise
        except Exception:
            logger.exception("LLM call failed for session %s", session.session_id[:8])
            speaker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await speaker
            raise
        finally:
            if collector is not None:
                collector.close()

        await speech_queue.put(None)
        await speaker
        return response_text

    def _tap_for(self, llm_handle: Any):
        """Reuse one :class:`LLMStreamTap` per orchestrator instance."""
        from voice.llm_stream_tap import LLMStreamTap

        key = id(llm_handle)
        tap = self._taps.get(key)
        if tap is not None:
            return tap
        tap = LLMStreamTap.attach(llm_handle)
        if tap is not None:
            self._taps[key] = tap
        return tap

    async def _speech_consumer(
        self, session: ChainedSession, queue: asyncio.Queue
    ) -> None:
        """Synthesise queued sentences in order and emit their audio.

        Serialised on purpose. Synthesising two sentences concurrently
        would finish them out of order on any provider whose latency
        varies with text length, and the seam would be audible.
        """
        spoke = False
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                if not chunk.strip():
                    continue
                if not spoke:
                    spoke = True
                    await self._set_state(session, VoiceState.SPEAKING)
                await self._speak_chunk(session, chunk)
        except asyncio.CancelledError:
            raise
        finally:
            if spoke and not session._cancelled:
                # Sentinel close frame so the client knows the TTS turn
                # is complete. data_b64="" matches the existing wire
                # protocol (``queueAudioPlayback`` no-ops on empty /
                # final).
                await self._emit_audio_chunk(
                    session, "", chunk_index=session._audio_seq, is_final=True,
                    encoding=self._tts_encoding(session),
                )

    def _tts_encoding(self, session: ChainedSession) -> str:
        """What the provider is producing.

        Providers advertise ``output_format``; anything that is not a
        raw-sample format is treated as a self-contained encoded file
        and buffered whole, because a slice of an MP3 is not an MP3
        and ``decodeAudioData`` rejects it (the 2026-05-15 silent-voice
        report).
        """
        fmt = str(getattr(session.tts_provider, "output_format", "mp3") or "mp3").lower()
        if fmt in ("pcm", "pcm16", "pcm_s16le", "linear16", "wav", "raw"):
            return PCM_ENCODING
        return fmt

    def _tts_sample_rate(self, session: ChainedSession) -> int:
        try:
            return int(
                getattr(session.tts_provider, "sample_rate", DEFAULT_TTS_SAMPLE_RATE)
                or DEFAULT_TTS_SAMPLE_RATE
            )
        except (TypeError, ValueError):
            return DEFAULT_TTS_SAMPLE_RATE

    async def _speak_chunk(self, session: ChainedSession, text: str) -> None:
        """Synthesise one sentence and emit it.

        PCM providers stream: every ``PCM_CHUNK_BYTES`` goes out as its
        own frame the moment it exists, so time-to-first-audio is one
        provider chunk rather than one whole response.

        Encoded providers (MP3) still buffer, but now per sentence
        rather than per turn. Each emitted frame is a complete,
        independently decodable file, which is the invariant the
        v2026.5.28 fix established and which per-slice streaming broke.
        """
        encoding = self._tts_encoding(session)
        sample_rate = self._tts_sample_rate(session)
        try:
            if encoding == PCM_ENCODING:
                buffer = bytearray()
                async for audio_chunk in session.tts_provider.synthesize(text):
                    if not audio_chunk:
                        continue
                    buffer.extend(audio_chunk)
                    while len(buffer) >= PCM_CHUNK_BYTES:
                        frame = bytes(buffer[:PCM_CHUNK_BYTES])
                        del buffer[:PCM_CHUNK_BYTES]
                        await self._emit_pcm(session, frame, sample_rate)
                if buffer:
                    await self._emit_pcm(session, bytes(buffer), sample_rate)
                return

            buffer = bytearray()
            async for audio_chunk in session.tts_provider.synthesize(text):
                buffer.extend(audio_chunk)
            if buffer:
                session._audio_seq += 1
                await self._emit_audio_chunk(
                    session,
                    base64.b64encode(bytes(buffer)).decode("ascii"),
                    chunk_index=session._audio_seq,
                    is_final=False,
                    encoding=encoding,
                    sample_rate=sample_rate,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("TTS failed for session %s", session.session_id[:8])
            raise

    async def _emit_pcm(
        self, session: ChainedSession, frame: bytes, sample_rate: int
    ) -> None:
        session._audio_seq += 1
        await self._emit_audio_chunk(
            session,
            base64.b64encode(frame).decode("ascii"),
            chunk_index=session._audio_seq,
            is_final=False,
            encoding=PCM_ENCODING,
            sample_rate=sample_rate,
        )

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

    # -- barge-in ----------------------------------------------------

    async def cancel(self, session_id: str, *, reason: str = "user_interrupt") -> bool:
        """Stop the in-flight turn. Returns True if there was one.

        Cancels the turn task, which takes the LLM call, the sentence
        queue and the in-flight synthesis with it, then tells the
        client to drop whatever audio it has already buffered. Without
        that last frame the user keeps hearing the old answer for as
        long as the client had queued ahead, which is exactly the
        experience barge-in exists to remove.

        Safe to call on an idle session, an unknown session, or twice.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False

        task = session._turn_task
        had_turn = task is not None and not task.done()
        session._cancelled = True
        session._turn_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        session._flushing = False

        # Drop transcript fragments belonging to the abandoned turn so
        # the next utterance is not prefixed with the interrupted one.
        session._pending_finals.clear()

        if had_turn:
            await self._emit_frame(
                session,
                {
                    "type": "voice_cancel",
                    "payload": {
                        "reason": reason,
                        "mode": "chained",
                        "drop_pending_audio": True,
                    },
                },
            )
            await self._set_state(session, VoiceState.LISTENING)
        return had_turn

    # -- frames ------------------------------------------------------

    async def _set_state(
        self, session: ChainedSession, state: VoiceState, *, error: str = ""
    ) -> None:
        """Transition state and emit a voice_state frame."""
        old = session.state
        session.state = state
        logger.debug(
            "Session %s: %s -> %s", session.session_id[:8], old.value, state.value
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

        await self._emit_frame(session, frame)

    async def _emit_transcript(
        self, session: ChainedSession, text: str, is_partial: bool
    ) -> None:
        """Emit a transcript frame to the phone."""
        await self._emit_frame(session, {
            "type": "transcript",
            "payload": {
                "text": text,
                "is_partial": is_partial,
                "role": "user",
            },
        })

    async def _emit_audio_chunk(
        self,
        session: ChainedSession,
        data_b64: str,
        chunk_index: int,
        is_final: bool,
        encoding: str = "mp3",
        sample_rate: int = DEFAULT_TTS_SAMPLE_RATE,
    ) -> None:
        """Emit a TTS audio chunk frame to the phone."""
        await self._emit_frame(session, {
            "type": "audio_chunk",
            "payload": {
                "data_b64": data_b64,
                "chunk_index": chunk_index,
                "is_final": is_final,
                "encoding": encoding,
                "sample_rate": sample_rate,
            },
        })

    async def _emit_frame(self, session: ChainedSession, frame: dict) -> None:
        if session.send_frame:
            await session.send_frame(session.session_id, frame)

    # -- teardown ----------------------------------------------------

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
        for task in (session._stt_task, session._silence_task, session._turn_task):
            if task is None or task.done() or task is asyncio.current_task():
                continue
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        session._stt_task = None
        session._silence_task = None
        session._turn_task = None
        session._endpointer = None

        logger.info("Chained voice session closed: %s", session_id[:8])

    async def shutdown(self) -> None:
        """Shut down all active sessions."""
        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            await self.close_session(sid)
        for tap in list(self._taps.values()):
            with contextlib.suppress(Exception):
                tap.uninstall()
        self._taps.clear()
