"""v2026.5.47 — the cost budget is OPEN BY DEFAULT.

Pins the contract that ``BudgetLoopGuard.allow()`` always passes (and
never broadcasts a ``cost_cap_hit`` frame) for a call_site that has
no operator-configured cap, even when the simulated rollup spend is
arbitrarily high. Once the operator types a number into Settings →
Cost (or sets a global), the existing enforcement / pause / banner
path fires exactly as before.

The complementary "cap is honoured when set" path is exercised by
``test_background_cost_budget.py`` and ``test_cost_budget.py``; this
file specifically pins the *unlimited-by-default* half so a future
refactor that quietly reintroduces a hardcoded dollar default trips
here.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def unbounded_budget(tmp_path, monkeypatch):
    """A real :class:`CostBudget` against an isolated SQLite file with
    NO caps configured — the default state after a fresh install."""
    from cost.budget import CostBudget

    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    budget = CostBudget(db_path=str(tmp_path / "cost.db"))
    await budget.ensure_ready()
    try:
        yield budget
    finally:
        await budget.close()


@pytest_asyncio.fixture
async def capped_budget(tmp_path, monkeypatch):
    """A budget where the operator HAS configured a $20/hr cap for
    screen_loop. Used to prove the enforcement path still fires once
    a number is set."""
    from cost.budget import CostBudget

    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    budget = CostBudget(
        settings={
            "cost": {
                "enabled": True,
                "screen_loop": {"per_hour_usd": 20.0},
            }
        },
        db_path=str(tmp_path / "cost.db"),
    )
    await budget.ensure_ready()
    try:
        yield budget
    finally:
        await budget.close()


@pytest.fixture
def broadcaster_capture():
    captured: list[tuple[str, dict]] = []

    async def _broadcast(event: str, payload: dict) -> None:
        captured.append((event, payload))

    return captured, _broadcast


# ─────────────────────────────────────────────────────────────────
# Unlimited-by-default contract
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unset_cap_always_allows_even_at_high_simulated_spend(
    unbounded_budget, broadcaster_capture, caplog,
):
    """With NO cap configured the guard must allow every call,
    regardless of how much the rollup has accumulated and how big
    the next estimate is. No ``cost_cap_hit`` frame, no warning log."""
    from cost.loop_guard import BudgetLoopGuard

    captured, broadcast = broadcaster_capture
    guard = BudgetLoopGuard(
        call_site="screen_loop",
        subsystem="ScreenLoop",
        budget=unbounded_budget,
        broadcaster=broadcast,
    )

    # Simulate substantial prior spend so the rollup is well past
    # any of the old factory defaults ($0.10/hr for screen_loop,
    # $5/hr for chat, $5 global). 1M+1M tokens at the catalog rate
    # is on the order of $18 — multiple orders of magnitude over
    # what the pre-v2026.5.47 default would have allowed.
    await unbounded_budget.record_usage(
        "screen_loop", "claude-sonnet-4-6", 1_000_000, 1_000_000,
    )
    assert unbounded_budget.current_spend("screen_loop", "hour") > 5.0

    # And ask the guard if a fresh enormous call is OK.
    with caplog.at_level(logging.WARNING, logger="feral.cost.loop_guard"):
        for _ in range(20):
            ok = guard.allow(
                model="claude-sonnet-4-6",
                estimated_max_tokens=10_000_000,
            )
            assert ok is True, "unset cap must never block"

    assert guard.is_paused is False
    await asyncio.sleep(0)  # let any scheduled broadcast settle
    assert captured == [], (
        f"unset cap broadcast cost_cap_hit anyway: {captured!r}"
    )
    assert not any("cost.cap_hit" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_unset_cap_record_never_raises_budget_exceeded(unbounded_budget):
    """``record()`` post-call must not raise BudgetExceeded when no
    cap is configured, even if the call would have tripped a tight
    legacy default."""
    from cost.loop_guard import BudgetLoopGuard

    guard = BudgetLoopGuard(
        call_site="screen_loop",
        subsystem="ScreenLoop",
        budget=unbounded_budget,
    )
    ok = await guard.record(
        model="claude-sonnet-4-6",
        prompt_tokens=500_000,
        completion_tokens=500_000,
    )
    assert ok is True
    assert guard.is_paused is False


@pytest.mark.asyncio
async def test_budget_check_and_reserve_unlimited_when_unset(unbounded_budget):
    """The lower-level ``CostBudget.check_and_reserve`` itself must
    return ``True`` for every call_site when no cap is configured —
    this is the primitive that ``BudgetLoopGuard.allow`` relies on."""
    for site in ("screen_loop", "proactive", "chat", "vision", "learner"):
        assert unbounded_budget.check_and_reserve(
            site, "claude-sonnet-4-6", estimated_max_tokens=10_000_000,
        ) is True


# ─────────────────────────────────────────────────────────────────
# Operator-set caps still enforce
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_configured_cap_still_trips_when_exceeded(
    capped_budget, broadcaster_capture,
):
    """The enforcement path stays intact: an explicitly configured
    $20/hr cap still pauses + emits when the projected spend would
    overshoot."""
    from cost.loop_guard import BudgetLoopGuard

    captured, broadcast = broadcaster_capture
    guard = BudgetLoopGuard(
        call_site="screen_loop",
        subsystem="ScreenLoop",
        budget=capped_budget,
        broadcaster=broadcast,
    )

    # 10M tokens at the $0.015/1k output rate ⇒ $150 estimated cost,
    # well over the $20 cap.
    ok = guard.allow(
        model="claude-sonnet-4-6",
        estimated_max_tokens=10_000_000,
    )
    assert ok is False
    assert guard.is_paused is True
    await asyncio.sleep(0)
    cap_hits = [p for e, p in captured if e == "cost_cap_hit"]
    assert cap_hits, "configured cap must broadcast cost_cap_hit"
    assert cap_hits[0]["cap_dollars"] == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_clearing_a_cap_returns_to_unlimited(capped_budget):
    """Operator path: set a cap, then clear it (write
    ``cost.<site> = null`` via the Settings UI). Hot-reload must
    re-resolve the cap as ``None`` and the guard must stop blocking."""
    from cost.loop_guard import BudgetLoopGuard

    guard = BudgetLoopGuard(
        call_site="screen_loop",
        subsystem="ScreenLoop",
        budget=capped_budget,
    )
    # Sanity: the $20 cap is in effect.
    assert capped_budget._cap_for("screen_loop", "hour") == pytest.approx(20.0)

    # Operator clears the cap via Settings → Cost ⇒ persisted as
    # ``cost.screen_loop = null``. The route invokes
    # ``reload_from_settings`` with the fresh dict.
    capped_budget.reload_from_settings(
        {"cost": {"enabled": True, "screen_loop": None}}
    )
    assert capped_budget._cap_for("screen_loop", "hour") is None

    # And the guard now allows arbitrarily large estimates.
    guard.reset()
    assert guard.allow(
        model="claude-sonnet-4-6",
        estimated_max_tokens=10_000_000,
    ) is True
    assert guard.is_paused is False
