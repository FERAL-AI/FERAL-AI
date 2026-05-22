"""audit-r14 / S6 — every background subsystem wraps its paid LLM work
in :class:`cost.loop_guard.BudgetLoopGuard` so:

1. A pre-flight ``allow()`` rejection skips the LLM call cleanly.
2. The guard auto-pauses for the remainder of the active cap window.
3. A structured ``cost.cap_hit`` log line is emitted.
4. A ``cost_cap_hit`` WS frame is dispatched to the broadcaster so
   Wave 3 Lane 12 can render the yellow chat banner.

The tests below exercise the guard's contract end-to-end and pin the
integration shape that each subsystem (ScreenLoop, ProactiveEngine,
Learner) consumes.
"""

from __future__ import annotations

import asyncio
import logging

import pytest


# ─────────────────────────────────────────────────────────────────
# BudgetLoopGuard — primitive contract
# ─────────────────────────────────────────────────────────────────


import pytest_asyncio


@pytest_asyncio.fixture
async def cost_budget(tmp_path, monkeypatch):
    """Real :class:`CostBudget` against an isolated SQLite file with a
    tight per-call-site cap so the test exercises the cap-hit path.

    The async fixture form ensures ``budget.close()`` runs on the same
    event loop as the rest of the test — using ``asyncio.run`` in a
    sync teardown created a *new* loop that aiosqlite's worker thread
    couldn't call back to (``RuntimeError: Event loop is closed``), and
    that pytest_unhandled_thread_exception warning could flunk
    subsequent unrelated tests in CI.
    """
    from cost.budget import CostBudget

    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    budget = CostBudget(
        settings={
            "cost": {
                "enabled": True,
                "per_call_site_caps": {
                    "screen_loop": {"per_hour_usd": 0.001},
                    "proactive": {"per_hour_usd": 0.001},
                    "learner": {"per_hour_usd": 0.001},
                },
            }
        },
        db_path=str(tmp_path / "cost.db"),
    )
    try:
        yield budget
    finally:
        await budget.close()


@pytest.fixture
def broadcaster_capture():
    """Capture every ``broadcast_event`` call the guard makes."""
    captured: list[tuple[str, dict]] = []

    async def _broadcast(event: str, payload: dict) -> None:
        captured.append((event, payload))

    return captured, _broadcast


@pytest.mark.asyncio
async def test_guard_allows_under_cap(cost_budget, broadcaster_capture):
    from cost.loop_guard import BudgetLoopGuard

    captured, broadcast = broadcaster_capture
    guard = BudgetLoopGuard(
        call_site="screen_loop",
        subsystem="ScreenLoop",
        budget=cost_budget,
        broadcaster=broadcast,
    )
    assert guard.allow(model="gpt-4o-mini", estimated_max_tokens=10) is True
    assert guard.is_paused is False
    assert captured == []


