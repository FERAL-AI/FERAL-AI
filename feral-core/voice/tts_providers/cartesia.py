"""
Cartesia TTS provider for the chained voice pipeline.

Implements two paths against Cartesia's Sonic API:

* **REST** (default) — ``POST /tts/bytes`` returning the full
  encoded audio body. Streams chunks via ``aiter_bytes`` so the
  pipeline can forward audio to the phone before the body is
  fully buffered. Lower setup cost, ~1-2s end-to-end latency.

* **WebSocket** — ``wss://api.cartesia.ai/tts/websocket`` with
  incremental text + audio framing. Lower first-audio latency
  (~250-400ms) at the cost of a persistent connection. Selected
  via ``websocket=True`` constructor flag.

Both paths use Cartesia's Sonic family of voices (sonic-2 by
default — replace via ``model_id``). Cartesia requires a date-
pinned ``Cartesia-Version`` header on every request; we send the
2024-11-13 stable version.

Closes Lane 05  acceptance: "add Cartesia TTS module
(`cartesia.py` — REST `/audio` endpoint, websocket variant for
streaming); each: probe via vault key; structured error on
failure; live test trace in PR".
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

from voice.tts_providers import TTSProvider, register_tts_provider

logger = logging.getLogger("feral.voice.tts.cartesia")

CARTESIA_REST_TTS_URL = "https://api.cartesia.ai/tts/bytes"
CARTESIA_WS_TTS_URL = "wss://api.cartesia.ai/tts/websocket"
CARTESIA_VERSION = "2024-11-13"
CHUNK_SIZE = 4096


# Cartesia rejects requests with an unfamiliar ``output_format``
# shape. The validated MP3 + raw_bytes shape below matches the
# 2024-11-13 schema.
def _output_format(container: str, sample_rate: int) -> dict:
    if container == "mp3":
        return {"container": "mp3", "encoding": "mp3", "sample_rate": sample_rate}
    return {
        "container": "raw",
        "encoding": "pcm_s16le",
        "sample_rate": sample_rate,
    }


@register_tts_provider("cartesia")
class CartesiaTTSProvider(TTSProvider):
    """Streaming TTS via Cartesia Sonic.

    Parameters
    ----------
    api_key:
        Cartesia API key (sourced from the vault by the boot
        wiring; raised if empty).
    voice_id:
        Voice id from Cartesia's voice catalogue. Defaults to
        the "Newsman" voice (a calm narration default).
    model_id:
        Sonic model id. Defaults to ``sonic-2`` (the 2026
        production model). Older accounts can pin ``sonic`` or
        ``sonic-english`` here.
    output_container:
        ``mp3`` (default) or ``raw`` (PCM s16le). MP3 is what the
        chained pipeline forwards to phone clients today; raw
        PCM is exposed for future direct-to-speaker pipelines.
    sample_rate:
        Output sample rate in Hz. 22050 matches Cartesia's
        recommendation for Sonic-2; 44100 also accepted.
    websocket:
        When True, route audio through the websocket variant for
        lower first-audio latency. Default False (REST).
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        voice_id: str = "5345cf08-6f37-424d-a5d9-8ae1101b9377",
        model_id: str = "sonic-2",
        output_container: str = "mp3",
        sample_rate: int = 22050,
        language: str = "en",
        websocket: bool = False,
    ):
        if not api_key:
            raise ValueError("CartesiaTTSProvider requires a CARTESIA_API_KEY")
        if output_container not in ("mp3", "raw"):
            raise ValueError(
                f"output_container must be 'mp3' or 'raw', got {output_container!r}"
            )
        self._api_key = api_key
        self._voice_id = voice_id
        self._model_id = model_id
        self._output_container = output_container
        self._sample_rate = sample_rate
        self._language = language
        self._use_websocket = websocket

    @property
    def use_websocket(self) -> bool:
        return self._use_websocket

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream audio chunks for *text*."""
        if not text.strip():
            return

        if self._use_websocket:
            async for chunk in self._synthesize_websocket(text):
                yield chunk
            return

        async for chunk in self._synthesize_rest(text):
            yield chunk

    # ── REST path ──────────────────────────────────────────────

    async def _synthesize_rest(self, text: str) -> AsyncIterator[bytes]:
        payload = {
            "model_id": self._model_id,
            "transcript": text,
            "voice": {"mode": "id", "id": self._voice_id},
            "output_format": _output_format(self._output_container, self._sample_rate),
            "language": self._language,
        }

        headers = {
            "X-API-Key": self._api_key,
            "Cartesia-Version": CARTESIA_VERSION,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST",
                CARTESIA_REST_TTS_URL,
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    detail = body.decode(errors="replace")[:300]
                    logger.error(
                        "Cartesia REST TTS failed: %d %s",
                        response.status_code,
                        detail,
                    )
                    raise httpx.HTTPStatusError(
                        f"Cartesia TTS HTTP {response.status_code}: {detail}",
                        request=response.request,
                        response=response,
                    )
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    if chunk:
                        yield chunk

    # ── WebSocket path ─────────────────────────────────────────

    async def _synthesize_websocket(self, text: str) -> AsyncIterator[bytes]:
        """Send the text over Cartesia's websocket TTS endpoint and
        yield incoming audio chunks as they arrive.

        Cartesia's WS protocol is JSON-framed: each outbound message
        is ``{model_id, transcript, voice, output_format, ...}``;
        each inbound message is either ``{"type": "chunk", "data":
        "<base64>"}`` (audio) or ``{"type": "done"}`` / ``{"type":
        "error", ...}``. We decode and yield the raw bytes.
        """
        try:
            from websockets.asyncio.client import connect as _ws_connect
        except ImportError:
            from websockets import connect as _ws_connect  # type: ignore

        url = (
            f"{CARTESIA_WS_TTS_URL}?api_key={self._api_key}"
            f"&cartesia_version={CARTESIA_VERSION}"
        )

        # The websocket variant accepts the same payload shape as
        # REST — modulo the ``context_id`` field which Cartesia uses
        # to correlate streamed text → streamed audio. We reuse the
        # voice id as the context (one TTS call = one context).
        request = {
            "model_id": self._model_id,
            "transcript": text,
            "voice": {"mode": "id", "id": self._voice_id},
            "output_format": _output_format(
                self._output_container, self._sample_rate
            ),
            "language": self._language,
            "context_id": self._voice_id,
            "continue": False,
        }

        import base64

        async with _ws_connect(url) as ws:
            await ws.send(json.dumps(request))
            try:
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, bytes):
                        # Some Cartesia builds send raw bytes for
                        # audio frames. Forward as-is.
                        yield raw
                        continue
                    try:
                        frame = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("Cartesia WS: non-JSON message %r", raw[:80])
                        continue

                    frame_type = frame.get("type")
                    if frame_type == "chunk" and frame.get("data"):
                        try:
                            yield base64.b64decode(frame["data"])
                        except (ValueError, TypeError) as exc:
                            logger.warning(
                                "Cartesia WS: bad base64 chunk: %s", exc
                            )
                    elif frame_type == "done":
                        break
                    elif frame_type == "error":
                        msg = frame.get("error") or frame.get("message") or "unknown"
                        logger.error("Cartesia WS error: %s", msg)
                        raise RuntimeError(f"Cartesia WS error: {msg}")
            finally:
                try:
                    await ws.close()
                except Exception:
                    pass
