"""
FERAL Wake Word Detector — "Hey FERAL"
==========================================
Local wake word detection gating audio flow to the Brain.
Uses openwakeword for ML-based detection with fallback to
energy-based keyword spotting.

States: LISTENING → ACTIVATED → TIMEOUT → LISTENING
"""

from __future__ import annotations
import asyncio
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Awaitable

logger = logging.getLogger("feral.wake_word")


class WakeState(str, Enum):
    LISTENING = "listening"
    ACTIVATED = "activated"
    TIMEOUT = "timeout"


@dataclass
class WakeWordEvent:
    timestamp: float
    confidence: float
    phrase: str
    pre_roll_b64: str = ""


@dataclass
class WakeWordConfig:
    enabled: bool = True
    phrase: str = "hey feral"
    sensitivity: float = 0.5
    timeout_seconds: float = 10.0
    pre_roll_ms: int = 500


class WakeWordDetector:
    """
    Streams raw PCM16 frames.  While in LISTENING state, only wake word
    detection runs (minimal CPU).  On detection, transitions to ACTIVATED
    and all subsequent audio flows through to the Brain until TIMEOUT.
    """

    def __init__(self, config: WakeWordConfig = None):
        self._config = config or WakeWordConfig(
            enabled=os.getenv("FERAL_WAKE_WORD", "false").lower() in ("true", "1", "yes"),
            phrase=os.getenv("FERAL_WAKE_PHRASE", "hey feral"),
            sensitivity=float(os.getenv("FERAL_WAKE_SENSITIVITY", "0.5")),
            timeout_seconds=float(os.getenv("FERAL_WAKE_TIMEOUT", "10")),
        )

        self._states: dict[str, WakeState] = {}
        self._activated_at: dict[str, float] = {}
        self._last_audio_at: dict[str, float] = {}
        self._pre_roll_buffer: dict[str, list[bytes]] = {}
        self._oww_model = None
        self._model_name: str = ""

        self._on_wake: Optional[Callable[[str, WakeWordEvent], Awaitable[None]]] = None

        if self._config.enabled:
            self._try_load_oww()

        logger.info(
            "WakeWordDetector: enabled=%s, detector=%s, configured phrase='%s', "
            "phrase actually detected='%s'",
            self._config.enabled,
            self.detector,
            self._config.phrase,
            self.effective_phrase,
        )
        self._report_phrase_mismatch()

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """Turn the detector on or off at runtime.

        ``enabled`` used to be read-only, and
        ``POST /api/ambient/wake_word/toggle`` assigns to it::

            state.wake_word.enabled = not state.wake_word.enabled

        against the real object that raises ``AttributeError: property
        'enabled' of 'WakeWordDetector' object has no setter``, so the
        route 500'd and the wake word could not be switched on from the
        API at all. (The route's test passed because its detector was a
        MagicMock, which accepts any assignment.)

        Enabling also loads the model. The detector is constructed
        disabled by default for privacy, and ``_try_load_oww`` only ran
        in ``__init__``, so a detector switched on later would have run
        the loudness fallback forever no matter what was installed.
        """
        value = bool(value)
        was = self._config.enabled
        self._config.enabled = value
        if value and not was and self._oww_model is None:
            self._try_load_oww()
            logger.info(
                "Wake word enabled at runtime: detector=%s, phrase actually "
                "detected='%s'", self.detector, self.effective_phrase,
            )
            self._report_phrase_mismatch()

    @property
    def phrase(self) -> str:
        """The configured wake phrase.

        Exposed because ``GET /api/ambient/wake_word/status`` reads
        ``getattr(state.wake_word, "phrase", "hey feral")``. There was no
        such attribute, so the getattr default won every time and the
        route reported "hey feral" whatever ``FERAL_WAKE_PHRASE`` said.
        """
        return self._config.phrase

    @property
    def detector(self) -> str:
        """Which detector is actually running: the honest name of it."""
        if not self._config.enabled:
            return "disabled"
        return "openwakeword" if self._oww_model is not None else "energy-fallback"

    @property
    def model_phrase(self) -> str:
        """The phrase the loaded openwakeword model was trained on.

        Derived from the model name, which is where the truth lives:
        ``hey_jarvis_v0.1`` detects "hey jarvis". Empty when no ML model
        is loaded.
        """
        if not self._model_name:
            return ""
        name = self._model_name
        # Strip a trailing ``_v<version>`` tag, then read the words.
        parts = name.split("_")
        while parts and parts[-1].startswith("v") and parts[-1][1:2].isdigit():
            parts.pop()
        return " ".join(parts).replace("-", " ").strip().lower()

    @property
    def effective_phrase(self) -> str:
        """What saying the phrase out loud will actually trigger.

        The energy fallback detects no phrase at all, so it reports the
        empty string rather than the configured one. Claiming a phrase
        that nothing is matching against is the defect this property
        exists to prevent.
        """
        if self.detector == "openwakeword":
            return self.model_phrase or self._config.phrase
        if self.detector == "energy-fallback":
            return ""
        return self._config.phrase

    def _report_phrase_mismatch(self) -> None:
        """Say so when the configured phrase is not what fires.

        ``FERAL_WAKE_MODEL`` defaults to openwakeword's pre-trained
        ``hey_jarvis_v0.1``, while the configured phrase defaults to "hey
        feral". No FERAL-branded wake model is shipped or referenced
        anywhere in this repo, so out of the box the product told the
        user to say a phrase that could never trigger, and a user who
        said it and got nothing had no way to find out why.
        """
        if not self._config.enabled:
            return
        if self.detector == "energy-fallback":
            logger.warning(
                "Wake word is ENABLED but openwakeword is not loaded, so "
                "detection is the energy fallback: it opens the microphone "
                "on any sufficiently LOUD audio, speech or not, and the "
                "phrase '%s' is not matched against anything. Install the "
                "detector with: pip install 'feral-ai[wake]'",
                self._config.phrase,
            )
            return
        detected = self.model_phrase
        if detected and detected != self._config.phrase.strip().lower():
            logger.warning(
                "Wake word phrase mismatch: FERAL reports '%s' but the loaded "
                "model %r detects '%s'. Saying '%s' will NOT wake FERAL. Set "
                "FERAL_WAKE_PHRASE='%s', or point FERAL_WAKE_MODEL at a model "
                "trained on '%s'.",
                self._config.phrase, self._model_name, detected,
                self._config.phrase, detected, self._config.phrase,
            )

    def _try_load_oww(self):
        """Attempt to load openwakeword; graceful fallback to energy-based."""
        try:
            import openwakeword
            from openwakeword.model import Model as OWWModel

            model_name = os.getenv("FERAL_WAKE_MODEL", "hey_jarvis_v0.1")

            try:
                openwakeword.utils.download_models([model_name])
            except Exception as exc:
                # Was ``except Exception: pass``. openwakeword ships its
                # pre-trained models inside the wheel, so this normally
                # fails only on a network or checksum problem for a model
                # that is not bundled - in which case the constructor
                # below raises and we drop to the loudness gate. Silently
                # was the wrong way to do that.
                logger.warning(
                    "openwakeword model fetch for %r failed: %s. Continuing "
                    "with whatever is already on disk.", model_name, exc,
                )

            self._oww_model = OWWModel(
                wakeword_models=[model_name],
                inference_framework="onnx",
            )
            self._model_name = model_name
            logger.info(f"openwakeword loaded (model={model_name}) — ML-based wake word detection active")
        except ImportError:
            logger.info(
                "openwakeword not installed — using energy-based fallback. "
                "Install with: pip install openwakeword onnxruntime"
            )
        except Exception as e:
            logger.warning(f"openwakeword init failed: {e} — using energy-based fallback")

    def set_on_wake(self, callback: Callable[[str, WakeWordEvent], Awaitable[None]]):
        self._on_wake = callback

    def get_state(self, session_id: str) -> WakeState:
        self._check_timeout(session_id)
        return self._states.get(session_id, WakeState.LISTENING)

    def force_activate(self, session_id: str):
        """Manually activate (e.g. button press)."""
        self._states[session_id] = WakeState.ACTIVATED
        self._activated_at[session_id] = time.time()
        self._last_audio_at[session_id] = time.time()

    def force_deactivate(self, session_id: str):
        self._states[session_id] = WakeState.LISTENING

    async def process_frame(self, session_id: str, pcm16_bytes: bytes) -> bool:
        """
        Process a PCM16 audio frame.
        Returns True if the audio should flow through to the Brain.
        """
        if not self._config.enabled:
            return True

        self._check_timeout(session_id)
        state = self._states.get(session_id, WakeState.LISTENING)

        if state == WakeState.ACTIVATED:
            self._last_audio_at[session_id] = time.time()
            return True

        # Maintain pre-roll buffer (last 500ms of audio at 24kHz PCM16 = ~24000 bytes)
        pre_roll = self._pre_roll_buffer.setdefault(session_id, [])
        pre_roll.append(pcm16_bytes)
        max_pre_roll_chunks = max(1, (self._config.pre_roll_ms * 24000 * 2) // (len(pcm16_bytes) * 1000)) if pcm16_bytes else 10
        while len(pre_roll) > max_pre_roll_chunks:
            pre_roll.pop(0)

        detected = False
        confidence = 0.0

        if self._oww_model is not None:
            detected, confidence = self._detect_oww(pcm16_bytes)
        else:
            detected, confidence = self._detect_energy(pcm16_bytes)

        if detected and confidence >= self._config.sensitivity:
            import base64
            pre_roll_audio = b"".join(pre_roll)
            event = WakeWordEvent(
                timestamp=time.time(),
                confidence=confidence,
                phrase=self._config.phrase,
                pre_roll_b64=base64.b64encode(pre_roll_audio).decode("ascii"),
            )

            self._states[session_id] = WakeState.ACTIVATED
            self._activated_at[session_id] = time.time()
            self._last_audio_at[session_id] = time.time()
            self._pre_roll_buffer.pop(session_id, None)

            logger.info(f"Wake word detected for {session_id[:8]} (confidence={confidence:.2f})")

            if self._on_wake:
                await self._on_wake(session_id, event)

            return True

        return False

    def _detect_oww(self, pcm16_bytes: bytes) -> tuple[bool, float]:
        """Use openwakeword ML model for detection."""
        try:
            import numpy as np
            audio_array = np.frombuffer(pcm16_bytes, dtype=np.int16)
            predictions = self._oww_model.predict(audio_array)
            for model_name, score in predictions.items():
                if score > self._config.sensitivity:
                    return True, float(score)
            return False, 0.0
        except Exception as e:
            logger.debug(f"OWW detection error: {e}")
            return False, 0.0

    def _detect_energy(self, pcm16_bytes: bytes) -> tuple[bool, float]:
        """
        Simple energy-based detection — not a true wake word detector,
        but detects loud audio that could be the wake phrase.
        This is a placeholder; real deployment uses openwakeword.
        """
        if len(pcm16_bytes) < 4:
            return False, 0.0

        n_samples = len(pcm16_bytes) // 2
        total_energy = 0.0
        for i in range(0, n_samples * 2, 2):
            sample = struct.unpack_from("<h", pcm16_bytes, i)[0]
            total_energy += abs(sample)

        avg_energy = total_energy / n_samples if n_samples > 0 else 0
        normalized = min(avg_energy / 3000.0, 1.0)

        return normalized > 0.7, normalized

    def _check_timeout(self, session_id: str):
        state = self._states.get(session_id)
        if state != WakeState.ACTIVATED:
            return

        last_audio = self._last_audio_at.get(session_id, 0)
        if time.time() - last_audio > self._config.timeout_seconds:
            self._states[session_id] = WakeState.LISTENING
            logger.info(f"Wake word timeout for {session_id[:8]} — returning to LISTENING")

    def cleanup_session(self, session_id: str):
        self._states.pop(session_id, None)
        self._activated_at.pop(session_id, None)
        self._last_audio_at.pop(session_id, None)
        self._pre_roll_buffer.pop(session_id, None)

    @property
    def stats(self) -> dict:
        detected = self.effective_phrase
        return {
            "enabled": self._config.enabled,
            "phrase": self._config.phrase,
            "active_sessions": sum(1 for s in self._states.values() if s == WakeState.ACTIVATED),
            "using_ml": self._oww_model is not None,
            # ``phrase`` alone was a claim FERAL could not keep: the
            # default model detects "hey jarvis" while the default phrase
            # says "hey feral", and the energy fallback matches no phrase
            # at all. These three report what is really happening.
            "detector": self.detector,
            "model": self._model_name,
            "effective_phrase": detected,
            "phrase_matches_model": (
                detected == self._config.phrase.strip().lower()
            ),
        }
