"""Unit tests for IdeasEngine — deterministic template composition."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from agents.ideas_engine import (
    IdeasEngine,
    IdeasStore,
    compose_about_me_confirm_idea,
    compose_baseline_alert_idea,
    compose_consciousness_waiting_idea,
    compose_morning_brief,
)


@pytest.fixture
def store(tmp_path):
    return IdeasStore(db_path=str(tmp_path / "ideas.db"))


# ----------------------------------------------------------------------
# Composers (deterministic)
# ----------------------------------------------------------------------


class TestMorningBrief:
    def test_empty_returns_none(self):
        assert compose_morning_brief() is None

    def test_counts_pending_and_inferred(self):
        idea = compose_morning_brief(
            pending_consciousness_count=2,
            inferred_about_me_count=1,
        )
        assert idea is not None
        assert idea.kind == "morning"
        assert "2 paused" in idea.text
        assert "1 new About-Me" in idea.text
        assert idea.action is not None
        assert idea.action.route == "/"

    def test_pluralization_singular(self):
        idea = compose_morning_brief(pending_consciousness_count=1)
        assert idea is not None
        assert "1 paused item from yesterday" in idea.text


class TestBaselineAlertIdea:
    def _alert(self, **overrides):
        base = dict(
            metric_id="hr_resting",
            alert_type="anomaly",
            severity="warning",
            deviation_sigma=2.5,
            baseline_mean=62.0,
            current_value=72.0,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_hr_template(self):
        idea = compose_baseline_alert_idea(self._alert())
        assert idea is not None
        assert "resting heart rate" in idea.text
        assert "2.5σ" in idea.text
        assert idea.severity == "warning"

    def test_critical_at_3_sigma(self):
        idea = compose_baseline_alert_idea(self._alert(deviation_sigma=3.5))
        assert idea is not None
        assert idea.severity == "critical"

    def test_sleep_template_attaches_routine_action(self):
        idea = compose_baseline_alert_idea(
            self._alert(metric_id="sleep_hours", current_value=5.5, baseline_mean=7.5, deviation_sigma=2.0)
        )
        assert idea is not None
        assert "wind-down" in idea.text
        assert idea.action is not None
        assert idea.action.kind == "install_routine"
        assert idea.action.payload == {"routine_id": "wind_down"}

    def test_unknown_metric_fallback_template(self):
        idea = compose_baseline_alert_idea(self._alert(metric_id="glucose"))
        assert idea is not None
        assert "glucose" in idea.text

    def test_direction_above_vs_below(self):
        above = compose_baseline_alert_idea(self._alert(current_value=95.0, baseline_mean=70.0))
        below = compose_baseline_alert_idea(self._alert(current_value=50.0, baseline_mean=70.0))
        assert above is not None and below is not None
        assert "above" in above.text
        assert "below" in below.text


class TestConsciousnessWaitingIdea:
    def test_references_summary_and_kind(self):
        entity = SimpleNamespace(
            id="abc",
            kind="intent",
            summary="ship v2026.4.23",
            updated_at=time.time() - 6 * 3600,
            status="waiting_user",
        )
        idea = compose_consciousness_waiting_idea(entity)
        assert idea is not None
        assert "ship v2026.4.23" in idea.text
        assert "intent" in idea.text
        assert idea.action is not None
        assert idea.action.kind == "resume_consciousness"
        assert idea.action.payload.get("consciousness_id") == "abc"

    def test_age_formatting(self):
        just_now = compose_consciousness_waiting_idea(
            SimpleNamespace(id="a", kind="thought", summary="s", updated_at=time.time(), status="waiting_user")
        )
        hours = compose_consciousness_waiting_idea(
            SimpleNamespace(id="b", kind="thought", summary="s", updated_at=time.time() - 5 * 3600, status="waiting_user")
        )
        days = compose_consciousness_waiting_idea(
            SimpleNamespace(id="c", kind="thought", summary="s", updated_at=time.time() - 2 * 86400, status="waiting_user")
        )
        assert just_now is not None and hours is not None and days is not None
        assert "just now" in just_now.text
        assert "h ago" in hours.text
        assert "d ago" in days.text


class TestAboutMeConfirmIdea:
    def test_reflects_fact_text(self):
        fact = SimpleNamespace(id="f1", text="Prefers: tea over coffee")
        idea = compose_about_me_confirm_idea(fact)
        assert idea is not None
        assert "Prefers: tea over coffee" in idea.text
        assert idea.action is not None
        assert idea.action.payload.get("fact_id") == "f1"

    def test_empty_text_returns_none(self):
        fact = SimpleNamespace(id="f1", text="   ")
        assert compose_about_me_confirm_idea(fact) is None


# ----------------------------------------------------------------------
# Engine-level
# ----------------------------------------------------------------------


class FakeConsciousness:
    def __init__(self, entities):
        self._entities = entities

    def list_active(self, *, kind=None, owner_session_id=None, include_abandoned=False):
        rows = list(self._entities)
        if kind:
            rows = [r for r in rows if getattr(r, "kind", "") == kind]
        return rows


class FakeAboutMe:
    def __init__(self, facts):
        self._facts = facts

    def list(self, **_kwargs):
        return list(self._facts)


class TestIdeasEngine:
    def test_morning_brief_triggers_updated(self, store):
        updates = []
        engine = IdeasEngine(
            store=store,
            consciousness=FakeConsciousness(
                [SimpleNamespace(id="a", kind="thought", summary="draft", updated_at=time.time(), status="paused")]
            ),
            about_me=FakeAboutMe(
                [SimpleNamespace(id="f1", text="Prefers: tea", source="inferred_from_chat", confidence=0.5)]
            ),
            on_ideas_updated=lambda xs: updates.append(len(xs)),
        )
        stored = engine.morning_brief()
        assert stored
        assert any(i.kind == "morning" for i in stored)
        assert any(i.kind == "about" for i in stored)
        assert updates  # notify fired

    def test_morning_brief_silent_when_nothing(self, store):
        engine = IdeasEngine(
            store=store,
            consciousness=FakeConsciousness([]),
            about_me=FakeAboutMe([]),
        )
        assert engine.morning_brief() == []

    def test_baseline_alert_records_idea(self, store):
        engine = IdeasEngine(store=store)
        alert = SimpleNamespace(
            metric_id="sleep_hours",
            alert_type="trend",
            severity="info",
            deviation_sigma=1.5,
            baseline_mean=7.5,
            current_value=5.0,
        )
        idea = engine.handle_baseline_alert(alert)
        assert idea is not None
        assert idea.kind == "health"
        today = engine.list_today()
        assert any(i.id == idea.id for i in today)

    def test_dismiss_weight_suppresses_future(self, store):
        engine = IdeasEngine(store=store)
        alert = SimpleNamespace(
            metric_id="hr_resting",
            alert_type="anomaly",
            severity="warning",
            deviation_sigma=2.2,
            baseline_mean=60.0,
            current_value=72.0,
        )
        ideas = []
        for _ in range(3):
            i = engine.handle_baseline_alert(alert)
            assert i is not None
            engine.dismiss(i.id)
            ideas.append(i)
        fourth = engine.handle_baseline_alert(alert)
        assert fourth is None

    def test_refresh_waiting_user_only_returns_waiting(self, store):
        waiting = SimpleNamespace(id="w1", kind="flow", summary="deploy x", updated_at=time.time(), status="waiting_user")
        active = SimpleNamespace(id="a1", kind="flow", summary="train y", updated_at=time.time(), status="active")
        engine = IdeasEngine(
            store=store,
            consciousness=FakeConsciousness([waiting, active]),
        )
        stored = engine.refresh_waiting_user()
        assert len(stored) == 1
        assert "deploy x" in stored[0].text

    def test_llm_polish_opt_in(self, store):
        called = []

        def polish(text):
            called.append(text)
            return text.upper()

        engine = IdeasEngine(
            store=store,
            llm_polish_enabled=False,
            llm_polish_fn=polish,
        )
        idea = engine.handle_baseline_alert(
            SimpleNamespace(
                metric_id="hrv",
                alert_type="anomaly",
                severity="warning",
                deviation_sigma=2.0,
                baseline_mean=50.0,
                current_value=32.0,
            )
        )
        assert idea is not None
        assert not called  # polish should NOT fire by default

        engine.set_llm_polish(True, polish)
        # Use a DIFFERENT signal (sleep_hours, not hrv) so the per-signal
        # dedup guard doesn't suppress this second card — the first hrv
        # idea above is still active. This test asserts polish opt-in, not
        # dedup behaviour.
        idea2 = engine.handle_baseline_alert(
            SimpleNamespace(
                metric_id="sleep_hours",
                alert_type="anomaly",
                severity="warning",
                deviation_sigma=2.0,
                baseline_mean=8.0,
                current_value=5.0,
            )
        )
        assert idea2 is not None
        assert called  # fired exactly on the opt-in path

    def test_repeated_same_signal_alert_deduped(self, store):
        """A sustained anomaly must not stack identical cards.

        Operator report 2026-06-07: 20 byte-identical "resting HR 51 /
        baseline 100" cards because the proactive loop fired one idea per
        tick. The store-level dedup keeps a single active card per signal.
        """
        engine = IdeasEngine(store=store)
        alert = SimpleNamespace(
            metric_id="hr_resting",
            alert_type="anomaly",
            severity="warning",
            deviation_sigma=2.0,
            baseline_mean=100.0,
            current_value=51.0,
        )
        first = engine.handle_baseline_alert(alert)
        second = engine.handle_baseline_alert(alert)
        third = engine.handle_baseline_alert(alert)
        assert first is not None
        assert second is None  # deduped — first is still active
        assert third is None
        active = [i for i in store.list_today() if i.kind == "health"]
        assert len(active) == 1

    def test_dedup_releases_after_dismiss(self, store):
        """Once the user clears the card, a fresh anomaly can surface again."""
        engine = IdeasEngine(store=store)
        alert = SimpleNamespace(
            metric_id="hr_resting",
            alert_type="anomaly",
            severity="warning",
            deviation_sigma=2.0,
            baseline_mean=100.0,
            current_value=51.0,
        )
        first = engine.handle_baseline_alert(alert)
        assert first is not None
        store.record_dismiss(first.id)
        # Dismiss weight is 1 (< 3 suppression threshold) so the signal is
        # eligible again, and no active card blocks it.
        again = engine.handle_baseline_alert(alert)
        assert again is not None

    def test_polish_failure_is_swallowed(self, store):
        engine = IdeasEngine(
            store=store,
            llm_polish_enabled=True,
            llm_polish_fn=lambda t: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        idea = engine.handle_baseline_alert(
            SimpleNamespace(
                metric_id="steps",
                alert_type="trend",
                severity="info",
                deviation_sigma=1.5,
                baseline_mean=8000,
                current_value=2000,
            )
        )
        assert idea is not None  # no crash


class TestIdeasStore:
    def test_list_today_excludes_accepted_and_dismissed(self, store):
        from agents.ideas_engine import Idea

        a = Idea(id="a", kind="work", text="a")
        b = Idea(id="b", kind="work", text="b")
        c = Idea(id="c", kind="work", text="c")
        for i in (a, b, c):
            store.insert(i)
        store.record_accept("a")
        store.record_dismiss("b")
        today = {i.id for i in store.list_today()}
        assert today == {"c"}

    def test_sweep_drops_expired(self, store):
        from agents.ideas_engine import Idea

        fresh = Idea(id="fresh", kind="morning", text="x")
        stale = Idea(id="stale", kind="morning", text="y", expires_at=time.time() - 10)
        store.insert(fresh)
        store.insert(stale)
        assert store.sweep_expired() == 1
        assert {i.id for i in store.list_today()} == {"fresh"}
