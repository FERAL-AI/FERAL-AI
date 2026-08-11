"""Background-loop cost guard.

audit-r14 (S6 release gate) — ambient/background subsystems that issue
LLM calls must:

1. ``check_and_reserve`` against the configured per-call-site cap
   BEFORE issuing the LLM call. If the projected spend exceeds the
   cap, skip the call AND emit a structured warning + WebSocket frame
   so the operator can see (and so Wave 3 Lane 12 can render the
   yellow chat banner).
2. ``record_usage`` AFTER the call returns so the rollup reflects
   real (not estimated) spend.
3. On a true ``BudgetExceeded`` (rollup goes over after the call
   completes), surface it the same way.

Every subsystem (ScreenLoop, ProactiveEngine, Learner, CronService,
EmailWatcher, MQTTBridge) plugs in a :class:`BudgetLoopGuard` instance
constructed at brain boot. The shared helper enforces the same wire
contract for the cost-cap-hit WebSocket frame and the same structured
log line, so the Wave 3 UI banner can rely on one shape.

WebSocket frame the guard emits (when a broadcaster is wired):

    {
      "type": "cost_cap_hit",
      "call_site": "screen_loop",
      "subsystem": "ScreenLoop",
      "cap_dollars": 0.10,
      "current_dollars": 0.10,
      "window": "hour",
      "reset_at": 1738012345.0,
      "paused_until": 1738012345.0,
      "ts": 1738012345.0
    }
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from cost.budget import BudgetExceeded, CostBudget

logger = logging.getLogger("feral.cost.loop_guard")


# Type aliases — the broadcaster is the same as ``BrainState.broadcast_event``.
EventBroadcaster = Callable[[str, dict[str, Any]], Awaitable[None]]


class BudgetLoopGuard:
    """Wrap a :class:`CostBudget` for one background subsystem.

    A subsystem creates one guard at startup with its call-site name
    (e.g. ``"screen_loop"``), the shared :class:`CostBudget`, and the
    WebSocket broadcaster supplied by ``BrainState``. Then every
    LLM-calling code path inside the subsystem wraps its work as:

        if not guard.allow(model="claude-sonnet-4-6", estimated_max_tokens=200):
            return  # paused / cap reached; emit already fired

        response = await llm.chat(...)
        await guard.record(
            model="claude-sonnet-4-6",
            prompt_tokens=p,
            completion_tokens=c,
        )

    The guard auto-pauses for the remainder of the current cap window
    when ``check_and_reserve`` rejects, so a hot loop won't spam the
    broadcaster with one frame per tick.
    """

    def __init__(
        self,
        *,
        call_site: str,
        subsystem: str,
        budget: Optional[CostBudget],
        broadcaster: Optional[EventBroadcaster] = None,
    ) -> None:
        if not call_site or not isinstance(call_site, str):
            raise ValueError("call_site must be a non-empty string")
        if not subsystem or not isinstance(subsystem, str):
            raise ValueError("subsystem must be a non-empty string")
        self._call_site = call_site
        self._subsystem = subsystem
        self._budget = budget
        self._broadcaster = broadcaster
        self._paused_until: float = 0.0
        self._last_emit: float = 0.0
        # Re-emit at most once per minute even if the loop keeps ticking;
        # the banner is sticky on the client side so we don't need a
        # frame per tick to keep it visible.
        self._emit_throttle_s: float = 60.0
        # AUDIT-FIXES F-06. Strong references to the fire-and-forget
        # cost_cap_hit broadcasts scheduled in ``_emit_cap_hit``. The loop
        # keeps tasks only weakly, so a collected task means the user is
        # never told their budget stopped the run, and the throttle above
        # then suppresses the next attempt for a full minute.
        self._bg_tasks: set[asyncio.Task] = set()

    @property
    def call_site(self) -> str:
        return self._call_site

    @property
    def is_paused(self) -> bool:
        return self._paused_until > time.time()

    @property
    def paused_until(self) -> float:
        return self._paused_until

    def reset(self) -> None:
        """Drop the pause state (used by tests and by operator overrides
        from the Settings UI raising the cap)."""
        self._paused_until = 0.0
        self._last_emit = 0.0

    def allow(self, *, model: str, estimated_max_tokens: int) -> bool:
        """Return ``True`` if the subsystem should issue the LLM call.

        v2026.5.47 — the cost budget is UNLIMITED by default. When
        no operator-configured cap exists for ``call_site`` (and no
        global per-hour / per-day cap is set), ``CostBudget``
        publishes an empty ``_active_caps()`` and
        ``check_and_reserve`` returns ``True`` without recording
        anything. This guard therefore always allows the call, never
        enters a pause window, and never broadcasts a
        ``cost_cap_hit`` frame. Numeric caps — once typed into
        Settings → Cost or set via ``cost.global_per_hour_usd`` —
        are enforced exactly as before.

        Returns ``False`` when either:
          * the guard is in a pause window from a prior cap hit, OR
          * the projected cost would exceed an active (operator-set)
            cap.
        On rejection the guard logs + (best-effort) broadcasts a
        ``cost_cap_hit`` frame so the UI banner appears.
        """
        if self._budget is None:
            return True

        now = time.time()
        if self._paused_until > now:
            # Still in the cap window. Re-emit at most every
            # ``_emit_throttle_s`` so a 1Hz loop doesn't flood.
            self._maybe_reemit(now)
            return False

        if self._budget.check_and_reserve(
            call_site=self._call_site,
            model=model,
            estimated_max_tokens=estimated_max_tokens,
        ):
            return True

        # Estimate would overshoot. Pause for the remainder of the
        # tightest active window and emit.
        reset_at = self._next_reset()
        self._enter_pause(reset_at)
        self._emit_cap_hit(
            cap_dollars=self._tight_cap(),
            current_dollars=self._current_spend(),
            window=self._tight_window(),
            reset_at=reset_at,
        )
        return False

    async def record(
        self,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        reasoning_tokens: int = 0,
    ) -> bool:
        """Record post-call usage. Returns ``False`` (and pauses +
        emits) when the call pushed the rollup over a cap."""
        if self._budget is None:
            return True
        try:
            await self._budget.record_usage(
                call_site=self._call_site,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                reasoning_tokens=reasoning_tokens,
            )
        except BudgetExceeded as exc:
            self._enter_pause(exc.reset_at)
            self._emit_cap_hit(
                cap_dollars=exc.cap_dollars,
                current_dollars=exc.current_dollars,
                window=exc.window,
                reset_at=exc.reset_at,
            )
            return False
        return True

    # ── internals ────────────────────────────────────────────────────

    def _enter_pause(self, until: float) -> None:
        self._paused_until = max(self._paused_until, float(until))

    def _next_reset(self) -> float:
        """Resolve the earliest active cap window's reset timestamp."""
        from cost.budget import window_reset_at as _wra
        return _wra("hour")

    def _tight_window(self) -> str:
        return "hour"

    def _tight_cap(self) -> float:
        if self._budget is None:
            return 0.0
        cap = self._budget._cap_for(self._call_site, "hour")  # noqa: SLF001
        return float(cap) if cap is not None else 0.0

    def _current_spend(self) -> float:
        if self._budget is None:
            return 0.0
        return float(self._budget.current_spend(self._call_site, "hour"))

    def _maybe_reemit(self, now: float) -> None:
        if now - self._last_emit < self._emit_throttle_s:
            return
        self._emit_cap_hit(
            cap_dollars=self._tight_cap(),
            current_dollars=self._current_spend(),
            window=self._tight_window(),
            reset_at=self._paused_until,
        )

    def _emit_cap_hit(
        self,
        *,
        cap_dollars: float,
        current_dollars: float,
        window: str,
        reset_at: float,
    ) -> None:
        now = time.time()
        self._last_emit = now
        payload = {
            "type": "cost_cap_hit",
            "call_site": self._call_site,
            "subsystem": self._subsystem,
            "cap_dollars": cap_dollars,
            "current_dollars": current_dollars,
            "window": window,
            "reset_at": reset_at,
            "paused_until": self._paused_until,
            "ts": now,
        }
        logger.warning(
            "cost.cap_hit subsystem=%s call_site=%s cap=$%.4f spend=$%.4f "
            "window=%s reset_at=%.0f paused_until=%.0f",
            self._subsystem,
            self._call_site,
            cap_dollars,
            current_dollars,
            window,
            reset_at,
            self._paused_until,
        )
        if self._broadcaster is None:
            return
        try:
            result = self._broadcaster("cost_cap_hit", payload)
            if asyncio.iscoroutine(result):
                # Schedule on the running loop without blocking the
                # subsystem (the loop may not be the one the caller's
                # coroutine is in; create a task on whichever loop is
                # running here).
                try:
                    loop = asyncio.get_running_loop()
                    task = loop.create_task(result, name="cost-cap-broadcast")
                    self._bg_tasks.add(task)
                    task.add_done_callback(self._bg_tasks.discard)
                except RuntimeError:
                    # No running loop (sync caller, e.g. CronService);
                    # best-effort: schedule on a new event loop only
                    # if absolutely necessary, else log the payload.
                    logger.debug(
                        "cost.cap_hit broadcast skipped: no running loop"
                    )
        except Exception as exc:  # noqa: BLE001 — broadcast must never crash the loop
            logger.debug("cost.cap_hit broadcast failed: %s", exc)


def make_disabled_guard(call_site: str, subsystem: str) -> BudgetLoopGuard:
    """Construct a guard with no budget — every ``allow()`` returns
    True. Useful for tests and for subsystems whose host process
    has cost tracking turned off."""
    return BudgetLoopGuard(
        call_site=call_site, subsystem=subsystem, budget=None, broadcaster=None,
    )
