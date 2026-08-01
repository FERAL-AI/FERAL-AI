"""Chained-voice turn latency bench (Lane: local voice + latency).

Run it::

    python3 -m tests.perf.voice_chained_latency_bench          # from feral-core/

What it measures
----------------
One full chained turn, driven through the REAL
:class:`voice.chained_pipeline.ChainedVoicePipeline` over its public
API only (``open_session`` / ``handle_audio`` / ``close_session``), so
the same file runs unchanged against an older checkout of the pipeline.

The clock starts at ``t_speech_end``: the moment the speaker stopped
making sound. Everything after that is dead air the user sits through.

Two numbers come out:

``endpoint_ms``
    ``t_speech_end`` to the pipeline entering ``processing``. This is
    pure architecture: client silence gate plus server silence timer,
    or server VAD once that lands.
``first_audio_ms``
    ``t_speech_end`` to the first audio frame that carries bytes. This
    is what the user actually experiences as "how long until it starts
    talking back".

Component costs are STUBS, not measurements: the STT, LLM and TTS
fakes below sleep for fixed budgets. They are inputs held constant
across runs so the delta between two runs is attributable to the
pipeline, not to a provider having a good day. Real engine costs are
measured separately (see ``voice/tts_providers/macos_say.py`` and the
local STT providers).

Two client models are simulated, because the endpointing contract
spans both sides of the socket:

``gated``
    What ``feral-client-v2/src/lib/voiceRealtime.js`` shipped before
    this lane: an energy VAD that keeps streaming for
    ``VAD_SILENCE_FRAMES`` (15 x 100ms = 1.5s) after the speaker stops
    and then goes quiet. The server's silence timer only starts once
    the client stops sending.
``continuous``
    The client streams every frame and lets the server decide where
    the utterance ended. On a pipeline whose only endpointer is the
    inbound-audio silence timer this NEVER flushes, which is the
    reason the client-side gate existed at all.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from voice.chained_pipeline import ChainedVoicePipeline  # noqa: E402
from voice.stt_providers import STTProvider, TranscriptFragment  # noqa: E402
from voice.tts_providers import TTSProvider  # noqa: E402

SAMPLE_RATE = 24000
FRAME_MS = 100
FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS // 1000

# Fixed component budgets. Inputs, not measurements.
STT_FLUSH_S = 0.40
LLM_TTFT_S = 0.40
LLM_TOKENS_PER_S = 30.0
TTS_TTFB_S = 0.25
TTS_S_PER_WORD = 0.04

REPLY = (
    "The kettle is already on. "
    "I moved your two o'clock to Thursday so the afternoon is clear. "
    "Nothing else needs you before then."
)

# Client-side energy gate, as shipped: 15 frames of 100ms.
CLIENT_GATE_FRAMES = 15


QUIET_FRAME = base64.b64encode(b"\x00" * FRAME_BYTES).decode()


def _real_speech_pcm() -> bytes:
    """PCM16 @ 24kHz of an actual sentence, or b"" if unavailable.

    A synthetic tone will not do. Silero is trained on speech and
    scores a 440Hz square wave near zero, so a bench driven by tones
    measures the fallback timer no matter what the VAD is doing. The
    first cut of this file made exactly that mistake and reported the
    VAD as a no-op.

    macOS ships a synthesiser, so on the platform this lane targets
    the bench can produce genuine speech with no assets in the repo
    and no download.
    """
    import shutil
    import subprocess
    import tempfile
    import wave

    if not shutil.which("say"):
        return b""
    tmp = Path(tempfile.gettempdir()) / "feral_bench_speech_24k.wav"
    if not tmp.exists():
        try:
            subprocess.run(
                [
                    "say",
                    "--file-format=WAVE",
                    f"--data-format=LEI16@{SAMPLE_RATE}",
                    "-o", str(tmp),
                    "Move my two o'clock meeting to Thursday afternoon please",
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
        except Exception:
            return b""
    try:
        with wave.open(str(tmp)) as handle:
            if handle.getframerate() != SAMPLE_RATE or handle.getnchannels() != 1:
                return b""
            return handle.readframes(handle.getnframes())
    except Exception:
        return b""


SPEECH_PCM = _real_speech_pcm()


class BenchSTT(STTProvider):
    """Buffered provider (Whisper-shaped): nothing until ``flush()``."""

    def __init__(self) -> None:
        # Named ``_result_queue`` on purpose: the pipeline drains that
        # attribute directly in ``_collect_transcript`` to close the
        # race between ``flush()`` enqueueing and the consumer task
        # being scheduled. The shipped buffered providers use the same
        # name, so the bench must too or it measures a different code
        # path from production.
        self._result_queue: asyncio.Queue = asyncio.Queue()
        self._buffer = bytearray()
        self._closed = False

    async def open_stream(self):
        while True:
            frag = await self._result_queue.get()
            if frag is None:
                break
            yield frag

    async def send_audio(self, audio_bytes: bytes) -> None:
        if not self._closed:
            self._buffer.extend(audio_bytes)

    async def flush(self) -> None:
        if not self._buffer:
            return
        self._buffer.clear()
        await asyncio.sleep(STT_FLUSH_S)
        await self._result_queue.put(
            TranscriptFragment(
                text="move my two o'clock",
                is_partial=False,
                is_final=True,
                speech_final=True,
            )
        )

    async def close(self) -> None:
        self._closed = True
        await self._result_queue.put(None)


class BenchTTS(TTSProvider):
    """Streams a fixed budget of PCM16 for the text it is given."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.output_format = "pcm"
        self.sample_rate = SAMPLE_RATE

    async def synthesize(self, text: str):
        self.calls.append(text)
        await asyncio.sleep(TTS_TTFB_S)
        words = max(1, len(text.split()))
        budget = words * TTS_S_PER_WORD
        slices = max(1, int(budget / 0.05))
        per = budget / slices
        for _ in range(slices):
            await asyncio.sleep(per)
            yield b"\x00\x01" * 512

    async def close(self) -> None:
        return None


