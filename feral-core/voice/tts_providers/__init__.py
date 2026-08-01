"""
TTS Provider abstraction for the chained voice pipeline.

Each provider implements ``TTSProvider`` and is selected at runtime by
``VoiceRouter._resolve_chained_config``: per-session ``provider_opts``
win, then ``voice.chained.tts_provider`` (what the phone Settings panel
writes), then ``audio.chained_fallback.tts_provider`` (what ``feral
setup`` writes and ``config/loader.py`` defaults).  All providers expose
``synthesize(text) -> async iterator of audio bytes``, streaming
base64-encoded audio chunks for the phone client.
"""

from __future__ import annotations

import abc
import logging
from typing import AsyncIterator

logger = logging.getLogger("feral.voice.tts")


class TTSProvider(abc.ABC):
    """Base class for text-to-speech providers.

    Usage::

        provider = SomeTTSProvider(api_key=..., voice=...)
        async for audio_chunk_bytes in provider.synthesize("Hello world"):
            send_to_phone(base64.b64encode(audio_chunk_bytes))
    """

    @abc.abstractmethod
    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Convert text to speech, yielding raw audio chunks.

        Each yielded ``bytes`` object is a chunk of encoded audio
        (MP3 or PCM) ready for base64-encoding and sending to the
        phone client.
        """
        ...

    async def close(self) -> None:
        """Release any held resources."""
        pass


_PROVIDER_REGISTRY: dict[str, type[TTSProvider]] = {}


def register_tts_provider(name: str):
    """Decorator to register a TTS provider by config name."""
    def decorator(cls: type[TTSProvider]):
        _PROVIDER_REGISTRY[name] = cls
        return cls
    return decorator


def get_tts_provider(name: str, **kwargs) -> TTSProvider:
    """Instantiate a TTS provider by its registered config name."""
    cls = _PROVIDER_REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown TTS provider {name!r}. "
            f"Available: {sorted(_PROVIDER_REGISTRY)}"
        )
    return cls(**kwargs)
