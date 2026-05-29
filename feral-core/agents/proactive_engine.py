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
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Awaitable, Optional

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
    ):
        self._perception = perception
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
        self._first_interaction_today = True
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
        if self._perception:
            for sid in list(getattr(self._perception, '_frames', {}).keys()):
                f = self._perception.get_frame(sid)
                if f:
                    frames.append(f)

        # --- Morning Briefing ---
        if self._first_interaction_today:
            hour = time.localtime().tm_hour
            if 5 <= hour <= 11:
                msg = await self._build_morning_briefing()
                if msg:
                    messages.append(msg)
                    self._first_interaction_today = False

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
        for frame in frames:
            hr_age = (now - getattr(frame, "heart_rate_sample_ts", 0.0)) if getattr(frame, "heart_rate_sample_ts", 0.0) > 0 else float("inf")
            spo2_age = (now - getattr(frame, "spo2_sample_ts", 0.0)) if getattr(frame, "spo2_sample_ts", 0.0) > 0 else float("inf")
            hr_src = getattr(frame, "heart_rate_source", "") or "unknown source"
            spo2_src = getattr(frame, "spo2_source", "") or "unknown source"

            if frame.heart_rate > 0 and hr_age <= FRESH_WINDOW_S:
                # Elevated HR
                if frame.heart_rate > 100 and self._can_fire("hr_elevated"):
                    messages.append(ProactiveMessage(
                        trigger_id="hr_elevated",
                        priority=Priority.IMPORTANT,
                        title="Heart Rate Alert",
                        body=(
                            f"Your heart rate is {frame.heart_rate} bpm — that's elevated. "
                            f"You've been {frame.activity_state}. "
                            f"(Source: {hr_src}, sample {int(hr_age)}s old.) "
                            "Want to take a short break?"
                        ),
                        voice_text=f"Hey, I noticed your heart rate jumped to {frame.heart_rate}. Maybe a short break would help?",
                        action="Take a break",
                        action_payload={"smart_home": "set_scene", "scene": "calming"},
                    ))

            if 0 < frame.spo2_pct < 94 and spo2_age <= FRESH_WINDOW_S:
                # Low SpO2
                if self._can_fire("spo2_low"):
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
                for frame in frames:
                    # Freshness gate (operator report 2026-05-09 round 3):
                    # `baseline_hr` was firing "Heart Rate Anomaly:
                    # hr_resting is 54.0 below baseline 110.6" while
                    # the W300 was disconnected — same root cause as
                    # `hr_elevated`: the trigger read `frame.heart_rate`
                    # without checking when the sample was taken. Gate
                    # on the same FRESH_WINDOW_S as elsewhere in
                    # _evaluate; reuse the same `now` (declared at top
                    # of method).
                    hr_age_baseline = (
                        (now - getattr(frame, "heart_rate_sample_ts", 0.0))
                        if getattr(frame, "heart_rate_sample_ts", 0.0) > 0
                        else float("inf")
                    )
                    if frame.heart_rate > 0 and hr_age_baseline <= FRESH_WINDOW_S:
                        alert = self._baseline.check_anomaly(
                            "hr_resting", frame.heart_rate
                        )
                        if alert and self._can_fire("baseline_hr"):
                            messages.append(ProactiveMessage(
                                trigger_id="baseline_hr",
                                priority=Priority.IMPORTANT,
                                title="Heart Rate Anomaly",
                                body=alert.message,
                                voice_text=alert.message,
                            ))
                for mid in ("sleep_hours", "hrv_ms"):
                    trend_alert = self._baseline.check_trend(mid)
                    if trend_alert and self._can_fire(f"baseline_trend_{mid}"):
                        messages.append(ProactiveMessage(
                            trigger_id=f"baseline_trend_{mid}",
                            priority=Priority.SUGGESTION,
                            title="Trend Detected",
                            body=trend_alert.message,
                            voice_text=trend_alert.message,
                        ))
            except Exception as e:
                logger.debug("Baseline check failed: %s", e)

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
                    completion_tokens=min(300, len(text) // 4 + 1),
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
        greeting = "Good morning" if hour < 12 else "Good afternoon"
        body = f"{greeting}! Here's your briefing:\n\n" + "\n".join(sections)
        voice = f"{greeting}! " + " ".join(sections[:3])

        return ProactiveMessage(
            trigger_id="morning_briefing",
            priority=Priority.IMPORTANT,
            title="Morning Briefing",
            body=body,
            voice_text=voice,
            sdui={
                "type": "Card",
                "children": [
                    {"type": "Text", "value": f"{greeting}, Alex!", "style": "headline"},
                    {"type": "Divider"},
                    *[{"type": "Text", "value": s, "style": "body"} for s in sections],
                ],
            },
        )

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
