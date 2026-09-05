"""
FERAL Proactive Intelligence Engine
======================================
The #1 differentiator: FERAL doesn't wait for commands — it observes
ambient context (screen, health, calendar, memory) and proactively
initiates when it has something valuable to say.

This is NOT a simple cron. It's a context-aware decision engine with:
  - Priority tiers (critical > important > suggestion > ambient)
  - Cooldown per trigger type (don't nag)
  - User preference learning (track dismiss rates)
  - Time-of-day awareness
  - LLM evaluation for complex triggers

Architecture:
  PerceptionFrame + MemoryStore + Clock
    → TriggerEvaluator (rules + LLM hybrid)
    → ProactiveMessage
    → Delivery (voice / toast / SDUI card)
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Awaitable, Optional

from agents.token_estimate import estimate_tokens


def _briefing_state_path() -> Path:
    """Where the delivered-briefing date lives.

    A small JSON file in FERAL_HOME, matching how the rest of the brain
    keeps this kind of flag (llm_provider_cooldowns.json,
    update-check.json). It is one date; it does not want a table.
    """
    home = os.getenv("FERAL_HOME") or os.path.join(os.path.expanduser("~"), ".feral")
    return Path(home) / "proactive_state.json"


def _read_briefing_date(path: "Path") -> str:
    """The last date a briefing went out, or "" when unknown.

    Every failure reads as "unknown", which costs at most one extra
    briefing. Refusing to start the proactive engine because a cache
    file is unreadable would be the worse trade.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh).get("briefing_delivered_on")
        return value if isinstance(value, str) else ""
    except Exception:
        return ""


