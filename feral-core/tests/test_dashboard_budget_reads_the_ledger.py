"""The cost vital must read the thing that enforces the cap.

``/api/dashboard`` fed the system bar's cost readout from
``LLMProvider._budget_snapshot()``, which resolves
``FERAL_LLM_DAILY_SPEND_USD`` or the settings key ``llm.daily_spend_usd``.
The only producer of that key is the static ``0.0`` default in
``config/loader.py:187``; nothing writes it and it is not connected to
the ledger. So on the audited install the header read "$0.00" while
``CostBudget`` was refusing chat turns at $9.99 of a $10 hourly cap, and
``enabled`` was False because ``llm.daily_budget_usd`` was also unset,
which hid the cap entirely.

``state.cost_budget`` is the object that bills every call
(``record_usage``) and the object that refuses them
(``check_and_reserve``). Reading anything else guarantees the header and
the enforcer can disagree.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from api.routes import dashboard


class FakeBudget:
    """The three CostBudget members ``_budget_from_ledger`` touches."""

    def __init__(self, *, spend=None, caps=None):
        self._spend = spend or {}
        self._caps = caps or {}

    def current_spend(self, call_site=None, window="hour"):
        site = call_site or "__global__"
        return float(self._spend.get((site, window), 0.0))

    def _cap_for(self, call_site, window):
        return self._caps.get((call_site, window))


def _state(budget):
    fake = MagicMock()
    fake.cost_budget = budget
    return fake


class TestLedgerIsPreferred:
    def test_hourly_chat_spend_and_cap_are_reported(self, monkeypatch):
        budget = FakeBudget(
            spend={("chat", "hour"): 9.992715, ("__global__", "day"): 24.5},
            caps={("chat", "hour"): 10.0, ("__global__", "day"): 50.0},
        )
        monkeypatch.setattr(dashboard, "state", _state(budget))

        out = dashboard._budget_status()
        assert out["source"] == "cost_budget"
        assert out["hour_spend_usd"] == pytest.approx(9.992715)
        assert out["hour_cap_usd"] == pytest.approx(10.0)
        assert out["daily_spend_usd"] == pytest.approx(24.5)
        assert out["daily_budget_usd"] == pytest.approx(50.0)
        assert out["enabled"] is True

    def test_an_hourly_cap_alone_counts_as_enabled(self, monkeypatch):
        """The defect: a $10/hour chat cap reported "no budget configured"
        because ``enabled`` keyed off a daily LLM budget nothing sets."""
        budget = FakeBudget(
            spend={("chat", "hour"): 1.0},
            caps={("chat", "hour"): 10.0},
        )
        monkeypatch.setattr(dashboard, "state", _state(budget))

        out = dashboard._budget_status()
        assert out["enabled"] is True
        assert out["hour_cap_usd"] == pytest.approx(10.0)
        assert out["daily_budget_usd"] == 0.0

    def test_global_hourly_cap_stands_in_for_a_missing_chat_cap(self, monkeypatch):
        budget = FakeBudget(
            spend={("chat", "hour"): 0.25},
            caps={("__global__", "hour"): 2.0},
        )
        monkeypatch.setattr(dashboard, "state", _state(budget))
        assert dashboard._budget_status()["hour_cap_usd"] == pytest.approx(2.0)

    def test_no_caps_reports_spend_without_claiming_a_budget(self, monkeypatch):
        budget = FakeBudget(spend={("chat", "hour"): 0.4, ("__global__", "day"): 3.0})
        monkeypatch.setattr(dashboard, "state", _state(budget))

        out = dashboard._budget_status()
        assert out["enabled"] is False
        assert out["hour_spend_usd"] == pytest.approx(0.4)
        assert out["daily_spend_usd"] == pytest.approx(3.0)


class TestFallback:
    def test_provider_snapshot_is_used_when_there_is_no_ledger(self, monkeypatch):
        fake = MagicMock()
        fake.cost_budget = None
        fake.orchestrator.llm._budget_snapshot = lambda: {
            "enabled": True, "daily_budget_usd": 5.0, "daily_spend_usd": 1.0,
        }
        monkeypatch.setattr(dashboard, "state", fake)

        out = dashboard._budget_status()
        assert out["daily_budget_usd"] == pytest.approx(5.0)
        assert "source" not in out

    def test_an_unreadable_ledger_falls_back_rather_than_raising(self, monkeypatch):
        """A MagicMock state is the shape that has taken this endpoint
        down before; it must degrade, not 500."""
        fake = MagicMock()
        fake.orchestrator.llm._budget_snapshot = lambda: {"enabled": False}
        monkeypatch.setattr(dashboard, "state", fake)

        assert dashboard._budget_status() == {"enabled": False}

    def test_nothing_readable_reports_nothing(self, monkeypatch):
        class Bare:
            cost_budget = None
            orchestrator = None

        monkeypatch.setattr(dashboard, "state", Bare())
        assert dashboard._budget_status() == {}