class BenchLLM:
    """Orchestrator stand-in.

    Mirrors the two things the pipeline reads off the real
    orchestrator: a ``send`` callable that carries ``stream_delta``
    frames, and a ``conversation_history`` dict the buffered path
    scrapes when the turn is over.
    """

    def __init__(self) -> None:
        self.conversation_history: dict[str, list[dict]] = {}
        self.send = self._noop_send

    async def _noop_send(self, session_id, message) -> None:
        return None

    async def handle_command_stream(self, session_id: str, text: str, context=None):
        await asyncio.sleep(LLM_TTFT_S)
        tokens = REPLY.split(" ")
        acc = ""
        for i, tok in enumerate(tokens):
            await asyncio.sleep(1.0 / LLM_TOKENS_PER_S)
            piece = tok if i == 0 else " " + tok
            acc += piece
            await self._emit_delta(session_id, piece)
        self.conversation_history.setdefault(session_id, []).append(
            {"role": "assistant", "text": acc}
        )
        return acc

    async def _emit_delta(self, session_id: str, delta: str) -> None:
        try:
            from models.protocol import FeralMessage

            msg = FeralMessage(
                session_id=session_id,
                hop="brain",
                type="stream_delta",
                payload={"delta": delta, "stream_id": "bench", "is_final": False},
            )
        except Exception:
            msg = {"type": "stream_delta", "payload": {"delta": delta}}
        await self.send(session_id, msg)


@dataclass
class TurnResult:
    endpoint_ms: float
    first_audio_ms: float
    last_audio_ms: float
    audio_frames: int
    tts_calls: int
    endpoint_source: str = ""
    endpoint_mode: str = ""
    states: list[str] = field(default_factory=list)


