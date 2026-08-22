"""
FERAL Audio Pipeline — STT + TTS + VAD
=========================================
Handles the full audio lifecycle:
  Client mic → opus chunks → VAD → STT (Whisper) → text
  Text response → TTS (OpenAI) → mp3 chunks → Client speaker

Supports:
  - OpenAI Whisper API for STT
  - Local STT via faster-whisper (offline, no API key required)
  - OpenAI TTS API for speech synthesis
  - Local TTS via Piper (offline, no API key required)
  - Simple energy-based VAD for utterance boundary detection

Environment variables:
  FERAL_STT_PROVIDER  — "openai" (default) | "local" | "whisper-local" | "faster-whisper"
  FERAL_STT_MODEL     — Cloud: "whisper-1" / Local: "tiny" | "base" | "small" | "medium" | "large"
  FERAL_TTS_PROVIDER  — "openai" (default) | "local" | "piper"
  FERAL_TTS_VOICE     — Cloud: "nova" / Local: "en_US-lessac-medium"
"""

from __future__ import annotations
import array
import asyncio
import base64
import io
import logging
import math
import os
import sys
import time
import wave
from typing import Optional

import httpx

logger = logging.getLogger("feral.audio")

STT_PROVIDER_OPENAI = "openai"
STT_PROVIDER_LOCAL = "local"

TTS_PROVIDER_OPENAI = "openai"
TTS_PROVIDER_LOCAL = "local"

_LOCAL_STT_PROVIDERS = frozenset({"local", "whisper-local", "faster-whisper"})
_LOCAL_TTS_PROVIDERS = frozenset({"local", "piper"})

_VALID_LOCAL_STT_MODELS = ("tiny", "base", "small", "medium", "large")


def _cloud_fallback_permitted() -> bool:
    """May a failed LOCAL engine reroute this audio to OpenAI?

    Default no. ``voice/local_models.py`` states the rule as a hard one:
    "an operator who chose local engines for privacy must not be silently
    rerouted to a cloud provider". This module broke it in four places -
    both ``except ImportError`` and both ``except Exception`` arms of
    ``_transcribe_local`` and ``_synthesize_local`` called the cloud
    method and logged at ``warning``/``error``, so a privacy-motivated
    operator whose Piper voice was missing had their text posted to
    ``api.openai.com`` and their microphone audio with it. Piper through
    this pipeline never worked at all (see ``_LocalTTS._ensure_voice``),
    so that reroute was not an edge case, it was the only path.

    The behaviour is preserved, not deleted, behind an explicit opt-in:
    an operator who wants "local, but cloud rather than nothing" sets
    ``FERAL_LOCAL_AUDIO_CLOUD_FALLBACK=1`` and has therefore been told.
    Read at call time so it can be changed without a restart.
    """
    return os.getenv("FERAL_LOCAL_AUDIO_CLOUD_FALLBACK", "0").lower() in (
        "1", "true", "yes",
    )


# ---------------------------------------------------------------------------
#  Local STT backend (faster-whisper)
# ---------------------------------------------------------------------------