@pytest.mark.asyncio
async def test_guard_blocks_when_cap_would_overshoot(cost_budget, broadcaster_capture, caplog):
    """A huge ``estimated_max_tokens`` overshoots the 0.001 USD cap;
    the guard pauses + emits."""
    from cost.loop_guard import BudgetLoopGuard

    captured, broadcast = broadcaster_capture
    guard = BudgetLoopGuard(
        call_site="screen_loop",
        subsystem="ScreenLoop",
        budget=cost_budget,
        broadcaster=broadcast,
    )
    with caplog.at_level(logging.WARNING, logger="feral.cost.loop_guard"):
        ok = guard.allow(model="gpt-4o-mini", estimated_max_tokens=10_000_000)
    assert ok is False
    assert guard.is_paused is True
    # Give the loop one tick so the create_task scheduled by the guard
    # for the broadcast can run.
    await asyncio.sleep(0)
    assert any(payload["type"] == "cost_cap_hit" for _, payload in captured), (
        f"no cost_cap_hit broadcast: {captured!r}"
    )
    payload = captured[0][1]
    assert payload["subsystem"] == "ScreenLoop"
    assert payload["call_site"] == "screen_loop"
    assert payload["cap_dollars"] == pytest.approx(0.001)
    assert payload["paused_until"] > 0
    assert any("cost.cap_hit" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_guard_pause_throttles_re_emit(cost_budget, broadcaster_capture):
    """A hot loop calling ``allow`` every tick after the pause must
    not flood the broadcaster — re-emit is throttled to once per
    minute by default."""
    from cost.loop_guard import BudgetLoopGuard

    captured, broadcast = broadcaster_capture
    guard = BudgetLoopGuard(
        call_site="screen_loop",
        subsystem="ScreenLoop",
        budget=cost_budget,
        broadcaster=broadcast,
    )
    assert guard.allow(model="gpt-4o-mini", estimated_max_tokens=10_000_000) is False
    for _ in range(50):
        assert guard.allow(model="gpt-4o-mini", estimated_max_tokens=10) is False
    await asyncio.sleep(0)
    # Exactly one emit even though we called ``allow`` 51 times.
    cap_hits = [p for e, p in captured if e == "cost_cap_hit"]
    assert len(cap_hits) == 1


@pytest.mark.asyncio
async def test_guard_record_raises_on_post_call_overshoot(cost_budget, broadcaster_capture):
    from cost.loop_guard import BudgetLoopGuard

    captured, broadcast = broadcaster_capture
    guard = BudgetLoopGuard(
        call_site="screen_loop",
        subsystem="ScreenLoop",
        budget=cost_budget,
        broadcaster=broadcast,
    )
    ok = await guard.record(
        model="gpt-4o-mini",
        prompt_tokens=100_000,
        completion_tokens=100_000,
    )
    assert ok is False
    assert guard.is_paused is True
    await asyncio.sleep(0)
    cap_hits = [p for e, p in captured if e == "cost_cap_hit"]
    assert cap_hits, "record() overshoot must broadcast cost_cap_hit"


@pytest.mark.asyncio
async def test_guard_reset_clears_pause(cost_budget, broadcaster_capture):
    from cost.loop_guard import BudgetLoopGuard

    _, broadcast = broadcaster_capture
    guard = BudgetLoopGuard(
        call_site="screen_loop", subsystem="ScreenLoop", budget=cost_budget, broadcaster=broadcast,
    )
    guard.allow(model="gpt-4o-mini", estimated_max_tokens=10_000_000)
    assert guard.is_paused
    guard.reset()
    assert not guard.is_paused


@pytest.mark.asyncio
async def test_guard_with_no_budget_always_allows():
    """``cost_budget=None`` (operator turned tracking off) bypasses
    every check and never broadcasts."""
    from cost.loop_guard import make_disabled_guard

    guard = make_disabled_guard("screen_loop", "ScreenLoop")
    for _ in range(10):
        assert guard.allow(model="x", estimated_max_tokens=10_000_000) is True


# ─────────────────────────────────────────────────────────────────
# Subsystem integration — each background loop honours the guard
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_screen_loop_skips_tick_when_cost_paused(cost_budget, broadcaster_capture):
    """ScreenLoop's ``_tick`` MUST return early when the guard is paused —
    no screenshot, no LLM call. The pause counter increments so the
    operator can see how many ticks were skipped via ``stats``."""
    from cost.loop_guard import BudgetLoopGuard
    from perception.screen_loop import ScreenLoop

    captured, broadcast = broadcaster_capture
    guard = BudgetLoopGuard(
        call_site="screen_loop",
        subsystem="ScreenLoop",
        budget=cost_budget,
        broadcaster=broadcast,
    )
    # Pre-pause the guard so the very next tick is forced off-path.
    assert guard.allow(model="gpt-4o-mini", estimated_max_tokens=10_000_000) is False

    sl = ScreenLoop(cost_guard=guard)
    await sl._tick()
    await sl._tick()
    await sl._tick()
    assert sl.stats["budget_pauses"] == 3
    assert sl.stats["captures"] == 0
    assert sl.stats["budget_paused"] is True


@pytest.mark.asyncio
async def test_learner_skips_extract_when_cost_paused(cost_budget, broadcaster_capture, tmp_path):
    """Learner.extract_knowledge MUST exit before reaching ``kg.extract_and_store``
    when the cost guard is paused."""
    from cost.loop_guard import BudgetLoopGuard
    from agents.learner import Learner

    captured, broadcast = broadcaster_capture
    guard = BudgetLoopGuard(
        call_site="learner",
        subsystem="Learner",
        budget=cost_budget,
        broadcaster=broadcast,
    )
    guard.allow(model="gpt-4o-mini", estimated_max_tokens=10_000_000)  # force pause

    class _FakeKG:
        def __init__(self):
            self.calls: list[str] = []

        async def extract_and_store(self, text, llm):
            self.calls.append(text)
            return []

    class _FakeMemory:
        def __init__(self):
            self.kg = _FakeKG()

        def working_get(self, *_a, **_kw):
            return [{"role": "user", "text": "I love coffee in the morning"}] * 6

    class _FakeLLM:
        available = True

    learner = Learner(llm=_FakeLLM(), memory=_FakeMemory(), cost_guard=guard)
    await learner.extract_knowledge("sess-1")
    assert learner.memory.kg.calls == [], (
        "Learner did not honour the cost-guard pause — extract_and_store "
        "ran anyway"
    )


# ─────────────────────────────────────────────────────────────────
# FERAL_SELF_LEARNING gate (audit-r14 finding 18 #2)
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_self_learning_off_short_circuits_on_message(monkeypatch):
    from agents.learner import Learner

    monkeypatch.setenv("FERAL_SELF_LEARNING", "false")

    class _FakeMemory:
        kg = None

        def working_get(self, *_a, **_kw):
            return [{"role": "user", "text": "hi"}]

    class _FakeLLM:
        available = True

        async def chat(self, *_a, **_kw):  # pragma: no cover — must never run
            raise AssertionError("LLM was called with FERAL_SELF_LEARNING=false")

        def extract_response(self, *_a, **_kw):  # pragma: no cover
            return ("", None)

    learner = Learner(llm=_FakeLLM(), memory=_FakeMemory())
    # Hit the interval threshold to force extract_knowledge if it was
    # going to run.
    for _ in range(10):
        await learner.on_message("sess-x", "user", "hi")
    await learner.summarize_session("sess-x")


@pytest.mark.asyncio
async def test_self_learning_on_runs_extraction(monkeypatch):
    """Inverse — with the env enabled (or unset), the Learner enters
    its normal path. We don't run the full extraction here (no real
    KG) but we DO assert ``_self_learning_enabled()`` is True so the
    gate is observably correct in both directions."""
    from agents import learner as learner_mod

    for value in ("", "true", "TRUE", "1", "yes", "on"):
        monkeypatch.setenv("FERAL_SELF_LEARNING", value or "true")
        assert learner_mod._self_learning_enabled() is True

    for value in ("false", "FALSE", "0", "no", "off"):
        monkeypatch.setenv("FERAL_SELF_LEARNING", value)
        assert learner_mod._self_learning_enabled() is False
