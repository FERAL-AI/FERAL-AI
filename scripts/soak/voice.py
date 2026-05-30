#!/usr/bin/env python3
"""
 — standalone voice soak harness.

Same protocol-shape soak loop the pytest suite runs, but exposed as a
plain script so ops can fire it from a runbook without touching pytest.

Examples:

  # 60-minute OpenAI Realtime soak against a local fake peer
  python scripts/soak/voice.py --provider openai --duration-min 60

  # 90-second smoke for the Gemini protocol shape
  python scripts/soak/voice.py --provider gemini --duration-min 1 \\
      --reconnect-interval-sec 30
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# Tests live in feral-core/tests; importing from there keeps the
# protocol fakes single-sourced.
sys.path.insert(0, str(REPO_ROOT / "feral-core"))
sys.path.insert(0, str(REPO_ROOT / "feral-core" / "tests"))

from test_voice_soak import (  # noqa: E402
    _fake_gemini_handler,
    _fake_openai_handler,
    _gemini_session,
    _openai_session,
    _running_server,
    _soak_loop,
)


PROVIDERS = {
    "openai": (_fake_openai_handler, _openai_session),
    "gemini": (_fake_gemini_handler, _gemini_session),
}


async def _run(provider: str, duration_sec: int, reconnect_sec: int) -> int:
    handler, runner = PROVIDERS[provider]
    async with _running_server(handler) as uri:
        stats = await _soak_loop(
            uri, runner,
            duration_sec=duration_sec,
            reconnect_sec=reconnect_sec,
        )
    print(
        f"[VOICE SOAK / {provider}] PASS: "
        f"elapsed={stats['elapsed_sec']:.1f}s reconnects={stats['reconnects']} "
        f"frames={stats['frames']} "
        f"rss_start={stats['rss_start_kb']}KB rss_peak={stats['rss_peak_kb']}KB "
        f"rss_end={stats['rss_end_kb']}KB growth={stats['rss_growth_kb']}KB"
    )
    # Mirror the pytest assertion budget so ops see the same threshold.
    if stats["rss_growth_kb"] >= 50 * 1024:
        print(
            f"[VOICE SOAK / {provider}] FAIL: RSS grew "
            f"{stats['rss_growth_kb']}KB (budget 51200KB)",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", choices=sorted(PROVIDERS), required=True)
    p.add_argument(
        "--duration-min", type=int, default=60,
        help="soak duration in minutes (default 60)",
    )
    p.add_argument(
        "--reconnect-interval-sec", type=int, default=90,
        help="force a fresh WS connect every N seconds (default 90)",
    )
    args = p.parse_args()
    duration_sec = max(15, args.duration_min * 60)
    reconnect_sec = max(5, min(args.reconnect_interval_sec, duration_sec))
    return asyncio.run(_run(args.provider, duration_sec, reconnect_sec))


if __name__ == "__main__":
    raise SystemExit(main())
