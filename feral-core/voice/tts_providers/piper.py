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

macOS: the espeak wheel defect, measured
----------------------------------------
Synthesis has now actually been run, and the picture is
version-dependent. On this machine (Apple M1, macOS 15, CPython
3.11):

``piper-tts`` **1.4.2 works completely**. Full end-to-end run through
this provider with ``en_US-lessac-medium``: 630ms to synthesise 2.23s
of 22050Hz audio, and feeding that audio back through whisper.cpp
returned the input sentence verbatim.

``piper-tts`` **1.5.0 and 1.6.0 are broken on macOS arm64** and cannot
be repaired from outside the wheel::

    Error processing file '/Users/runner/work/piper1-gpl/piper1-gpl/
    _skbuild/macosx-11.0-arm64-3.9/cmake-build/espeak_ng-install/
    share/espeak-ng-data/phontab': No such file or directory.

The wheel bundles a complete ``espeak-ng-data`` directory and the
Python layer passes its path in, but ``espeakbridge.so`` has the build
machine's absolute path linked into it and uses that instead. Tested
and ruled out on 1.6.0: the ``espeak_data_dir=`` argument to
``PiperVoice.load``, a direct ``EspeakPhonemizer(<path>)``, a bare
``espeakbridge.initialize(<path>)``, and the ``ESPEAK_DATA_PATH``,
``ESPEAK_DATA_DIR`` and ``PIPER_ESPEAK_DATA`` environment variables.
All ten combinations fail identically.

**The failure is a process exit, not an exception.** espeak-ng calls
``exit(1)`` from native code, so the interpreter dies with status 1
and no traceback. ``try``/``except Exception`` cannot contain it. That
is why :func:`piper_available` probes in a subprocess on Darwin
(:func:`_probe_synthesis_out_of_process`) instead of calling
``synthesize`` and hoping: an in-process probe would take the whole
brain down at setup time.

None of this makes Piper the right choice on a Mac anyway. ``say`` is
tier 0 here, ships with the OS, needs no download and carries no
GPL-3.0 obligation. Piper on macOS is supported only in the narrow
sense that a working version exists and is pinned for.
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


#: How long the out-of-process synthesis probe may take. A cold ONNX
#: load plus one short sentence is well under this on any machine that
#: could run Piper at all; the timeout exists so a wedged native
#: library cannot hang ``feral setup`` forever.
PROBE_TIMEOUT_S = 60.0

_PROBE_SOURCE = """\
import sys
voice, config = sys.argv[1], sys.argv[2]
from piper import PiperVoice
v = PiperVoice.load(voice, config_path=config)
n = 0
for chunk in v.synthesize("Test."):
    data = getattr(chunk, "audio_int16_bytes", None)
    if data is None:
        data = chunk if isinstance(chunk, (bytes, bytearray)) else b""
    n += len(data)
if n <= 0:
    raise SystemExit("piper produced no audio")
print("PIPER_PROBE_OK", n)
"""


#: Probe results, keyed by voice name. The probe costs a process spawn
#: plus a cold ONNX load, and the answer cannot change while the
#: process is running (neither the installed wheel nor the weights move
#: underneath it). ``PiperTTSProvider.__init__`` calls
#: :func:`piper_available` on every session open, so without this the
#: probe would be paid per conversation.
_PROBE_CACHE: dict[str, tuple[bool, str]] = {}


def clear_piper_probe_cache() -> None:
    """Drop cached probe results. For tests and for post-install retry."""
    _PROBE_CACHE.clear()


def _probe_synthesis_out_of_process(voice: str) -> tuple[bool, str]:
    """Synthesise one word in a child process. ``(ok, reason)``.

    This has to be a subprocess. The macOS espeak failure is
    ``exit(1)`` raised from native code inside espeak-ng, not a Python
    exception, so an in-process probe does not return False, it kills
    the interpreter. Running ``feral setup`` would simply terminate.
    """
    import subprocess
    import sys

    cached = _PROBE_CACHE.get(voice)
    if cached is not None:
        return cached

    from voice import local_models

    weights, config = local_models.piper_voice_specs(voice)
    argv = [
        sys.executable,
        "-c",
        _PROBE_SOURCE,
        str(local_models.model_path(weights.family, weights.filename)),
        str(local_models.model_path(config.family, config.filename)),
    ]
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        # Cached: a wedged native library stays wedged, and paying the
        # timeout again on every session open is worse than the wrong
        # answer would be.
        outcome = (
            False,
            f"piper synthesis probe timed out after {PROBE_TIMEOUT_S:.0f}s",
        )
        _PROBE_CACHE[voice] = outcome
        return outcome
    except Exception as exc:  # pragma: no cover - spawn failure
        return False, f"piper synthesis probe could not run: {exc}"
    if result.returncode == 0 and "PIPER_PROBE_OK" in result.stdout:
        outcome = (True, "ready")
    else:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit status {result.returncode}"
        outcome = (False, f"piper cannot synthesise here: {tail}")
    _PROBE_CACHE[voice] = outcome
    return outcome


def piper_available(voice: str = DEFAULT_VOICE) -> tuple[bool, str]:
    """``(ready, reason)``. Import, weights, and a real synthesis probe.

    The probe matters: on macOS arm64 the package imports cleanly, the
    weights are present, ``PiperVoice.load`` succeeds, and synthesis
    still dies on an espeak data path baked into the wheel. Reporting
    "ready" on importability alone pushes that failure into the middle
    of a conversation.

    The previous version called :func:`_load_voice_blocking` and called
    that a synthesis probe. It is not: loading only reads the ONNX
    graph and the JSON config, and espeak is not touched until the
    first ``synthesize`` call. It therefore returned "ready" on exactly
    the platform where synthesis is fatal.
    """
    try:
        from piper import PiperVoice  # noqa: F401
    except Exception:
        return False, "piper-tts not installed (pip install 'feral-ai[tts-piper]')"
    from voice import local_models

    if not local_models.piper_voice_present(voice):
        return False, f"Piper voice {voice!r} not downloaded"
    return _probe_synthesis_out_of_process(voice)


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
