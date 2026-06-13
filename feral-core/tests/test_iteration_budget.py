"""v2026.6.11 — tool-iteration budget: unlimited by default, progress-guarded.

Pins the contract that replaced the hard-coded iteration caps (20 in the
orchestrator, 4 in AgentWorker):

* default = UNLIMITED — no artificial stop while progress is being made;
* a limit exists only as a user-set option (settings.json / env);
* the no-progress guard fires when the exact same tool call with the same
  arguments returns the same failing result twice in a row;
* the wall-clock backstop is generous and configurable.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.iteration_budget import (  # noqa: E402
    IterationBudget,
    NoProgressGuard,
    resolve_max_tool_iterations,
    resolve_tool_loop_max_seconds,
)
from agents.multi_agent import AgentWorker  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("FERAL_MAX_ITERATIONS", raising=False)
    monkeypatch.delenv("FERAL_TOOL_LOOP_MAX_SECONDS", raising=False)


# ── Setting resolution ───────────────────────────────────────────────────────


def test_default_is_unlimited():
    assert resolve_max_tool_iterations(settings={}) == 0


def test_settings_json_limit_is_respected():
    s = {"agents": {"max_tool_iterations": 12}}
    assert resolve_max_tool_iterations(settings=s) == 12


def test_env_var_wins_over_settings(monkeypatch):
    monkeypatch.setenv("FERAL_MAX_ITERATIONS", "5")
    s = {"agents": {"max_tool_iterations": 12}}
    assert resolve_max_tool_iterations(settings=s) == 5


def test_env_zero_means_unlimited(monkeypatch):
    monkeypatch.setenv("FERAL_MAX_ITERATIONS", "0")
    assert resolve_max_tool_iterations(settings={"agents": {"max_tool_iterations": 3}}) == 0


def test_garbage_values_degrade_to_unlimited():
    assert resolve_max_tool_iterations(settings={"agents": {"max_tool_iterations": "lots"}}) == 0


def test_wall_clock_default_generous_and_configurable():
    assert resolve_tool_loop_max_seconds(settings={}) >= 600
    s = {"agents": {"tool_loop_max_seconds": 30}}
    assert resolve_tool_loop_max_seconds(settings=s) == 30.0


# ── IterationBudget ──────────────────────────────────────────────────────────


def test_unlimited_budget_never_stops_on_count():
    b = IterationBudget(max_iterations=0, max_seconds=0)
    for _ in range(500):
        assert b.start_iteration() is True
    assert b.iterations == 500
    assert b.stop_reason == ""


def test_user_set_limit_stops_loop():
    b = IterationBudget(max_iterations=3, max_seconds=0)
    assert [b.start_iteration() for _ in range(4)] == [True, True, True, False]
    assert "max_tool_iterations=3" in b.stop_reason


def test_wall_clock_backstop_stops_loop():
    b = IterationBudget(max_iterations=0, max_seconds=0.000001)
    assert b.start_iteration() is True  # first round always runs
    import time
    time.sleep(0.01)
    assert b.start_iteration() is False
    assert "wall-clock" in b.stop_reason


# ── NoProgressGuard ──────────────────────────────────────────────────────────


def test_guard_fires_on_identical_failing_call_twice_in_a_row():
    g = NoProgressGuard()
    fail = {"success": False, "error": "robot did not enter explore mode"}
    assert g.observe("cutebot__explore", {}, False, fail) is False
    assert g.observe("cutebot__explore", {}, False, fail) is True
    assert g.tripped is True


def test_guard_does_not_fire_on_successful_repeats():
    g = NoProgressGuard()
    ok = {"success": True, "data": {"mode": "explore"}}
    for _ in range(10):
        assert g.observe("cutebot__status", {}, True, ok) is False
    assert g.tripped is False


def test_guard_resets_when_args_change():
    g = NoProgressGuard()
    fail = {"success": False, "error": "nope"}
    assert g.observe("cutebot__drive", {"left": 30, "right": 30}, False, fail) is False
    assert g.observe("cutebot__drive", {"left": 20, "right": 20}, False, fail) is False
    assert g.tripped is False


def test_guard_resets_when_failing_result_changes():
    # Same call, same args, but the failure text changed — that is new
    # information (progress), not a stuck loop.
    g = NoProgressGuard()
    assert g.observe("x", {}, False, {"error": "timeout"}) is False
    assert g.observe("x", {}, False, {"error": "denied"}) is False
    assert g.tripped is False


def test_guard_success_breaks_a_failing_streak():
    g = NoProgressGuard()
    fail = {"error": "e"}
    assert g.observe("x", {}, False, fail) is False
    assert g.observe("x", {}, True, {"ok": True}) is False
    assert g.observe("x", {}, False, fail) is False  # streak restarted at 1
    assert g.tripped is False


# ── AgentWorker integration ─────────────────────────────────────────────────


def _tool_call(name: str, args: dict | None = None) -> dict:
    return {"id": "t1", "name": name, "args": args or {}}


def _make_worker(llm, executor=None):
    skills = MagicMock()
    endpoint = MagicMock()
    endpoint.id = "explore"
    skill = MagicMock()
    skill.endpoints = [endpoint]
    skills.skills = {"cutebot": skill}
    return AgentWorker(
        "hw", "Hardware", "SYS", ["cutebot"],
        llm=llm, skill_registry=skills, skill_executor=executor,
    )


@pytest.mark.asyncio
async def test_worker_unlimited_no_artificial_stop_while_progressing(monkeypatch):
    """Six distinct tool rounds (more than the old hard cap of 4) followed
    by a text answer must run to completion under the default budget."""
    monkeypatch.setenv("FERAL_MAX_ITERATIONS", "0")

    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={})
    rounds = [("", [_tool_call("cutebot__explore", {"round": i})]) for i in range(6)]
    rounds.append(("all six steps done", []))
    responses = iter(rounds)
    llm.extract_response = MagicMock(side_effect=lambda _r: next(responses))

    executor = MagicMock()
    # Distinct successful results — clear forward progress each round.
    executor.execute = AsyncMock(
        side_effect=lambda name, args, *_a: {"success": True, "data": {"round": args}}
    )

    w = _make_worker(llm, executor)
    r = await w.run("s1", "do a six-step hardware task")

    assert r.text == "all six steps done"
    assert executor.execute.await_count == 6  # old cap of 4 would have cut this
    assert not r.error


@pytest.mark.asyncio
async def test_worker_no_progress_guard_stops_identical_failing_loop(monkeypatch):
    """An LLM that repeats the exact same failing call forever must be
    stopped by the progress guard, not spin (the budget is unlimited)."""
    monkeypatch.setenv("FERAL_MAX_ITERATIONS", "0")

    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={})
    # Always the same tool call — never a text answer.
    llm.extract_response = MagicMock(
        return_value=("", [_tool_call("cutebot__explore", {})])
    )

    executor = MagicMock()
    failing = {"success": False, "data": None, "error": "robot did not move"}
    executor.execute = AsyncMock(return_value=failing)

    w = _make_worker(llm, executor)
    r = await w.run("s1", "explore")

    # Guard fires after the 2nd identical failing round; tools are then
    # withdrawn, and the stuck model triggers the hard stop + synthesis.
    assert executor.execute.await_count == 2
    assert llm.chat.await_count <= 5
    assert r.text  # synthesized/fallback answer, never an infinite loop


@pytest.mark.asyncio
async def test_worker_user_set_limit_still_applies(monkeypatch):
    monkeypatch.setenv("FERAL_MAX_ITERATIONS", "2")

    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={})
    llm.extract_response = MagicMock(
        return_value=("", [_tool_call("cutebot__explore", {})])
    )
    executor = MagicMock()
    executor.execute = AsyncMock(return_value={"success": True, "data": {"ok": True}})

    w = _make_worker(llm, executor)
    await w.run("s1", "explore")

    assert executor.execute.await_count == 2


# ── Orchestrator wiring ──────────────────────────────────────────────────────


def _make_orchestrator():
    from agents.orchestrator import Orchestrator
    reg = MagicMock()
    reg.skills = {}
    reg.find_skills_for_query = MagicMock(return_value=[])
    reg.get_tools_for_skills = MagicMock(return_value=[])
    return Orchestrator(
        skill_registry=reg,
        send_to_client=AsyncMock(),
        daemons={},
        memory=None,
        vision_buffer=None,
        perception=None,
        learner=None,
    )


def test_orchestrator_default_budget_is_unlimited(monkeypatch):
    monkeypatch.setenv("FERAL_MAX_ITERATIONS", "0")
    orch = _make_orchestrator()
    assert orch._max_iterations == 0  # unlimited
    assert orch._tool_loop_max_seconds > 0  # wall-clock backstop present


def test_orchestrator_env_limit_applies(monkeypatch):
    monkeypatch.setenv("FERAL_MAX_ITERATIONS", "7")
    orch = _make_orchestrator()
    assert orch._max_iterations == 7
