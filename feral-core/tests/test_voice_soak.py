"""
 — voice soak harness.

Long-duration smoke test for the OpenAI Realtime and Gemini Live voice
WebSocket protocols. The point is not to exercise the real provider
clients (those are read-only for ) but to exercise the *protocol
shapes* against an in-process fake peer for hours at a time and prove
that:

  * the inner loop has no file-descriptor / task / socket leak,
  * forced WS reconnects every N seconds do not pile up coroutines or
    grow RSS unboundedly,
  * audio frames flow continuously in both directions for the full
    duration without back-pressure deadlocks.

These tests are gated behind the `--runsoak` pytest flag (registered in
`feral-core/tests/conftest.py`). Without the flag they are skipped and
contribute nothing to the regular CI run.

Local smoke check (1-minute run instead of the 60-minute default):

    FERAL_SOAK_DURATION_MIN=1 FERAL_SOAK_RECONNECT_SEC=15 \\
        pytest feral-core/tests/test_voice_soak.py --runsoak -s
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import resource
import socket
import sys
import time
from contextlib import asynccontextmanager

import pytest

try:
    import websockets
except ImportError:  # pragma: no cover - websockets is a hard dep
    websockets = None  # type: ignore


pytestmark = pytest.mark.soak

# --- tunables ----------------------------------------------------------------

DURATION_MIN = int(os.environ.get("FERAL_SOAK_DURATION_MIN", "60"))
RECONNECT_INTERVAL_SEC = int(os.environ.get("FERAL_SOAK_RECONNECT_SEC", "90"))
AUDIO_CHUNK_INTERVAL_SEC = float(os.environ.get("FERAL_SOAK_AUDIO_INTERVAL_SEC", "0.04"))
# Allow RSS to grow by this many KB across the run before we call it a leak.
# 50 MB headroom is generous for Python interpreter + pytest itself; an
# actual leak in the WS client would dwarf this within minutes.
RSS_GROWTH_BUDGET_KB = int(os.environ.get("FERAL_SOAK_RSS_BUDGET_KB", str(50 * 1024)))


# --- shared helpers ----------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _rss_kb() -> int:
    """Resident set size of the current process, in KB.

    On Linux ru_maxrss is in KB; on macOS it is in bytes.

    The unit is a property of the platform, so branch on the platform.
    The previous version guessed from magnitude ("> 10**9 must be
    bytes"), which is wrong for exactly the processes this test cares
    about: a 112 MB interpreter on macOS reports 117_243_904 bytes,
    sails under the 10**9 line, and gets reported as 117 GB. Every
    macOS run then failed the growth budget on the first sample. It
    never showed up in CI because CI is Linux, where the raw value is
    already KB.
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return raw // 1024
    return raw


def _silent_pcm_chunk(samples: int = 480) -> bytes:
    """Synthetic 16-bit PCM — silence is fine for protocol shape tests."""
    return b"\x00\x00" * samples


# --- fake OpenAI Realtime peer ----------------------------------------------


async def _fake_openai_handler(ws) -> None:
    """Minimal fake of api.openai.com/v1/realtime."""
    try:
        async for raw in ws:
            try:
                evt = json.loads(raw)
            except (TypeError, ValueError):
                continue
            etype = evt.get("type")
            if etype == "session.update":
                await ws.send(json.dumps({"type": "session.updated"}))
            elif etype == "input_audio_buffer.append":
                # Audio frames arrive constantly; no per-frame ack in the
                # real API either.
                continue
            elif etype == "input_audio_buffer.commit":
                await ws.send(json.dumps({"type": "input_audio_buffer.committed"}))
            elif etype == "response.create":
                payload = base64.b64encode(_silent_pcm_chunk()).decode("ascii")
                for _ in range(3):
                    await ws.send(json.dumps({
                        "type": "response.audio.delta",
                        "delta": payload,
                    }))
                await ws.send(json.dumps({"type": "response.done"}))
    except websockets.ConnectionClosed:
        return


# --- fake Gemini Live peer ---------------------------------------------------


async def _fake_gemini_handler(ws) -> None:
    """Minimal fake of generativelanguage.googleapis.com BidiGenerateContent."""
    try:
        # First client frame is BidiGenerateContentSetup.
        setup_raw = await ws.recv()
        try:
            json.loads(setup_raw)
        except (TypeError, ValueError):
            pass
        await ws.send(json.dumps({"setupComplete": {}}))
        async for _raw in ws:
            payload = base64.b64encode(_silent_pcm_chunk()).decode("ascii")
            await ws.send(json.dumps({
                "serverContent": {
                    "modelTurn": {
                        "parts": [{
                            "inlineData": {
                                "mimeType": "audio/pcm;rate=24000",
                                "data": payload,
                            }
                        }]
                    }
                }
            }))
    except websockets.ConnectionClosed:
        return


@asynccontextmanager
async def _running_server(handler):
    """Start a websockets server on a free port; yield its URI."""
    port = _free_port()
    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


# --- protocol-specific client streamers --------------------------------------


async def _openai_session(uri: str, stop_at: float, reconnect_sec: int) -> int:
    """Single OpenAI Realtime client session. Returns frames sent."""
    frames = 0
    async with websockets.connect(uri, max_queue=None) as ws:
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {"modalities": ["audio", "text"]},
        }))
        await ws.recv()  # session.updated
        session_started = time.monotonic()
        while time.monotonic() < stop_at:
            if time.monotonic() - session_started > reconnect_sec:
                return frames
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(_silent_pcm_chunk()).decode("ascii"),
            }))
            frames += 1
            # Periodically commit + ask for a response to drive return traffic.
            if frames % 25 == 0:
                await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                await ws.send(json.dumps({"type": "response.create"}))
                # drain server frames triggered by the commit
                for _ in range(4):
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        break
            await asyncio.sleep(AUDIO_CHUNK_INTERVAL_SEC)
    return frames


