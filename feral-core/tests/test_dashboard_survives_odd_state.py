"""`/api/dashboard` is the one endpoint the whole shell polls.

Every vital in the system bar, the counts on the dock tiles and the
rail's idea of what is running all come from this one response. When it
500s the entire chrome goes blank at once, so a field added to it has to
be unable to take it down.

One did. `uptime_s` was written as:

    max(0.0, time.time() - getattr(state, "started_at", time.time()))

which looks defensive and is not. A `MagicMock` has every attribute, so
the `getattr` default never applied under test, `time.time() - <mock>`
raised TypeError, and the endpoint answered 500. Two suites caught it as
`assert 500 == 200` with no traceback, which is a slow way to find a
one-line arithmetic bug.

The same shape reaches production: a BrainState restored without the
field, or anything that leaves a non-numeric there, would have done
exactly the same to a real install.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from api.routes import dashboard


class TestUptimeCannotTakeTheDashboardDown:
    def test_a_mock_state_does_not_raise(self, monkeypatch):
        """The literal shape that shipped a 500.

        Asserts "returns a number without raising", not a particular
        number. `MagicMock` implements `__float__` and answers 1.0, so a
        mocked state yields an absurd uptime measured from the epoch.
        That is fine and is not what this guards: the property is that
        the endpoint every surface polls cannot be taken down by this
        field. My first version of this test asserted 0.0 and failed for
        that reason, which is the test being wrong rather than the code.
        """
        monkeypatch.setattr(dashboard, "state", MagicMock())
        value = dashboard._uptime_seconds()
        assert isinstance(value, float)
        assert value >= 0.0

    @pytest.mark.parametrize("value", [None, "", "not a number", object(), [], {}])
    def test_a_junk_start_time_reads_as_unknown(self, monkeypatch, value):
        fake = MagicMock()
        fake.started_at = value
        monkeypatch.setattr(dashboard, "state", fake)
        assert dashboard._uptime_seconds() == 0.0

    def test_a_missing_attribute_reads_as_unknown(self, monkeypatch):
        class Bare:
            pass

        monkeypatch.setattr(dashboard, "state", Bare())
        assert dashboard._uptime_seconds() == 0.0

    def test_a_real_start_time_measures_up(self, monkeypatch):
        import time

        fake = MagicMock()
        fake.started_at = time.time() - 120.0
        monkeypatch.setattr(dashboard, "state", fake)
        assert 119.0 <= dashboard._uptime_seconds() <= 130.0

    def test_a_future_start_time_is_clamped_not_negative(self, monkeypatch):
        """A clock step backwards must not produce a negative uptime."""
        import time

        fake = MagicMock()
        fake.started_at = time.time() + 500.0
        monkeypatch.setattr(dashboard, "state", fake)
        assert dashboard._uptime_seconds() == 0.0


class TestTheOtherAddedFieldsAreEquallyGuarded:
    """`budget` and `autonomy` were added to this payload at the same
    time and reach into the orchestrator and the LLM provider, both of
    which are absent or mocked in plenty of states."""

    def test_budget_on_a_mock_state_returns_a_dict(self, monkeypatch):
        monkeypatch.setattr(dashboard, "state", MagicMock())
        assert isinstance(dashboard._budget_status(), dict)

    def test_budget_with_no_orchestrator_is_empty_not_zeroed(self, monkeypatch):
        class Bare:
            pass

        monkeypatch.setattr(dashboard, "state", Bare())
        # Empty, deliberately: reporting $0.00 when the number could not
        # be read is a claim, and a bar cannot tell that apart from a
        # real zero.
        assert dashboard._budget_status() == {}

    def test_autonomy_on_a_mock_state_returns_a_string(self, monkeypatch):
        monkeypatch.setattr(dashboard, "state", MagicMock())
        assert isinstance(dashboard._autonomy_mode(), str)

    def test_autonomy_with_no_orchestrator_is_empty(self, monkeypatch):
        class Bare:
            pass

        monkeypatch.setattr(dashboard, "state", Bare())
        assert dashboard._autonomy_mode() == ""
