#!/usr/bin/env python3
"""
 — standalone channel soak harness.

Same env-gated harness the pytest suite runs, but exposed as a script
ops can fire from a runbook. One message every N seconds for the
configured duration; prints a single PASS/FAIL summary line per channel.

Examples:

  # Default: 1440 minutes (24h), 30s cadence
  python scripts/soak/channels.py --channel telegram

  # 5-minute smoke
  python scripts/soak/channels.py --channel slack --duration-min 5
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "feral-core"))
sys.path.insert(0, str(REPO_ROOT / "feral-core" / "tests"))

from test_channels_soak import (  # noqa: E402
    RATE_LIMIT_CEILING,
    SUCCESS_RATE_FLOOR,
    _discord_sender,
    _drive_soak,
    _slack_sender,
    _telegram_sender,
)


def _resolve_sender(channel: str):
    if channel == "telegram":
        token = os.environ.get("FERAL_SOAK_TELEGRAM_TOKEN")
        chat_id = os.environ.get("FERAL_SOAK_TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            print(
                "FAIL: telegram needs FERAL_SOAK_TELEGRAM_TOKEN + "
                "FERAL_SOAK_TELEGRAM_CHAT_ID",
                file=sys.stderr,
            )
            return None
        return _telegram_sender(token, chat_id)
    if channel == "slack":
        token = os.environ.get("FERAL_SOAK_SLACK_TOKEN")
        chan = os.environ.get("FERAL_SOAK_SLACK_CHANNEL")
        if not token or not chan:
            print(
                "FAIL: slack needs FERAL_SOAK_SLACK_TOKEN + "
                "FERAL_SOAK_SLACK_CHANNEL",
                file=sys.stderr,
            )
            return None
        return _slack_sender(token, chan)
    if channel == "discord":
        webhook = os.environ.get("FERAL_SOAK_DISCORD_WEBHOOK")
        if not webhook:
            print("FAIL: discord needs FERAL_SOAK_DISCORD_WEBHOOK", file=sys.stderr)
            return None
        return _discord_sender(webhook)
    print(f"FAIL: unknown channel {channel!r}", file=sys.stderr)
    return None


async def _run(channel: str, duration_sec: int, interval_sec: int) -> int:
    sender = _resolve_sender(channel)
    if sender is None:
        return 2
    stats = await _drive_soak(
        channel, sender, duration_sec=duration_sec, interval_sec=interval_sec,
    )
    print(
        f"[CHANNELS SOAK / {channel}]: "
        f"sent={stats.sent} ok={stats.succeeded} "
        f"success_rate={stats.success_rate:.4f} "
        f"auth_failures={stats.auth_failures} "
        f"rate_limited={stats.rate_limited} "
        f"max_consecutive_429s={stats.max_consecutive_429s} "
        f"other_failures={stats.other_failures}"
    )
    if stats.sent == 0:
        print(f"[CHANNELS SOAK / {channel}] FAIL: nothing was sent", file=sys.stderr)
        return 1
    if stats.auth_failures > 0:
        print(
            f"[CHANNELS SOAK / {channel}] FAIL: auth churn "
            f"({stats.auth_failures} 401/403)",
            file=sys.stderr,
        )
        return 1
    if stats.success_rate < SUCCESS_RATE_FLOOR:
        print(
            f"[CHANNELS SOAK / {channel}] FAIL: success_rate "
            f"{stats.success_rate:.4f} < floor {SUCCESS_RATE_FLOOR:.4f}",
            file=sys.stderr,
        )
        return 1
    if stats.rate_limit_rate > RATE_LIMIT_CEILING or stats.max_consecutive_429s > 1:
        print(
            f"[CHANNELS SOAK / {channel}] FAIL: rate-limit cascade "
            f"(rate={stats.rate_limit_rate:.4f}, "
            f"max_consecutive_429s={stats.max_consecutive_429s})",
            file=sys.stderr,
        )
        return 1
    print(f"[CHANNELS SOAK / {channel}] PASS")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--channel", choices=["telegram", "slack", "discord"], required=True,
    )
    p.add_argument(
        "--duration-min", type=int, default=1440,
        help="soak duration in minutes (default 1440 = 24h)",
    )
    p.add_argument(
        "--interval-sec", type=int, default=30,
        help="seconds between messages (default 30)",
    )
    args = p.parse_args()
    duration_sec = max(15, args.duration_min * 60)
    interval_sec = max(1, min(args.interval_sec, duration_sec))
    return asyncio.run(_run(args.channel, duration_sec, interval_sec))


if __name__ == "__main__":
    raise SystemExit(main())
