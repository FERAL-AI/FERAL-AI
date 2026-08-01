"""faster-whisper STT (buffered, fully local). Linux and NVIDIA.

The alternate local STT. On Linux, and anywhere with a CUDA GPU, this
is the faster engine: CTranslate2's int8 kernels beat whisper.cpp's
CPU path comfortably.

On macOS it is the wrong choice and this provider says so. CTranslate2
has no Metal backend. ``device="mps"`` is not a supported value and
raises; ``device="auto"`` silently resolves to CPU, which is how a Mac
ends up running the "fast" engine at a fraction of whisper.cpp's Metal
throughput while every log line claims local acceleration.
:func:`faster_whisper_available` therefore reports a warning on Darwin
and the setup wizard defaults Macs to
:mod:`voice.stt_providers.whispercpp`.

The engine was already in the tree, wired only to the legacy
``perception/audio_pipeline.py``. This exposes it to the chained
pipeline, which is the path that actually runs.

Measured, through this provider class
-------------------------------------
faster-whisper 1.2.1, tiny.en int8, Apple M1, 4.87s of macOS ``say``
speech at 24kHz pushed through ``send_audio``/``flush``:

    model load        0.15s
    flush run 1       0.285s   (0.059x realtime)
    flush run 2       0.247s
    flush run 3       0.241s

whisper.cpp on the same machine, same audio, same model size: 0.110s
warm. That is the CPU-versus-Metal gap the paragraph above predicts,
and it is why macOS defaults to whispercpp.

Weights are 78MB on disk for tiny.en and are fetched by
:func:`voice.local_models.ensure_faster_whisper_model` in ``feral
setup``, never here.
"""

from __future__ import annotations

import asyncio
import logging
import platform
from typing import Any, AsyncIterator

from voice.stt_providers import (
    STTProvider,
    TranscriptFragment,
    register_stt_provider,
)

logger = logging.getLogger("feral.voice.stt.faster_whisper")

DEFAULT_MODEL = "base.en"
WHISPER_SAMPLE_RATE = 16000

#: Silence prepended to every decode, in milliseconds. Whisper eats the
#: first word of audio that starts at sample zero, and VAD endpointing
#: guarantees exactly that shape.
LEAD_PAD_MS = 100

_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}
_CACHE_LOCK = asyncio.Lock()
_DECODE_LOCK = asyncio.Lock()


def faster_whisper_available(model: str = DEFAULT_MODEL) -> tuple[bool, str]:
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False, "faster-whisper not installed (pip install 'feral-ai[stt]')"
    from voice import local_models

    if not local_models.faster_whisper_model_present(model):
        return False, f"faster-whisper model {model!r} not downloaded"
    if platform.system() == "Darwin":
        return True, "ready (CPU only on macOS - whispercpp is faster here)"
    return True, "ready"


@register_stt_provider("faster_whisper")
class FasterWhisperSTTProvider(STTProvider):
    """Buffered local STT via faster-whisper / CTranslate2."""

    is_local = True

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        language: str = "en",
        sample_rate: int = 24000,
        device: str = "auto",
        compute_type: str = "int8",
        allow_download: bool = False,
    ):
        # Accepted and ignored - the router passes ``api_key=`` to
        # every STT constructor. Never read, never sent anywhere.
        del api_key

        if device == "mps":
            # Refuse rather than let CTranslate2 raise a confusing
            # "unsupported device" three layers down, and rather than
            # quietly demote to CPU while the operator believes the GPU
            # is in play.
            raise RuntimeError(
                "faster-whisper cannot use Metal: CTranslate2 has no MPS "
                "backend. Use the 'whispercpp' STT provider on macOS."
            )

        self._model_name = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self._language = language or "en"
        self._sample_rate = int(sample_rate or 24000)
        self._device = device or "auto"
        self._compute_type = compute_type or "int8"
        self._allow_download = bool(allow_download)

        ready, reason = faster_whisper_available(self._model_name)
        if not ready and not self._allow_download:
            raise RuntimeError(
                f"Local faster-whisper STT is unavailable: {reason}. "
                "Run `feral setup` and choose local voice to install it. "
                "FERAL will not silently fall back to a cloud provider."
            )

        self._buffer = bytearray()
        self._result_queue: asyncio.Queue[TranscriptFragment | None] = asyncio.Queue()
        self._closed = False
        self._warmup: asyncio.Task | None = None

    def _cache_key(self) -> tuple[str, str, str]:
        return (self._model_name, self._device, self._compute_type)

    def _load_model_blocking(self):
        from faster_whisper import WhisperModel

        from voice import local_models

        # Resolve to a directory on disk and pass local_files_only.
        #
        # The obvious spelling, ``WhisperModel(size, download_root=X)``,
        # is a trap: download_root is forwarded as *cache_dir*, so
        # constructing the model reaches out to HuggingFace and pulls
        # the weights inside whatever call happens to be first. In
        # chained mode that is a voice turn, which is the exact
        # mid-session download voice/local_models.py exists to forbid.
        # Naming the directory and refusing the network makes a missing
        # model an error at open time instead of a stall mid-sentence.
        path = local_models.faster_whisper_model_path(self._model_name)
        return WhisperModel(
            str(path),
            device=self._device,
            compute_type=self._compute_type,
            local_files_only=True,
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
            logger.info("faster-whisper model %r loaded", self._model_name)
            return model

    async def open_stream(self) -> AsyncIterator[TranscriptFragment]:
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
            logger.warning(
                "faster-whisper warm-up failed for model %r", self._model_name,
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
            return
        # Same first-word loss as whisper.cpp, same fix. See
        # ``voice/stt_providers/whispercpp.py``. Applied after the
        # length check so a click stays too short to decode.
        samples = self._pad_lead(samples)

        loop = asyncio.get_running_loop()
        async with _DECODE_LOCK:
            text = await loop.run_in_executor(
                None, self._transcribe_blocking, model, samples
            )
        if text:
            await self._result_queue.put(
                TranscriptFragment(
                    text=text,
                    is_partial=False,
                    is_final=True,
                    speech_final=True,
                )
            )

    def _transcribe_blocking(self, model, samples) -> str:
        kwargs: dict[str, Any] = {"beam_size": 3}
        if not self._model_name.endswith(".en"):
            kwargs["language"] = self._language
        segments, _info = model.transcribe(samples, **kwargs)
        return " ".join((seg.text or "").strip() for seg in segments).strip()

    def _pad_lead(self, samples):
        if LEAD_PAD_MS <= 0 or samples.size == 0:
            return samples
        import numpy as np

        pad = np.zeros(
            int(WHISPER_SAMPLE_RATE * LEAD_PAD_MS / 1000), dtype=samples.dtype
        )
        return np.concatenate([pad, samples])

    def _to_float32_16k(self, pcm: bytes):
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
