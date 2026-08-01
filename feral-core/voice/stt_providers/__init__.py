"""
STT Provider abstraction for the chained voice pipeline.

Each provider implements ``STTProvider`` and is selected at runtime by
``VoiceRouter._resolve_chained_config``: per-session ``provider_opts``
win, then ``voice.chained.stt_provider`` (what the phone Settings panel
writes), then ``audio.chained_fallback.stt_provider`` (what ``feral
setup`` writes and ``config/loader.py`` defaults).  Two integration
patterns exist:

* **Streaming** (e.g. Deepgram): audio chunks are forwarded in real-time and
  partial/final transcripts arrive asynchronously.
* **Buffered** (e.g. OpenAI Whisper, Groq Whisper): audio is accumulated
  until the utterance is complete, then sent as a single HTTP request.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import AsyncIterator

logger = logging.getLogger("feral.voice.stt")


@dataclass
class TranscriptFragment:
    """A single STT result fragment."""
    text: str
    is_partial: bool = True
    is_final: bool = False
    confidence: float = 1.0
    speech_final: bool = False


class STTProvider(abc.ABC):
    """Base class for speech-to-text providers.

    Lifecycle::

        provider = SomeSTTProvider(api_key=..., model=...)
        async for fragment in provider.open_stream():
            await provider.send_audio(chunk_bytes)
            ...
        await provider.close()

    Streaming providers yield ``TranscriptFragment`` objects as audio
    arrives.  Buffered providers yield a single fragment after ``close()``
    is called (or ``flush()`` for explicit end-of-utterance).
    """

    @abc.abstractmethod
    async def open_stream(self) -> AsyncIterator[TranscriptFragment]:
        """Open a recognition stream and yield transcript fragments."""
        ...

    @abc.abstractmethod
    async def send_audio(self, audio_bytes: bytes) -> None:
        """Send a chunk of raw audio (PCM16 / linear16, 16 kHz)."""
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        """Signal end-of-audio and clean up resources."""
        ...

    async def flush(self) -> None:
        """Flush buffered audio (no-op for streaming providers)."""
        pass


_PROVIDER_REGISTRY: dict[str, type[STTProvider]] = {}


def register_stt_provider(name: str):
    """Decorator to register an STT provider by config name."""
    def decorator(cls: type[STTProvider]):
        _PROVIDER_REGISTRY[name] = cls
        return cls
    return decorator


def get_stt_provider(name: str, **kwargs) -> STTProvider:
    """Instantiate an STT provider by its registered config name."""
    cls = _PROVIDER_REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown STT provider {name!r}. "
            f"Available: {sorted(_PROVIDER_REGISTRY)}"
        )
    return cls(**kwargs)
