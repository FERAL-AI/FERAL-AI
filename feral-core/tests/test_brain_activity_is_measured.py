"""The dashboard reported a field that was permanently empty.

`brain_activity` carries the two rows the design's Brain readout wants
and the brain never measured: when a turn last ran, and how full the
context view was.

The first version of the dashboard helper did this:

    status = getattr(orch, "runtime_status", None)
    if not callable(status):
        return {}
    data = status()

`Orchestrator.runtime_status` is a `@property`. So `status` was already
the dict, `callable()` was False, and the helper returned `{}` on every
single request. The field was present, always empty, and raised nothing:
no error, no log, no failing test. It was found by making the bail-out
say which type it saw.

That is the shape this codebase keeps producing, so the tests below pin
both halves: the orchestrator actually measures the numbers, and the
dashboard actually reads them off a property rather than calling it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from api.routes import dashboard


class TestRuntimeStatusIsAPropertyAndStaysOne:
    def test_it_is_declared_as_a_property(self):
        """If this becomes a method, `_brain_activity` must change with
        it, and the failure mode is a silently empty field rather than
        an exception."""
        from agents.orchestrator import Orchestrator

        attr = inspect_attr(Orchestrator, "runtime_status")
        assert isinstance(attr, property), (
            "runtime_status is no longer a property. api/routes/dashboard.py "
            "and /api/system/info both read it WITHOUT calling it; update "
            "them together or the dashboard field goes quietly empty."
        )

    def test_it_carries_the_two_activity_numbers(self):
        from agents.orchestrator import Orchestrator

        src = inspect_source(Orchestrator, "runtime_status")
        assert "last_turn_at" in src
        assert "context_used_pct" in src


class TestTheDashboardReadsIt:
    def test_a_property_is_read_not_called(self, monkeypatch):
        orch = MagicMock()
        # A MagicMock attribute is callable, which is exactly what made
        # the original guard look correct under a mock and fail in
        # production. Set a real dict instead.
        orch.runtime_status = {"last_turn_at": 1234.5, "context_used_pct": 18.0}
        fake = MagicMock()
        fake.orchestrator = orch
        monkeypatch.setattr(dashboard, "state", fake)

        out = dashboard._brain_activity()
        assert out == {"last_turn_at": 1234.5, "context_used_pct": 18.0}

    def test_no_orchestrator_reports_nothing_rather_than_zeros(self, monkeypatch):
        class Bare:
            pass

        monkeypatch.setattr(dashboard, "state", Bare())
        # Empty, deliberately. "0% used" and "we cannot tell" are
        # different facts and a readout cannot distinguish them.
        assert dashboard._brain_activity() == {}

    @pytest.mark.parametrize("junk", [None, "nope", 42, []])
    def test_a_non_dict_status_is_refused(self, monkeypatch, junk):
        orch = MagicMock()
        orch.runtime_status = junk
        fake = MagicMock()
        fake.orchestrator = orch
        monkeypatch.setattr(dashboard, "state", fake)
        assert dashboard._brain_activity() == {}

    def test_missing_keys_read_as_unknown(self, monkeypatch):
        orch = MagicMock()
        orch.runtime_status = {"multi_agent_enabled": True}
        fake = MagicMock()
        fake.orchestrator = orch
        monkeypatch.setattr(dashboard, "state", fake)
        assert dashboard._brain_activity() == {
            "last_turn_at": 0.0, "context_used_pct": 0.0,
        }


class TestContextPercentIsMeasuredAgainstTheRightBudget:
    def test_zero_when_nothing_has_been_built(self):
        from agents.context_manager import ContextManager

        orch = _bare_orchestrator()
        orch.context_manager = ContextManager(max_messages=15)
        orch._last_context_chars = 0
        assert orch.context_used_pct() == 0.0

    def test_it_is_a_share_of_the_history_budget_not_the_whole_window(self):
        """The conversation is only allowed part of the model's window:
        the rest is the system prompt, tool schemas and the memory
        block. Dividing by the full window reports a number that is
        always comfortable and never true."""
        from agents.context_manager import ContextManager

        cm = ContextManager(max_messages=15, context_window_tokens=100_000)
        orch = _bare_orchestrator()
        orch.context_manager = cm
        orch._last_context_chars = cm.history_budget_chars // 2
        assert 45.0 <= orch.context_used_pct() <= 55.0

    def test_it_never_exceeds_one_hundred(self):
        from agents.context_manager import ContextManager

        cm = ContextManager(max_messages=15, context_window_tokens=1_000)
        orch = _bare_orchestrator()
        orch.context_manager = cm
        orch._last_context_chars = cm.history_budget_chars * 50
        assert orch.context_used_pct() == 100.0

    def test_a_broken_manager_reports_unknown_rather_than_raising(self):
        orch = _bare_orchestrator()
        orch.context_manager = object()   # no history_budget_chars
        orch._last_context_chars = 500
        assert orch.context_used_pct() == 0.0


class TestTheTurnStampIsRecordedBeforeTheEarlyReturn:
    def test_compaction_being_disabled_still_records_the_turn(self, monkeypatch):
        """`_maybe_auto_compact` returns early when compaction is off.
        A turn still happened, so the stamp is taken first.

        Asserted on behaviour rather than on source order. The source
        check this replaces looked for ``load_settings`` inside
        ``_maybe_auto_compact``; the settings read has since moved into
        ``_compaction_cfg`` so that the consolidation scheduler can
        share it, and a test that fails on a refactor while the defect
        it guards is still fixed is a test that measures the wrong
        thing.
        """
        import config.loader as loader

        from agents.orchestrator import Orchestrator

        monkeypatch.setattr(
            loader, "load_settings",
            lambda: {"memory": {"compaction": {"enabled": False}}},
        )

        orch = Orchestrator.__new__(Orchestrator)
        orch._last_turn_at = 0.0
        orch._session_last_turn_at = {}
        orch._turns_since_compaction = {}
        orch._compaction_inflight = {}
        orch._pending_since = {}

        orch._maybe_auto_compact("s")

        assert orch._last_turn_at > 0.0, (
            "the turn stamp is taken after the settings read, so a brain "
            "with compaction disabled would report that it has never run "
            "a turn"
        )
        assert orch._turns_since_compaction == {}, (
            "compaction is disabled; the backlog counter must not advance"
        )


def _bare_orchestrator():
    """An Orchestrator shell with only what these tests touch.

    Constructing a real one boots the whole agent stack, which is not
    what is under test here.
    """
    from agents.orchestrator import Orchestrator

    return Orchestrator.__new__(Orchestrator)


def inspect_attr(cls, name):
    for klass in cls.__mro__:
        if name in klass.__dict__:
            return klass.__dict__[name]
    raise AssertionError(f"{cls.__name__} has no attribute {name}")


def inspect_source(cls, name):
    import inspect as _inspect

    attr = inspect_attr(cls, name)
    fn = attr.fget if isinstance(attr, property) else attr
    return _inspect.getsource(fn)
