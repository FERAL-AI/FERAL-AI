"""
 — channels soak harness.

Posts one message every 30 seconds for 24 hours to personal *staging*
Telegram, Slack, and Discord bots and asserts:

  * delivery success rate ≥ 99%,
  * no auth churn (no 401/403 mid-run),
  * no rate-limit cascade (≤ 1% of attempts come back 429, and we never
    hit two 429s back-to-back).

These are env-gated. If the corresponding token is missing, the test
skips cleanly so CI never accidentally calls a real provider.

  Telegram: FERAL_SOAK_TELEGRAM_TOKEN + FERAL_SOAK_TELEGRAM_CHAT_ID
  Slack:    FERAL_SOAK_SLACK_TOKEN    + FERAL_SOAK_SLACK_CHANNEL
  Discord:  FERAL_SOAK_DISCORD_WEBHOOK

Local smoke (1-minute run with 5-second cadence):

    FERAL_SOAK_DURATION_MIN=1 FERAL_SOAK_INTERVAL_SEC=5 \\
        pytest feral-core/tests/test_channels_soak.py --runsoak -s
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import pytest

try:
    import httpx
except ImportError:  # pragma: no cover - httpx is a hard dep
    httpx = None  # type: ignore


pytestmark = pytest.mark.soak


# --- tunables ----------------------------------------------------------------

# Default 1440 minutes = 24 hours (per spec). Overridable per run / for
# local smoke.
DURATION_MIN = int(os.environ.get("FERAL_SOAK_DURATION_MIN", "1440"))
INTERVAL_SEC = int(os.environ.get("FERAL_SOAK_INTERVAL_SEC", "30"))
SUCCESS_RATE_FLOOR = float(os.environ.get("FERAL_SOAK_SUCCESS_FLOOR", "0.99"))
RATE_LIMIT_CEILING = float(os.environ.get("FERAL_SOAK_RATE_LIMIT_CEILING", "0.01"))

# httpx timeout for every individual request.
HTTP_TIMEOUT_SEC = float(os.environ.get("FERAL_SOAK_HTTP_TIMEOUT_SEC", "15"))


# --- shared loop -------------------------------------------------------------


@dataclass
class SoakStats:
    sent: int = 0
    succeeded: int = 0
    auth_failures: int = 0
    rate_limited: int = 0
    other_failures: int = 0
    consecutive_429s: int = 0
    max_consecutive_429s: int = 0
    statuses: list[int] = field(default_factory=list)

    def record(self, status: int) -> None:
        self.sent += 1
        self.statuses.append(status)
        if 200 <= status < 300:
            self.succeeded += 1
            self.consecutive_429s = 0
        elif status in (401, 403):
            self.auth_failures += 1
            self.consecutive_429s = 0
        elif status == 429:
            self.rate_limited += 1
            self.consecutive_429s += 1
            self.max_consecutive_429s = max(
                self.max_consecutive_429s, self.consecutive_429s
            )
        else:
            self.other_failures += 1
            self.consecutive_429s = 0

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.sent if self.sent else 0.0

    @property
    def rate_limit_rate(self) -> float:
        return self.rate_limited / self.sent if self.sent else 0.0


async def _drive_soak(
    label: str,
    sender: Callable[[httpx.AsyncClient, str], Awaitable[int]],
    *,
    duration_sec: int,
    interval_sec: int,
) -> SoakStats:
    stats = SoakStats()
    deadline = time.monotonic() + duration_sec
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as client:
        while time.monotonic() < deadline:
            tick_started = time.monotonic()
            text = f"[FERAL soak/{label}] heartbeat {uuid.uuid4().hex[:8]}"
            try:
                status = await sender(client, text)
            except httpx.HTTPError:
                # Treat transport errors as "other failure"; we want them
                # surfaced in the failure budget, not as a hard stop.
                status = 0
            stats.record(status)
            if stats.auth_failures > 0:
                # No point in burning the rest of the soak window — auth
                # broke and the success-rate assertion will fail anyway.
                break
            elapsed = time.monotonic() - tick_started
            sleep_for = interval_sec - elapsed
            if sleep_for > 0 and time.monotonic() + sleep_for < deadline:
                await asyncio.sleep(sleep_for)
            else:
                break
    return stats


def _assert_clean(label: str, stats: SoakStats) -> None:
    print(
        f"\n[CHANNELS SOAK / {label}] PASS check: "
        f"sent={stats.sent} ok={stats.succeeded} "
        f"success_rate={stats.success_rate:.4f} "
        f"auth_failures={stats.auth_failures} "
        f"rate_limited={stats.rate_limited} "
        f"max_consecutive_429s={stats.max_consecutive_429s} "
        f"other_failures={stats.other_failures}"
    )
    assert stats.sent > 0, f"{label}: no messages were attempted"
    assert stats.auth_failures == 0, (
        f"{label}: auth churn detected ({stats.auth_failures} 401/403 responses)"
    )
    assert stats.success_rate >= SUCCESS_RATE_FLOOR, (
        f"{label}: success rate {stats.success_rate:.4f} below floor "
        f"{SUCCESS_RATE_FLOOR:.4f}"
    )
    assert stats.rate_limit_rate <= RATE_LIMIT_CEILING, (
        f"{label}: rate-limit rate {stats.rate_limit_rate:.4f} above ceiling "
        f"{RATE_LIMIT_CEILING:.4f} — likely cascade"
    )
    assert stats.max_consecutive_429s <= 1, (
        f"{label}: rate-limit cascade — {stats.max_consecutive_429s} "
        f"consecutive 429 responses"
    )


# --- per-provider senders ----------------------------------------------------


def _telegram_sender(token: str, chat_id: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    async def send(client: httpx.AsyncClient, text: str) -> int:
        resp = await client.post(url, json={"chat_id": chat_id, "text": text})
        return resp.status_code

    return send


def _slack_sender(token: str, channel: str):
    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    async def send(client: httpx.AsyncClient, text: str) -> int:
        resp = await client.post(
            url, headers=headers, json={"channel": channel, "text": text}
        )
        if resp.status_code == 200:
            # Slack returns HTTP 200 even on logical failures; surface
            # auth failures with the right HTTP-ish status so the loop
            # treats them as auth churn.
            try:
                body = resp.json()
            except ValueError:
                return 200
            if body.get("ok"):
                return 200
            err = (body.get("error") or "").lower()
            if err in {"invalid_auth", "not_authed", "token_revoked", "account_inactive"}:
                return 401
            if err in {"ratelimited", "rate_limited"}:
                return 429
            return 502
        return resp.status_code

    return send


def _discord_sender(webhook: str):
    async def send(client: httpx.AsyncClient, text: str) -> int:
        resp = await client.post(webhook, json={"content": text})
        # Discord webhooks return 204 No Content on success.
        return resp.status_code

    return send


# --- tests -------------------------------------------------------------------


def _require_httpx():
    if httpx is None:
        pytest.skip("httpx not installed")


def _duration_sec() -> int:
    return max(15, DURATION_MIN * 60)


def _interval_sec() -> int:
    return max(1, min(INTERVAL_SEC, _duration_sec()))


@pytest.mark.asyncio
async def test_telegram_channel_soak():
    _require_httpx()
    token = os.environ.get("FERAL_SOAK_TELEGRAM_TOKEN")
    chat_id = os.environ.get("FERAL_SOAK_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        pytest.skip(
            "FERAL_SOAK_TELEGRAM_TOKEN and FERAL_SOAK_TELEGRAM_CHAT_ID required"
        )
    stats = await _drive_soak(
        "telegram",
        _telegram_sender(token, chat_id),
        duration_sec=_duration_sec(),
        interval_sec=_interval_sec(),
    )
    _assert_clean("telegram", stats)


@pytest.mark.asyncio
async def test_slack_channel_soak():
    _require_httpx()
    token = os.environ.get("FERAL_SOAK_SLACK_TOKEN")
    channel = os.environ.get("FERAL_SOAK_SLACK_CHANNEL")
    if not token or not channel:
        pytest.skip(
            "FERAL_SOAK_SLACK_TOKEN and FERAL_SOAK_SLACK_CHANNEL required"
        )
    stats = await _drive_soak(
        "slack",
        _slack_sender(token, channel),
        duration_sec=_duration_sec(),
        interval_sec=_interval_sec(),
    )
    _assert_clean("slack", stats)


@pytest.mark.asyncio
async def test_discord_channel_soak():
    _require_httpx()
    webhook = os.environ.get("FERAL_SOAK_DISCORD_WEBHOOK")
    if not webhook:
        pytest.skip("FERAL_SOAK_DISCORD_WEBHOOK required")
    stats = await _drive_soak(
        "discord",
        _discord_sender(webhook),
        duration_sec=_duration_sec(),
        interval_sec=_interval_sec(),
    )
    _assert_clean("discord", stats)