async def run_turn(*, client: str, speech_s: float = 1.6, timeout_s: float = 30.0) -> TurnResult:
    pipeline = ChainedVoicePipeline()
    stt, tts, llm = BenchSTT(), BenchTTS(), BenchLLM()

    marks: dict[str, float] = {}
    states: list[str] = []
    audio_frames = 0
    done = asyncio.Event()

    async def send_frame(sid, frame) -> None:
        nonlocal audio_frames
        ftype = frame.get("type") if isinstance(frame, dict) else getattr(frame, "type", "")
        payload = (
            frame.get("payload", {})
            if isinstance(frame, dict)
            else getattr(frame, "payload", {})
        ) or {}
        now = time.monotonic()
        if ftype == "voice_state":
            st = payload.get("state", "")
            states.append(st)
            if st == "processing":
                marks.setdefault("processing", now)
            elif st == "idle" and "processing" in marks:
                marks.setdefault("idle", now)
                done.set()
        elif ftype in ("audio_chunk", "tts_chunk"):
            if payload.get("data_b64"):
                audio_frames += 1
                marks.setdefault("first_audio", now)
                marks["last_audio"] = now

    await pipeline.open_session(
        session_id="bench-session",
        stt_provider=stt,
        tts_provider=tts,
        llm_handle=llm,
        send_frame=send_frame,
    )

    async def feed() -> None:
        # Real speech when the host can make some, so the VAD is
        # scoring something it was trained on. Silence otherwise, which
        # measures the timer path honestly rather than pretending a
        # tone is a voice.
        wanted = int(speech_s * 1000 / FRAME_MS)
        if SPEECH_PCM:
            frames = [
                SPEECH_PCM[i: i + FRAME_BYTES]
                for i in range(0, len(SPEECH_PCM), FRAME_BYTES)
            ]
            frames = [f for f in frames if len(f) == FRAME_BYTES][:wanted]
            payloads = [base64.b64encode(f).decode() for f in frames]
        else:
            payloads = [QUIET_FRAME] * wanted
        for payload in payloads:
            await pipeline.handle_audio("bench-session", payload)
            await asyncio.sleep(FRAME_MS / 1000.0)
        marks["speech_end"] = time.monotonic()
        quiet = CLIENT_GATE_FRAMES if client == "gated" else 10_000
        for _ in range(quiet):
            if done.is_set():
                return
            await pipeline.handle_audio("bench-session", QUIET_FRAME)
            await asyncio.sleep(FRAME_MS / 1000.0)

    feeder = asyncio.create_task(feed())
    endpoint_source = ""
    endpoint_mode = ""
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        marks.setdefault("processing", float("nan"))
        marks.setdefault("first_audio", float("nan"))
        marks.setdefault("last_audio", float("nan"))
    finally:
        live = pipeline.get_session("bench-session")
        endpoint_source = getattr(live, "last_endpoint_source", "")
        if hasattr(pipeline, "endpoint_mode"):
            endpoint_mode = pipeline.endpoint_mode("bench-session")
        feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)
        await pipeline.close_session("bench-session")

    t0 = marks.get("speech_end", time.monotonic())

    def _ms(key: str) -> float:
        v = marks.get(key)
        if v is None or v != v:
            return float("nan")
        return (v - t0) * 1000.0

    return TurnResult(
        endpoint_ms=_ms("processing"),
        first_audio_ms=_ms("first_audio"),
        last_audio_ms=_ms("last_audio"),
        audio_frames=audio_frames,
        tts_calls=len(tts.calls),
        endpoint_source=endpoint_source,
        endpoint_mode=endpoint_mode,
        states=states,
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--clients", default="gated,continuous")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report: dict[str, dict] = {}
    for client in args.clients.split(","):
        client = client.strip()
        if not client:
            continue
        rows = [await run_turn(client=client) for _ in range(args.runs)]
        ok = [r for r in rows if r.endpoint_ms == r.endpoint_ms]

        def med(attr: str) -> float:
            vals = [getattr(r, attr) for r in ok if getattr(r, attr) == getattr(r, attr)]
            return statistics.median(vals) if vals else float("nan")

        report[client] = {
            "runs": len(rows),
            "completed": len(ok),
            "endpoint_ms": round(med("endpoint_ms"), 1),
            "first_audio_ms": round(med("first_audio_ms"), 1),
            "last_audio_ms": round(med("last_audio_ms"), 1),
            "audio_frames": rows[0].audio_frames,
            "tts_calls": rows[0].tts_calls,
            "endpoint_source": rows[0].endpoint_source,
            "endpoint_mode": rows[0].endpoint_mode,
        }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"budgets: stt={STT_FLUSH_S}s llm_ttft={LLM_TTFT_S}s "
              f"tok/s={LLM_TOKENS_PER_S} tts_ttfb={TTS_TTFB_S}s")
        print(f"drive audio: {'real speech (say)' if SPEECH_PCM else 'SILENCE ONLY - no VAD signal'}")
        for client, row in report.items():
            print(f"\nclient={client}  ({row['completed']}/{row['runs']} turns completed)")
            if not row["completed"]:
                print("  NEVER ENDPOINTED - no flush, no answer")
                continue
            print(f"  speech_end -> processing   {row['endpoint_ms']:8.1f} ms")
            print(f"  speech_end -> first audio  {row['first_audio_ms']:8.1f} ms")
            print(f"  speech_end -> last audio   {row['last_audio_ms']:8.1f} ms")
            print(f"  audio frames={row['audio_frames']}  tts calls={row['tts_calls']}")
            print(f"  endpointer={row['endpoint_mode'] or 'n/a'} "
                  f"fired_by={row['endpoint_source'] or 'n/a'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
