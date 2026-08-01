"""Server-side voice activity detection for the chained pipeline.

Why this exists
---------------
Before this module the chained pipeline decided an utterance had ended
by watching for the *absence of packets*. That needed the browser to
stop sending, which it only did after its own energy gate had counted
15 quiet 100ms frames (``feral-client-v2/src/lib/voiceRealtime.js``),
and then the server waited another ``SILENCE_FLUSH_SECONDS`` on top.
Two independent silence timers stacked in series, roughly 2.3s of dead
air before the STT request was even issued, on every single turn.

Neither timer could be shortened on its own. Drop the client gate and
the server's timer never fires, because every arriving frame pushes
``_last_audio_ts`` forward, silence or not: a continuously streaming
client would be listened to forever. Drop the server timer and
buffered providers never flush. The pair only comes apart if the
server can tell speech from silence in the bytes themselves.

Silero VAD does that. MIT licensed, about 2.2MB of ONNX, and it runs
in well under a millisecond per frame on CPU, so it can sit in the
audio hot path without a thread pool or a torch dependency.

Degradation
-----------
:func:`load_endpointer` returns ``None`` when onnxruntime is missing or
the weights have not been downloaded. The pipeline treats ``None`` as
"fall back to the packet-absence timer" and keeps working exactly as
it did. VAD is a latency optimisation, never a hard dependency, so a
machine that cannot run it must still hold a conversation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("feral.voice.vad")

# Silero v5 is fixed-frame: 512 samples at 16kHz, 256 at 8kHz. Feeding
# it anything else produces garbage probabilities rather than an error,
# so the endpointer below reframes the inbound stream itself.
VAD_SAMPLE_RATE = 16000
VAD_FRAME_SAMPLES = 512
VAD_FRAME_MS = VAD_FRAME_SAMPLES * 1000 // VAD_SAMPLE_RATE  # 32ms

# Speech is declared above ``threshold`` and retracted below
# ``neg_threshold``. The gap is hysteresis: a single frame dipping
# under 0.5 mid-word must not read as end-of-utterance.
DEFAULT_THRESHOLD = 0.5
DEFAULT_NEG_THRESHOLD = 0.35

# How much continuous silence ends the utterance. 300ms sits inside
# the 250-400ms target: short enough that the reply feels immediate,
# long enough to ride out the pause between "move my" and "two
# o'clock". Below about 200ms normal inter-word pauses start cutting
# people off mid-sentence.
DEFAULT_MIN_SILENCE_MS = 300

# Ignore blips shorter than this so a cough or a door does not open an
# utterance that then has to be endpointed and transcribed to nothing.
DEFAULT_MIN_SPEECH_MS = 96  # 3 frames


class VadEvent(str, Enum):
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"


@dataclass
class VadConfig:
    threshold: float = DEFAULT_THRESHOLD
    neg_threshold: float = DEFAULT_NEG_THRESHOLD
    min_silence_ms: int = DEFAULT_MIN_SILENCE_MS
    min_speech_ms: int = DEFAULT_MIN_SPEECH_MS

    @classmethod
    def from_settings(cls, block: dict | None) -> "VadConfig":
        block = block or {}

        def _num(key: str, default, cast):
            value = block.get(key, default)
            try:
                return cast(value)
            except (TypeError, ValueError):
                return default

        return cls(
            threshold=_num("threshold", DEFAULT_THRESHOLD, float),
            neg_threshold=_num("neg_threshold", DEFAULT_NEG_THRESHOLD, float),
            min_silence_ms=_num("min_silence_ms", DEFAULT_MIN_SILENCE_MS, int),
            min_speech_ms=_num("min_speech_ms", DEFAULT_MIN_SPEECH_MS, int),
        )


class SileroVAD:
    """Thin ONNX wrapper. One instance per session: it carries state.

    Silero is recurrent. The hidden state threaded between calls is
    what lets it tell a plosive from the start of a word, so two
    sessions must never share an instance.
    """

    def __init__(self, model_path: str | Path, *, providers: list[str] | None = None):
        import numpy as np
        import onnxruntime as ort

        self._np = np
        options = ort.SessionOptions()
        # One thread. This runs per 32ms frame inside the event loop's
        # thread; spinning up an intra-op pool costs more in scheduling
        # than the convolution saves.
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.log_severity_level = 3
        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=providers or ["CPUExecutionProvider"],
        )
        input_names = {i.name for i in self._session.get_inputs()}
        # v5 fused the two LSTM tensors into one ``state``; v4 exports
        # still take ``h`` and ``c``. Support both so an operator with
        # an older cached file is not silently mis-inferred.
        self._v5 = "state" in input_names
        self._has_sr = "sr" in input_names
        self.reset()

    def reset(self) -> None:
        np = self._np
        if self._v5:
            self._state = np.zeros((2, 1, 128), dtype=np.float32)
        else:
            self._h = np.zeros((2, 1, 64), dtype=np.float32)
            self._c = np.zeros((2, 1, 64), dtype=np.float32)

    def probability(self, frame: Any) -> float:
        """Speech probability for exactly one ``VAD_FRAME_SAMPLES`` frame."""
        np = self._np
        audio = np.asarray(frame, dtype=np.float32).reshape(1, -1)
        feeds: dict[str, Any] = {"input": audio}
        if self._has_sr:
            feeds["sr"] = np.array(VAD_SAMPLE_RATE, dtype=np.int64)
        if self._v5:
            feeds["state"] = self._state
            out, self._state = self._session.run(None, feeds)
        else:
            feeds["h"] = self._h
            feeds["c"] = self._c
            out, self._h, self._c = self._session.run(None, feeds)
        return float(out[0][0])


class _Resampler:
    """Streaming linear resampler to 16kHz.

    Stateful on purpose. Resampling each inbound chunk independently
    drops or duplicates a fraction of a sample at every boundary, and
    at 10 chunks a second that drift is audible to the VAD as periodic
    clicks, which reads as speech onset.
    """

    def __init__(self, in_rate: int, out_rate: int = VAD_SAMPLE_RATE):
        import numpy as np

        self._np = np
        self._ratio = float(in_rate) / float(out_rate)
        self._passthrough = in_rate == out_rate
        self._residue = np.zeros(0, dtype=np.float32)
        self._phase = 0.0

    def process(self, samples: Any) -> Any:
        np = self._np
        if self._passthrough:
            return samples
        buf = np.concatenate((self._residue, samples))
        if buf.size < 2:
            self._residue = buf
            return np.zeros(0, dtype=np.float32)
        span = (buf.size - 1) - self._phase
        if span < 0:
            self._residue = buf
            return np.zeros(0, dtype=np.float32)
        count = int(span / self._ratio) + 1
        positions = self._phase + np.arange(count, dtype=np.float64) * self._ratio
        out = np.interp(
            positions, np.arange(buf.size, dtype=np.float64), buf
        ).astype(np.float32)
        next_pos = self._phase + count * self._ratio
        drop = int(next_pos)
        self._residue = buf[drop:] if drop < buf.size else np.zeros(0, dtype=np.float32)
        self._phase = next_pos - drop
        return out


class VadEndpointer:
    """Turns a PCM16 byte stream into speech-start / speech-end events.

    ``feed`` is cheap and synchronous. It reframes, resamples, runs the
    model over whole frames only, and returns whatever boundaries it
    crossed. Callers drive their own state machine off the events; the
    endpointer holds no opinion about what happens next.
    """

    def __init__(
        self,
        vad: SileroVAD,
        *,
        sample_rate: int = 24000,
        config: VadConfig | None = None,
    ):
        import numpy as np

        self._np = np
        self._vad = vad
        self._cfg = config or VadConfig()
        self._resampler = _Resampler(sample_rate)
        self._pending = np.zeros(0, dtype=np.float32)
        self._speaking = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._frames_seen = 0
        self._infer_seconds = 0.0
        self.last_probability = 0.0

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def frames_seen(self) -> int:
        return self._frames_seen

    @property
    def mean_infer_ms(self) -> float:
        if not self._frames_seen:
            return 0.0
        return (self._infer_seconds / self._frames_seen) * 1000.0

    def reset(self) -> None:
        np = self._np
        self._vad.reset()
        self._pending = np.zeros(0, dtype=np.float32)
        self._speaking = False
        self._speech_frames = 0
        self._silence_frames = 0

    def feed(self, pcm16: bytes) -> list[VadEvent]:
        np = self._np
        if not pcm16:
            return []
        # Odd trailing byte would misalign every subsequent sample.
        usable = len(pcm16) - (len(pcm16) % 2)
        if usable <= 0:
            return []
        raw = np.frombuffer(pcm16[:usable], dtype="<i2").astype(np.float32) / 32768.0
        resampled = self._resampler.process(raw)
        if resampled.size:
            self._pending = np.concatenate((self._pending, resampled))

        events: list[VadEvent] = []
        min_speech_frames = max(1, self._cfg.min_speech_ms // VAD_FRAME_MS)
        min_silence_frames = max(1, self._cfg.min_silence_ms // VAD_FRAME_MS)

        while self._pending.size >= VAD_FRAME_SAMPLES:
            frame = self._pending[:VAD_FRAME_SAMPLES]
            self._pending = self._pending[VAD_FRAME_SAMPLES:]
            started = time.perf_counter()
            prob = self._vad.probability(frame)
            self._infer_seconds += time.perf_counter() - started
            self._frames_seen += 1
            self.last_probability = prob

            if not self._speaking:
                if prob >= self._cfg.threshold:
                    self._speech_frames += 1
                    if self._speech_frames >= min_speech_frames:
                        self._speaking = True
                        self._silence_frames = 0
                        events.append(VadEvent.SPEECH_START)
                else:
                    self._speech_frames = 0
            else:
                if prob < self._cfg.neg_threshold:
                    self._silence_frames += 1
                    if self._silence_frames >= min_silence_frames:
                        self._speaking = False
                        self._speech_frames = 0
                        self._silence_frames = 0
                        events.append(VadEvent.SPEECH_END)
                else:
                    self._silence_frames = 0
        return events


def vad_available() -> tuple[bool, str]:
    """``(ready, reason)`` for the setup wizard and the probe surface.

    Readiness for a local engine is importability plus weights on
    disk, never a credential.
    """
    try:
        import onnxruntime  # noqa: F401
    except Exception as exc:
        return False, f"onnxruntime not installed ({exc.__class__.__name__})"
    try:
        import numpy  # noqa: F401
    except Exception:
        return False, "numpy not installed"
    from voice import local_models

    if not local_models.silero_vad_present():
        return False, f"weights not downloaded ({local_models.silero_vad_path()})"
    return True, "ready"


def load_endpointer(
    *,
    sample_rate: int = 24000,
    config: VadConfig | None = None,
) -> VadEndpointer | None:
    """Build an endpointer, or return ``None`` if VAD cannot run here.

    Never downloads. A voice turn must not be the thing that decides
    to pull 2MB off the network, and an operator who has not run setup
    gets the legacy timer rather than a stall.
    """
    ready, reason = vad_available()
    if not ready:
        logger.info("Server VAD unavailable (%s) - falling back to silence timer", reason)
        return None
    from voice import local_models

    try:
        vad = SileroVAD(local_models.silero_vad_path())
        return VadEndpointer(vad, sample_rate=sample_rate, config=config)
    except Exception:
        logger.warning(
            "Server VAD failed to load - falling back to silence timer",
            exc_info=True,
        )
        return None
