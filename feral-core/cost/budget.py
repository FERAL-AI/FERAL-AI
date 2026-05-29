"""Async-safe per-call-site LLM cost budgeting with persistent rollups."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from cost.pricing import ModelPricing, compute_token_cost
from cost import telemetry as cost_telemetry


class BudgetExceeded(Exception):
    """Raised when a call site exceeds its configured spend cap."""

    def __init__(
        self,
        *,
        call_site: str,
        cap_dollars: float,
        current_dollars: float,
        window: str,
        reset_at: float,
    ) -> None:
        self.call_site = call_site
        self.cap_dollars = cap_dollars
        self.current_dollars = current_dollars
        self.window = window
        self.reset_at = reset_at
        super().__init__(
            f"budget exceeded for {call_site}: "
            f"${current_dollars:.6f} / ${cap_dollars:.6f} "
            f"({window}, resets at {reset_at:.0f})"
        )

DEFAULT_COST_SETTINGS: dict[str, Any] = {
    "cost": {
        "enabled": True,
        # v2026.5.47 — the cost budget ships UNLIMITED by default.
        # The operator opts in to a cap by typing a number into
        # Settings → Cost (persisted as
        # ``cost.<subsystem>.per_hour_usd``) or by setting a global
        # via ``cost.global_per_hour_usd`` / ``cost.global_per_day_usd``.
        # Until they do, ``_cap_for`` returns ``None`` for every
        # call_site/window and ``BudgetLoopGuard.allow()`` always
        # passes — no ``cost_cap_hit`` frame, no ``BudgetExceeded``,
        # no yellow banner. Previously this file shipped hardcoded
        # dollar caps (``screen_loop: $0.10/hr``, ``chat: $5/hr``,
        # ``global_per_hour_usd: $5``) that starved background
        # subsystems even when the operator never asked for a limit.
        #
        # ``per_call_site_caps`` is kept as a registry of known
        # subsystems so introspection surfaces (the CLI doctor probe,
        # the Settings UI) can list every wired call-site without
        # each subsystem having to register itself. An empty
        # per-site dict here means "no cap configured → unlimited";
        # a configured cap is written by the Settings UI as the flat
        # ``cost.<subsystem>.per_hour_usd`` key, which
        # :meth:`_cap_for` prefers over the legacy nested shape.
        "per_call_site_caps": {
            "screen_loop": {},
            "proactive": {},
            "routing": {},
            "chat": {},
            "vision": {},
            "embedding": {},
            "learner": {},
            "compaction": {},
        },
    }
}

_GLOBAL_SITE = "__global__"
_VALID_WINDOWS = frozenset({"hour", "day", "month"})


def _feral_home() -> Path:
    return Path(os.environ.get("FERAL_HOME", Path.home() / ".feral")).expanduser()


def _default_db_path() -> Path:
    return _feral_home() / "cost.db"


def _utc_dt(ts: float | None = None) -> datetime:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)


def window_start(window: str, ts: float | None = None) -> float:
    dt = _utc_dt(ts)
    if window == "hour":
        return dt.replace(minute=0, second=0, microsecond=0).timestamp()
    if window == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if window == "month":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    raise ValueError(f"unsupported window: {window}")


def window_reset_at(window: str, ts: float | None = None) -> float:
    dt = _utc_dt(ts)
    if window == "hour":
        start = dt.replace(minute=0, second=0, microsecond=0)
        return (start.timestamp() + 3600.0)
    if window == "day":
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp() + 86400.0
    if window == "month":
        year, month = dt.year, dt.month
        if month == 12:
            next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)
        return next_month.timestamp()
    raise ValueError(f"unsupported window: {window}")


class CostBudget:
    """Track LLM spend per call site with hourly/daily/global caps."""

    def __init__(
        self,
        *,
        settings: dict[str, Any] | None = None,
        db_path: str | Path | None = None,
        pricing: ModelPricing | None = None,
        enabled: bool | None = None,
    ) -> None:
        merged = DEFAULT_COST_SETTINGS["cost"]
        if settings:
            cost_cfg = settings.get("cost", settings)
            merged = {**merged, **cost_cfg}
            if "per_call_site_caps" in cost_cfg:
                site_caps = dict(merged.get("per_call_site_caps") or {})
                site_caps.update(cost_cfg["per_call_site_caps"])
                merged["per_call_site_caps"] = site_caps

        self.enabled = merged.get("enabled", True) if enabled is None else enabled
        self._settings = merged
        self.db_path = Path(db_path) if db_path else _default_db_path()
        self.pricing = pricing or ModelPricing()
        self._lock = asyncio.Lock()
        self._conn: aiosqlite.Connection | None = None
        self._ready = False
        self._spend: dict[tuple[str, str], float] = {}
        self._override_caps: dict[tuple[str, str], float] = {}

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(self.db_path)
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cost_rollup (
                    call_site TEXT NOT NULL,
                    window TEXT NOT NULL,
                    window_start REAL NOT NULL,
                    dollars REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (call_site, window, window_start)
                )
                """
            )
            await self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cost_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    call_site TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    dollars REAL NOT NULL
                )
                """
            )
            await self._conn.commit()
            await self._load_rollups()
            self._ready = True

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None
            self._ready = False

    async def _load_rollups(self) -> None:
        assert self._conn is not None
        now = time.time()
        self._spend.clear()
        for window in ("hour", "day", "month"):
            start = window_start(window, now)
            async with self._conn.execute(
                "SELECT call_site, dollars FROM cost_rollup WHERE window = ? AND window_start = ?",
                (window, start),
            ) as cursor:
                async for call_site, dollars in cursor:
                    self._spend[(call_site, window)] = float(dollars)

    def set_cap(self, call_site: str, window: str, cap_dollars: float) -> None:
        if window not in _VALID_WINDOWS:
            raise ValueError(f"unsupported window: {window}")
        self._override_caps[(call_site, window)] = float(cap_dollars)

    def _cap_for(self, call_site: str, window: str) -> float | None:
        key = (call_site, window)
        if key in self._override_caps:
            return self._override_caps[key]
        if window == "hour":
            if call_site == _GLOBAL_SITE:
                val = self._settings.get("global_per_hour_usd")
                return float(val) if val is not None else None
            # v2026.5.44 — schema tolerance. Operators historically
            # ended up with two shapes for per-call-site caps in
            # ``~/.feral/settings.json``:
            #   (a) flat:    ``cost.<site>.per_hour_usd`` (the shape
            #       the v2 Settings UI now writes)
            #   (b) nested:  ``cost.per_call_site_caps.<site>.per_hour_usd``
            #       (legacy DEFAULT_COST_SETTINGS shape, still used by
            #       a few internal callers and existing tests)
            # We honour either, with flat winning when both are
            # present. Without this, an operator who typed "$20/hour"
            # in Settings → Cost would silently see the factory
            # default of $0.10 trip the ScreenLoop banner.
            flat_cfg = self._settings.get(call_site)
            if isinstance(flat_cfg, dict):
                val = flat_cfg.get("per_hour_usd")
                if val is not None:
                    return float(val)
            site_cfg = (self._settings.get("per_call_site_caps") or {}).get(call_site) or {}
            val = site_cfg.get("per_hour_usd")
            return float(val) if val is not None else None
        if window == "day" and call_site == _GLOBAL_SITE:
            val = self._settings.get("global_per_day_usd")
            return float(val) if val is not None else None
        return None

    def reload_from_settings(
        self, settings: dict[str, Any] | None = None
    ) -> None:
        """Re-read cost caps from the current ``settings`` dict (or
        from ``config.loader.load_settings()`` when ``settings`` is
        ``None``) and update ``self._settings`` in place.

        v2026.5.44 — the brain instantiates ``CostBudget`` once at
        boot via ``api.state.BrainState`` with the settings snapshot
        at that moment. Without this method, an operator who raised
        ``cost.screen_loop.per_hour_usd`` to $20 in Settings → Cost
        would still see the in-memory cap stuck at the factory
        default of $0.10 until the next process restart, and the
        yellow ``cost_cap_hit`` banner would keep tripping. The
        ``/api/config/update`` route now invokes this after persisting
        any ``cost.*`` write so the caps take effect immediately
        without a brain restart. Override caps set via
        :meth:`set_cap` are preserved (they're considered authoritative
        in-memory operator overrides).
        """
        if settings is None:
            try:
                from config.loader import load_settings as _load_settings

                settings = _load_settings() or {}
            except Exception:
                settings = {}
        merged = dict(DEFAULT_COST_SETTINGS["cost"])
        cost_cfg = settings.get("cost", settings) if isinstance(settings, dict) else {}
        if isinstance(cost_cfg, dict):
            merged.update(cost_cfg)
            if "per_call_site_caps" in cost_cfg and isinstance(
                cost_cfg["per_call_site_caps"], dict
            ):
                site_caps = dict(merged.get("per_call_site_caps") or {})
                site_caps.update(cost_cfg["per_call_site_caps"])
                merged["per_call_site_caps"] = site_caps
        self._settings = merged
        if "enabled" in merged:
            self.enabled = bool(merged.get("enabled", True))

    def _active_caps(self, call_site: str) -> list[tuple[str, str, float]]:
        caps: list[tuple[str, str, float]] = []
        hour_cap = self._cap_for(call_site, "hour")
        if hour_cap is not None:
            caps.append((call_site, "hour", hour_cap))
        global_hour = self._cap_for(_GLOBAL_SITE, "hour")
        if global_hour is not None:
            caps.append((_GLOBAL_SITE, "hour", global_hour))
        global_day = self._cap_for(_GLOBAL_SITE, "day")
        if global_day is not None:
            caps.append((_GLOBAL_SITE, "day", global_day))
        return caps

    def _get_spend(self, call_site: str, window: str) -> float:
        return float(self._spend.get((call_site, window), 0.0))

    def current_spend(
        self,
        call_site: str | None = None,
        window: str = "hour",
    ) -> float:
        if window not in _VALID_WINDOWS:
            raise ValueError(f"unsupported window: {window}")
        site = call_site or _GLOBAL_SITE
        if call_site is None and window == "hour":
            return self._get_spend(_GLOBAL_SITE, "hour")
        if call_site is None and window == "day":
            return self._get_spend(_GLOBAL_SITE, "day")
        return self._get_spend(site, window)

    def _estimate_cost(self, model: str, estimated_max_tokens: int) -> float:
        rates = self.pricing.lookup(model)
        tokens = max(0, int(estimated_max_tokens))
        return (tokens / 1000.0) * rates["output"]

    def check_and_reserve(
        self,
        call_site: str,
        model: str,
        estimated_max_tokens: int,
    ) -> bool:
        if not self.enabled:
            return True
        est = self._estimate_cost(model, estimated_max_tokens)
        for site, window, cap in self._active_caps(call_site):
            projected = self._get_spend(site, window) + est
            if projected > cap + 1e-12:
                cost_telemetry.record_cap_hit(call_site, window)
                return False
        return True

    def _raise_budget_exceeded(
        self,
        call_site: str,
        window: str,
        cap: float,
        current: float,
    ) -> None:
        cost_telemetry.record_cap_hit(call_site, window)
        raise BudgetExceeded(
            call_site=call_site,
            cap_dollars=cap,
            current_dollars=current,
            window=window,
            reset_at=window_reset_at(window),
        )

    async def record_usage(
        self,
        call_site: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        reasoning_tokens: int = 0,
    ) -> float:
        await self.ensure_ready()
        if not self.enabled:
            return 0.0

        dollars, rates = compute_token_cost(
            self.pricing,
            model,
            prompt_tokens,
            completion_tokens,
            reasoning_tokens,
        )
        prompt = max(0, int(prompt_tokens))
        completion = max(0, int(completion_tokens))
        reasoning = max(0, int(reasoning_tokens))
        input_dollars = (prompt / 1000.0) * rates["input"]
        output_dollars = ((completion + reasoning) / 1000.0) * rates["output"]

        async with self._lock:
            for site, window, cap in self._active_caps(call_site):
                projected = self._get_spend(site, window) + dollars
                if projected > cap + 1e-12:
                    self._raise_budget_exceeded(site, window, cap, projected)

            for window in ("hour", "day", "month"):
                for site in (call_site, _GLOBAL_SITE):
                    key = (site, window)
                    self._spend[key] = self._get_spend(site, window) + dollars

            assert self._conn is not None
            ts = time.time()
            await self._conn.execute(
                """
                INSERT INTO cost_events
                    (ts, call_site, model, prompt_tokens, completion_tokens, reasoning_tokens, dollars)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, call_site, model, prompt, completion, reasoning, dollars),
            )
            for window in ("hour", "day", "month"):
                start = window_start(window, ts)
                for site in (call_site, _GLOBAL_SITE):
                    await self._conn.execute(
                        """
                        INSERT INTO cost_rollup (call_site, window, window_start, dollars)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(call_site, window, window_start) DO UPDATE SET
                            dollars = cost_rollup.dollars + excluded.dollars
                        """,
                        (site, window, start, dollars),
                    )
            await self._conn.commit()

        cost_telemetry.record_call(call_site, model)
        cost_telemetry.record_dollars(
            call_site,
            model,
            input_dollars=input_dollars,
            output_dollars=output_dollars,
        )
        return dollars

    async def reset(self, call_site: str | None = None) -> None:
        await self.ensure_ready()
        async with self._lock:
            assert self._conn is not None
            if call_site is None:
                await self._conn.execute("DELETE FROM cost_rollup")
                await self._conn.execute("DELETE FROM cost_events")
                self._spend.clear()
            else:
                await self._conn.execute(
                    "DELETE FROM cost_rollup WHERE call_site = ? OR call_site = ?",
                    (call_site, _GLOBAL_SITE),
                )
                await self._conn.execute(
                    "DELETE FROM cost_events WHERE call_site = ?",
                    (call_site,),
                )
                for window in ("hour", "day", "month"):
                    self._spend.pop((call_site, window), None)
            await self._conn.commit()
            await self._load_rollups()