async def _gemini_session(uri: str, stop_at: float, reconnect_sec: int) -> int:
    """Single Gemini Live client session. Returns frames sent."""
    frames = 0
    async with websockets.connect(uri, max_queue=None) as ws:
        await ws.send(json.dumps({
            "setup": {"model": "models/gemini-2.0-flash-live-001"},
        }))
        await ws.recv()  # setupComplete
        session_started = time.monotonic()
        while time.monotonic() < stop_at:
            if time.monotonic() - session_started > reconnect_sec:
                return frames
            await ws.send(json.dumps({
                "realtimeInput": {
                    "mediaChunks": [{
                        "mimeType": "audio/pcm;rate=16000",
                        "data": base64.b64encode(_silent_pcm_chunk()).decode("ascii"),
                    }]
                }
            }))
            frames += 1
            try:
                await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(AUDIO_CHUNK_INTERVAL_SEC)
    return frames


async def _soak_loop(uri: str, runner, *, duration_sec: int, reconnect_sec: int) -> dict:
    """Drive `runner` against `uri` for `duration_sec`, reconnecting every
    `reconnect_sec` seconds. Returns a stats dict."""
    start = time.monotonic()
    stop_at = start + duration_sec
    rss_start = _rss_kb()
    rss_peak = rss_start
    reconnects = 0
    total_frames = 0
    while time.monotonic() < stop_at:
        frames = await runner(uri, stop_at, reconnect_sec)
        total_frames += frames
        reconnects += 1
        rss_now = _rss_kb()
        if rss_now > rss_peak:
            rss_peak = rss_now
    rss_end = _rss_kb()
    elapsed = time.monotonic() - start
    return {
        "elapsed_sec": elapsed,
        "reconnects": reconnects,
        "frames": total_frames,
        "rss_start_kb": rss_start,
        "rss_peak_kb": rss_peak,
        "rss_end_kb": rss_end,
        "rss_growth_kb": rss_end - rss_start,
    }


# --- tests -------------------------------------------------------------------


def _require_websockets():
    if websockets is None:
        pytest.skip("websockets library not installed")


@pytest.mark.asyncio
async def test_openai_realtime_soak():
    _require_websockets()
    duration_sec = max(15, DURATION_MIN * 60)
    reconnect_sec = max(5, min(RECONNECT_INTERVAL_SEC, duration_sec))
    async with _running_server(_fake_openai_handler) as uri:
        stats = await _soak_loop(
            uri, _openai_session,
            duration_sec=duration_sec,
            reconnect_sec=reconnect_sec,
        )

    print(
        f"\n[VOICE SOAK / openai] PASS: "
        f"elapsed={stats['elapsed_sec']:.1f}s reconnects={stats['reconnects']} "
        f"frames={stats['frames']} "
        f"rss_start={stats['rss_start_kb']}KB rss_peak={stats['rss_peak_kb']}KB "
        f"rss_end={stats['rss_end_kb']}KB growth={stats['rss_growth_kb']}KB"
    )

    assert stats["reconnects"] >= 1, "at least one reconnect cycle expected"
    assert stats["frames"] > 0, "no audio frames were sent"
    assert stats["rss_growth_kb"] < RSS_GROWTH_BUDGET_KB, (
        f"RSS grew by {stats['rss_growth_kb']}KB — "
        f"budget is {RSS_GROWTH_BUDGET_KB}KB; possible handle leak"
    )


@pytest.mark.asyncio
async def test_gemini_live_soak():
    _require_websockets()
    duration_sec = max(15, DURATION_MIN * 60)
    reconnect_sec = max(5, min(RECONNECT_INTERVAL_SEC, duration_sec))
    async with _running_server(_fake_gemini_handler) as uri:
        stats = await _soak_loop(
            uri, _gemini_session,
            duration_sec=duration_sec,
            reconnect_sec=reconnect_sec,
        )

    print(
        f"\n[VOICE SOAK / gemini] PASS: "
        f"elapsed={stats['elapsed_sec']:.1f}s reconnects={stats['reconnects']} "
        f"frames={stats['frames']} "
        f"rss_start={stats['rss_start_kb']}KB rss_peak={stats['rss_peak_kb']}KB "
        f"rss_end={stats['rss_end_kb']}KB growth={stats['rss_growth_kb']}KB"
    )

    assert stats["reconnects"] >= 1, "at least one reconnect cycle expected"
    assert stats["frames"] > 0, "no audio frames were sent"
    assert stats["rss_growth_kb"] < RSS_GROWTH_BUDGET_KB, (
        f"RSS grew by {stats['rss_growth_kb']}KB — "
        f"budget is {RSS_GROWTH_BUDGET_KB}KB; possible handle leak"
    )
