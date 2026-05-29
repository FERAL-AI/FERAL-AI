"""Unit tests for the CostBudget service."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cost.budget import (
    DEFAULT_COST_SETTINGS,
    BudgetExceeded,
    CostBudget,
    window_reset_at,
    window_start,
)
from cost.pricing import ModelPricing, compute_token_cost
from cost import telemetry as cost_telemetry


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    path = tmp_path / "model_catalog.json"
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "anthropic": {
                        "pricing": {
                            "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
                            "claude-opus-4-7": {"input": 0.005, "output": 0.025},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def pricing(catalog_path: Path) -> ModelPricing:
    return ModelPricing(catalog_path=catalog_path)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cost.db"


@pytest.fixture
async def budget(db_path: Path, pricing: ModelPricing) -> CostBudget:
    bud = CostBudget(
        db_path=db_path,
        pricing=pricing,
        settings={
            "cost": {
                "enabled": True,
                "per_call_site_caps": {"screen_loop": {"per_hour_usd": 1.0}},
                "global_per_hour_usd": 10.0,
                "global_per_day_usd": 100.0,
            }
        },
    )
    await bud.ensure_ready()
    yield bud
    await bud.close()


class TestPricing:
    def test_compute_token_cost_basic(self, pricing: ModelPricing):
        total, rates = compute_token_cost(
            pricing, "claude-sonnet-4-6", 1000, 1000, 0
        )
        assert rates["input"] == pytest.approx(0.003)
        assert rates["output"] == pytest.approx(0.015)
        assert total == pytest.approx(0.003 + 0.015)

    def test_reasoning_tokens_use_output_rate(self, pricing: ModelPricing):
        base, _ = compute_token_cost(pricing, "claude-sonnet-4-6", 0, 1000, 0)
        with_reasoning, _ = compute_token_cost(
            pricing, "claude-sonnet-4-6", 0, 1000, 500
        )
        assert with_reasoning == pytest.approx(base + (500 / 1000.0) * 0.015)

    def test_openrouter_slug_normalization(self, pricing: ModelPricing):
        rates = pricing.lookup("anthropic/claude-sonnet-4.6")
        assert rates["output"] == pytest.approx(0.015)


class TestCostBudget:
    async def test_record_usage_increments_spend(self, budget: CostBudget):
        spent = await budget.record_usage(
            "screen_loop", "claude-sonnet-4-6", 1000, 1000
        )
        assert spent == pytest.approx(0.018)
        assert budget.current_spend("screen_loop", "hour") == pytest.approx(0.018)
        assert budget.current_spend(None, "hour") == pytest.approx(0.018)

    async def test_check_and_reserve_allows_under_cap(self, budget: CostBudget):
        assert budget.check_and_reserve("screen_loop", "claude-sonnet-4-6", 1000)

    async def test_check_and_reserve_blocks_at_cap(self, budget: CostBudget):
        budget.set_cap("screen_loop", "hour", 0.01)
        await budget.record_usage("screen_loop", "claude-sonnet-4-6", 1000, 100)
        assert not budget.check_and_reserve("screen_loop", "claude-sonnet-4-6", 5000)

    async def test_record_usage_raises_budget_exceeded(self, budget: CostBudget):
        budget.set_cap("screen_loop", "hour", 0.005)
        with pytest.raises(BudgetExceeded) as exc:
            await budget.record_usage("screen_loop", "claude-sonnet-4-6", 1000, 1000)
        err = exc.value
        assert err.call_site == "screen_loop"
        assert err.window == "hour"
        assert err.cap_dollars == pytest.approx(0.005)
        assert err.current_dollars > err.cap_dollars
        assert err.reset_at == pytest.approx(window_reset_at("hour"))

    async def test_hourly_rollover_resets_counters(
        self, db_path: Path, pricing: ModelPricing, monkeypatch
    ):
        hour_one = datetime(2026, 5, 21, 10, 30, tzinfo=timezone.utc).timestamp()
        hour_two = datetime(2026, 5, 21, 11, 5, tzinfo=timezone.utc).timestamp()
        monkeypatch.setattr("cost.budget.time.time", lambda: hour_one)

        bud = CostBudget(db_path=db_path, pricing=pricing)
        bud.set_cap("screen_loop", "hour", 10.0)
        await bud.ensure_ready()
        await bud.record_usage("screen_loop", "claude-sonnet-4-6", 1000, 1000)
        assert bud.current_spend("screen_loop", "hour") == pytest.approx(0.018)

        monkeypatch.setattr("cost.budget.time.time", lambda: hour_two)
        bud2 = CostBudget(db_path=db_path, pricing=pricing)
        await bud2.ensure_ready()
        assert bud2.current_spend("screen_loop", "hour") == pytest.approx(0.0)
        await bud.close()
        await bud2.close()

    async def test_persistence_across_restart(
        self, db_path: Path, pricing: ModelPricing
    ):
        bud = CostBudget(db_path=db_path, pricing=pricing)
        await bud.ensure_ready()
        await bud.record_usage("screen_loop", "claude-sonnet-4-6", 2000, 500)
        await bud.close()

        bud2 = CostBudget(db_path=db_path, pricing=pricing)
        await bud2.ensure_ready()
        assert bud2.current_spend("screen_loop", "hour") == pytest.approx(0.0135)
        await bud2.close()

    async def test_budget_exceeded_shape(self, budget: CostBudget):
        budget.set_cap("screen_loop", "hour", 0.001)
        with pytest.raises(BudgetExceeded) as exc:
            await budget.record_usage("screen_loop", "claude-sonnet-4-6", 500, 50)
        err = exc.value
        assert hasattr(err, "call_site")
        assert hasattr(err, "cap_dollars")
        assert hasattr(err, "current_dollars")
        assert hasattr(err, "window")
        assert hasattr(err, "reset_at")

    async def test_reset_clears_spend(self, budget: CostBudget):
        await budget.record_usage("screen_loop", "claude-sonnet-4-6", 1000, 100)
        await budget.reset("screen_loop")
        assert budget.current_spend("screen_loop", "hour") == pytest.approx(0.0)

    async def test_telemetry_counters_increment(self, budget: CostBudget):
        await budget.record_usage("screen_loop", "claude-sonnet-4-6", 1000, 100)
        values = cost_telemetry.counter_values(
            {"call_site": "screen_loop", "model": "claude-sonnet-4-6"}
        )
        assert values.get("feral_cost_calls_total", 0) >= 1.0


class TestDefaults:
    def test_default_cost_settings_schema(self):
        """v2026.5.47 — the factory defaults ship UNLIMITED. The
        per-call-site registry is still populated so introspection
        surfaces (the CLI doctor probe + the Settings UI) can list
        every wired subsystem, but each entry has NO ``per_hour_usd``
        — that's the contract for "no cap configured". Globals are
        omitted entirely."""
        cost = DEFAULT_COST_SETTINGS["cost"]
        assert cost["enabled"] is True
        assert "global_per_day_usd" not in cost
        assert "global_per_hour_usd" not in cost
        for site in (
            "screen_loop", "proactive", "routing", "chat",
            "vision", "embedding", "learner", "compaction",
        ):
            assert site in cost["per_call_site_caps"], (
                f"{site} missing from the known-subsystem registry"
            )
            assert "per_hour_usd" not in cost["per_call_site_caps"][site], (
                f"{site} ships a factory cap; expected unlimited"
            )


class TestLiveCapTrip:
    async def test_loop_until_cap(self, db_path: Path, pricing: ModelPricing):
        bud = CostBudget(db_path=db_path, pricing=pricing)
        bud.set_cap("screen_loop", "hour", 0.05)
        await bud.ensure_ready()
        capped_at = None
        try:
            for i in range(100):
                await bud.record_usage("screen_loop", "claude-sonnet-4-6", 5000, 50)
                if not bud.check_and_reserve("screen_loop", "claude-sonnet-4-6", 1000):
                    capped_at = i
                    break
        except BudgetExceeded:
            capped_at = "raised"
        assert capped_at is not None
        await bud.close()
