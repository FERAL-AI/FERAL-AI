"""
Deepgram streaming STT provider.

Uses the Deepgram Live Streaming API over WebSocket::

    wss://api.deepgram.com/v1/listen?model=nova-3&encoding=linear16
        &sample_rate=16000&language=en&interim_results=true&endpointing=300

Auth is via ``Authorization: Token <DEEPGRAM_API_KEY>`` header.

Deepgram returns ``Results`` events with ``is_final`` (utterance segment
finalised) and ``speech_final`` (speaker has stopped — full utterance done).
We surface both so the pipeline can decide when to flush to LLM.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Protocol

from voice.stt_providers import (
    STTProvider,
    TranscriptFragment,
    register_stt_provider,
)
from voice.transcript_filter import should_commit_user_transcript

logger = logging.getLogger("feral.voice.stt.deepgram")

DEEPGRAM_WS_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model={model}"
    "&encoding=linear16"
    "&sample_rate={sample_rate}"
    "&language={language}"
    "&interim_results=true"
    "&endpointing=300"
)


class RecognitionTransport(Protocol):
    """The shared modern/legacy WebSocket surface this provider actually uses."""

    async def send(self, message: str | bytes) -> None: ...
    async def close(self) -> None: ...
    def __aiter__(self) -> AsyncIterator[str | bytes]: ...


@register_stt_provider("deepgram")
class DeepgramSTTProvider(STTProvider):
    """Streaming STT via Deepgram Nova."""

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = "nova-3",
        language: str = "en",
        sample_rate: int = 16000,
    ):
        if not api_key:
            raise ValueError("DeepgramSTTProvider requires a DEEPGRAM_API_KEY")
        self._api_key = api_key
        self._model = model
        self._language = language
        self._sample_rate = sample_rate
        self._ws: RecognitionTransport | None = None
        self._transcript_queue: asyncio.Queue[TranscriptFragment | None] = asyncio.Queue()
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self._prepare_lock = asyncio.Lock()

    async def prepare(self) -> None:
        """Open one transport before accepting the first microphone bytes."""
        import websockets
        async with self._prepare_lock:
            if self._closed:
                raise RuntimeError("Recognition provider is closed")
            if self._ws is not None:
                return
            url = DEEPGRAM_WS_URL.format(
                model=self._model, sample_rate=self._sample_rate, language=self._language,
            )
            auth_headers = {"Authorization": f"Token {self._api_key}"}
            socket: RecognitionTransport
            try:
                from websockets.asyncio.client import connect as _ws_connect
            except ImportError:
                socket = await websockets.connect(url, extra_headers=auth_headers)
            else:
                socket = await _ws_connect(url, additional_headers=auth_headers)
            # A delayed connection must not resurrect a closed provider.
            if self._closed:
                await socket.close()
                raise RuntimeError("Recognition provider is closed")
            self._ws = socket
            self._recv_task = asyncio.create_task(self._receive_loop())

    async def open_stream(self) -> AsyncIterator[TranscriptFragment]:
        """Prepare the transport and yield transcript fragments."""
        await self.prepare()

        try:
            while True:
                fragment = await self._transcript_queue.get()
                if fragment is None:
                    break
                yield fragment
        finally:
            await self.close()

    async def _receive_loop(self) -> None:
        """Read Deepgram WebSocket events and enqueue transcript fragments."""
        socket = self._ws
        if socket is None:
            return
        try:
            async for raw_msg in socket:
                try:
                    event = json.loads(raw_msg)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Deepgram: non-JSON message received")
                    continue

                msg_type = event.get("type", "")

                if msg_type == "Results":
                    self._handle_results(event)
                elif msg_type == "Metadata":
                    logger.debug("Deepgram metadata: %s", event)
                elif msg_type == "Error":
                    err_msg = event.get("description", event.get("message", str(event)))
                    logger.error("Deepgram error: %s", err_msg)
                    await self._transcript_queue.put(None)
                    raise RuntimeError(f"Deepgram error: {err_msg}")
                else:
                    logger.debug("Deepgram event type=%s", msg_type)
        except Exception:
            if not self._closed:
                logger.exception("Deepgram receive loop error")
                raise
        finally:
            await self._transcript_queue.put(None)

    def _handle_results(self, event: dict) -> None:
        """Parse a Deepgram Results event into TranscriptFragment(s)."""
        channel = event.get("channel", {})
        alternatives = channel.get("alternatives", [])
        if not alternatives:
            return

        best = alternatives[0]
        text = best.get("transcript", "").strip()
        if not text:
            return

        is_final = event.get("is_final", False)
        speech_final = event.get("speech_final", False)
        confidence = best.get("confidence", 1.0)

        # Bug 3 (phantom commit gate): Deepgram nova-3 commits the same
        # stock closers whisper does ("bye-bye", "thank you", "thanks
        # for watching") on trailing silence — usually with low
        # confidence + speech_final=True. Apply the shared gate to
        # FINAL fragments only so partials still stream through for
        # latency visibility; partial-only phantoms never reach the
        # LLM either way because the pipeline acts on finals.
        if is_final and not should_commit_user_transcript(
            text, confidence=confidence, speech_final=speech_final,
        ):
            logger.info(
                "deepgram: dropped phantom/low-conf final commit "
                "text=%r conf=%.2f speech_final=%s",
                text, confidence, speech_final,
            )
            return

        fragment = TranscriptFragment(
            text=text,
            is_partial=not is_final,
            is_final=is_final,
            confidence=confidence,
            speech_final=speech_final,
        )
        self._transcript_queue.put_nowait(fragment)

    async def send_audio(self, audio_bytes: bytes) -> None:
        """Forward raw PCM16 audio to Deepgram."""
        await self.prepare()
        socket = self._ws
        if self._closed:
            raise RuntimeError("Recognition provider is closed")
        if socket is None:
            raise RuntimeError("Recognition transport is unavailable")
        await socket.send(audio_bytes)

    async def close(self) -> None:
        """Send CloseStream and tear down the WebSocket."""
        if self._closed:
            return
        self._closed = True
        self._transcript_queue.put_nowait(None)

        if self._ws:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except Exception:
                logger.debug("Deepgram close: ws already closed")

        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