class _LocalSTT:
    """Lazy-loaded local speech-to-text via faster-whisper."""

    def __init__(self):
        self._model = None
        self._model_size: str = os.getenv("FERAL_STT_MODEL", "base")
        if self._model_size not in _VALID_LOCAL_STT_MODELS:
            logger.warning(
                "Unknown local STT model '%s', falling back to 'base'",
                self._model_size,
            )
            self._model_size = "base"

    def _ensure_model(self):
        """Load the model from FERAL's own store. Never downloads.

        This used to be ``WhisperModel(self._model_size,
        compute_type="int8")``, and the log line said "first call - may
        download". It did: with ``HF_HUB_OFFLINE=1`` on a machine that
        has never fetched the weights it raises
        ``LocalEntryNotFoundError``, and without it, it pulls ~145MB from
        HuggingFace in the middle of a voice turn. ``voice/local_models.py``
        exists specifically to forbid that ("a first-run download inside
        synthesize() turns one turn into a 90-second stall that looks
        identical to a hang").

        It also meant the two halves of the product disagreed:
        ``feral voice providers`` reported faster-whisper as "model not
        downloaded" while ``AudioPipeline.__init__`` logged "Audio
        pipeline ready - STT: local/faster-whisper". Both now read the
        same store, so they cannot disagree again.
        """
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise ImportError(
                "faster-whisper is not installed. "
                "Install with: pip install 'feral-ai[stt]'"
            )
        from voice.local_models import ensure_faster_whisper_model

        # allow_download=False: raises ModelUnavailable carrying the exact
        # fetch command, which _transcribe_local surfaces to the operator.
        path = ensure_faster_whisper_model(self._model_size, allow_download=False)
        logger.info("Loading local STT model %s from %s", self._model_size, path)
        self._model = WhisperModel(
            str(path), compute_type="int8", local_files_only=True
        )
        logger.info("Local STT model loaded: %s", self._model_size)

    def transcribe(self, audio_bytes: bytes, sample_rate: int = 16000) -> str:
        """Transcribe PCM16 mono audio bytes and return the text."""
        self._ensure_model()
        wav_bytes = _pcm16_to_wav(audio_bytes, sample_rate)
        segments, _info = self._model.transcribe(
            io.BytesIO(wav_bytes),
            language="en",
            beam_size=3,
            vad_filter=True,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()


# ---------------------------------------------------------------------------
#  Local TTS backend (Piper)
# ---------------------------------------------------------------------------

class _LocalTTS:
    """Lazy-loaded local text-to-speech via piper-tts."""

    def __init__(self):
        self._voice = None
        self._voice_name: str = os.getenv("FERAL_TTS_VOICE", "en_US-lessac-medium")

    def _ensure_voice(self):
        """Load the Piper voice from FERAL's own store.

        This used to be ``PiperVoice.load(self._voice_name)`` with a bare
        voice name. ``PiperVoice.load`` takes a *path*, so that call has
        never worked: it raises ``FileNotFoundError: [Errno 2] No such
        file or directory: 'en_US-lessac-medium.json'`` immediately, on
        every machine, with the voice installed or not. Local TTS through
        this pipeline has therefore never produced a single byte of
        audio, and the failure was caught and rerouted to OpenAI.

        Resolves both artefacts the way ``voice/tts_providers/piper.py``
        does, so the two Piper call sites cannot drift.
        """
        if self._voice is not None:
            return
        try:
            from piper import PiperVoice
        except ImportError:
            raise ImportError(
                "piper-tts is not installed. "
                "Install with: pip install 'feral-ai[tts]'"
            )
        from voice import local_models

        # Raises ModelUnavailable (with the fetch command) when absent.
        local_models.ensure_piper_voice(self._voice_name, allow_download=False)
        weights, config = local_models.piper_voice_specs(self._voice_name)
        model_path = local_models.model_path(weights.family, weights.filename)
        config_path = local_models.model_path(config.family, config.filename)
        logger.info("Loading local TTS voice %s from %s", self._voice_name, model_path)
        self._voice = PiperVoice.load(str(model_path), config_path=str(config_path))
        logger.info("Local TTS voice loaded: %s", self._voice_name)

    def synthesize(self, text: str) -> bytes:
        """Synthesize *text* to WAV audio bytes (PCM16, mono)."""
        self._ensure_voice()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            self._voice.synthesize(text, wf)
        return buf.getvalue()


# ---------------------------------------------------------------------------
#  Capability auto-detection
# ---------------------------------------------------------------------------

#: Voices `feral voice` knows how to fetch. Presence is checked, not assumed.
_KNOWN_PIPER_VOICES = ("en_US-lessac-medium", "en_US-amy-low", "en_GB-alan-medium")


def detect_local_audio_capabilities() -> dict:
    """Probe whether local audio backends can actually run.

    Returns a dict suitable for ``feral doctor`` reporting::

        {
            "local_stt": True/False,      # importable AND a model on disk
            "local_tts": True/False,      # importable AND a voice on disk
            "stt_models": [...],          # models you may CHOOSE
            "tts_voices": [...],          # voices you may CHOOSE
            "stt_models_present": [...],  # models actually on disk
            "tts_voices_present": [...],  # voices actually on disk
            "stt_importable": True/False,
            "tts_importable": True/False,
        }

    ``stt_models`` stays the selectable list, because
    ``/api/audio/providers/stt/faster-whisper/models`` is a picker: a
    fresh install has nothing downloaded, and a picker that lists only
    what is present would be empty with no way to choose something to
    fetch. ``*_present`` is the separate question doctor asks.

    ``local_stt`` used to be set by ``import faster_whisper`` alone, and
    ``stt_models`` returned the list of *valid model names* rather than
    the ones present. ``tts_voices`` was three hard-coded strings. So on
    a machine with the packages installed and no weights downloaded,
    which is what `pip install feral-ai[stt]` leaves behind, doctor
    reported local STT and TTS as available and listed models that were
    not on disk. The transcribe path then failed at first use with
    "model not downloaded", having been told twice that it would work.

    The two facts are kept separate rather than collapsed, because
    "install the package" and "fetch the weights" are different fixes
    and the operator needs to know which one they are missing.
    """
    result: dict = {
        "local_stt": False,
        "local_tts": False,
        "stt_models": [],
        "tts_voices": [],
        "stt_models_present": [],
        "tts_voices_present": [],
        "stt_importable": False,
        "tts_importable": False,
    }

    try:
        import faster_whisper  # noqa: F401
        result["stt_importable"] = True
        result["stt_models"] = list(_VALID_LOCAL_STT_MODELS)
    except ImportError:
        pass

    try:
        import piper  # noqa: F401
        result["tts_importable"] = True
        result["tts_voices"] = list(_KNOWN_PIPER_VOICES)
    except ImportError:
        pass

    # Presence is a filesystem question and must never make the probe
    # raise: doctor calls this to describe a broken machine.
    try:
        from voice.local_models import faster_whisper_model_present, piper_voice_present

        if result["stt_importable"]:
            result["stt_models_present"] = [
                m for m in _VALID_LOCAL_STT_MODELS if faster_whisper_model_present(m)
            ]
        if result["tts_importable"]:
            result["tts_voices_present"] = [
                v for v in _KNOWN_PIPER_VOICES if piper_voice_present(v)
            ]
    except Exception:
        logger.debug("local model presence probe failed", exc_info=True)

    result["local_stt"] = bool(result["stt_models_present"])
    result["local_tts"] = bool(result["tts_voices_present"])
    return result


# ---------------------------------------------------------------------------
#  Shared helpers
# ---------------------------------------------------------------------------

def _pcm16_to_wav(audio_bytes: bytes, sample_rate: int) -> bytes:
    """Wrap raw PCM16 mono audio in a WAV container."""
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(max(sample_rate, 8000))
        wav_file.writeframes(audio_bytes)
    return output.getvalue()


def _split_wav_into_playable_chunks(wav_bytes: bytes, chunk_bytes: int) -> list[bytes]:
    """Cut a WAV into pieces that each decode on their own.

    A WAV is a RIFF header followed by raw samples, so slicing the file
    at a byte offset produces one playable piece and a tail of headerless
    noise. Every consumer decodes a ``tts_chunk`` on its own
    (``RealtimeVoiceEngine.handleTtsChunk`` calls ``decodeAudioData``
    per frame), so the tail is not merely unplayable, it raises.

    Measured in Chrome on a 3 second Piper reply cut at 32KB:

        chunk 0  32768 bytes  ok, 0.742s
        chunk 1  32768 bytes  EncodingError
        chunk 2  32768 bytes  EncodingError
        chunk 3  32768 bytes  EncodingError
        chunk 4   1272 bytes  EncodingError

    The user heard 0.74 seconds of a 3 second answer and the client
    raised four playback errors. MP3 survived the same treatment, which
    is why only the local Piper path was affected and why it went
    unnoticed: cloud TTS returns MP3, whose frames are self-describing.

    Each piece is re-wrapped in its own header here, so the wire format
    and every existing client stay exactly as they are.
    """
    if not wav_bytes:
        return []
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as src:
            channels = src.getnchannels()
            width = src.getsampwidth()
            rate = src.getframerate()
            pcm = src.readframes(src.getnframes())
    except (wave.Error, EOFError):
        # Not a WAV we can parse. Hand it back whole rather than
        # shredding it: one oversized chunk still plays.
        logger.warning("TTS audio is not a readable WAV; sending it unsliced")
        return [wav_bytes]

    if channels != 1 or width != 2:
        # `_pcm16_to_wav` only writes mono 16-bit, so re-wrapping
        # anything else would mislabel the samples and play noise.
        logger.warning(
            "TTS WAV is %dch/%dbit, not mono 16-bit; sending it unsliced",
            channels, width * 8,
        )
        return [wav_bytes]

    frame = channels * width
    # Cut on a frame boundary, or a chunk starts half a sample in and
    # every following sample is byte-swapped into noise.
    step = max(frame, (chunk_bytes // frame) * frame)
    return [
        _pcm16_to_wav(pcm[i:i + step], rate)
        for i in range(0, len(pcm), step)
    ] or [_pcm16_to_wav(b"", rate)]


# ---------------------------------------------------------------------------
#  Main pipeline
# ---------------------------------------------------------------------------

class AudioPipeline:
    """
    Full-duplex audio pipeline for the FERAL brain.

    STT: Accumulates audio chunks, detects utterance boundaries (VAD),
         then transcribes via Whisper API or locally via faster-whisper.
    TTS: Converts text responses to audio chunks streamed back to client
         via OpenAI TTS or locally via Piper.
    """

    def __init__(self, wake_word_detector=None):
        self._api_key = os.getenv("OPENAI_API_KEY", "")
        self._stt_provider = os.getenv("FERAL_STT_PROVIDER", STT_PROVIDER_OPENAI).lower()
        self._tts_provider = os.getenv("FERAL_TTS_PROVIDER", TTS_PROVIDER_OPENAI).lower()
        self._tts_voice = os.getenv("FERAL_TTS_VOICE", "nova")
        self._tts_model = os.getenv("FERAL_TTS_MODEL", "tts-1")
        self._stt_model = os.getenv("FERAL_STT_MODEL", "whisper-1")

        self._client = httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=30.0,
        )

        self._buffers: dict[str, AudioBuffer] = {}
        self._wake_word = wake_word_detector

        # Lazily-initialised local backends (created on first use)
        self._local_stt: Optional[_LocalSTT] = None
        self._local_tts: Optional[_LocalTTS] = None

        # Determine availability
        self._use_local_stt = self._stt_provider in _LOCAL_STT_PROVIDERS
        self._use_local_tts = self._tts_provider in _LOCAL_TTS_PROVIDERS

        has_cloud = bool(self._api_key)
        has_local_stt = self._use_local_stt
        has_local_tts = self._use_local_tts
        self.available = has_cloud or has_local_stt or has_local_tts

        # "Ready" used to mean only "an env var selects this engine".
        # `feral voice providers` was meanwhile reporting the same engines
        # as "model not downloaded", from the same machine, at the same
        # time. Boot now reads the same store the CLI reads, so the two
        # cannot contradict each other, and a missing model is named at
        # boot rather than at the first voice turn.
        self.local_stt_ready, self.local_stt_detail = self._probe_local_stt()
        self.local_tts_ready, self.local_tts_detail = self._probe_local_tts()
        if has_local_stt and not self.local_stt_ready:
            logger.warning(
                "STT is set to a LOCAL engine but it cannot run: %s. Voice "
                "turns will produce no transcript until this is fixed.",
                self.local_stt_detail,
            )
        if has_local_tts and not self.local_tts_ready:
            logger.warning(
                "TTS is set to a LOCAL engine but it cannot run: %s. Voice "
                "replies will produce no audio until this is fixed.",
                self.local_tts_detail,
            )

        parts = []
        if has_local_stt:
            state = "ready" if self.local_stt_ready else f"NOT READY: {self.local_stt_detail}"
            parts.append(f"STT: local/faster-whisper ({self._stt_model}, {state})")
        elif has_cloud:
            parts.append(f"STT: openai/{self._stt_model}")
        if has_local_tts:
            state = "ready" if self.local_tts_ready else f"NOT READY: {self.local_tts_detail}"
            parts.append(f"TTS: local/piper ({self._tts_voice}, {state})")
        elif has_cloud:
            parts.append(f"TTS: openai/{self._tts_voice}")

        if self.available:
            logger.info("Audio pipeline configured — %s", ", ".join(parts))
        else:
            logger.warning("Audio pipeline unavailable — no OPENAI_API_KEY and no local backend configured")

    def _probe_local_stt(self) -> tuple[bool, str]:
        """Can the selected local STT engine run right now? Filesystem only."""
        if not self._use_local_stt:
            return False, "not selected"
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False, "faster-whisper not installed (pip install 'feral-ai[stt]')"
        try:
            from voice.local_models import (
                faster_whisper_model_present,
                faster_whisper_model_dir,
            )
        except Exception as exc:  # pragma: no cover - import-time only
            return False, f"local model store unavailable ({exc})"
        model = self._stt_model if self._stt_model in _VALID_LOCAL_STT_MODELS else "base"
        if not faster_whisper_model_present(model):
            return False, (
                f"faster-whisper model {model!r} not downloaded (expected at "
                f"{faster_whisper_model_dir(model)}); fetch it with "
                f"`python -m voice.local_models fetch-faster-whisper {model}`"
            )
        return True, "ready"

    def _probe_local_tts(self) -> tuple[bool, str]:
        """Can the selected local TTS voice run right now? Filesystem only."""
        if not self._use_local_tts:
            return False, "not selected"
        try:
            import piper  # noqa: F401
        except ImportError:
            return False, "piper-tts not installed (pip install 'feral-ai[tts]')"
        try:
            from voice.local_models import piper_voice_present
        except Exception as exc:  # pragma: no cover - import-time only
            return False, f"local model store unavailable ({exc})"
        if not piper_voice_present(self._tts_voice):
            return False, (
                f"Piper voice {self._tts_voice!r} not downloaded; fetch it "
                f"with `python -m voice.local_models fetch-piper "
                f"{self._tts_voice}`"
            )
        return True, "ready"

    # ── buffer management ──────────────────────────────────────────────

    def get_buffer(self, session_id: str) -> "AudioBuffer":
        if session_id not in self._buffers:
            self._buffers[session_id] = AudioBuffer(session_id)
        return self._buffers[session_id]

    # ── STT ────────────────────────────────────────────────────────────

    async def process_audio_chunk(
        self,
        session_id: str,
        chunk_b64: str,
        chunk_index: int,
        is_final: bool,
        encoding: str = "opus",
        sample_rate: int = 16000,
    ) -> Optional[str]:
        """
        Accumulate an audio chunk.  When *is_final* is ``True`` or the VAD
        detects an utterance boundary, transcribe the accumulated audio.

        The boundary is evaluated **before** the append, and that
        ordering is the whole point. The original code appended first::

            buf.append(chunk_bytes, encoding, sample_rate)
            if is_final or buf.vad_triggered():

        ``AudioBuffer.append`` stamps ``_last_chunk_time = time.time()``
        and ``vad_triggered`` returns ``time.time() - _last_chunk_time >
        1.5``, so the check always measured a gap of approximately zero.
        The silence-gap VAD could not fire, ever, from the only place
        that called it, and ``is_final`` was in practice the sole thing
        that ever flushed the buffer.

        A browser client sends ``is_final`` on its last chunk, so the web
        path hid this. A HUP ``audio_frame`` has no ``is_final`` field at
        all (HUP_SPEC.md §5.4.1) - a media frame is a 20ms slice, not an
        utterance boundary - so on the device path the buffer grew
        without bound and nothing was ever transcribed. Measured before
        this change: 10 chunks x 3000 bytes in, 30,100 bytes still
        resident, zero transcriptions.

        Read the new order as "did the previous utterance end before this
        chunk arrived?", which is what a silence gap actually means.
        """
        buf = self.get_buffer(session_id)
        chunk_bytes = base64.b64decode(chunk_b64)

        completed = buf.flush() if buf.vad_triggered() else b""

        buf.append(chunk_bytes, encoding, sample_rate)
        # The packet-gap check above only fires when a client stops
        # sending. Every real client streams continuously, so these two
        # are what actually end an utterance in practice: the audio went
        # quiet, or it has run long enough that waiting for a pause is no
        # longer reasonable.
        if is_final or buf.speech_ended():
            completed += buf.flush()
        elif buf.overflowing():
            # The ceiling exists so a speaker who never pauses still gets
            # transcribed. It must not turn an open mic in a quiet room
            # into a paid STT call every twelve seconds.
            #
            # `speech_ended` has always required voiced audio first, and
            # its docstring says why. `overflowing` did not, and the two
            # were ORed, so silence took the ceiling instead: measured on
            # 60s of digital silence, four calls of 387,200 bytes each
            # with zero non-zero samples. On the default provider that is
            # roughly 297 Whisper requests an hour, and Whisper
            # hallucinates words on silence ("Thank you."), which
            # voice/router feeds to the orchestrator as a user turn. The
            # brain answers a room that said nothing.
            #
            # Where we can see the samples and none is speech, drop it.
            # Where we cannot (compressed audio), the ceiling still
            # fires, because an unmeasurable buffer that grows forever is
            # the worse failure.
            if buf.is_measurably_silent():
                buf.discard()
            else:
                completed += buf.flush()

        if not completed or len(completed) < 1000:
            return None
        return await self._transcribe(completed, encoding, sample_rate)

    async def _transcribe(
        self,
        audio_bytes: bytes,
        encoding: str = "opus",
        sample_rate: int = 16000,
    ) -> Optional[str]:
        """Transcribe accumulated audio via local or cloud backend."""
        if not self.available:
            return None

        # ── Local STT path ──
        if self._use_local_stt:
            return await self._transcribe_local(audio_bytes, encoding, sample_rate)

        # ── Cloud (OpenAI Whisper API) path ──
        return await self._transcribe_cloud(audio_bytes, encoding, sample_rate)

    async def _transcribe_local(
        self,
        audio_bytes: bytes,
        encoding: str,
        sample_rate: int,
    ) -> Optional[str]:
        """Run STT locally via faster-whisper with graceful fallback."""
        try:
            if self._local_stt is None:
                self._local_stt = _LocalSTT()

            pcm = audio_bytes
            if encoding.lower() != "pcm16":
                logger.debug("Local STT received %s encoding; treating as raw PCM", encoding)

            loop = asyncio.get_running_loop()
            transcript = await loop.run_in_executor(
                None, self._local_stt.transcribe, pcm, sample_rate
            )
            if transcript:
                logger.info("Local STT transcript: %s", transcript[:100])
            return transcript or None
        except Exception as e:
            # One arm for every local failure. The three that used to be
            # separate (ImportError, ModelUnavailable, everything else)
            # all ended in the same silent cloud reroute; what differs is
            # only the remedy text, and ModelUnavailable already carries
            # its own.
            if not _cloud_fallback_permitted():
                logger.error(
                    "Local STT failed and FERAL_STT_PROVIDER selects a LOCAL "
                    "engine, so this audio was NOT sent to a cloud provider "
                    "and this turn produced no transcript: %s. Fix the local "
                    "engine, or set FERAL_LOCAL_AUDIO_CLOUD_FALLBACK=1 to "
                    "allow rerouting to OpenAI.", e,
                )
                return None
            logger.error(
                "Local STT failed: %s - rerouting this audio to OpenAI "
                "because FERAL_LOCAL_AUDIO_CLOUD_FALLBACK is set.", e,
            )
            if isinstance(e, ImportError):
                self._use_local_stt = False
            return await self._transcribe_cloud(audio_bytes, encoding, sample_rate)

    async def _transcribe_cloud(
        self,
        audio_bytes: bytes,
        encoding: str,
        sample_rate: int,
    ) -> Optional[str]:
        """Send accumulated audio to OpenAI Whisper API."""
        if not self._api_key:
            logger.error("Cloud STT unavailable — no OPENAI_API_KEY")
            return None

        encoding = (encoding or "opus").lower()
        ext_map = {"opus": "ogg", "wav": "wav", "mp3": "mp3", "webm": "webm", "ogg": "ogg", "pcm16": "wav"}
        ext = ext_map.get(encoding, "ogg")
        filename = f"audio.{ext}"
        payload_audio = audio_bytes
        if encoding == "pcm16":
            payload_audio = _pcm16_to_wav(audio_bytes, sample_rate=sample_rate)
        mime_type = "audio/wav" if ext == "wav" else f"audio/{ext}"

        try:
            files = {"file": (filename, io.BytesIO(payload_audio), mime_type)}
            data = {"model": self._stt_model, "response_format": "text"}

            resp = await self._client.post("/audio/transcriptions", files=files, data=data)
            resp.raise_for_status()
            transcript = resp.text.strip()
            logger.info("STT transcript: %s", transcript[:100])
            return transcript if transcript else None
        except Exception as e:
            logger.error("STT transcription failed: %s", e)
            return None

    # ── TTS ────────────────────────────────────────────────────────────

    async def synthesize_speech(
        self,
        text: str,
        voice: str = None,
    ) -> Optional[list[dict]]:
        """
        Convert text to speech audio chunks.
        Returns a list of chunk dicts: [{chunk_index, encoding, data_b64, is_final}]
        """
        if not self.available or not text.strip():
            return None

        if self._use_local_tts:
            return await self._synthesize_local(text)

        return await self._synthesize_cloud(text, voice)

    async def _synthesize_local(self, text: str) -> Optional[list[dict]]:
        """Run TTS locally via Piper with graceful fallback."""
        try:
            if self._local_tts is None:
                self._local_tts = _LocalTTS()

            loop = asyncio.get_running_loop()
            wav_bytes = await loop.run_in_executor(
                None, self._local_tts.synthesize, text[:4096]
            )

            # Each piece carries its own RIFF header, because the client
            # decodes every chunk independently. See
            # `_split_wav_into_playable_chunks`.
            segments = _split_wav_into_playable_chunks(wav_bytes, 32 * 1024)
            chunks = [
                {
                    "chunk_index": index,
                    "encoding": "wav",
                    "data_b64": base64.b64encode(segment).decode("ascii"),
                    "is_final": index == len(segments) - 1,
                }
                for index, segment in enumerate(segments)
            ]

            logger.info("Local TTS synthesized: %d bytes, %d chunks", len(wav_bytes), len(chunks))
            return chunks
        except Exception as e:
            # Same policy as _transcribe_local. This arm mattered more:
            # `PiperVoice.load(<bare name>)` raised FileNotFoundError on
            # every call, so "local TTS" silently meant "OpenAI TTS" for
            # every operator who selected it.
            if not _cloud_fallback_permitted():
                logger.error(
                    "Local TTS failed and FERAL_TTS_PROVIDER selects a LOCAL "
                    "engine, so this text was NOT sent to a cloud provider and "
                    "no audio was produced: %s. Fix the local engine, or set "
                    "FERAL_LOCAL_AUDIO_CLOUD_FALLBACK=1 to allow rerouting to "
                    "OpenAI.", e,
                )
                return None
            logger.error(
                "Local TTS failed: %s - rerouting this text to OpenAI because "
                "FERAL_LOCAL_AUDIO_CLOUD_FALLBACK is set.", e,
            )
            if isinstance(e, ImportError):
                self._use_local_tts = False
            return await self._synthesize_cloud(text, None)

    async def _synthesize_cloud(
        self,
        text: str,
        voice: str = None,
    ) -> Optional[list[dict]]:
        """Synthesize via OpenAI TTS API."""
        if not self._api_key:
            logger.error("Cloud TTS unavailable — no OPENAI_API_KEY")
            return None

        voice = voice or self._tts_voice

        try:
            resp = await self._client.post(
                "/audio/speech",
                json={
                    "model": self._tts_model,
                    "input": text[:4096],
                    "voice": voice,
                    "response_format": "mp3",
                },
            )
            resp.raise_for_status()
            audio_bytes = resp.content

            chunk_size = 32 * 1024
            chunks = []
            for i in range(0, len(audio_bytes), chunk_size):
                segment = audio_bytes[i:i + chunk_size]
                is_final = (i + chunk_size) >= len(audio_bytes)
                chunks.append({
                    "chunk_index": len(chunks),
                    "encoding": "mp3",
                    "data_b64": base64.b64encode(segment).decode("ascii"),
                    "is_final": is_final,
                })

            logger.info("TTS synthesized: %d bytes, %d chunks", len(audio_bytes), len(chunks))
            return chunks
        except Exception as e:
            logger.error("TTS synthesis failed: %s", e)
            return None

    # ── Wake word gating ───────────────────────────────────────────────

    async def process_audio_with_wake_word(
        self,
        session_id: str,
        chunk_b64: str,
        chunk_index: int,
        is_final: bool,
        encoding: str = "opus",
        sample_rate: int = 16000,
    ) -> Optional[str]:
        """
        Wake-word-gated variant of process_audio_chunk.
        Audio only flows to STT after the wake word is detected.
        """
        if not self._wake_word or not self._wake_word.enabled:
            return await self.process_audio_chunk(session_id, chunk_b64, chunk_index, is_final, encoding, sample_rate)

        pcm_bytes = base64.b64decode(chunk_b64)
        should_process = await self._wake_word.process_frame(session_id, pcm_bytes)

        if should_process:
            return await self.process_audio_chunk(session_id, chunk_b64, chunk_index, is_final, encoding, sample_rate)

        return None

    # ── Lifecycle ──────────────────────────────────────────────────────

    def clear_session(self, session_id: str):
        buf = self._buffers.pop(session_id, None)
        # A stream that stops mid-utterance leaves audio here that no
        # silence gap will ever arrive to flush, and teardown throws it
        # away. That is a real (and still unfixed) hole in the whisper
        # path: the last thing the user said before the socket dropped
        # is never transcribed. It was invisible; say it out loud so the
        # gap is attributable when it shows up as "FERAL ignored me".
        if buf is not None and buf.pending_bytes:
            logger.warning(
                "Discarding %d bytes of untranscribed audio for session %s "
                "on teardown - the stream ended mid-utterance and no "
                "silence gap flushed it.",
                buf.pending_bytes, session_id,
            )
        if self._wake_word:
            self._wake_word.cleanup_session(session_id)

    async def close(self):
        await self._client.aclose()


# ---------------------------------------------------------------------------
#  Audio buffer / VAD
# ---------------------------------------------------------------------------

# A speaker who never pauses still has to be transcribed. Beyond this
# much buffered audio the utterance is cut and sent regardless of what
# the VAD thinks, so a continuous stream always makes progress.
MAX_UTTERANCE_SEC = 12.0

# RMS below this (on PCM16, whose full scale is 32768) counts as silence.
# Room tone on a laptop mic sits well under 300; speech is thousands.
SILENCE_RMS = 300.0

# Silence this long ends an utterance. Shorter than the packet-gap
# threshold because this measures real silence, not a stalled network.
SILENCE_END_SEC = 0.8

# Below this much *voiced* audio there is nothing worth sending to STT.
MIN_VOICED_BYTES = 4000

# Fallback bytes-per-second for compressed encodings, so a duration
# ceiling still exists for them. Opus at a voice bitrate is roughly
# 4 KB/s; being wrong here only shifts when the backstop cuts, and the
# backstop is far better than never cutting at all.
_ASSUMED_COMPRESSED_BPS = 4000


def _is_pcm16(encoding: str) -> bool:
    return (encoding or "").lower() in ("pcm16", "pcm", "linear16")


def _chunk_duration_sec(chunk: bytes, encoding: str, sample_rate: int) -> float:
    """How much wall-clock audio a chunk holds.

    Exact for PCM16, where it is just bytes over (rate * 2 bytes per
    mono sample). Estimated for anything compressed, which is only used
    to decide when to cut an over-long utterance.
    """
    if not chunk:
        return 0.0
    if _is_pcm16(encoding):
        rate = int(sample_rate or 16000) or 16000
        return len(chunk) / float(rate * 2)
    return len(chunk) / float(_ASSUMED_COMPRESSED_BPS)


def _is_silent(chunk: bytes, encoding: str) -> bool:
    """True when a PCM16 chunk is quiet enough to count as silence.

    Compressed audio always answers False: its bytes are not samples, so
    any RMS computed over them is noise. Those encodings fall back to
    the packet-gap boundary and the duration ceiling.
    """
    if not _is_pcm16(encoding) or len(chunk) < 2:
        return False
    samples = array.array("h")
    # An odd trailing byte is not a whole sample; drop it rather than
    # letting frombytes raise on the length check.
    samples.frombytes(chunk[: len(chunk) - (len(chunk) % 2)])
    if not samples:
        return False
    if sys.byteorder == "big":
        samples.byteswap()
    total = 0
    for s in samples:
        total += s * s
    return math.sqrt(total / len(samples)) < SILENCE_RMS


class AudioBuffer:
    """Per-session audio chunk accumulator with energy-based VAD.

    Two independent boundaries end an utterance, because they catch two
    different clients:

    * a **packet gap**, for a device that stops sending when the user
      stops talking, and
    * **silence in the audio**, for a client that streams continuously.

    Only the first existed, and the docstring called it "energy-based"
    while no energy was ever computed. Every real client streams
    continuously at 100ms (BrowserNode, voiceRealtime and
    usePerceptionShare all send ``pcm16`` back to back), so
    ``_last_chunk_time`` was refreshed before the gap could ever elapse
    and the only thing that flushed the buffer was ``is_final``. A HUP
    ``audio_frame`` has no ``is_final`` field at all, so on the device
    path nothing was transcribed, ever. Measured before this change: 50
    chunks, 5.0s of audio, 0 calls to ``_transcribe``, 160,000 bytes
    still resident.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._chunks: list[bytes] = []
        self._encoding = "opus"
        self._sample_rate = 16000
        self._last_chunk_time = time.time()
        self._silence_threshold_sec = 1.5
        self._total_bytes = 0
        self._duration_sec = 0.0
        self._silent_run_sec = 0.0
        self._voiced_bytes = 0

    def append(self, chunk: bytes, encoding: str, sample_rate: int):
        self._chunks.append(chunk)
        self._encoding = encoding
        self._sample_rate = sample_rate
        self._last_chunk_time = time.time()
        self._total_bytes += len(chunk)

        secs = _chunk_duration_sec(chunk, encoding, sample_rate)
        self._duration_sec += secs
        if _is_silent(chunk, encoding):
            self._silent_run_sec += secs
        else:
            self._silent_run_sec = 0.0
            self._voiced_bytes += len(chunk)

    @property
    def duration_sec(self) -> float:
        """Seconds of audio held. Exact for PCM16, estimated otherwise."""
        return self._duration_sec

    @property
    def voiced_bytes(self) -> int:
        return self._voiced_bytes

    def speech_ended(self) -> bool:
        """True when the audio itself has gone quiet after real speech.

        Unlike :meth:`vad_triggered` this reads the samples, so it fires
        on a continuously streaming client. It deliberately requires
        voiced audio first, or a silent room would flush an empty buffer
        into STT every second.
        """
        return (
            self._silent_run_sec >= SILENCE_END_SEC
            and self._voiced_bytes >= MIN_VOICED_BYTES
        )

    def overflowing(self) -> bool:
        """True when the utterance has run long enough to cut it.

        The backstop for someone who does not pause, and for any encoding
        whose silence cannot be measured.
        """
        return self._duration_sec >= MAX_UTTERANCE_SEC

    def is_measurably_silent(self) -> bool:
        """True when we can see the samples AND none of them is speech.

        Only answerable for PCM16. Compressed bytes are not samples, so
        an Opus buffer is never "measurably" anything and the ceiling
        stays its only backstop.
        """
        return _is_pcm16(self._encoding) and self._voiced_bytes < MIN_VOICED_BYTES

    def discard(self) -> None:
        """Drop the buffer without handing it to anyone."""
        self.flush()

    @property
    def pending_bytes(self) -> int:
        """Accumulated, not-yet-transcribed audio in this buffer."""
        return self._total_bytes

    def vad_triggered(self) -> bool:
        """Simple VAD: silence gap detection + minimum buffer size.

        Must be evaluated *before* :meth:`append` for the current chunk.
        See :meth:`AudioPipeline.process_audio_chunk` for why.
        """
        if not self._chunks:
            return False
        elapsed = time.time() - self._last_chunk_time
        return elapsed > self._silence_threshold_sec and self._total_bytes > 2000

    def flush(self) -> bytes:
        """Return all accumulated audio and reset."""
        if not self._chunks:
            return b""
        audio = b"".join(self._chunks)
        self._chunks.clear()
        self._total_bytes = 0
        self._duration_sec = 0.0
        self._silent_run_sec = 0.0
        self._voiced_bytes = 0
        return audio
