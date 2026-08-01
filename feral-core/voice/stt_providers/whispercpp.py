"""whisper.cpp STT (buffered, fully local).

The default local STT on macOS, and the only member of the Whisper
family with real Apple Silicon GPU acceleration. ``pywhispercpp`` is
MIT and ships prebuilt wheels for macOS arm64 and manylinux, so it
installs without a toolchain.

Measured on an Apple M1 (tiny.en, 2.16s of 16kHz speech, this
provider's own resampling path):

    model load        17.7s cold  (9.2s of it is Metal library init)
    transcribe run 1   0.602s     (first call, graph warm-up)
    transcribe run 2   0.101s
    transcribe run 3   0.096s

whisper.cpp reported ``use gpu = 1`` and bound ``MTL0 / Apple M1``.

Two things follow from those numbers and both are implemented here:

* **The model is loaded once and cached process-wide.** 17.7s inside a
  voice turn is indistinguishable from a hang. The cache is keyed by
  (model, directory) so a second session pays nothing.
* **Warm-up is kicked off at session open**, from ``open_stream``,
  rather than at first ``flush``. The user is still finishing their
  first sentence while the graph compiles.

Why not faster-whisper here: CTranslate2 has no Metal backend, and
``device="mps"`` raises rather than falling back. On a Mac it would run
on CPU while pretending to be the fast option. It stays available as
:mod:`voice.stt_providers.faster_whisper_local` for Linux and NVIDIA.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from voice.stt_providers import (
    STTProvider,
    TranscriptFragment,
    register_stt_provider,
)

logger = logging.getLogger("feral.voice.stt.whispercpp")

DEFAULT_MODEL = "base.en"
WHISPER_SAMPLE_RATE = 16000

# Process-wide model cache. whisper.cpp models are read-only after
# load and ``transcribe`` is called under a lock, so sharing one
# across sessions is safe and saves the whole load cost.
_MODEL_CACHE: dict[tuple[str, str], Any] = {}
_CACHE_LOCK = asyncio.Lock()
# whisper.cpp is not re-entrant per context. One decode at a time.
_DECODE_LOCK = asyncio.Lock()


def whispercpp_available(model: str = DEFAULT_MODEL) -> tuple[bool, str]:
    """``(ready, reason)``. Importability plus weights, never a key."""
    try:
        import pywhispercpp  # noqa: F401
    except Exception:
        return False, "pywhispercpp not installed (pip install 'feral-ai[stt-local]')"
    from voice import local_models

    if not local_models.whispercpp_model_present(model):
        return False, f"whisper.cpp model {model!r} not downloaded"
    return True, "ready"


@register_stt_provider("whispercpp")
class WhisperCppSTTProvider(STTProvider):
    """Buffered local STT via whisper.cpp.

    Same shape as :class:`~voice.stt_providers.openai_whisper.OpenAIWhisperSTTProvider`:
    audio accumulates until ``flush()``, then one transcript fragment
    comes out. The difference is that nothing leaves the machine.
    """

    #: Advertised so the router and the setup wizard can tell local
    #: engines from cloud ones without a name lookup table.
    is_local = True

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        language: str = "en",
        sample_rate: int = 24000,
        n_threads: int = 0,
        allow_download: bool = False,
    ):
        # ``api_key`` is accepted and ignored on purpose. The router
        # passes ``api_key=`` to every STT constructor
        # (``voice/router.py``), so a local provider that rejected the
        # kwarg would be unconstructible through the normal path. It is
        # never read, never logged and never sent anywhere.
        del api_key

        self._model_name = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self._language = language or "en"
        self._sample_rate = int(sample_rate or 24000)
        self._n_threads = int(n_threads or 0)
        self._allow_download = bool(allow_download)

        # Fail at construction, not mid-turn. An operator who selected
        # a local engine for privacy must be told it is unusable before
        # the session opens, never quietly rerouted to a cloud vendor.
        ready, reason = whispercpp_available(self._model_name)
        if not ready and not self._allow_download:
            raise RuntimeError(
                f"Local whisper.cpp STT is unavailable: {reason}. "
                "Run `feral setup` and choose local voice to install it. "
                "FERAL will not silently fall back to a cloud provider."
            )

        self._buffer = bytearray()
        self._result_queue: asyncio.Queue[TranscriptFragment | None] = asyncio.Queue()
        self._closed = False
        self._warmup: asyncio.Task | None = None

    # -- model ------------------------------------------------------

    def _cache_key(self) -> tuple[str, str]:
        from voice import local_models

        return (self._model_name, str(local_models.whispercpp_model_dir()))

    def _load_model_blocking(self):
        from pywhispercpp.model import Model

        from voice import local_models

        return Model(
            model=self._model_name,
            models_dir=str(local_models.whispercpp_model_dir()),
            # whisper.cpp writes its init banner straight to stderr,
            # which lands in the brain's log as a wall of unattributed
            # ggml output on every boot.
            redirect_whispercpp_logs_to=False,
            print_progress=False,
            print_realtime=False,
        )

    async def _ensure_model(self):
        key = self._cache_key()
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached
        async with _CACHE_LOCK:
            cached = _MODEL_CACHE.get(key)
            if cached is not None:
                return cached
            loop = asyncio.get_running_loop()
            model = await loop.run_in_executor(None, self._load_model_blocking)
            _MODEL_CACHE[key] = model
            logger.info(
                "whisper.cpp model %r loaded (%s)", self._model_name, key[1]
            )
            return model

    # -- STTProvider ------------------------------------------------

    async def open_stream(self) -> AsyncIterator[TranscriptFragment]:
        """Yield transcript fragments (one final fragment per flush).

        Also the warm-up hook. This is called once at session open, so
        it is the earliest point at which the 17s cold load can be
        started without a user waiting on it.
        """
        if self._warmup is None:
            self._warmup = asyncio.create_task(self._warm())
        try:
            while True:
                fragment = await self._result_queue.get()
                if fragment is None:
                    break
                yield fragment
        finally:
            self._closed = True

    async def _warm(self) -> None:
        try:
            await self._ensure_model()
        except Exception:
            # A failed warm-up is not fatal here: ``flush`` retries and
            # raises there, where the pipeline turns it into a visible
            # error state rather than a silent dead session.
            logger.warning(
                "whisper.cpp warm-up failed for model %r", self._model_name,
                exc_info=True,
            )

    async def send_audio(self, audio_bytes: bytes) -> None:
        if not self._closed:
            self._buffer.extend(audio_bytes)

    async def flush(self) -> None:
        if not self._buffer:
            return
        pcm = bytes(self._buffer)
        self._buffer.clear()

        model = await self._ensure_model()
        samples = self._to_float32_16k(pcm)
        if samples.size < WHISPER_SAMPLE_RATE // 20:
            # Under 50ms. whisper pads to 30s regardless, so decoding a
            # click costs a full inference and returns a hallucination.
            return

        loop = asyncio.get_running_loop()
        async with _DECODE_LOCK:
            segments = await loop.run_in_executor(
                None, self._transcribe_blocking, model, samples
            )
        text = " ".join((seg.text or "").strip() for seg in segments).strip()
        if text:
            await self._result_queue.put(
                TranscriptFragment(
                    text=text,
                    is_partial=False,
                    is_final=True,
                    speech_final=True,
                )
            )

    def _transcribe_blocking(self, model, samples):
        params: dict[str, Any] = {"print_progress": False}
        if self._n_threads > 0:
            params["n_threads"] = self._n_threads
        # ``.en`` models are single-language and reject a language
        # parameter; the multilingual ones need it or they guess.
        if not self._model_name.endswith(".en"):
            params["language"] = self._language
        return model.transcribe(samples, **params)

    def _to_float32_16k(self, pcm: bytes):
        """PCM16 at the session rate to float32 mono at 16kHz.

        whisper.cpp only accepts 16kHz. Handing it 24kHz samples makes
        every word land 1.5x too fast, which is the same class of bug
        that produced the live "weird words then degraded" report on
        the Deepgram path.
        """
        import numpy as np

        usable = len(pcm) - (len(pcm) % 2)
        audio = np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32) / 32768.0
        if self._sample_rate == WHISPER_SAMPLE_RATE or audio.size < 2:
            return audio
        count = int(audio.size * WHISPER_SAMPLE_RATE / self._sample_rate)
        if count < 2:
            return np.zeros(0, dtype=np.float32)
        positions = np.arange(count, dtype=np.float64) * (
            self._sample_rate / WHISPER_SAMPLE_RATE
        )
        return np.interp(
            positions, np.arange(audio.size, dtype=np.float64), audio
        ).astype(np.float32)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._warmup is not None and not self._warmup.done():
            self._warmup.cancel()
        try:
            await self.flush()
        finally:
            await self._result_queue.put(None)