def _write_briefing_date(path: "Path", day: str) -> None:
    """Record the delivered date. Never raises into the caller.

    Written through a temporary file and replaced, so a crash mid-write
    cannot leave a truncated file that reads back as "unknown" and
    re-delivers the briefing, which is the failure this whole change
    exists to stop.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"briefing_delivered_on": day}, fh)
        os.replace(tmp, path)
    except Exception:
        logger.debug("could not persist the briefing date to %s", path, exc_info=True)


logger = logging.getLogger("feral.proactive")


# Module-level constant — single source of truth for "is this
# perception-frame sample fresh enough to drive a real-time alert?"
# Operator report 2026-05-09 (rounds 1-3): without this gate, stale
# Apple HealthKit samples (HR=115 from a workout 4h earlier) fired
# `hr_elevated`, `spo2_low`, AND `baseline_hr` as if they were
# real-time. Two minutes is enough for genuine W300 / HealthKit polls
# but short enough to drop a HealthKit "last recorded" reading from
# hours ago. Promoted to module level so all health-trigger sections
# of `_evaluate` consult the same window.
_FRESH_WINDOW_S = 120.0

# Cognitive load at which the health triggers speak up. Deliberately the
# SAME 0.7 that SomaticEngine.get_behavioral_policy treats as high load,
# so "the agent went quiet and started answering in two sentences" and
# "the agent said something about your heart rate" describe one state
# rather than two thresholds that drift apart.
SOMATIC_LOAD_ALERT = 0.7


class Priority(Enum):
    CRITICAL = 4    # health emergency, urgent calendar
    IMPORTANT = 3   # meeting in 10 min, stress detected
    SUGGESTION = 2  # "want to take a break?", "you might like..."
    AMBIENT = 1     # weather update, daily summary


@dataclass
class ProactiveMessage:
    trigger_id: str
    priority: Priority
    title: str
    body: str
    action: str = ""           # optional action button label
    action_payload: dict = field(default_factory=dict)
    sdui: dict | None = None   # optional GenUI card
    voice_text: str = ""       # what to say aloud
    timestamp: float = field(default_factory=time.time)


@dataclass
class TriggerState:
    last_fired: float = 0.0
    fire_count: int = 0
    dismiss_count: int = 0
    cooldown_s: float = 300.0  # 5 min default


class ProactiveEngine:
    """Continuously evaluates ambient context and fires proactive messages.

    Usage:
        engine = ProactiveEngine(perception, memory)
        engine.on_message(my_callback)
        await engine.start()
    """

    def __init__(
        self,
        perception=None,
        memory=None,
        orchestrator=None,
        llm=None,
        calendar=None,
        health_aggregator=None,
        baseline_engine=None,
        check_interval_s: float = 15.0,
        config: dict | None = None,
        cost_guard=None,
        cost_model: str = "gpt-4o-mini",
        cron_service=None,
        skill_registry=None,
        somatic_engine=None,
    ):
        self._perception = perception
        # Read-only. Lets the health triggers ask "is this person under
        # load" instead of "is this number above 100". Optional, and
        # every trigger degrades to its raw-threshold form without it,
        # so an engine built without one behaves exactly as before.
        self._somatic_engine = somatic_engine
        # Read-only. Source of manifest TriggerDefinitions for
        # ``_evaluate_manifest_triggers``. Nothing on this path executes a
        # skill: see that method's docstring for why.
        self._skill_registry = skill_registry
        # Read-only, for the stalled-routine check. Optional so every existing
        # construction of this engine keeps working.
        self._cron_service = cron_service
        self._memory = memory
        self._orchestrator = orchestrator
        self._llm = llm
        self._calendar = calendar
        self._health = health_aggregator
        self._baseline = baseline_engine
        self._interval = check_interval_s
        self._running = False
        # audit-r14 / S6 — gate the LLM eval call on the shared cost
        # budget. ``cost_guard`` is wired from ``BrainState.init`` with
        # the brain's CostBudget + the WS broadcaster so a cap hit
        # both pauses this engine AND lights up the UI banner.
        self._cost_guard = cost_guard
        self._cost_model = cost_model
        # A7 — Hold the evaluation loop task so stop() can cancel it
        # rather than only flipping ``_running`` (which could still let
        # one more LLM evaluation fire while waiting on the interval
        # sleep).
        self._task: Optional[asyncio.Task] = None
        self._callbacks: list[Callable[[ProactiveMessage], Awaitable[None]]] = []
        self._trigger_states: dict[str, TriggerState] = {}
        self._trigger_counts: dict[str, int] = defaultdict(int)
        # The date, in the operator's local calendar, whose morning
        # briefing has already been delivered. Read from disk rather than
        # started at "not yet today", because this used to be a bare
        # in-memory flag: every restart reset it and the briefing fired
        # again. On 2026-09-05 the operator got the same "Good morning,
        # Omar!" card twice within twenty minutes, once per restart,
        # while we were verifying other work. A restart is not a new day.
        self._briefing_state_path = _briefing_state_path()
        self._briefing_delivered_on = _read_briefing_date(self._briefing_state_path)
        self._last_hr_alert = 0.0
        self._last_break_suggestion = 0.0
        self._last_llm_eval = 0.0
        self._session_start = time.time()

        cfg = config or {}
        features = cfg.get("features", {})
        self._nag_cooldown_s = float(features.get("proactive_nag_cooldown_s", 300))

    def on_message(self, callback: Callable[[ProactiveMessage], Awaitable[None]]):
        self._callbacks.append(callback)

    def stats(self) -> dict:
        """Per-trigger fire counts and current cooldown state."""
        return {
            "trigger_counts": dict(self._trigger_counts),
            "trigger_states": {
                tid: {"fire_count": s.fire_count, "dismiss_count": s.dismiss_count, "cooldown_s": s.cooldown_s}
                for tid, s in self._trigger_states.items()
            },
            "nag_cooldown_s": self._nag_cooldown_s,
            "running": self._running,
        }

    async def start(self):
        """Start the evaluation loop as a background task.

        Returns once the task is scheduled — callers should NOT ``await``
        the coroutine expecting it to block for the lifetime of the
        engine. Idempotent: a second call while running is a no-op.
        """
        if self._running and self._task and not self._task.done():
            return
        self._running = True
        self._session_start = time.time()
        logger.info("Proactive engine started (interval=%.0fs)", self._interval)
        self._task = asyncio.create_task(self._run_loop(), name="feral-proactive-loop")

    async def _run_loop(self):
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            try:
                await self._evaluate()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Proactive evaluation error: %s", e)

    async def stop(self):
        """Stop the evaluation loop and wait for it to exit.

        A7: Cancel the running task so any in-progress ``asyncio.sleep``
        returns immediately, then await completion. Flipping ``_running``
        alone is not enough — a pending interval sleep would still wake
        up and fire one more LLM evaluation after shutdown began.
        """
        self._running = False
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def evaluate(self, session_id: str = ""):
        """Public entry point for on-demand evaluation."""
        await self._evaluate()

    def record_dismiss(self, trigger_id: str):
        state = self._trigger_states.setdefault(trigger_id, TriggerState())
        state.dismiss_count += 1
        state.cooldown_s = min(state.cooldown_s * 1.5, 3600)

    async def _evaluate(self):
        """Run all trigger checks against current ambient context."""
        now = time.time()
        messages: list[ProactiveMessage] = []

        # Gather perception frames from all sessions
        frames = []
        # Parallel to `frames`, same order. PerceptionFrame carries no
        # session id, and the somatic vector is keyed by one, so the
        # health triggers below need the id that produced each frame.
        # Kept as a separate list rather than added to the frame so
        # nothing else that consumes frames has to change.
        frame_sids: list[str] = []
        if self._perception:
            for sid in list(getattr(self._perception, '_frames', {}).keys()):
                f = self._perception.get_frame(sid)
                if f:
                    frames.append(f)
                    frame_sids.append(sid)

        # --- Morning Briefing ---
        # Once per local calendar day, and the day is remembered across
        # restarts (see __init__).
        today = time.strftime("%Y-%m-%d", time.localtime())
        if self._briefing_delivered_on != today:
            hour = time.localtime().tm_hour
            if 5 <= hour <= 11:
                msg = await self._build_morning_briefing()
                if msg:
                    messages.append(msg)
                    self._briefing_delivered_on = today
                    _write_briefing_date(self._briefing_state_path, today)

        # --- Health Triggers ---
        # Freshness gate (operator report 2026-05-09: web-UI showed
        # "Heart Rate Alert: 115 BPM" while the W300 glasses were
        # disconnected — the value was a STALE Apple HealthKit sample
        # from hours earlier that the perception layer had cached as
        # "current"). Alerts now require:
        #   1. A non-zero reading (existing check).
        #   2. The sample timestamp is within FRESH_WINDOW_S (default
        #      120s) of "now" — older samples represent past state and
        #      shouldn't drive a real-time notification.
        # The source is surfaced in the body so the user knows where
        # the reading came from. Pinned by
        # tests/test_proactive_freshness_gate.py. The same constant is
        # also consulted by the Baseline Anomaly section below
        # (`baseline_hr`) — operator report round 3 caught that
        # trigger firing on stale data without a freshness gate.
        FRESH_WINDOW_S = _FRESH_WINDOW_S
        # Operator report 2026-06-08 demo prep: a fresh-looking
        # `apple_healthkit` HR=115 read (HealthKit was relabelling the
        # underlying workout sample's `endDate` to "now") cleared the
        # FRESH_WINDOW_S gate and fired `hr_elevated` →
        # `scene.calming` while the W300 + Veepoo were both showing a
        # resting 60s. Reuse the canonical lagging-source predicate
        # from `perception.fusion` (single source of truth) so any
        # cloud-mirror source automatically inherits the block —
        # adding a new lagging source there propagates here without
        # further edits.
        from perception.fusion import _is_lagging_source
        for frame_index, frame in enumerate(frames):
            frame_sid = frame_sids[frame_index] if frame_index < len(frame_sids) else ""
            hr_age = (now - getattr(frame, "heart_rate_sample_ts", 0.0)) if getattr(frame, "heart_rate_sample_ts", 0.0) > 0 else float("inf")
            spo2_age = (now - getattr(frame, "spo2_sample_ts", 0.0)) if getattr(frame, "spo2_sample_ts", 0.0) > 0 else float("inf")
            hr_src_raw = getattr(frame, "heart_rate_source", "") or ""
            spo2_src_raw = getattr(frame, "spo2_source", "") or ""
            hr_src = hr_src_raw or "unknown source"
            spo2_src = spo2_src_raw or "unknown source"
            hr_src_is_lagging = _is_lagging_source(hr_src_raw)
            spo2_src_is_lagging = _is_lagging_source(spo2_src_raw)

            if (
                frame.heart_rate > 0
                and hr_age <= FRESH_WINDOW_S
                and not hr_src_is_lagging
            ):
                # Elevated HR — only on live wearables (or unlabelled
                # fresh sources). Cloud-mirror reads are surfaced in
                # `health_summary` / `latest_health` but never drive
                # a real-time push; their fresh-looking timestamps
                # cannot be trusted.
                #
                # The condition is cognitive load where the somatic
                # engine has a reading, and a raw threshold only where
                # it does not. `heart_rate > 100` alone fires on any
                # physical exertion: a flight of stairs is 110-130 bpm
                # in a healthy adult and is not a thing to interrupt
                # someone about. Cognitive load already divides those
                # cases, because its HR term applies only below an
                # activity level of 0.3 and it weights HRV and circadian
                # phase alongside it. That turns a threshold alarm into
                # a judgement, which is the point.
                #
                # The raw path is kept rather than removed: a brain with
                # no wearable HRV, or with the somatic engine absent,
                # still gets the old alert instead of silently losing
                # the feature.
                load = self._cognitive_load_for(frame_sid)
                if load is not None:
                    should_fire = load >= SOMATIC_LOAD_ALERT
                    basis = f"cognitive_load={load:.2f}"
                else:
                    should_fire = frame.heart_rate > 100
                    basis = "raw heart rate (no somatic reading)"

                if should_fire and self._can_fire("hr_elevated"):
                    logger.info(
                        "proactive.hr_elevated firing: bpm=%d source=%s age=%ds basis=%s",
                        frame.heart_rate, hr_src, int(hr_age), basis,
                    )
                    if load is not None:
                        body = (
                            f"Your heart rate is {frame.heart_rate} bpm and your "
                            f"body is showing signs of strain rather than exertion. "
                            f"You've been {frame.activity_state}. "
                            f"(Source: {hr_src}, sample {int(hr_age)}s old.) "
                            "Want to take a short break?"
                        )
                        voice = (
                            "Hey, your heart rate is up and it doesn't look like "
                            "activity. Maybe a short break would help?"
                        )
                    else:
                        body = (
                            f"Your heart rate is {frame.heart_rate} bpm, that's elevated. "
                            f"You've been {frame.activity_state}. "
                            f"(Source: {hr_src}, sample {int(hr_age)}s old.) "
                            "Want to take a short break?"
                        )
                        voice = (
                            f"Hey, I noticed your heart rate jumped to "
                            f"{frame.heart_rate}. Maybe a short break would help?"
                        )
                    messages.append(ProactiveMessage(
                        trigger_id="hr_elevated",
                        priority=Priority.IMPORTANT,
                        title="Heart Rate Alert",
                        body=body,
                        voice_text=voice,
                        action="Take a break",
                        action_payload={"smart_home": "set_scene", "scene": "calming"},
                    ))

            if (
                0 < frame.spo2_pct < 94
                and spo2_age <= FRESH_WINDOW_S
                and not spo2_src_is_lagging
            ):
                # Low SpO2 — same lagging-source guard as HR.
                if self._can_fire("spo2_low"):
                    logger.info(
                        "proactive.spo2_low firing: spo2=%d source=%s age=%ds",
                        frame.spo2_pct, spo2_src, int(spo2_age),
                    )
                    messages.append(ProactiveMessage(
                        trigger_id="spo2_low",
                        priority=Priority.CRITICAL,
                        title="Low Blood Oxygen",
                        body=(
                            f"Your SpO2 is {frame.spo2_pct}%. This is below normal. "
                            f"(Source: {spo2_src}, sample {int(spo2_age)}s old.) "
                            "Please take some deep breaths and consider moving to fresh air."
                        ),
                        voice_text=f"Your blood oxygen is at {frame.spo2_pct} percent, which is low. Please take some deep breaths.",
                        action="Start breathing exercise",
                        action_payload={"smart_home": "breathing_exercise", "duration_minutes": 3},
                    ))

        # --- Screen Context Triggers ---
        for frame in frames:
            if frame.scene_description:
                desc_lower = frame.scene_description.lower()
                # Error detection
                if any(w in desc_lower for w in ["error", "exception", "traceback", "failed", "crash"]):
                    if self._can_fire("screen_error"):
                        messages.append(ProactiveMessage(
                            trigger_id="screen_error",
                            priority=Priority.SUGGESTION,
                            title="Error Detected",
                            body="I see an error on your screen. Want me to take a look and help debug it?",
                            voice_text="I noticed an error on your screen. Want me to take a look?",
                            action="Help me debug",
                        ))

        # --- Break Reminder ---
        session_minutes = (now - self._session_start) / 60
        if session_minutes > 90 and (now - self._last_break_suggestion > 1800):
            messages.append(ProactiveMessage(
                trigger_id="break_reminder",
                priority=Priority.SUGGESTION,
                title="Time for a Break",
                body=f"You've been working for {int(session_minutes)} minutes straight. A short break can boost focus by 20%.",
                voice_text=f"You've been at it for about {int(session_minutes)} minutes. How about a quick stretch?",
                action="Remind me in 30 min",
            ))
            self._last_break_suggestion = now

        # --- Sleep Trend Check ---
        if self._health and self._can_fire("sleep_declining"):
            try:
                trend = await self._health.get_sleep_trend(days=3)
                if len(trend) >= 3:
                    hours = [e.get("total_sleep_hours") or e.get("sleep_score") for e in trend[-3:]]
                    hours = [h for h in hours if h is not None]
                    if len(hours) >= 3 and hours[-1] < hours[-2] < hours[-3]:
                        hr_str = ", ".join(f"{h:.1f}h" if isinstance(h, float) else str(h) for h in hours)
                        messages.append(ProactiveMessage(
                            trigger_id="sleep_declining",
                            priority=Priority.SUGGESTION,
                            title="Sleep Trend Declining",
                            body=f"Your sleep has been declining — {hr_str}. Want to set up a wind-down routine?",
                            voice_text="I noticed your sleep has been trending down the last few nights. Want to set up a wind-down routine?",
                            action="Set up routine",
                        ))
            except Exception as e:
                logger.debug("Sleep trend check failed: %s", e)

        # --- Productivity Coaching ---
        if session_minutes > 90 and self._can_fire("focus_break"):
            same_app = False
            for frame in frames:
                if frame.scene_description:
                    same_app = True
                    break
            if same_app:
                messages.append(ProactiveMessage(
                    trigger_id="focus_break",
                    priority=Priority.SUGGESTION,
                    title="Focus Break",
                    body=f"You've been focused for {int(session_minutes)}m. A 5-minute break improves sustained performance.",
                    voice_text=f"You've been locked in for {int(session_minutes)} minutes. A short break will help you stay sharp.",
                    action="Take 5 min",
                ))

        # --- Meeting Prep ---
        if self._calendar and self._can_fire("meeting_prep"):
            try:
                result = await self._calendar.next_event()
                if result.get("success") and result.get("data"):
                    ev = result["data"]
                    start_str = ev.get("start", "")
                    title = ev.get("summary", "Untitled")
                    if start_str and "No upcoming" not in str(ev.get("message", "")):
                        try:
                            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                            minutes_until = (start_dt - datetime.now(timezone.utc)).total_seconds() / 60
                            if 0 < minutes_until < 15:
                                messages.append(ProactiveMessage(
                                    trigger_id="meeting_prep",
                                    priority=Priority.IMPORTANT,
                                    title="Meeting Soon",
                                    body=f"Meeting '{title}' in {int(minutes_until)} minutes. Want a quick briefing on related context?",
                                    voice_text=f"You have '{title}' coming up in {int(minutes_until)} minutes. Want me to prep a quick briefing?",
                                    action="Brief me",
                                    action_payload={"event": ev},
                                ))
                        except (ValueError, TypeError):
                            pass
            except Exception as e:
                logger.debug("Meeting prep check failed: %s", e)

        # --- Baseline Anomaly Detection ---
        if self._baseline:
            try:
                # Cooldown gate moved BEFORE the call (operator report
                # 2026-06-07): `check_anomaly` / `check_trend` are
                # *mutating* — each call persists a baseline_alert row
                # AND fans out to the IdeasEngine via the on_alert
                # listener. The old code called check_anomaly once per
                # perception frame on every 15s tick and only checked
                # `_can_fire` afterwards, so a sustained anomaly stacked
                # up 78 duplicate alerts + 20 identical "For you today"
                # cards. Gating the call itself (and evaluating only the
                # single freshest frame per tick) keeps anomaly creation
                # on the same nag cooldown as message delivery.
                if self._can_fire("baseline_hr"):
                    fresh_hr_frame = None
                    for frame in frames:
                        # Freshness gate (operator report 2026-05-09
                        # round 3): `baseline_hr` was firing while the
                        # W300 was disconnected because the trigger read
                        # `frame.heart_rate` without checking the sample
                        # age. Reuse the same FRESH_WINDOW_S / `now`.
                        # Lagging-source guard (Fix #2): never anomaly-
                        # check a stale-relabeled HealthKit reading.
                        hr_age_baseline = (
                            (now - getattr(frame, "heart_rate_sample_ts", 0.0))
                            if getattr(frame, "heart_rate_sample_ts", 0.0) > 0
                            else float("inf")
                        )
                        hr_src_baseline = (
                            getattr(frame, "heart_rate_source", "") or ""
                        )
                        if (
                            frame.heart_rate > 0
                            and hr_age_baseline <= FRESH_WINDOW_S
                            and not _is_lagging_source(hr_src_baseline)
                        ):
                            fresh_hr_frame = frame
                            break
                    if fresh_hr_frame is not None:
                        # Fix #5: query the active source's namespaced
                        # baseline first (`hr_resting:jw_health_glasses`)
                        # so the W300 vs Veepoo means stay independent.
                        # Fall back to bare `hr_resting` for legacy /
                        # unknown sources where the per-source row
                        # doesn't exist.
                        hr_src_norm = (
                            (
                                getattr(fresh_hr_frame, "heart_rate_source", "")
                                or ""
                            )
                            .strip()
                            .lower()
                        )
                        candidate_metric_ids = []
                        if hr_src_norm:
                            candidate_metric_ids.append(
                                f"hr_resting:{hr_src_norm}"
                            )
                        candidate_metric_ids.append("hr_resting")
                        alert = None
                        chosen_metric_id = None
                        for metric_id in candidate_metric_ids:
                            try:
                                metric = self._baseline.get_baseline(metric_id)
                            except Exception:
                                metric = None
                            if metric and len(getattr(metric, "values", []) or []) >= 3:
                                alert = self._baseline.check_anomaly(
                                    metric_id, fresh_hr_frame.heart_rate
                                )
                                chosen_metric_id = metric_id
                                break
                        if alert:
                            hr_age_log = int(now - fresh_hr_frame.heart_rate_sample_ts) if fresh_hr_frame.heart_rate_sample_ts else -1
                            logger.info(
                                "proactive.baseline_hr firing: bpm=%d source=%s metric=%s age=%ds",
                                fresh_hr_frame.heart_rate,
                                hr_src_norm or "unknown",
                                chosen_metric_id,
                                hr_age_log,
                            )
                            messages.append(ProactiveMessage(
                                trigger_id="baseline_hr",
                                priority=Priority.IMPORTANT,
                                title="Heart Rate Anomaly",
                                body=alert.message,
                                voice_text=alert.message,
                            ))
                for mid in ("sleep_hours", "hrv_ms"):
                    if not self._can_fire(f"baseline_trend_{mid}"):
                        continue
                    trend_alert = self._baseline.check_trend(mid)
                    if trend_alert:
                        messages.append(ProactiveMessage(
                            trigger_id=f"baseline_trend_{mid}",
                            priority=Priority.SUGGESTION,
                            title="Trend Detected",
                            body=trend_alert.message,
                            voice_text=trend_alert.message,
                        ))
            except Exception as e:
                logger.debug("Baseline check failed: %s", e)

        # --- Manifest-declared triggers ---
        # Contained so one malformed manifest cannot cost the tick its
        # remaining checks (stalled routines, delivery). Logged at warning,
        # never at debug: a trigger evaluator that has silently stopped
        # evaluating is indistinguishable from a gate that always passes.
        try:
            self._evaluate_manifest_triggers(frames, messages, now)
        except Exception as exc:
            logger.warning("Manifest trigger evaluation failed: %s", exc, exc_info=True)

        # --- Routines that have stopped working ---
        self._check_stalled_routines(messages)

        # --- Routines the runtime has switched off, reported once each ---
        self._check_auto_disabled_routines(messages)

        # --- LLM-based evaluation (additive, runs last) ---
        await self._evaluate_with_llm(frames, messages)

        # --- Deliver Messages ---
        for msg in sorted(messages, key=lambda m: m.priority.value, reverse=True):
            if self._can_fire(msg.trigger_id):
                await self._deliver(msg)
                self._record_fire(msg.trigger_id)

    async def _evaluate_with_llm(self, frames: list, existing_triggers: list[ProactiveMessage]):
        """Ask the LLM whether FERAL should proactively say something.

        Only called when an LLM client is configured and enough time has
        elapsed since the last LLM evaluation (60s cooldown).  Results are
        appended to *existing_triggers* — they don't replace rule-based ones.
        """
        if not self._llm:
            return

        now = time.time()
        if now - self._last_llm_eval < 60:
            return
        self._last_llm_eval = now

        frame_summaries = []
        for frame in frames[:3]:
            frame_summaries.append(frame.to_system_context())

        recent_trigger_ids = [m.trigger_id for m in existing_triggers[:5]]

        prompt = (
            "You are FERAL's proactive intelligence layer. Given the current "
            "perception context and recent rule-based triggers, decide if FERAL "
            "should proactively say something ADDITIONAL.\n\n"
            f"Perception frames:\n{chr(10).join(frame_summaries) or 'No sensor data.'}\n\n"
            f"Already-triggered rules: {recent_trigger_ids or 'none'}\n\n"
            "If you think FERAL should speak up, return ONLY valid JSON:\n"
            '{"trigger_id": "llm_<topic>", "priority": "SUGGESTION"|"IMPORTANT", '
            '"title": "...", "body": "...", "action": "..."}\n\n'
            "If nothing useful to add, return exactly: null"
        )

        # audit-r14 / S6 — pre-flight cost gate. When the projected
        # spend would overshoot the proactive cap, skip the eval and
        # let the loop tick again at the next interval; the guard
        # already pauses + emits the WS frame.
        if self._cost_guard is not None and not self._cost_guard.allow(
            model=self._cost_model, estimated_max_tokens=300,
        ):
            return

        try:
            response = await self._llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=300,
            )
            text = ""
            if isinstance(response, dict):
                text = response.get("content", "") or response.get("text", "")
            elif isinstance(response, str):
                text = response
            else:
                text = str(response)

            # audit-r14 / S6 — record post-call usage. We pass
            # conservative estimates (prompt ~200 + completion bounded
            # at the ``max_tokens=300`` we sent) since the LLM provider
            # surface here doesn't return per-call usage; pricing.py
            # treats this as a worst-case lower bound on the rollup.
            if self._cost_guard is not None:
                await self._cost_guard.record(
                    model=self._cost_model,
                    prompt_tokens=200,
                    completion_tokens=min(300, estimate_tokens(text) + 1),
                )

            text = text.strip()
            if not text or text == "null":
                return

            data = json.loads(text)
            if not isinstance(data, dict) or "trigger_id" not in data:
                return

            priority_str = data.get("priority", "SUGGESTION").upper()
            priority = Priority[priority_str] if priority_str in Priority.__members__ else Priority.SUGGESTION

            existing_triggers.append(ProactiveMessage(
                trigger_id=data["trigger_id"],
                priority=priority,
                title=data.get("title", "FERAL Insight"),
                body=data.get("body", ""),
                voice_text=data.get("body", ""),
                action=data.get("action", ""),
            ))
            logger.info("LLM proactive trigger: %s", data["trigger_id"])

        except (json.JSONDecodeError, KeyError) as e:
            logger.debug("LLM eval returned non-JSON: %s", e)
        except Exception as e:
            logger.warning("LLM proactive evaluation failed: %s", e)

    async def _build_morning_briefing(self) -> ProactiveMessage | None:
        """Build a personalized morning briefing from memory and health data."""
        sections = []
        now = time.time()

        # Health (audit-r8 brief #08 HIGH fix): the prior implementation
        # verbalised `frame.heart_rate` / `frame.spo2_pct` straight from
        # the first frame regardless of `*_sample_ts`, so a stale Apple
        # HealthKit reading from hours ago could be spoken aloud as "your
        # resting heart rate is …" — same root cause as the chat
        # hallucination fix in 2026.5.18 but missed in `_build_morning_briefing`.
        # Now uses the same `_FRESH_WINDOW_S` gate as `_evaluate`.
        frames = []
        if self._perception:
            for sid in list(getattr(self._perception, '_frames', {}).keys()):
                f = self._perception.get_frame(sid)
                if f and f.heart_rate > 0:
                    frames.append(f)

        if frames:
            f = frames[0]
            hr_age = (
                (now - getattr(f, "heart_rate_sample_ts", 0.0))
                if getattr(f, "heart_rate_sample_ts", 0.0) > 0
                else float("inf")
            )
            spo2_age = (
                (now - getattr(f, "spo2_sample_ts", 0.0))
                if getattr(f, "spo2_sample_ts", 0.0) > 0
                else float("inf")
            )
            hr_fresh = hr_age <= _FRESH_WINDOW_S
            spo2_fresh = (f.spo2_pct > 0) and (spo2_age <= _FRESH_WINDOW_S)
            if hr_fresh and spo2_fresh:
                sections.append(
                    f"Your resting heart rate is {f.heart_rate} bpm, SpO2 {f.spo2_pct}%."
                )
            elif hr_fresh:
                sections.append(f"Your resting heart rate is {f.heart_rate} bpm.")
            # else: do NOT verbalise stale vitals — silence is honest.

        # Recent memory
        if self._memory:
            try:
                recent = await self._memory.episode_recent(limit=3, session_id=None)
                if recent:
                    sections.append("Here's what happened recently:")
                    for ep in recent[:2]:
                        sections.append(f"  - {ep.get('summary', '')[:100]}")
            except Exception as exc:
                # Audit-r8 brief #08 MEDIUM fix: surface the exception
                # so an operator can debug a blank briefing instead of
                # silently dropping the memory section.
                logger.warning(
                    "morning briefing: memory.episode_recent failed (%s); skipping memory section",
                    exc,
                )

        if not sections:
            return None

        hour = time.localtime().tm_hour
        # "Good afternoon" ran from noon to midnight, so a briefing at
        # 11pm opened by calling it the afternoon.
        if hour < 12:
            greeting = "Good morning"
        elif hour < 18:
            greeting = "Good afternoon"
        else:
            greeting = "Good evening"

        # The operator's actual name, or none at all.
        #
        # The SDUI card below said "Good morning, Alex!" to everyone: a
        # placeholder hardcoded into the headline while the real name sat
        # in USER.md, unread. Greeting somebody by the wrong name is
        # worse than not greeting them by name, and on a product whose
        # whole claim is that it knows you it is the first thing they
        # see.
        name = ""
        try:
            from identity.workspace import IdentityWorkspace

            name = IdentityWorkspace().read_user_name()
        except Exception:
            logger.debug("morning briefing: could not read operator name", exc_info=True)
        salutation = f"{greeting}, {name}!" if name else f"{greeting}!"

        body = f"{salutation} Here's your briefing:\n\n" + "\n".join(sections)
        voice = f"{salutation} " + " ".join(sections[:3])

        return ProactiveMessage(
            trigger_id="morning_briefing",
            priority=Priority.IMPORTANT,
            title="Morning Briefing",
            body=body,
            voice_text=voice,
            sdui={
                "type": "Card",
                "children": [
                    {"type": "Text", "value": salutation, "style": "headline"},
                    {"type": "Divider"},
                    *[{"type": "Text", "value": s, "style": "body"} for s in sections],
                ],
            },
        )

    # A routine is only reported once a day, and only after a fortnight of
    # failing, because the whole point is to catch the slow silent death of
    # something the user set up and forgot, not to chirp about a flaky morning.
    _STALLED_ROUTINE_WINDOW_S = 14 * 86400
    _STALLED_ROUTINE_MIN_RUNS = 20

    def _check_stalled_routines(self, messages: list) -> None:
        """Report routines that have been running and achieving nothing.

        Nothing has ever watched routine outcomes. record_run_finish writes
        every result to routine_runs and no code path has ever read it back to
        raise, so on this install one routine failed DNS 4,824 times out of
        4,824 over six weeks and another failed on an unconnected calendar 23
        times out of 23, and the user learned about both from a manual
        database query during an audit.

        That is the exact shape of failure the user described: a routine that
        fires at something it is not connected to, wastes the run, and nobody
        is told. A scheduled task is a promise the machine made, and quietly
        breaking it every minute for six weeks is worse than never having
        offered.

        Reported at IMPORTANT so it escalates off the screen. This costs one
        SQL query on the proactive tick and never calls a model.
        """
        if not self._can_fire("routine_stalled"):
            return

        svc = getattr(self, "_cron_service", None)
        conn = getattr(svc, "_conn", None)
        if conn is None:
            return

        try:
            rows = conn.execute(
                """
                SELECT r.job_id AS job_id,
                       COUNT(*) AS runs,
                       SUM(CASE WHEN r.status = 'success' THEN 1 ELSE 0 END) AS wins,
                       MAX(r.error) AS last_error
                  FROM routine_runs r
                  JOIN scheduled_jobs j ON j.id = r.job_id
                 WHERE r.started_at > ?
                   AND j.enabled = 1
                 GROUP BY r.job_id
                HAVING wins = 0 AND runs >= ?
                 ORDER BY runs DESC
                """,
                (time.time() - self._STALLED_ROUTINE_WINDOW_S,
                 self._STALLED_ROUTINE_MIN_RUNS),
            ).fetchall()
        except Exception as exc:
            # Not debug: a watcher that cannot watch is precisely the class of
            # silence this trigger exists to end.
            logger.warning("Stalled-routine check failed: %s", exc)
            return

        if not rows:
            return

        worst = rows[0]
        job_id = worst["job_id"] if hasattr(worst, "keys") else worst[0]
        runs = worst["runs"] if hasattr(worst, "keys") else worst[1]
        last_error = (worst["last_error"] if hasattr(worst, "keys") else worst[3]) or ""

        name = f"Routine {job_id}"
        try:
            row = conn.execute(
                "SELECT description FROM scheduled_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row and (row["description"] if hasattr(row, "keys") else row[0]):
                name = row["description"] if hasattr(row, "keys") else row[0]
        except Exception:
            pass

        others = ""
        if len(rows) > 1:
            others = f" ({len(rows) - 1} other routine(s) are failing too.)"
        reason = f" Last error: {last_error[:120]}" if last_error else ""

        body = (
            f"'{name}' has run {runs} times without succeeding once. "
            f"It is still enabled and still firing.{reason}{others}"
        )
        messages.append(ProactiveMessage(
            trigger_id="routine_stalled",
            priority=Priority.IMPORTANT,
            title="A routine has stopped working",
            body=body,
            voice_text=f"{name} has failed {runs} times in a row.",
            action="Show routines",
        ))

    def _check_auto_disabled_routines(self, messages: list) -> None:
        """Say ONCE that the runtime turned a routine off, and why.

        The stalled-routine alert above only sees ``enabled = 1`` routines, so
        the moment a routine is auto-disabled it drops out of that query and
        the user hears nothing more about it. That is the right behaviour for
        the nag (the routine has stopped costing a run a minute, so there is
        nothing left to escalate every day) and the wrong behaviour for the
        event: a routine the user set up has just been switched off by the
        machine, and silence there is the same defect in a new place.

        So this fires exactly once per routine. ``disabled_notified`` is a
        column, not an in-memory set, because an in-memory flag would reset on
        every brain restart and turn the one-shot back into a nag.
        """
        if not self._can_fire("routine_auto_disabled"):
            return

        svc = getattr(self, "_cron_service", None)
        conn = getattr(svc, "_conn", None)
        if conn is None:
            return

        try:
            rows = conn.execute(
                """
                SELECT id, description, disabled_reason
                  FROM scheduled_jobs
                 WHERE enabled = 0
                   AND disabled_reason != ''
                   AND disabled_notified = 0
                 ORDER BY id ASC
                """
            ).fetchall()
        except Exception as exc:
            # Not debug, for the same reason as the stalled check: a watcher
            # that cannot watch is the silence this exists to end.
            logger.warning("Auto-disabled routine check failed: %s", exc)
            return

        if not rows:
            return

        def _col(row, key, idx):
            return row[key] if hasattr(row, "keys") else row[idx]

        first = rows[0]
        job_id = _col(first, "id", 0)
        name = _col(first, "description", 1) or f"Routine {job_id}"
        why = _col(first, "disabled_reason", 2) or ""

        others = ""
        if len(rows) > 1:
            others = f" ({len(rows) - 1} other routine(s) were turned off too.)"

        messages.append(ProactiveMessage(
            trigger_id="routine_auto_disabled",
            priority=Priority.IMPORTANT,
            title="A routine was turned off",
            body=f"'{name}' has been disabled. {why}{others}",
            voice_text=f"I turned off the routine {name}, because it could never succeed.",
            action="Show routines",
        ))

        # Mark every row reported, not just the one named: the others are
        # accounted for by the "(N other routine(s))" line, and leaving them
        # unnotified would make this fire again on the next tick for the same
        # batch. _can_fire was checked above and nothing else claims this
        # trigger id, so the delivery loop will send this message.
        try:
            svc.mark_disabled_notified([_col(r, "id", 0) for r in rows])
        except Exception as exc:
            logger.warning("Could not mark auto-disabled routines notified: %s", exc)

    # Namespace for manifest-declared trigger ids, so they can never collide
    # with the hardcoded trigger ids above (a skill is free to call its
    # trigger "hr_elevated") and so a fired alert names its origin.
    MANIFEST_TRIGGER_PREFIX = "manifest"

    def _manifest_trigger_defs(self) -> list[tuple[str, Any]]:
        """(skill_id, TriggerDefinition) for every trusted manifest trigger.

        Trust clamp, same rule as skills/result_budget.py: only manifests
        that ship in feral-core/skills/manifests/ are honoured. A manifest
        is untrusted input, and a marketplace skill that declares
        `condition: "biometric.heart_rate_bpm > 0"` would otherwise get a
        notification every cooldown forever, which is the spam half of the
        4,766-run incident even with no action attached.
        """
        registry = self._skill_registry
        if registry is None:
            return []
        try:
            from skills.result_budget import builtin_skill_ids
            trusted = builtin_skill_ids()
        except Exception as exc:
            logger.warning("manifest triggers disabled, trust check failed: %s", exc)
            return []

        out: list[tuple[str, Any]] = []
        for skill_id, manifest in (getattr(registry, "skills", {}) or {}).items():
            if skill_id not in trusted:
                continue
            for tdef in (getattr(manifest, "triggers", None) or []):
                if getattr(tdef, "condition", ""):
                    out.append((skill_id, tdef))
        return out

    def _evaluate_manifest_triggers(self, frames: list, messages: list, now: float) -> None:
        """Evaluate manifest TriggerDefinitions and NOTIFY. Never execute.

        This is the reader that never existed. Manifests have declared
        `triggers[].condition` since the schema was written, and no code
        anywhere read those strings, so skills/registry.py turned each one
        into a JobType.TRIGGERED routine polling "every 1m" with the
        condition parked in a payload nobody looked at. The action ran
        unconditionally: 4,766 runs each on two routines, one of them a
        Telegram send gated on a stress reading that was never checked.

        What fires here is a ProactiveMessage and nothing else.
        `action_flow_id` / `action_endpoint_id` are reported in the body
        and deliberately NOT dispatched: `action_payload` stays empty so
        `_deliver` skips `_execute_automation` entirely, and this method
        imports no executor. Connecting a brand-new evaluator straight to
        a send, with no soak time, is how the original incident happened.
        Wiring the action is a separate change that has to go through the
        cron/surface safety pre-flight in api/server.py.
        """
        defs = self._manifest_trigger_defs()
        if not defs:
            return

        from agents.trigger_conditions import (
            build_biometric_namespace,
            evaluate_condition,
        )

        namespace = None
        for skill_id, tdef in defs:
            raw_id = getattr(tdef, "id", "") or "unnamed"
            trigger_id = f"{self.MANIFEST_TRIGGER_PREFIX}:{skill_id}:{raw_id}"

            # Honour the manifest's own cooldown_seconds. setdefault (not
            # assignment) so record_dismiss's exponential back-off is not
            # reset to the manifest value on the next tick.
            cooldown = getattr(tdef, "cooldown_seconds", None)
            try:
                cooldown = float(cooldown)
            except (TypeError, ValueError):
                cooldown = float(self._nag_cooldown_s)
            if cooldown <= 0:
                cooldown = float(self._nag_cooldown_s)
            self._trigger_states.setdefault(trigger_id, TriggerState(cooldown_s=cooldown))

            if not self._can_fire(trigger_id):
                continue

            if namespace is None:
                namespace = build_biometric_namespace(
                    frames=frames,
                    baseline_engine=self._baseline,
                    now=now,
                    fresh_window_s=_FRESH_WINDOW_S,
                )

            result = evaluate_condition(
                getattr(tdef, "condition", ""),
                namespace,
                trigger_id=trigger_id,
            )
            if not result.satisfied:
                continue

            action = (
                getattr(tdef, "action_flow_id", None)
                or getattr(tdef, "action_endpoint_id", None)
                or "none declared"
            )
            logger.info(
                "manifest trigger fired (observation only): %s condition=%r seen=%s action=%s",
                trigger_id, result.condition, result.describe(), action,
            )
            messages.append(ProactiveMessage(
                trigger_id=trigger_id,
                priority=Priority.IMPORTANT,
                title=f"Trigger matched: {raw_id}",
                body=(
                    f"The '{raw_id}' trigger declared by skill '{skill_id}' matched.\n"
                    f"Condition: {result.condition}\n"
                    f"Readings: {result.describe()}\n"
                    f"Declared action: {action}. It was NOT run. Manifest "
                    "triggers are evaluation and notification only."
                ),
                voice_text=(
                    f"A condition from your {skill_id} skill just matched: "
                    f"{result.describe()}."
                ),
            ))

    def _cognitive_load_for(self, session_id: str) -> float | None:
        """Cognitive load for this session, or None to use raw thresholds.

        None, not 0.0, when the answer is unknown. 0.0 is a real value
        meaning "this person is fine" and would silence a genuine alert
        on a brain that simply has no wearable attached; None routes the
        caller to the raw threshold it used before.

        Returns None unless the reading is FRESH. A somatic vector
        outlives the wearable that fed it, so without this an alert
        could be suppressed (or raised) hours later on the strength of a
        body state that no longer exists. Same window the rest of the
        health triggers gate on.
        """
        engine = self._somatic_engine
        if not engine or not session_id:
            return None
        try:
            vector = engine.get_vector(session_id)
        except Exception:
            logger.debug("somatic lookup failed for %s", session_id, exc_info=True)
            return None
        stamp = getattr(vector, "timestamp", 0.0) or 0.0
        if stamp <= 0 or (time.time() - stamp) > _FRESH_WINDOW_S:
            return None
        # An all-zero vector produces a load figure from circadian phase
        # alone. That is not a statement about this person's body, so it
        # must not be allowed to decide whether to interrupt them.
        if not (vector.heart_rate > 0 or vector.hrv_ms > 0):
            return None
        return float(getattr(vector, "cognitive_load", 0.0) or 0.0)

    def _can_fire(self, trigger_id: str) -> bool:
        state = self._trigger_states.get(trigger_id)
        if not state:
            return True
        if state.dismiss_count > 5 and state.fire_count > 10:
            return False
        return (time.time() - state.last_fired) >= state.cooldown_s

    def _record_fire(self, trigger_id: str):
        state = self._trigger_states.setdefault(trigger_id, TriggerState(cooldown_s=self._nag_cooldown_s))
        state.last_fired = time.time()
        state.fire_count += 1
        self._trigger_counts[trigger_id] += 1

    async def _deliver(self, msg: ProactiveMessage):
        logger.info("Proactive [%s] %s: %s", msg.priority.name, msg.trigger_id, msg.title)

        try:
            from observability.metrics import increment
            increment("feral.proactive.trigger_total", attributes={"trigger": msg.trigger_id})
        except Exception:
            pass

        if msg.action_payload:
            await self._execute_automation(msg)

        for cb in self._callbacks:
            try:
                await cb(msg)
            except Exception as e:
                logger.warning("Proactive delivery error: %s", e)

    async def _execute_automation(self, msg: ProactiveMessage):
        """Execute smart home / automation actions attached to proactive alerts.

        This path bypasses Orchestrator.handle_command (the supervisor
        only wraps chat-style entry points). So we explicitly call
        ``state.supervisor.record(source="proactive", actor="system", ...)``
        so the automation still lands in the audit log.
        """
        if not self._orchestrator:
            return

        payload = msg.action_payload
        action_type = payload.get("smart_home") or payload.get("action_type")
        if not action_type:
            return

        supervisor = None
        try:
            from api.state import state as _state
            supervisor = getattr(_state, "supervisor", None)
        except Exception:
            supervisor = None

        decision = "allowed"
        result_summary = ""

        try:
            from skills.impl import get_implementation

            if action_type == "set_scene":
                scene = payload.get("scene", "calming")
                impl = get_implementation("smart_home_hue")
                if impl:
                    # `set_scene` is not a smart_home_hue endpoint — it
                    # silently no-op'd. Home Assistant activates scenes via
                    # the real `call_service` endpoint (scene.turn_on on the
                    # `scene.<name>` entity), which IS in the manifest.
                    await impl.execute(
                        "call_service",
                        {"domain": "scene", "service": "turn_on", "entity_id": f"scene.{scene}"},
                        {},
                    )
                result_summary = f"set_scene={scene}"
                logger.info("Automation executed: scene.turn_on scene.%s (trigger=%s)", scene, msg.trigger_id)

            elif action_type == "breathing_exercise":
                duration = payload.get("duration_minutes", 3)
                impl = get_implementation("smart_home_hue")
                if impl:
                    await impl.execute(
                        "call_service",
                        {"domain": "scene", "service": "turn_on", "entity_id": "scene.breathing"},
                        {},
                    )
                result_summary = f"breathing_exercise={duration}min"
                logger.info("Automation executed: breathing exercise %dmin (trigger=%s)", duration, msg.trigger_id)

            elif action_type == "notification":
                result_summary = "notification"
                logger.info("Automation: notification-only for trigger=%s", msg.trigger_id)

        except Exception as e:
            decision = "error"
            result_summary = f"error: {e}"
            logger.warning("Automation execution failed for %s: %s", msg.trigger_id, e)

        if supervisor is not None:
            try:
                supervisor.record(
                    source="proactive",
                    kind="automation",
                    session_id="",
                    actor="system",
                    payload={
                        "trigger_id": msg.trigger_id,
                        "action_type": action_type,
                        "summary": result_summary,
                    },
                    decision=decision,
                    detail={"payload": payload},
                )
            except Exception as exc:
                logger.debug("supervisor.record(proactive) failed: %s", exc)
