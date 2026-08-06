"""The briefing must be built against the real engines, not against fakes.

Every data block in GET /api/ambient/briefing was broken, permanently, for
the whole life of the endpoint:

* ``baseline_engine.get_all_baselines()`` returns ``BaselineMetric``
  dataclasses. The route called ``.get("metric")`` on them, so it raised
  ``AttributeError`` on the first element and ``sleep`` was always None.
* ``intent_compiler.today()`` does not exist. The method is
  ``get_today_actions()``, and it returns the action list directly rather
  than ``{"actions": [...]}``.
* ``intent_compiler.list_active()`` does not exist. The method is
  ``list_plans()``, it returns every plan rather than only active ones, and
  its key is ``plan_id``, so ``p["id"]`` would have been a KeyError too.
* ``email_watcher.get_recent_vip`` is defined nowhere in the tree.

Each failure was caught and logged at debug, so the endpoint returned
``200`` with empty fields and nothing anywhere said why.

The reason it survived is the test that covered it. ``test_track0_fixes``
asserted the route did not 500, using hand-written fakes that implemented
``today()`` and ``list_active()``: fakes shaped like the bug rather than
like the code they stood in for. It passed for exactly as long as the
endpoint was broken, and would have kept passing.

So these tests use the real ``BaselineEngine`` and the real
``IntentCompiler``. Both are cheap to construct (in-memory SQLite, no LLM
needed for the read paths), which means there is no reason to fake them,
and a rename in either class fails here instead of being absorbed.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.baseline_engine import BaselineEngine
from agents.intent_compiler import ExecutionPlan, IntentCompiler, MicroAction
from api.routes import ambient as mod


def _state(**kw):
    """A stand-in for BrainState carrying only what the route reads."""
    s = type("S", (), {})()
    s.orchestrator = object()
    s.baseline_engine = kw.get("baseline_engine")
    s.intent_compiler = kw.get("intent_compiler")
    s.email_watcher = kw.get("email_watcher")
    s.vault = kw.get("vault")
    return s


def _briefing(monkeypatch, state):
    monkeypatch.setattr(mod, "state", state)
    monkeypatch.delenv("OPENWEATHER_API_KEY", raising=False)
    return asyncio.run(mod.get_briefing())


class TestSleepFromRealBaselineEngine:
    def test_recorded_hrv_reaches_the_briefing(self, monkeypatch):
        """The regression: this block returned None on real dataclasses."""
        engine = BaselineEngine()
        for v in (60.0, 62.0, 64.0):
            engine.record("hrv_ms", v, category="health")

        result = _briefing(monkeypatch, _state(baseline_engine=engine))

        assert result["sleep"] is not None, "sleep block is empty again"
        assert result["sleep"]["hrv_ms"] == pytest.approx(62.0)
        assert result["sleep"]["samples"] == 3
        assert "sleep" not in result["degraded"]

    def test_the_alternate_hrv_spelling_is_accepted(self, monkeypatch):
        """proactive_engine writes hrv_ms, ideas_engine writes hrv."""
        engine = BaselineEngine()
        engine.record("hrv", 55.0, category="health")
        result = _briefing(monkeypatch, _state(baseline_engine=engine))
        assert result["sleep"] is not None

    def test_no_hrv_recorded_is_absence_not_degradation(self, monkeypatch):
        """A brain that never saw HRV has nothing to report, which is not
        the same as a broken lookup. This install is exactly that case: it
        records hr_resting, spo2_pct and steps_daily, and no HRV at all."""
        engine = BaselineEngine()
        engine.record("hr_resting", 58.0)
        engine.record("steps_daily", 8000.0)

        result = _briefing(monkeypatch, _state(baseline_engine=engine))

        assert result["sleep"] is None
        assert result["degraded"] == []

    def test_reading_a_briefing_does_not_write_alerts(self, monkeypatch):
        """check_trend answers the same question but persists a
        BaselineAlert. A GET that manufactures alert history by being
        polled would corrupt the record it is reporting on."""
        engine = BaselineEngine()
        for v in (50.0, 55.0, 60.0, 65.0, 70.0, 75.0, 80.0, 85.0):
            engine.record("hrv_ms", v)

        before = len(engine.get_alerts())
        result = _briefing(monkeypatch, _state(baseline_engine=engine))

        assert result["sleep"]["trend"] == "upward"
        assert len(engine.get_alerts()) == before, "briefing wrote an alert"


class TestAgendaAndGoalsFromRealIntentCompiler:
    @staticmethod
    def _compiler_with_plans():
        c = IntentCompiler()
        active = ExecutionPlan(
            plan_id="p-active",
            intent="ship the relay",
            progress=0.4,
            status="active",
            micro_actions=[MicroAction(action_id="a1", description="write the broker")],
        )
        done = ExecutionPlan(
            plan_id="p-done",
            intent="already finished",
            status="completed",
            micro_actions=[MicroAction(action_id="a2", description="nope", completed=True)],
        )
        c._plans = {p.plan_id: p for p in (active, done)}
        return c

    def test_todays_actions_reach_the_agenda(self, monkeypatch):
        result = _briefing(monkeypatch, _state(intent_compiler=self._compiler_with_plans()))

        assert result["agenda"], "agenda is empty again"
        assert result["agenda"][0]["action"] == "write the broker"
        assert "agenda" not in result["degraded"]

    def test_goals_use_the_real_keys(self, monkeypatch):
        """list_plans returns plan_id and intent. The old code read p["id"]
        and p["goal"], neither of which the compiler has ever produced."""
        result = _briefing(monkeypatch, _state(intent_compiler=self._compiler_with_plans()))

        assert result["goals"], "goals is empty again"
        goal = result["goals"][0]
        assert goal["id"] == "p-active"
        assert goal["title"] == "ship the relay"
        assert goal["progress"] == pytest.approx(0.4)

    def test_completed_plans_are_not_listed_as_goals(self, monkeypatch):
        """list_active() implied a filter that list_plans() does not apply,
        so the filter has to live in the route or finished work comes back
        as a live goal."""
        result = _briefing(monkeypatch, _state(intent_compiler=self._compiler_with_plans()))
        assert [g["id"] for g in result["goals"]] == ["p-active"]


class TestFailureIsVisible:
    def test_a_broken_engine_is_named_in_degraded(self, monkeypatch):
        """The whole point: an empty field and a broken field must be
        distinguishable from the response alone."""
        class Exploding:
            def get_all_baselines(self):
                raise RuntimeError("db is gone")

        result = _briefing(monkeypatch, _state(baseline_engine=Exploding()))

        assert result["sleep"] is None
        assert "sleep" in result["degraded"]

    def test_a_broken_engine_logs_at_warning_not_debug(self, monkeypatch, caplog):
        """Debug is why this survived: nobody runs a brain at debug."""
        class Exploding:
            def get_all_baselines(self):
                raise RuntimeError("db is gone")

        with caplog.at_level("WARNING", logger="feral.ambient"):
            _briefing(monkeypatch, _state(baseline_engine=Exploding()))

        assert any("db is gone" in r.getMessage() for r in caplog.records), caplog.text

    def test_unimplemented_vip_recall_is_declared(self, monkeypatch):
        """EmailWatcher has no get_recent_vip, so an empty list here means
        nothing looked, not that the inbox was quiet."""
        result = _briefing(monkeypatch, _state(email_watcher=object()))

        assert result["vip_emails"] == []
        assert "vip_emails:not_implemented" in result["degraded"]

    def test_a_healthy_briefing_declares_nothing_degraded(self, monkeypatch):
        engine = BaselineEngine()
        engine.record("hrv_ms", 61.0)
        result = _briefing(
            monkeypatch,
            _state(baseline_engine=engine, intent_compiler=IntentCompiler()),
        )
        assert result["degraded"] == []


class TestTheRouteOnlyCallsMethodsThatExist:
    """Pin the call sites by name. If a rename lands in either engine, this
    fails with the missing name rather than the endpoint quietly emptying."""

    @pytest.mark.parametrize("method", ["get_all_baselines", "get_alerts", "record"])
    def test_baseline_engine_api(self, method):
        assert callable(getattr(BaselineEngine, method, None)), method

    @pytest.mark.parametrize("method", ["get_today_actions", "list_plans"])
    def test_intent_compiler_api(self, method):
        assert callable(getattr(IntentCompiler, method, None)), method

    @pytest.mark.parametrize("dead", ["today", "list_active"])
    def test_the_old_names_really_are_absent(self, dead):
        """Guard the guard: if someone adds today() later, the test above
        stops proving anything and this says so."""
        assert getattr(IntentCompiler, dead, None) is None
