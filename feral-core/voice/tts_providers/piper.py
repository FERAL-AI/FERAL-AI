"""Piper TTS (fully local, cross-platform). Tier 1.

Piper is the cross-platform local voice: small ONNX models, dozens of
languages, runs on a Raspberry Pi. It is the right default anywhere
macOS ``say`` is not available.

Licensing
---------
**Piper is GPL-3.0-or-later.** It relicensed away from MIT with the
``piper1-gpl`` rewrite. FERAL is Apache-2.0, and Apache-2.0 code that
must be combined with GPL-3.0 code to function creates an obligation
the rest of the project does not carry.

So it lives behind its own extra, ``feral-ai[tts-piper]``, and nothing
installs it implicitly. It is never the default on macOS, where ``say``
does the same job with no licensing question at all. The dependency is
an explicit, informed opt-in or it is absent.

Voice weights are a separate matter: the ``piper-voices`` models on
HuggingFace carry permissive per-voice licences, and they are
downloaded on demand into ``$FERAL_HOME/models/piper`` during setup,
never mid-session.

Verified, and not verified
--------------------------
Verified on this machine: the voice download path (``en_US-amy-low``,
63MB, fetched through :mod:`voice.local_models`), the package metadata
(``piper-tts`` 1.6.0, ``GPL-3.0-or-later``), and the 1.6 Python API
shape (``PiperVoice.load`` then ``synthesize()`` yielding ``AudioChunk``
with ``audio_int16_bytes`` / ``sample_rate``).

NOT verified: actual synthesis. The macOS arm64 wheels for both
``piper-tts`` 1.4.2 and 1.6.0 abort with::

    Error processing file '/Users/runner/work/piper1-gpl/piper1-gpl/
    _skbuild/.../espeak-ng-data/phontab': No such file or directory

The wheel bundles a complete ``espeak-ng-data`` directory, but the
native library resolves the build machine's absolute path instead, and
neither the ``espeak_data_dir`` argument nor ``ESPEAK_DATA_PATH``
overrides it. That is an upstream packaging defect on this platform, it
is another reason ``say`` is tier 0 here, and it means the synthesis
path below is written against the documented API rather than an
observed one. :func:`piper_available` runs a real phonemisation so an
operator finds out at setup time rather than mid-conversation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from voice.tts_providers import TTSProvider, register_tts_provider

logger = logging.getLogger("feral.voice.tts.piper")

DEFAULT_VOICE = "en_US-lessac-medium"
STREAM_CHUNK_BYTES = 4800

# Piper voices are typically 22050Hz, but the value is per-voice and
# read off the model config at load time rather than assumed.
FALLBACK_SAMPLE_RATE = 22050

_VOICE_CACHE: dict[str, Any] = {}
_CACHE_LOCK = asyncio.Lock()
_SYNTH_LOCK = asyncio.Lock()


def piper_available(voice: str = DEFAULT_VOICE) -> tuple[bool, str]:
    """``(ready, reason)``. Import, weights, and a real synthesis probe.

    The probe matters: on macOS arm64 the package imports cleanly and
    the weights are present, and synthesis still fails on a bad espeak
    data path baked into the wheel. Reporting "ready" on importability
    alone would push that failure into the middle of a conversation.
    """
    try:
        from piper import PiperVoice  # noqa: F401
    except Exception:
        return False, "piper-tts not installed (pip install 'feral-ai[tts-piper]')"
    from voice import local_models

    if not local_models.piper_voice_present(voice):
        return False, f"Piper voice {voice!r} not downloaded"
    try:
        _load_voice_blocking(voice)
    except Exception as exc:
        return False, f"piper cannot synthesise here: {exc}"
    return True, "ready"


def _load_voice_blocking(voice: str):
    """Load (and cache) a Piper voice. Blocking; call in an executor."""
    from piper import PiperVoice

    from voice import local_models

    weights, config = local_models.piper_voice_specs(voice)
    model_path = local_models.model_path(weights.family, weights.filename)
    config_path = local_models.model_path(config.family, config.filename)
    return PiperVoice.load(str(model_path), config_path=str(config_path))


@register_tts_provider("piper")
class PiperTTSProvider(TTSProvider):
    """Local TTS via Piper. GPL-3.0-or-later, opt-in extra."""

    is_local = True
    #: Piper hands back raw int16 samples, so the pipeline can stream
    #: frames as they are produced rather than buffer a whole file.
    output_format = "pcm"
    #: Piper's per-call overhead is small (no process spawn, model is
    #: resident), so sentence-sized chunks are fine here. Contrast with
    #: ``macos_say``, which pays ~1s per invocation.
    min_chunk_chars = 24

    def __init__(
        self,
        *,
        api_key: str = "",
        voice: str = DEFAULT_VOICE,
        voice_id: str = "",
        sample_rate: int = 0,
        length_scale: float = 0.0,
        allow_download: bool = False,
    ):
        # Accepted and ignored - the router passes ``api_key=`` to every
        # TTS constructor. Nothing local reads it.
        del api_key

        # ``voice_id`` is what the cloud providers call their voice
        # selector and what the settings block already carries, so
        # accept it as an alias rather than make the operator learn a
        # second key name.
        self._voice = (voice_id or voice or DEFAULT_VOICE).strip()
        self._length_scale = float(length_scale or 0.0)
        self._allow_download = bool(allow_download)
        # Populated from the voice config at load; the constructor
        # cannot know it without loading the model.
        self.sample_rate = int(sample_rate or 0) or FALLBACK_SAMPLE_RATE

        if not self._allow_download:
            ready, reason = piper_available(self._voice)
            if not ready:
                raise RuntimeError(
                    f"Local Piper TTS is unavailable: {reason}. "
                    "Run `feral setup` and choose local voice to install it. "
                    "FERAL will not silently fall back to a cloud provider."
                )

    async def _ensure_voice(self):
        cached = _VOICE_CACHE.get(self._voice)
        if cached is not None:
            return cached
        async with _CACHE_LOCK:
            cached = _VOICE_CACHE.get(self._voice)
            if cached is not None:
                return cached
            loop = asyncio.get_running_loop()
            handle = await loop.run_in_executor(
                None, _load_voice_blocking, self._voice
            )
            _VOICE_CACHE[self._voice] = handle
            logger.info("Piper voice %r loaded", self._voice)
            return handle

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        text = (text or "").strip()
        if not text:
            return
        handle = await self._ensure_voice()
        loop = asyncio.get_running_loop()
        async with _SYNTH_LOCK:
            pcm, rate = await loop.run_in_executor(
                None, self._synthesize_blocking, handle, text
            )
        if rate:
            self.sample_rate = rate
        for offset in range(0, len(pcm), STREAM_CHUNK_BYTES):
            yield pcm[offset: offset + STREAM_CHUNK_BYTES]

    def _synthesize_blocking(self, handle, text: str) -> tuple[bytes, int]:
        """Run Piper and return ``(pcm16_bytes, sample_rate)``.

        Two API generations are handled. 1.3+ yields ``AudioChunk``
        objects carrying their own sample rate; 1.2 exposed
        ``synthesize_stream_raw`` returning bare bytes at the rate in
        the voice config. Pinning one would break the other for no
        benefit, and the extra is version-ranged loosely on purpose so
        an operator's existing install keeps working.
        """
        buffer = bytearray()
        rate = 0

        synthesize = getattr(handle, "synthesize", None)
        if callable(synthesize):
            for chunk in synthesize(text):
                data = getattr(chunk, "audio_int16_bytes", None)
                if data is None:
                    # Very old builds yielded raw bytes here.
                    data = chunk if isinstance(chunk, (bytes, bytearray)) else b""
                buffer.extend(data)
                rate = int(getattr(chunk, "sample_rate", rate) or rate)
            if buffer:
                return bytes(buffer), rate or self._config_rate(handle)

        legacy = getattr(handle, "synthesize_stream_raw", None)
        if callable(legacy):
            for data in legacy(text):
                buffer.extend(data)
            return bytes(buffer), self._config_rate(handle)

        raise RuntimeError(
            "Installed piper-tts exposes neither synthesize() nor "
            "synthesize_stream_raw(); cannot produce audio"
        )

    def _config_rate(self, handle) -> int:
        config = getattr(handle, "config", None)
        rate = getattr(config, "sample_rate", 0) if config is not None else 0
        return int(rate or self.sample_rate or FALLBACK_SAMPLE_RATE)

    async def close(self) -> None:
        # The voice stays in the process-wide cache on purpose: loading
        # it again costs seconds and it is read-only after load.
        return None
