"""
Tests for agents/proactive_engine.py — ProactiveEngine init, cooldowns,
trigger evaluation, health alerts, break reminders, and delivery callbacks.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.proactive_engine import (
    Priority,
    ProactiveEngine,
    ProactiveMessage,
    TriggerState,
)


@pytest.fixture
def engine():
    return ProactiveEngine(
        perception=MagicMock(),
        memory=MagicMock(),
        orchestrator=MagicMock(),
        llm=None,
        calendar=None,
        health_aggregator=None,
        baseline_engine=None,
        check_interval_s=1.0,
    )


class TestProactiveInit:
    def test_defaults(self, engine):
        assert engine._running is False
        assert engine._callbacks == []
        assert engine._trigger_states == {}

    def test_on_message_registers_callback(self, engine):
        cb = AsyncMock()
        engine.on_message(cb)
        assert cb in engine._callbacks


class TestCanFire:
    def test_fires_when_no_state(self, engine):
        assert engine._can_fire("brand_new") is True

    def test_blocked_during_cooldown(self, engine):
        engine._trigger_states["test"] = TriggerState(
            last_fired=time.time(),
            cooldown_s=300,
        )
        assert engine._can_fire("test") is False

    def test_fires_after_cooldown(self, engine):
        engine._trigger_states["test"] = TriggerState(
            last_fired=time.time() - 600,
            cooldown_s=300,
        )
        assert engine._can_fire("test") is True

    def test_blocked_when_too_many_dismissals(self, engine):
        engine._trigger_states["annoying"] = TriggerState(
            last_fired=0,
            fire_count=15,
            dismiss_count=10,
            cooldown_s=0,
        )
        assert engine._can_fire("annoying") is False

    def test_not_blocked_if_dismiss_below_threshold(self, engine):
        engine._trigger_states["ok"] = TriggerState(
            last_fired=0,
            fire_count=15,
            dismiss_count=3,
            cooldown_s=0,
        )
        assert engine._can_fire("ok") is True


class TestRecordDismiss:
    def test_increases_cooldown(self, engine):
        engine.record_dismiss("t1")
        state = engine._trigger_states["t1"]
        assert state.dismiss_count == 1
        initial_cd = state.cooldown_s
        engine.record_dismiss("t1")
        assert engine._trigger_states["t1"].cooldown_s > initial_cd

    def test_cooldown_caps_at_one_hour(self, engine):
        for _ in range(50):
            engine.record_dismiss("t1")
        assert engine._trigger_states["t1"].cooldown_s <= 3600


class TestEvaluateHealthTriggers:
    @pytest.mark.asyncio
    async def test_elevated_hr_triggers_alert(self, engine):
        frame = MagicMock()
        frame.heart_rate = 120
        # Freshness contract (operator report 2026-05-09): proactive
        # alerts now require the sample to be within 120s of "now",
        # otherwise stale Apple HealthKit reads fire phantom alerts.
        # Tests must mirror what live senders would set.
        # Lagging-source guard (operator report 2026-06-08, fix #2):
        # alerts also require a live wearable source — HealthKit
        # relabels stale reads as fresh, so freshness alone is not
        # enough. Use a live wearable here so the trigger fires.
        frame.heart_rate_sample_ts = time.time() - 5.0
        frame.heart_rate_source = "jw_health_glasses"
        frame.spo2_pct = 98
        frame.spo2_sample_ts = time.time() - 5.0
        frame.spo2_source = "jw_health_glasses"
        frame.activity_state = "working"
        frame.scene_description = ""
        engine._perception._frames = {"s1": frame}
        engine._perception.get_frame.return_value = frame

        delivered = []
        async def capture(msg):
            delivered.append(msg)
        engine.on_message(capture)

        engine._session_start = time.time() - 30
        engine._last_hr_alert = 0
        await engine._evaluate()
        hr_alerts = [m for m in delivered if m.trigger_id == "hr_elevated"]
        assert len(hr_alerts) >= 1
        assert hr_alerts[0].priority == Priority.IMPORTANT

    @pytest.mark.asyncio
    async def test_low_spo2_triggers_critical(self, engine):
        frame = MagicMock()
        frame.heart_rate = 70
        frame.heart_rate_sample_ts = time.time() - 5.0
        # Same lagging-source guard as `hr_elevated` — use a live
        # wearable so the trigger fires.
        frame.heart_rate_source = "jw_health_glasses"
        frame.spo2_pct = 90
        frame.spo2_sample_ts = time.time() - 5.0
        frame.spo2_source = "jw_health_glasses"
        frame.activity_state = "resting"
        frame.scene_description = ""
        engine._perception._frames = {"s1": frame}
        engine._perception.get_frame.return_value = frame

        delivered = []
        async def capture(msg):
            delivered.append(msg)
        engine.on_message(capture)

        engine._session_start = time.time() - 30
        await engine._evaluate()
        spo2 = [m for m in delivered if m.trigger_id == "spo2_low"]
        assert len(spo2) >= 1
        assert spo2[0].priority == Priority.CRITICAL


class TestBreakReminder:
    @pytest.mark.asyncio
    async def test_break_after_long_session(self, engine):
        engine._perception._frames = {}
        engine._session_start = time.time() - (100 * 60)
        engine._last_break_suggestion = 0
        engine._first_interaction_today = False

        delivered = []
        async def capture(msg):
            delivered.append(msg)
        engine.on_message(capture)
        await engine._evaluate()

        breaks = [m for m in delivered if m.trigger_id == "break_reminder"]
        assert len(breaks) >= 1


class TestSleepTrend:
    @pytest.mark.asyncio
    async def test_declining_sleep_triggers(self, engine):
        health = AsyncMock()
        health.get_sleep_trend.return_value = [
            {"total_sleep_hours": 8.0},
            {"total_sleep_hours": 7.0},
            {"total_sleep_hours": 6.0},
        ]
        engine._health = health
        engine._perception._frames = {}
        engine._session_start = time.time() - 30
        engine._first_interaction_today = False

        delivered = []
        async def capture(msg):
            delivered.append(msg)
        engine.on_message(capture)
        await engine._evaluate()

        sleep_alerts = [m for m in delivered if m.trigger_id == "sleep_declining"]
        assert len(sleep_alerts) >= 1


class TestDelivery:
    @pytest.mark.asyncio
    async def test_deliver_calls_callbacks(self, engine):
        cb = AsyncMock()
        engine.on_message(cb)
        msg = ProactiveMessage(
            trigger_id="test_msg",
            priority=Priority.SUGGESTION,
            title="Test",
            body="Body",
        )
        await engine._deliver(msg)
        cb.assert_awaited_once_with(msg)

    @pytest.mark.asyncio
    async def test_deliver_tolerates_callback_error(self, engine):
        bad_cb = AsyncMock(side_effect=RuntimeError("oops"))
        engine.on_message(bad_cb)
        msg = ProactiveMessage(
            trigger_id="err", priority=Priority.AMBIENT, title="T", body="B",
        )
        await engine._deliver(msg)  # should not raise


class TestLLMEvaluationIsolation:
    @pytest.mark.asyncio
    async def test_slow_model_does_not_delay_rule_delivery(self, engine):
        import asyncio

        release = asyncio.Event()

        class SlowLLM:
            async def chat(self, **_kwargs):
                await release.wait()
                return "null"

        frame = MagicMock()
        frame.heart_rate = 120
        frame.heart_rate_sample_ts = time.time() - 2
        frame.heart_rate_source = "jw_health_glasses"
        frame.spo2_pct = 98
        frame.spo2_sample_ts = time.time() - 2
        frame.spo2_source = "jw_health_glasses"
        frame.activity_state = "working"
        frame.scene_description = ""
        frame.to_system_context.return_value = "Heart rate: 120"
        engine._perception._frames = {"s1": frame}
        engine._perception.get_frame.return_value = frame
        engine._llm = SlowLLM()
        engine._first_interaction_today = False

        delivered = []

        async def capture(msg):
            delivered.append(msg)

        engine.on_message(capture)
        await engine._evaluate()

        assert any(msg.trigger_id == "hr_elevated" for msg in delivered)
        assert engine._llm_task is not None
        assert not engine._llm_task.done()

        release.set()
        await engine._llm_task

    @pytest.mark.asyncio
    async def test_model_is_not_called_without_context(self, engine):
        engine._llm = AsyncMock()
        engine._perception._frames = {}
        engine._first_interaction_today = False
        engine._session_start = time.time()

        await engine._evaluate()

        assert engine._llm_task is None
        engine._llm.chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_repeated_ticks_keep_one_model_call_in_flight(self, engine):
        import asyncio

        release = asyncio.Event()

        class SlowLLM:
            def __init__(self):
                self.calls = 0

            async def chat(self, **_kwargs):
                self.calls += 1
                await release.wait()
                return "null"

        frame = MagicMock()
        frame.heart_rate = 72
        frame.heart_rate_sample_ts = time.time()
        frame.heart_rate_source = "test"
        frame.spo2_pct = 98
        frame.spo2_sample_ts = time.time()
        frame.spo2_source = "test"
        frame.activity_state = "working"
        frame.scene_description = "desk"
        frame.to_system_context.return_value = "Working at a desk"
        engine._perception._frames = {"s1": frame}
        engine._perception.get_frame.return_value = frame
        llm = SlowLLM()
        engine._llm = llm
        engine._first_interaction_today = False

        await engine._evaluate()
        first_task = engine._llm_task
        await asyncio.sleep(0)
        await engine._evaluate()

        assert engine._llm_task is first_task
        assert llm.calls == 1

        release.set()
        await first_task

    @pytest.mark.asyncio
    async def test_stop_returns_if_provider_ignores_cancellation(self, engine):
        import asyncio

        release = asyncio.Event()

        class StubbornLLM:
            async def chat(self, **_kwargs):
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                return (
                    '{"trigger_id":"llm_late","priority":"SUGGESTION",'
                    '"title":"Late","body":"Too late","action":""}'
                )

        frame = MagicMock()
        frame.heart_rate = 72
        frame.heart_rate_sample_ts = time.time()
        frame.heart_rate_source = "test"
        frame.spo2_pct = 98
        frame.spo2_sample_ts = time.time()
        frame.spo2_source = "test"
        frame.activity_state = "working"
        frame.scene_description = "desk"
        frame.to_system_context.return_value = "Working at a desk"
        engine._perception._frames = {"s1": frame}
        engine._perception.get_frame.return_value = frame
        engine._llm = StubbornLLM()
        engine._first_interaction_today = False
        engine._llm_shutdown_grace_s = 0.01
        delivered = []
        engine.on_message(delivered.append)

        await engine._evaluate()
        detached = engine._llm_task
        await asyncio.sleep(0)
        await engine.stop()

        assert not detached.done()
        release.set()
        await detached
        assert delivered == []

    @pytest.mark.asyncio
    async def test_stale_model_result_is_not_delivered(self, engine, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr("agents.proactive_engine.time.time", lambda: clock[0])
        engine._llm_result_max_age_s = 60
        engine._llm = AsyncMock()
        engine._llm.chat.return_value = (
            '{"trigger_id":"llm_stale","priority":"SUGGESTION",'
            '"title":"Stale","body":"Old context","action":""}'
        )
        frame = MagicMock()
        frame.heart_rate = 72
        frame.heart_rate_sample_ts = clock[0]
        frame.heart_rate_source = "test"
        frame.spo2_pct = 98
        frame.spo2_sample_ts = clock[0]
        frame.spo2_source = "test"
        frame.activity_state = "working"
        frame.scene_description = "desk"
        frame.to_system_context.return_value = "Old sensor context"
        engine._perception._frames = {"s1": frame}
        engine._perception.get_frame.return_value = frame
        engine._first_interaction_today = False
        delivered = []
        engine.on_message(delivered.append)

        await engine._evaluate()
        clock[0] = 1061.0
        await engine._llm_task

        assert delivered == []

    def test_llm_interval_is_configurable_without_shortening_below_one_minute(self):
        configured = ProactiveEngine(
            config={"features": {"proactive_llm_interval_s": 300}},
        )
        assert configured._llm_interval_s == 300

        bounded = ProactiveEngine(
            config={"features": {"proactive_llm_interval_s": 2}},
        )
        assert bounded._llm_interval_s == 60

        result_age = ProactiveEngine(
            config={"features": {"proactive_llm_result_max_age_s": 3600}},
        )
        assert result_age._llm_result_max_age_s == 3600


class TestRecordFire:
    def test_record_fire_updates_state(self, engine):
        engine._record_fire("t1")
        assert engine._trigger_states["t1"].fire_count == 1
        assert engine._trigger_states["t1"].last_fired > 0


class TestStopStart:
    def test_stop_sets_flag(self, engine):
        import asyncio

        engine._running = True
        # A7: stop() is now ``async def`` so it can cancel the running
        # evaluation task rather than merely flagging _running=False.
        asyncio.run(engine.stop())
        assert engine._running is False


class TestTriggerCounters:
    def test_counter_increments_on_fire(self, engine):
        engine._record_fire("test_trigger")
        assert engine._trigger_counts["test_trigger"] == 1
        engine._record_fire("test_trigger")
        assert engine._trigger_counts["test_trigger"] == 2

    def test_stats_returns_counts(self, engine):
        engine._record_fire("a")
        engine._record_fire("a")
        engine._record_fire("b")
        s = engine.stats()
        assert s["trigger_counts"]["a"] == 2
        assert s["trigger_counts"]["b"] == 1
        assert "nag_cooldown_s" in s

    def test_stats_empty_engine(self, engine):
        s = engine.stats()
        assert s["trigger_counts"] == {}
        assert s["running"] is False


class TestNagCooldown:
    def test_default_nag_cooldown(self, engine):
        assert engine._nag_cooldown_s == 300

    def test_custom_nag_cooldown_from_config(self):
        eng = ProactiveEngine(
            config={"features": {"proactive_nag_cooldown_s": 600}},
        )
        assert eng._nag_cooldown_s == 600

    def test_cooldown_applied_to_new_triggers(self, engine):
        engine._nag_cooldown_s = 120
        engine._record_fire("custom_cd")
        state = engine._trigger_states["custom_cd"]
        assert state.cooldown_s == 120

    @pytest.mark.asyncio
    async def test_cooldown_honored_blocks_second_fire(self, engine):
        """After firing, the same trigger can't fire again within cooldown."""
        engine._nag_cooldown_s = 9999
        engine._record_fire("once")
        assert engine._can_fire("once") is False
