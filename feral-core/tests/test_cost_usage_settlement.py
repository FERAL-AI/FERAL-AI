"""Incurred usage survives cap crossings, restart and ledger write failure."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from agents.llm_provider import LLMProvider
from cost.budget import BudgetExceeded, CostBudget
from cost.pricing import ModelPricing


@pytest.fixture
def ledger(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"providers": {"fixture": {"pricing": {
        "fixture-model": {"input": 0.5, "output": 1.0},
    }}}}))
    return {
        "db_path": tmp_path / "cost.db",
        "pricing": ModelPricing(catalog_path=catalog),
        "settings": {"cost": {"global_per_hour_usd": 1.5}},
    }


async def _charge(budget):
    return await budget.record_usage("chat", "fixture-model", 0, 1000)


def _events(ledger):
    with sqlite3.connect(ledger["db_path"]) as conn:
        return conn.execute("SELECT count(*), sum(dollars) FROM cost_events").fetchone()


async def test_cap_crossing_is_persisted_before_warning_and_survives_restart(ledger):
    budget = CostBudget(**ledger)
    try:
        await _charge(budget)
        with pytest.raises(BudgetExceeded) as exc:
            await _charge(budget)
        assert exc.value.current_dollars == pytest.approx(2.0)
        assert budget.current_spend() == pytest.approx(2.0)
        assert budget.current_spend("chat") == pytest.approx(2.0)
        assert _events(ledger) == (2, 2.0)
        assert not budget.check_and_reserve("chat", "fixture-model", 1)
    finally:
        await budget.close()
    reopened = CostBudget(**ledger)
    try:
        await reopened.ensure_ready()
        assert reopened.current_spend() == pytest.approx(2.0)
        assert not reopened.check_and_reserve("chat", "fixture-model", 1)
    finally:
        await reopened.close()


async def test_real_provider_record_boundary_keeps_over_cap_usage(ledger):
    budget = CostBudget(**ledger)
    provider = LLMProvider.__new__(LLMProvider)
    provider._cost_budget = budget
    try:
        for _ in range(2):
            await provider._budget_record("chat", "fixture-model", {
                "usage": {"prompt_tokens": 0, "completion_tokens": 1000},
            })
        assert _events(ledger) == (2, 2.0)
        blocked = await provider._budget_check("chat", "fixture-model", 1)
        assert blocked is not None and "budget_exceeded" in blocked
    finally:
        await budget.close()


async def test_distinct_budget_instances_settle_against_persisted_total(ledger):
    first, second = CostBudget(**ledger), CostBudget(**ledger)
    try:
        # Open the shared WAL before racing settlement. Concurrent first-time
        # schema initialization is a separate lifecycle contract.
        await first.ensure_ready()
        await second.ensure_ready()
        results = await asyncio.gather(_charge(first), _charge(second), return_exceptions=True)
        assert sum(isinstance(result, BudgetExceeded) for result in results) == 1
        assert _events(ledger) == (2, 2.0)
    finally:
        await asyncio.gather(first.close(), second.close())


async def test_failed_rollup_write_rolls_back_event_and_cache(ledger):
    budget = CostBudget(**ledger)
    try:
        await budget.ensure_ready()
        assert budget._conn is not None
        await budget._conn.execute(
            "CREATE TRIGGER reject_rollup BEFORE INSERT ON cost_rollup "
            "BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END"
        )
        await budget._conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="fixture write failure"):
            await _charge(budget)
        assert _events(ledger) == (0, None)
        assert budget.current_spend() == 0.0
        await budget._conn.execute("DROP TRIGGER reject_rollup")
        await budget._conn.commit()
        await _charge(budget)
        assert _events(ledger) == (1, 1.0)
    finally:
        await budget.close()


async def test_settlement_uses_new_hour_without_restarting_budget(ledger, monkeypatch):
    first_hour = datetime(2026, 10, 4, 10, 30, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr("cost.budget.time.time", lambda: first_hour)
    budget = CostBudget(**ledger)
    try:
        await _charge(budget)
        monkeypatch.setattr("cost.budget.time.time", lambda: first_hour + 3600)
        assert budget.current_spend() == 0.0
        assert budget.check_and_reserve("chat", "fixture-model", 1000)
        await _charge(budget)
        assert budget.current_spend() == pytest.approx(1.0)
        assert budget.current_spend(window="day") == pytest.approx(2.0)
        assert _events(ledger) == (2, 2.0)
    finally:
        await budget.close()


async def test_global_site_is_not_double_counted(ledger):
    budget = CostBudget(**ledger)
    try:
        await budget.record_usage("__global__", "fixture-model", 0, 1000)
        assert budget.current_spend() == pytest.approx(1.0)
        assert _events(ledger) == (1, 1.0)
    finally:
        await budget.close()


async def test_cancellation_after_begin_rolls_back_before_ledger_reuse(ledger, monkeypatch):
    budget = CostBudget(**ledger)
    try:
        await budget.ensure_ready()
        execute = budget._conn.execute

        async def cancel_after_begin(sql, *args, **kwargs):
            cursor = await execute(sql, *args, **kwargs)
            if sql == "BEGIN IMMEDIATE":
                # Restore aiosqlite's awaitable/context-manager interface for
                # the cleanup reads; only the BEGIN boundary is interrupted.
                monkeypatch.setattr(budget._conn, "execute", execute)
                raise asyncio.CancelledError
            return cursor

        with monkeypatch.context() as patch:
            patch.setattr(budget._conn, "execute", cancel_after_begin)
            with pytest.raises(asyncio.CancelledError):
                await _charge(budget)
        assert not budget._conn.in_transaction
        assert _events(ledger) == (0, None)
        assert budget.current_spend() == 0.0
        await _charge(budget)
        assert _events(ledger) == (1, 1.0)
    finally:
        await budget.close()


async def test_cancellation_after_commit_reloads_actual_charge(ledger, monkeypatch):
    budget = CostBudget(**ledger)
    try:
        await budget.ensure_ready()
        commit = budget._conn.commit

        async def cancel_after_commit():
            await commit()
            raise asyncio.CancelledError

        with monkeypatch.context() as patch:
            patch.setattr(budget._conn, "commit", cancel_after_commit)
            with pytest.raises(asyncio.CancelledError):
                await _charge(budget)
        assert _events(ledger) == (1, 1.0)
        assert budget.current_spend() == pytest.approx(1.0)
        assert not budget.check_and_reserve("chat", "fixture-model", 1000)
    finally:
        await budget.close()


@pytest.mark.parametrize("failure", ["ledger", "admission"])
async def test_configured_cap_fails_closed_when_accounting_is_unavailable(ledger, monkeypatch, failure):
    budget = CostBudget(**ledger)
    provider = LLMProvider.__new__(LLMProvider)
    provider._cost_budget = budget
    try:
        if failure == "ledger":
            monkeypatch.setattr(budget, "ensure_ready", AsyncMock(side_effect=RuntimeError("private fixture path")))
        else:
            def broken(*args, **kwargs):
                raise RuntimeError("private fixture path")
            monkeypatch.setattr(budget, "check_and_reserve", broken)
        result = await provider._budget_check("chat", "fixture-model", 1000)
        assert result["error_code"] == "budget_unavailable"
        assert result["choices"] == []
        assert "private fixture path" not in str(result)
    finally:
        await budget.close()


@pytest.mark.parametrize("disabled", [False, True])
async def test_unlimited_or_disabled_budget_remains_available_on_ledger_failure(ledger, monkeypatch, disabled):
    ledger["settings"] = {"cost": {"enabled": not disabled}}
    budget = CostBudget(**ledger)
    provider = LLMProvider.__new__(LLMProvider)
    provider._cost_budget = budget
    monkeypatch.setattr(budget, "ensure_ready", AsyncMock(side_effect=RuntimeError("fixture failure")))
    assert await provider._budget_check("chat", "fixture-model", 1000) is None
