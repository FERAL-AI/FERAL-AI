"""
Integration ↔ probe-cache bridge.

The ``security.probe`` registry is async-first because real liveness
checks must do an HTTP round-trip. Most integration ``connected``
properties are sync, however, and changing that signature would ripple
through every caller (skill executor, channel manager, observability
endpoints, the WebUI etc.).

This module is a thin in-process cache of the most-recent probe result
per provider. Writers (the OAuth manager after a successful callback,
the probe-all sweeper, operator-initiated "Refresh status" actions in
the WebUI) call :func:`mark_probe_result` to record the latest result.
Readers (integrations' ``connected`` property) call
:func:`is_connected_cached` to consult it synchronously.

Semantics:

* When a probe has run within :data:`STATUS_TTL_SECONDS` we trust its
  ``ok`` value verbatim — the integration's ``connected`` reflects a
  real round-trip, not just token presence. **This is the contract
  finding 19 calls out: "Every integration's ``connected`` property
  uses ``probe(provider).ok``".**
* When there's no recent probe result, the cache returns ``None`` and
  the integration falls back to its token-presence baseline so we
  don't downgrade a known-good connection just because nobody's
  pinged the provider in a minute.
* Failed probes (``ok=False``) override token presence — if Whoop
  rejects our token with 401, ``connected`` must report False even
  when the access_token is still locally stored, so the LLM doesn't
  waste a tool call.

The cache is process-local; no persistence. Cross-restart probe-state
is rebuilt by the next probe call (which is cheap — see
``security.probe.PROBE_CACHE_TTL_SECONDS`` for cadence).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("feral.integrations.probe_status")


STATUS_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class _Status:
    ok: bool
    recorded_at: float
    reason: str
    detail: str


_STATUS: dict[str, _Status] = {}


def mark_probe_result(
    provider_id: str,
    *,
    ok: bool,
    reason: str = "",
    detail: str = "",
) -> None:
    """Record the most-recent probe outcome for ``provider_id``."""
    if not provider_id:
        return
    _STATUS[provider_id] = _Status(
        ok=bool(ok),
        recorded_at=time.time(),
        reason=reason,
        detail=detail,
    )


def latest(provider_id: str) -> Optional[bool]:
    """Return the cached ``ok`` value, or ``None`` if no recent result."""
    if not provider_id:
        return None
    entry = _STATUS.get(provider_id)
    if entry is None:
        return None
    if time.time() - entry.recorded_at > STATUS_TTL_SECONDS:
        return None
    return entry.ok


def is_connected_cached(
    provider_id: str,
    *,
    fallback: bool,
) -> bool:
    """Resolve the integration's ``connected`` flag.

    * Recent probe → return its ``ok`` value (overrides ``fallback``).
    * No recent probe → return the operator-supplied ``fallback``
      (typically the existing token-presence check).
    """
    cached = latest(provider_id)
    if cached is None:
        return bool(fallback)
    return cached


def clear() -> None:
    """Reset the in-process cache (tests)."""
    _STATUS.clear()


def snapshot() -> dict[str, dict]:
    """Read-only debug view of the cache (status endpoints)."""
    now = time.time()
    return {
        pid: {
            "ok": s.ok,
            "age_s": round(now - s.recorded_at, 2),
            "reason": s.reason,
            "detail": s.detail,
        }
        for pid, s in _STATUS.items()
    }


async def refresh(provider_id: str, *, vault=None) -> Optional[bool]:
    """Force-run the registered probe and update the cache. Returns the
    fresh ``ok`` value (or ``None`` when no probe is registered for
    this provider so the caller can decide what fallback to apply)."""
    try:
        from security.probe import probe as _probe, registered_probe_ids
    except Exception as exc:
        logger.debug("probe_status.refresh: import failed: %s", exc)
        return None
    if provider_id not in registered_probe_ids():
        return None
    try:
        result = await _probe(provider_id, vault=vault, force=True)
    except Exception as exc:
        logger.warning("probe_status.refresh(%s) raised: %s", provider_id, exc)
        return None
    mark_probe_result(
        provider_id,
        ok=bool(result.ok),
        reason=result.reason or "",
        detail=result.detail or "",
    )
    return bool(result.ok)
