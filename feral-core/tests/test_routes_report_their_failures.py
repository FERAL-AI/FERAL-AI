"""A route that lost data must not answer as though it had none.

Twenty-one handlers under api/routes caught an exception, logged it at
debug and returned a shape indistinguishable from the healthy empty case.
Nobody runs a brain at debug, so each one converted a broken subsystem
into a calm-looking answer.

The endpoint these tests care about most is /api/jobs, which exists purely
to answer "what is the brain doing right now" and had five aggregators
that each returned [] on failure. A dead aggregator and an idle system
produced byte-identical responses.

The rule being pinned: isolation stays (one bad source must not take the
endpoint down) but it is reported, never hidden.
"""

from __future__ import annotations

import asyncio

import pytest

from api.routes import ideas as ideas_mod
from api.routes import jobs as jobs_mod


def _run(coro):
    return asyncio.run(coro)


class TestJobsReportsDeadSources:
    @pytest.fixture(autouse=True)
    def _quiet_state(self, monkeypatch):
        s = type("S", (), {})()
        s.taskflows = None
        s.cron_service = None
        s.agent_mitosis = None
        s.tool_genesis = None
        s.daemons = {}
        monkeypatch.setattr(jobs_mod, "state", s)
        return s

    def test_an_idle_system_reports_nothing_degraded(self):
        body = _run(jobs_mod.list_jobs())
        assert body["count"] == 0
        assert body["degraded"] == {}

    def test_a_failing_aggregator_is_named(self, monkeypatch):
        """The regression: this used to be identical to an idle system."""
        class Exploding:
            def list_flows(self, **kw):
                raise RuntimeError("taskflow store is gone")

        monkeypatch.setattr(jobs_mod.state, "taskflows", Exploding())
        body = _run(jobs_mod.list_jobs())

        assert body["degraded"], "a dead aggregator still reports as idle"
        assert "taskflow" in body["degraded"]
        assert "taskflow store is gone" in body["degraded"]["taskflow"]

    def test_one_dead_source_does_not_take_the_endpoint_down(self, monkeypatch):
        """Isolation is still the contract; it just is not silent."""
        class Exploding:
            def list_flows(self, **kw):
                raise RuntimeError("boom")

        class OneDaemon(dict):
            pass

        monkeypatch.setattr(jobs_mod.state, "taskflows", Exploding())
        ws = type("WS", (), {"_feral_node_type": "glasses", "_feral_capabilities": []})()
        monkeypatch.setattr(jobs_mod.state, "daemons", {"node-1": ws})

        body = _run(jobs_mod.list_jobs())

        assert "taskflow" in body["degraded"]
        assert body["counts_by_kind"]["daemon"] == 1
        assert any(i["kind"] == "daemon" for i in body["items"])

    def test_a_failing_aggregator_logs_at_warning(self, monkeypatch, caplog):
        class Exploding:
            def list_flows(self, **kw):
                raise RuntimeError("store is gone")

        monkeypatch.setattr(jobs_mod.state, "taskflows", Exploding())
        with caplog.at_level("WARNING", logger="feral.api.jobs"):
            _run(jobs_mod.list_jobs())

        assert any("store is gone" in r.getMessage() for r in caplog.records), caplog.text

    def test_filtering_by_kind_still_reports_that_kind(self, monkeypatch):
        class Exploding:
            def list_flows(self, **kw):
                raise RuntimeError("boom")

        monkeypatch.setattr(jobs_mod.state, "taskflows", Exploding())
        body = _run(jobs_mod.list_jobs(kind="taskflow"))
        assert "taskflow" in body["degraded"]


class TestIdeasStopsClaimingSuccess:
    @staticmethod
    def _engine(brief_exc=None, waiting_exc=None):
        class Engine:
            def morning_brief(self):
                if brief_exc:
                    raise brief_exc
                return []

            def refresh_waiting_user(self):
                if waiting_exc:
                    raise waiting_exc
                return []

            def list_today(self):
                return []
        return Engine()

    def test_both_generators_failing_is_not_success(self, monkeypatch):
        """It returned success:True unconditionally, so an empty pane meant
        either a quiet day or a wholly broken engine."""
        monkeypatch.setattr(
            ideas_mod, "_require_engine",
            lambda: self._engine(RuntimeError("a"), RuntimeError("b")),
        )
        body = _run(ideas_mod.refresh_ideas()) if asyncio.iscoroutinefunction(
            ideas_mod.refresh_ideas) else ideas_mod.refresh_ideas()

        assert body["success"] is False
        assert set(body["degraded"]) == {"morning_brief", "refresh_waiting_user"}

    def test_one_generator_failing_still_succeeds_but_is_reported(self, monkeypatch):
        monkeypatch.setattr(
            ideas_mod, "_require_engine",
            lambda: self._engine(brief_exc=RuntimeError("only one")),
        )
        body = _run(ideas_mod.refresh_ideas()) if asyncio.iscoroutinefunction(
            ideas_mod.refresh_ideas) else ideas_mod.refresh_ideas()

        assert body["success"] is True
        assert "morning_brief" in body["degraded"]
        assert "refresh_waiting_user" not in body["degraded"]

    def test_a_healthy_refresh_declares_nothing(self, monkeypatch):
        monkeypatch.setattr(ideas_mod, "_require_engine", lambda: self._engine())
        body = _run(ideas_mod.refresh_ideas()) if asyncio.iscoroutinefunction(
            ideas_mod.refresh_ideas) else ideas_mod.refresh_ideas()

        assert body["success"] is True
        assert body["degraded"] == {}


class TestWindDownRecapIsImplemented:
    """get_completed_today sat behind a hasattr guard for a method that did
    not exist, so the evening recap reported an empty day forever."""

    def test_intent_compiler_actually_has_the_method(self):
        from agents.intent_compiler import IntentCompiler
        assert callable(getattr(IntentCompiler, "get_completed_today", None))

    def test_it_returns_work_finished_today(self):
        import time

        from agents.intent_compiler import (
            ExecutionPlan,
            IntentCompiler,
            MicroAction,
        )

        c = IntentCompiler()
        plan = ExecutionPlan(
            plan_id="p1",
            intent="ship the relay",
            micro_actions=[
                MicroAction(action_id="a1", description="wrote the broker",
                            completed=True, completed_at=time.time()),
                MicroAction(action_id="a2", description="not done yet"),
            ],
        )
        c._plans = {"p1": plan}

        done = c.get_completed_today()
        assert [d["action"] for d in done] == ["wrote the broker"]
        assert done[0]["intent"] == "ship the relay"

    def test_yesterdays_work_is_not_todays_recap(self):
        import time

        from agents.intent_compiler import (
            ExecutionPlan,
            IntentCompiler,
            MicroAction,
        )

        c = IntentCompiler()
        c._plans = {"p1": ExecutionPlan(
            plan_id="p1",
            micro_actions=[MicroAction(action_id="a1", description="old",
                                       completed=True,
                                       completed_at=time.time() - 86400 * 2)],
        )}
        assert c.get_completed_today() == []

    def test_completed_plans_still_count_toward_today(self):
        """Unlike get_today_actions this must not filter on plan status:
        finishing the last action completes the plan, and that is exactly
        the thing worth recapping."""
        import time

        from agents.intent_compiler import (
            ExecutionPlan,
            IntentCompiler,
            MicroAction,
        )

        c = IntentCompiler()
        c._plans = {"p1": ExecutionPlan(
            plan_id="p1",
            status="completed",
            micro_actions=[MicroAction(action_id="a1", description="finished it",
                                       completed=True, completed_at=time.time())],
        )}
        assert len(c.get_completed_today()) == 1
