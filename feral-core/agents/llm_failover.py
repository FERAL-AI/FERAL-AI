"""LLM failover support: retry, error classification, cooldown tracking.

Extracted from ``agents.llm_provider`` (-A15) to keep the orchestrator
facade focused on dispatch. No behaviour changes — every public symbol
here was previously a module-level name in ``agents.llm_provider`` and
is re-exported from there for import compatibility.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import httpx
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("feral.llm")

MAX_RETRIES = 3
RETRY_DELAYS = [1, 2, 4]  # seconds
_RETRIABLE_CODES = ("429", "500", "502", "503", "504", "timeout", "connection")

# Hard ceiling for how long a single same-provider retry will block on a
# server-supplied Retry-After hint. If the upstream asks us to wait longer
# than this, we'd rather hand control back to the failover loop so a
# healthy fallback (when configured) can serve the request immediately.
RETRY_AFTER_MAX_INLINE_SLEEP = 5.0  # seconds

# Upper bound for cooldown windows derived from a Retry-After header. We
# trust the upstream's hint, but cap it so a misconfigured proxy that
# returns ``Retry-After: 31536000`` can't pull a provider out of rotation
# for a year.
RETRY_AFTER_MAX_COOLDOWN = 24 * 3600.0  # 24h

# DeepSeek's streaming SSE keeps the connection open by sending
# ``: keep-alive`` comment lines (per the 2026-04-26 spec, the
# connection may stay open up to 10 minutes during thinking). The
# streaming path must treat these as liveness signals, not terminators.
_SSE_KEEPALIVE_PREFIXES = (": ", ":", ": keep-alive", ":OPENROUTER PROCESSING")


def parse_retry_after(
    error: Exception,
    *,
    max_seconds: float = RETRY_AFTER_MAX_COOLDOWN,
) -> Optional[float]:
    """Extract the upstream ``Retry-After`` value from an LLM error.

    Returns the requested wait in seconds, or ``None`` when the error
    carries no usable hint. Handles both numeric ("``Retry-After: 30``")
    and HTTP-date ("``Retry-After: Wed, 21 Oct 2026 07:28:00 GMT``")
    encodings as documented in RFC 7231 §7.1.3.

    The value is clamped to ``[0, max_seconds]`` so a misbehaving
    upstream cannot keep a provider out of rotation indefinitely.
    """
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None

    seconds: Optional[float] = None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            target = None
        if target is not None:
            try:
                from datetime import datetime, timezone
                now = datetime.now(target.tzinfo or timezone.utc)
                seconds = max(0.0, (target - now).total_seconds())
            except Exception:
                seconds = None

    if seconds is None:
        return None
    if seconds < 0:
        seconds = 0.0
    if seconds > max_seconds:
        seconds = max_seconds
    return seconds


async def _retry_llm_call(
    coro_factory,
    *,
    max_retries: Optional[int] = None,
    delays: Optional[list[float]] = None,
):
    """Retry an LLM HTTP call with exponential backoff on transient errors.

    ``max_retries`` and ``delays`` are optional overrides used by the
    failover loop to fail fast on the current provider when fallbacks
    are configured. When omitted, the historical defaults
    (``MAX_RETRIES`` / ``RETRY_DELAYS``) are used so direct callers see
    no behaviour change.

    On HTTP 429 responses that carry a ``Retry-After`` header we honour
    the upstream hint instead of blindly sleeping the static backoff —
    but only if the requested wait fits inside
    ``RETRY_AFTER_MAX_INLINE_SLEEP``. Anything longer is re-raised so
    the caller (typically ``chat_with_failover``) can route around the
    rate-limited provider immediately.
    """
    eff_max = max_retries if max_retries is not None else MAX_RETRIES
    eff_delays = delays if delays is not None else RETRY_DELAYS
    if eff_max < 1:
        eff_max = 1

    for attempt in range(eff_max):
        try:
            return await coro_factory()
        except Exception as e:
            err_str = str(e).lower()
            retriable = any(code in err_str for code in _RETRIABLE_CODES)
            if not retriable or attempt == eff_max - 1:
                raise

            base_delay = eff_delays[attempt] if attempt < len(eff_delays) else eff_delays[-1]
            sleep_for = float(base_delay)

            retry_after = parse_retry_after(e)
            if retry_after is not None:
                if retry_after > RETRY_AFTER_MAX_INLINE_SLEEP:
                    logger.warning(
                        "LLM call rate-limited; Retry-After=%.1fs exceeds "
                        "inline-sleep cap (%.1fs) — handing off to failover.",
                        retry_after, RETRY_AFTER_MAX_INLINE_SLEEP,
                    )
                    raise
                sleep_for = max(sleep_for, retry_after)

            logger.warning(
                "LLM call failed (attempt %d/%d): %s — retrying in %.2fs",
                attempt + 1, eff_max, e, sleep_for,
            )
            await asyncio.sleep(sleep_for)


class FailoverReason(str, Enum):
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    AUTH_PERMANENT = "auth_permanent"
    BILLING = "billing"
    MODEL_NOT_FOUND = "model_not_found"
    CONTEXT_OVERFLOW = "context_overflow"
    TIMEOUT = "timeout"
    OVERLOADED = "overloaded"
    UNKNOWN = "unknown"
    # Catalog descriptor exists but the runtime has no adapter
    # wired — used for failover chip text so the UI can say
    # "skipped <provider>: not supported by this build" instead
    # of leaving the chip blank.
    NOT_SUPPORTED = "not_supported"
    # The candidate is in active circuit-breaker cooldown after
    # consecutive failures; surfaced in the failover chip so the
    # user can tell the difference between "we never tried it" and
    # "it failed and is parked".
    COOLDOWN = "cooldown"


def classify_error(error: Exception) -> FailoverReason:
    """Classify an LLM error into a failover reason for routing decisions."""
    err_str = str(error).lower()
    status = getattr(error, "status_code", 0) or 0
    if hasattr(error, "response"):
        status = getattr(error.response, "status_code", status) or status

    if status == 429 or "rate" in err_str or "quota" in err_str:
        return FailoverReason.RATE_LIMIT
    # 401/403 with an explicit "invalid" or "incorrect" API-key message is a
    # permanent-while-current-key-is-in-place failure. Promote so the cooldown
    # tracker keeps the broken provider out of rotation for 24 h — otherwise
    # we'd probe it every 30 s and the user would see an error log stream.
    hard_key = (
        "invalid api key" in err_str
        or "incorrect api key" in err_str
        or "invalid_api_key" in err_str
        or "api key not valid" in err_str
    )
    if status in (401, 403) and hard_key:
        return FailoverReason.AUTH_PERMANENT
    if status in (401, 403) or "unauthorized" in err_str or "invalid api key" in err_str:
        return FailoverReason.AUTH
    if "billing" in err_str or "payment" in err_str or "insufficient" in err_str:
        return FailoverReason.BILLING
    if status == 404 or ("model" in err_str and "not found" in err_str):
        return FailoverReason.MODEL_NOT_FOUND
    if "context" in err_str and ("length" in err_str or "overflow" in err_str or "too long" in err_str):
        return FailoverReason.CONTEXT_OVERFLOW
    if "timeout" in err_str or status == 408 or "timed out" in err_str:
        return FailoverReason.TIMEOUT
    if status in (500, 502, 503) or "overloaded" in err_str or "server error" in err_str:
        return FailoverReason.OVERLOADED
    return FailoverReason.UNKNOWN


def _describe_http_status_error(error: httpx.HTTPStatusError) -> str:
    """Return a safe, structured summary for an HTTP status failure."""
    status = getattr(error.response, "status_code", "unknown")
    try:
        payload = error.response.json()
    except Exception:
        payload = {}

    detail = ""
    if isinstance(payload, dict):
        err = payload.get("error", payload)
        if isinstance(err, dict):
            parts: list[str] = []
            if err.get("type"):
                parts.append(str(err["type"]))
            if err.get("code"):
                parts.append(f"code={err['code']}")
            if err.get("param"):
                parts.append(f"param={err['param']}")
            msg = str(err.get("message", "")).strip()
            if parts and msg:
                detail = f"{', '.join(parts)}: {msg}"
            elif parts:
                detail = ", ".join(parts)
            elif msg:
                detail = msg

    if not detail:
        try:
            detail = (error.response.text or "").strip()[:500]
        except Exception:
            detail = str(error)

    if detail:
        return f"HTTP {status} — {detail}"
    return f"HTTP {status}"


def _describe_error(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return _describe_http_status_error(error)
    return str(error)


def _chat_completions_model_guard(provider: str, model: str) -> str:
    """Return a guardrail error string for non-chat model classes.

    Empty string means "no guard hit".
    """
    if not provider or not model:
        return ""
    try:
        from providers.model_classes import classify
        model_class = classify(provider, model)
    except Exception:
        return ""

    incompatible = {"completion-only", "embedding", "audio", "image", "video", "realtime"}
    if model_class in incompatible:
        return (
            f"Model {model!r} for provider {provider!r} is classified as "
            f"{model_class!r} and is not valid for chat completions. "
            "Pick a chat or reasoning model in Settings -> Providers."
        )
    return ""


class ProviderCooldownTracker:
    """Tracks per-provider cooldown state for failover decisions."""

    _COOLDOWN_MAP: dict[FailoverReason, int] = {
        FailoverReason.RATE_LIMIT: 60,
        FailoverReason.AUTH: 300,
        FailoverReason.AUTH_PERMANENT: 86400,
        FailoverReason.BILLING: 3600,
        FailoverReason.OVERLOADED: 30,
        FailoverReason.TIMEOUT: 15,
    }
    _PROBE_INTERVAL = 30.0

    def __init__(self, storage_path: str | None = None):
        self._cooldowns: dict[str, float] = {}
        self._last_probe: dict[str, float] = {}
        self._storage_path = str(storage_path or "").strip()
        self._load_state()

    def _load_state(self) -> None:
        if not self._storage_path:
            return
        path = Path(self._storage_path)
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("cooldown state load skipped (%s): %s", path, exc)
            return

        now = time.time()
        cool_raw = payload.get("cooldowns", {}) if isinstance(payload, dict) else {}
        probe_raw = payload.get("last_probe", {}) if isinstance(payload, dict) else {}

        if isinstance(cool_raw, dict):
            for provider, until in cool_raw.items():
                try:
                    ts = float(until)
                except Exception:
                    continue
                if ts > now:
                    self._cooldowns[str(provider)] = ts
        if isinstance(probe_raw, dict):
            for provider, last in probe_raw.items():
                try:
                    ts = float(last)
                except Exception:
                    continue
                if ts > 0:
                    self._last_probe[str(provider)] = ts

    def _persist_state(self) -> None:
        if not self._storage_path:
            return
        path = Path(self._storage_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            now = time.time()
            cooldowns = {
                provider: until
                for provider, until in self._cooldowns.items()
                if float(until) > now
            }
            # Keep probe timestamps only for providers we currently track in
            # cooldown; stale historical probes are not useful across restarts.
            probes = {
                provider: self._last_probe.get(provider, 0.0)
                for provider in cooldowns.keys()
                if self._last_probe.get(provider, 0.0) > 0.0
            }
            payload = {
                "version": 1,
                "saved_at": now,
                "cooldowns": cooldowns,
                "last_probe": probes,
            }
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            tmp_path.replace(path)
        except Exception as exc:
            logger.debug("cooldown state persist skipped (%s): %s", path, exc)

    def record_failure(
        self,
        provider: str,
        reason: FailoverReason,
        retry_after: Optional[float] = None,
    ):
        """Record a provider failure and start the cooldown clock.

        When the upstream supplies an explicit ``Retry-After`` hint
        (parsed via :func:`parse_retry_after`), it overrides the
        reason-based default for rate-limit / overloaded / timeout
        failures. The hint is clamped to
        ``RETRY_AFTER_MAX_COOLDOWN`` so a misconfigured proxy can't
        sideline a provider for a year.
        """
        cooldown_seconds = float(self._COOLDOWN_MAP.get(reason, 10))
        if retry_after is not None and reason in (
            FailoverReason.RATE_LIMIT,
            FailoverReason.OVERLOADED,
            FailoverReason.TIMEOUT,
        ):
            hint = max(0.0, min(float(retry_after), RETRY_AFTER_MAX_COOLDOWN))
            cooldown_seconds = max(cooldown_seconds, hint)
        self._cooldowns[provider] = time.time() + cooldown_seconds
        self._persist_state()

    def is_available(self, provider: str) -> bool:
        return time.time() >= self._cooldowns.get(provider, 0)

    def should_probe(self, provider: str) -> bool:
        if self.is_available(provider):
            return True
        last = self._last_probe.get(provider, 0)
        if time.time() - last >= self._PROBE_INTERVAL:
            self._last_probe[provider] = time.time()
            self._persist_state()
            return True
        return False

    def record_success(self, provider: str):
        self._cooldowns.pop(provider, None)
        self._last_probe.pop(provider, None)
        self._persist_state()


__all__ = [
    "MAX_RETRIES",
    "RETRY_DELAYS",
    "RETRY_AFTER_MAX_INLINE_SLEEP",
    "RETRY_AFTER_MAX_COOLDOWN",
    "_RETRIABLE_CODES",
    "_SSE_KEEPALIVE_PREFIXES",
    "_retry_llm_call",
    "parse_retry_after",
    "FailoverReason",
    "classify_error",
    "_describe_http_status_error",
    "_describe_error",
    "_chat_completions_model_guard",
    "ProviderCooldownTracker",
]
