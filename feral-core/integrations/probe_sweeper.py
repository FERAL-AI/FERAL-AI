"""Something that actually runs the probes.

The gap this closes
===================
``security.probe`` has a registry of real liveness probes and
``integrations._probe_status`` has a cache their results are supposed to
land in. Nothing connected the two. Grep for ``probe_all`` or
``probe_connected`` across ``api/``, ``agents/`` and every background
task and you find no caller: the only in-brain writer of the cache was
``OAuthManager`` firing once after a token exchange. With a 60 second
TTL on that entry, every integration's ``connected`` property fell back
to "a token string exists" forever after, including for a token the
provider had already revoked.

So the badge said connected, the docstring in ``_probe_status`` claimed
"every integration's ``connected`` uses ``probe(provider).ok``", the
Settings page told the user status "comes from a real backend probe",
and all three were describing a code path that ran once per OAuth
callback and never again.

This module is the missing runner. Two entry points:

* :func:`sweep_once`, probe providers now and write the results into
  the cache. ``/api/integrations`` awaits it for providers whose entry
  has gone stale, and ``POST /api/integrations/refresh`` forces it.
* :func:`ensure_started`, a periodic loop so the cache stays warm for
  readers that never go through those routes (the dashboard endpoint,
  the orchestrator's tool-availability check). The interval defaults to
  under the cache TTL, because a sweep slower than the TTL leaves gaps
  where ``connected`` silently reverts to token presence.

Cadence and cost. Probes are read-only HTTP calls and a provider with no
credential resolves without touching the network at all
(``_http_probe`` short-circuits on ``had_key=False``), so a sweep on a
brain with two integrations configured is two requests. Set
``FERAL_PROBE_SWEEP_SECONDS=0`` to disable the periodic loop; explicit
refreshes keep working.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from integrations import _probe_status

logger = logging.getLogger("feral.integrations.probe_sweeper")

# Under ``_probe_status.STATUS_TTL_SECONDS`` (60s) on purpose: a sweep
# that runs exactly at the TTL leaves a window in which every badge falls
# back to token presence, which is the bug this module exists to kill.
DEFAULT_SWEEP_SECONDS = 45.0
ENV_SWEEP_SECONDS = "FERAL_PROBE_SWEEP_SECONDS"

# Probes are independent; run them concurrently but do not open thirty
# sockets at once on a home network.
MAX_CONCURRENCY = 8

_task: Optional[asyncio.Task] = None


def sweep_interval_seconds() -> float:
    """Periodic sweep interval. ``0`` (or negative) disables the loop."""
    raw = (os.environ.get(ENV_SWEEP_SECONDS) or "").strip()
    if not raw:
        return DEFAULT_SWEEP_SECONDS
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number; using the %.0fs default.",
            ENV_SWEEP_SECONDS, raw, DEFAULT_SWEEP_SECONDS,
        )
        return DEFAULT_SWEEP_SECONDS


def known_provider_ids() -> list[str]:
    """Provider ids with a registered probe, or ``[]`` when unavailable."""
    try:
        from security.probe import registered_probe_ids
    except Exception as exc:  # pragma: no cover, import-time failure
        logger.debug("probe registry unavailable: %s", exc)
        return []
    try:
        return list(registered_probe_ids())
    except Exception as exc:  # pragma: no cover, defensive
        logger.warning("registered_probe_ids() raised: %s", exc)
        return []


async def sweep_once(
    *,
    vault=None,
    provider_ids: Optional[list[str]] = None,
    only_stale: bool = False,
) -> dict[str, Optional[bool]]:
    """Probe providers and record the results.

    ``only_stale`` skips providers whose cached verdict is still inside
    the TTL, which is what a page load wants: fresh truth without a
    burst of duplicate requests when three panels render at once.

    Returns ``{provider_id: ok}``. A ``None`` value means the probe
    produced no verdict (no probe registered, or it raised) and the
    caller should keep whatever fallback it already had.
    """
    ids = list(provider_ids) if provider_ids is not None else known_provider_ids()
    if only_stale:
        ids = [pid for pid in ids if _probe_status.latest(pid) is None]
    if not ids:
        return {}

    gate = asyncio.Semaphore(MAX_CONCURRENCY)

    async def _one(provider_id: str) -> tuple[str, Optional[bool]]:
        async with gate:
            try:
                return provider_id, await _probe_status.refresh(
                    provider_id, vault=vault,
                )
            except Exception as exc:  # pragma: no cover, defensive
                logger.warning("probe sweep for %s raised: %s", provider_id, exc)
                return provider_id, None

    results = await asyncio.gather(*(_one(pid) for pid in ids))
    return dict(results)


async def _loop(vault) -> None:
    interval = sweep_interval_seconds()
    logger.info("Probe sweeper started (every %.0fs)", interval)
    try:
        while True:
            # Sleep first. Every caller of ``ensure_started`` today has
            # just swept (that is what made it want a sweeper), so
            # probing immediately would double the request burst on a
            # page load for no extra freshness.
            await asyncio.sleep(interval)
            try:
                await sweep_once(vault=vault)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover, defensive
                logger.warning("probe sweep failed: %s", exc)
    except asyncio.CancelledError:
        logger.info("Probe sweeper stopped")
        raise


def ensure_started(*, vault=None, register=None) -> bool:
    """Start the periodic sweep if it is not already running.

    Idempotent, and safe to call from a request handler: the first caller
    starts the loop, everyone else is a no-op. Returns True when a loop
    is running afterwards.

    ``register`` is the brain's background-task registry
    (``BrainState.register_background_task``) so shutdown can cancel the
    loop instead of leaving a pending task behind.

    Callable from a route because there is no boot hook this module is
    allowed to touch; ``api/server.py`` should start it at brain startup
    so the cache is warm before the first page load rather than one sweep
    behind it.
    """
    global _task
    if _task is not None and not _task.done():
        return True
    if sweep_interval_seconds() <= 0:
        logger.debug("Probe sweeper disabled via %s", ENV_SWEEP_SECONDS)
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("ensure_started called with no running loop; skipping")
        return False
    _task = loop.create_task(_loop(vault))
    if register is not None:
        try:
            register(_task)
        except Exception as exc:  # pragma: no cover, defensive
            logger.debug("probe sweeper task registration failed: %s", exc)
    return True


def is_running() -> bool:
    return _task is not None and not _task.done()


async def stop() -> None:
    """Cancel the periodic sweep (shutdown, tests)."""
    global _task
    task, _task = _task, None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # pragma: no cover, defensive
        logger.debug("probe sweeper shutdown raised: %s", exc)
