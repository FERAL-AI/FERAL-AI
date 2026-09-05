"""v2026.6.11 — tool-iteration budget: unlimited by default, progress-guarded.

Pins the contract that replaced the hard-coded iteration caps (20 in the
orchestrator, 4 in AgentWorker):

* default = UNLIMITED — no artificial stop while progress is being made;
* a limit exists only as a user-set option (settings.json / env);
* the no-progress guard fires when the exact same tool call with the same
  arguments keeps returning the same failing result;
* the wall-clock backstop is generous and configurable.

v2026.7.30 — the guard is two-level. WARN (default 4 identical failing
repeats) leaves the toolset intact so an agent that hit one broken tool
can route around it; only STOP (default 8 consecutive failures of the same
call+args, whatever the failure body) withdraws tools. The old contract
tripped at 2 and disarmed the agent outright.
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
    DEFAULT_NO_PROGRESS_STOP_THRESHOLD,
    DEFAULT_NO_PROGRESS_WARN_THRESHOLD,
    DEFAULT_UNAVAILABLE_TOOL_THRESHOLD,
    GUARD_OK,
    GUARD_STOP,
    GUARD_WARN,
    IterationBudget,
    NoProgressGuard,
    drop_unavailable_tools,
    precondition_failure_reason,
    resolve_max_tool_iterations,
    resolve_no_progress_stop_threshold,
    resolve_no_progress_warn_threshold,
    resolve_tool_loop_max_seconds,
    resolve_unavailable_tool_threshold,
    unavailable_tool_notice,
)
from agents.multi_agent import AgentWorker  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("FERAL_MAX_ITERATIONS", raising=False)
    monkeypatch.delenv("FERAL_TOOL_LOOP_MAX_SECONDS", raising=False)
    monkeypatch.delenv("FERAL_NO_PROGRESS_WARN_THRESHOLD", raising=False)
    monkeypatch.delenv("FERAL_NO_PROGRESS_STOP_THRESHOLD", raising=False)
    monkeypatch.delenv("FERAL_UNAVAILABLE_TOOL_THRESHOLD", raising=False)


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


def test_guard_default_thresholds_are_not_two():
    """The v2026.7.30 fix: two transient failures against one unavailable
    tool must NOT be enough to conclude the agent is stuck."""
    assert DEFAULT_NO_PROGRESS_WARN_THRESHOLD > 2
    assert DEFAULT_NO_PROGRESS_STOP_THRESHOLD > DEFAULT_NO_PROGRESS_WARN_THRESHOLD
    g = NoProgressGuard()
    assert g.warn_threshold == DEFAULT_NO_PROGRESS_WARN_THRESHOLD
    assert g.stop_threshold == DEFAULT_NO_PROGRESS_STOP_THRESHOLD


def test_guard_stays_quiet_below_the_warn_threshold():
    g = NoProgressGuard()
    fail = {"success": False, "error": "robot did not enter explore mode"}
    for _ in range(DEFAULT_NO_PROGRESS_WARN_THRESHOLD - 1):
        assert g.observe("cutebot__explore", {}, False, fail) == GUARD_OK
    assert g.tripped is False


def test_guard_warns_then_stops():
    g = NoProgressGuard(warn_threshold=3, stop_threshold=5)
    fail = {"success": False, "error": "robot did not enter explore mode"}
    assert g.observe("cutebot__explore", {}, False, fail) == GUARD_OK
    assert g.observe("cutebot__explore", {}, False, fail) == GUARD_OK
    assert g.observe("cutebot__explore", {}, False, fail) == GUARD_WARN
    assert g.observe("cutebot__explore", {}, False, fail) == GUARD_WARN
    assert g.observe("cutebot__explore", {}, False, fail) == GUARD_STOP


def test_guard_stop_level_survives_a_changing_failure_body():
    """ToolRunner's anti-loop block envelope carries an incrementing
    ``anti_loop_streak``, so the failing body is never byte-identical. The
    strict signature therefore never warns — the loose call signature is
    what has to terminate the spin."""
    g = NoProgressGuard(warn_threshold=3, stop_threshold=5)
    levels = [
        g.observe("x__y", {}, False, {"error": "blocked", "anti_loop_streak": i})
        for i in range(1, 6)
    ]
    assert GUARD_WARN not in levels
    assert levels[-1] == GUARD_STOP


def test_guard_does_not_fire_on_successful_repeats():
    g = NoProgressGuard()
    ok = {"success": True, "data": {"mode": "explore"}}
    for _ in range(20):
        assert g.observe("cutebot__status", {}, True, ok) == GUARD_OK
    assert g.tripped is False


def test_guard_resets_when_args_change():
    g = NoProgressGuard(warn_threshold=2, stop_threshold=2)
    fail = {"success": False, "error": "nope"}
    assert g.observe("cutebot__drive", {"left": 30, "right": 30}, False, fail) == GUARD_OK
    assert g.observe("cutebot__drive", {"left": 20, "right": 20}, False, fail) == GUARD_OK
    assert g.tripped is False


def test_guard_warn_resets_when_failing_result_changes():
    # Same call, same args, but the failure text changed — that is new
    # information (progress), not a stuck WARN-level loop.
    g = NoProgressGuard(warn_threshold=2, stop_threshold=99)
    assert g.observe("x", {}, False, {"error": "timeout"}) == GUARD_OK
    assert g.observe("x", {}, False, {"error": "denied"}) == GUARD_OK
    assert g.tripped is False


def test_guard_success_breaks_a_failing_streak():
    g = NoProgressGuard(warn_threshold=2, stop_threshold=2)
    fail = {"error": "e"}
    assert g.observe("x", {}, False, fail) == GUARD_OK
    assert g.observe("x", {}, True, {"ok": True}) == GUARD_OK
    assert g.observe("x", {}, False, fail) == GUARD_OK  # streak restarted at 1
    assert g.tripped is False


def test_guard_thresholds_are_configurable(monkeypatch):
    monkeypatch.setenv("FERAL_NO_PROGRESS_WARN_THRESHOLD", "6")
    monkeypatch.setenv("FERAL_NO_PROGRESS_STOP_THRESHOLD", "9")
    assert resolve_no_progress_warn_threshold() == 6
    assert resolve_no_progress_stop_threshold() == 9
    assert NoProgressGuard().warn_threshold == 6


def test_stop_threshold_never_precedes_warn():
    g = NoProgressGuard(warn_threshold=7, stop_threshold=3)
    assert g.stop_threshold >= g.warn_threshold


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
    # A real list, not a MagicMock. ``AgentWorker.get_tools`` passes the
    # registry's output through the availability gate
    # (skills/availability.py), which returns a list; a MagicMock double
    # would come back as [] and the worker would look like it had lost
    # its tools when it had not.
    skills._manifest_to_tools = MagicMock(return_value=[{
        "type": "function",
        "function": {"name": "cutebot__explore", "parameters": {}},
    }])
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

    # The guard now warns first and only withdraws tools at the STOP
    # threshold, so the worker gets its full allowance of retries before
    # being cut off — but it is still bounded, never an infinite spin.
    assert executor.execute.await_count == DEFAULT_NO_PROGRESS_STOP_THRESHOLD
    assert llm.chat.await_count <= DEFAULT_NO_PROGRESS_STOP_THRESHOLD + 3
    assert r.text  # synthesized/fallback answer, never an infinite loop


@pytest.mark.asyncio
async def test_worker_keeps_tools_after_a_couple_of_failures(monkeypatch):
    """Regression for the v2026.7.30 complaint: two failures against one
    unavailable tool used to withdraw the worker's ENTIRE toolset. A third
    round must still be offered tools."""
    monkeypatch.setenv("FERAL_MAX_ITERATIONS", "0")

    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={})
    rounds = [("", [_tool_call("cutebot__explore", {})]) for _ in range(3)]
    rounds.append(("done anyway", []))
    responses = iter(rounds)
    llm.extract_response = MagicMock(side_effect=lambda _r: next(responses))

    executor = MagicMock()
    executor.execute = AsyncMock(
        return_value={"success": False, "data": None, "error": "robot did not move"}
    )

    w = _make_worker(llm, executor)
    await w.run("s1", "explore")

    # Every chat round after the failures still carried a toolset.
    assert llm.chat.await_count >= 3
    for call in llm.chat.await_args_list:
        assert call.kwargs.get("tools") is not None


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


# ── Precondition failures, regardless of arguments ───────────────────────────
#
# Finding 11 of the 2026-09-04 audit. Both streaks above key on
# ``(tool, args)`` and reset when the args change, while the WARN text
# they emit tells the model to "change the arguments", so an agent that
# follows the advice resets the counter meant to stop it.


CUTEBOT_503 = {
    "success": False,
    "status_code": 503,
    "data": None,
    "error": "CuteBot is not connected (no USB robot found)",
}


def _lights(red: int) -> dict:
    return {"red": red, "green": 0, "blue": 0}


def test_walking_the_arguments_does_not_escape_the_precondition_guard():
    """routine-10, reproduced.

    It called ``cutebot__set_lights`` 46 times against a disconnected
    robot, walking rgb 255,0,0 then 254, 253, 252, and every call came
    back with the same deterministic 503. Neither existing streak ever
    reached its threshold because the args changed every single time.
    """
    g = NoProgressGuard(warn_threshold=4, stop_threshold=8)

    levels = [
        g.observe("cutebot__set_lights", _lights(red), False, CUTEBOT_503)
        for red in (255, 254, 253, 252, 251, 250)
    ]

    # The old guards still see nothing, which is the point.
    assert set(levels) == {GUARD_OK}
    # The new one has withdrawn the tool after three.
    assert "cutebot__set_lights" in g.unavailable_tools
    assert "not connected" in g.unavailable_tools["cutebot__set_lights"]


def test_the_tool_is_withdrawn_exactly_at_the_threshold():
    g = NoProgressGuard()
    for i in range(DEFAULT_UNAVAILABLE_TOOL_THRESHOLD - 1):
        g.observe("cutebot__set_lights", _lights(255 - i), False, CUTEBOT_503)
        assert g.unavailable_tools == {}
    g.observe("cutebot__set_lights", _lights(1), False, CUTEBOT_503)
    assert list(g.unavailable_tools) == ["cutebot__set_lights"]


def test_only_the_broken_tool_is_withdrawn():
    """The whole objection to the old behaviour: one dead tool must not
    cost the agent its hands."""
    g = NoProgressGuard(unavailable_threshold=2)
    for _ in range(3):
        g.observe("cutebot__set_lights", {}, False, CUTEBOT_503)

    tools = [
        {"function": {"name": "cutebot__set_lights"}},
        {"function": {"name": "cutebot__halt"}},
        {"function": {"name": "coding_tools__read_file"}},
    ]
    kept = drop_unavailable_tools(tools, g.unavailable_tools)

    assert [t["function"]["name"] for t in kept] == [
        "cutebot__halt", "coding_tools__read_file",
    ]


def test_the_model_is_told_once_and_by_name():
    g = NoProgressGuard(unavailable_threshold=1)
    g.observe("cutebot__set_lights", {}, False, CUTEBOT_503)

    first = g.take_unannounced_unavailable()
    assert [name for name, _ in first] == ["cutebot__set_lights"]
    notice = unavailable_tool_notice(*first[0])
    assert "cutebot__set_lights" in notice
    assert "CuteBot is not connected" in notice
    assert "do not try different values" in notice.lower()

    # Announced once. Further failures must not re-announce...
    g.observe("cutebot__set_lights", {"red": 9}, False, CUTEBOT_503)
    assert g.take_unannounced_unavailable() == []
    # ...but the tool stays withdrawn for the rest of the turn.
    assert "cutebot__set_lights" in g.unavailable_tools


def test_a_failure_arguments_could_fix_is_left_to_the_other_streaks():
    """A 404, a 500, a rejected parameter: retrying differently is exactly
    the right move, so this guard must not claim them."""
    g = NoProgressGuard(unavailable_threshold=2)
    for path in ("/a", "/b", "/c", "/d"):
        g.observe(
            "coding_tools__read_file", {"path": path}, False,
            {"success": False, "status_code": 404, "error": "no such file"},
        )
    assert g.unavailable_tools == {}


def test_a_success_clears_the_precondition_streak():
    g = NoProgressGuard(unavailable_threshold=3)
    g.observe("cutebot__set_lights", {}, False, CUTEBOT_503)
    g.observe("cutebot__set_lights", {}, False, CUTEBOT_503)
    g.observe("cutebot__set_lights", {}, True, {"success": True})
    g.observe("cutebot__set_lights", {}, False, CUTEBOT_503)
    assert g.unavailable_tools == {}


@pytest.mark.parametrize(
    "result, expected",
    [
        ({"success": True}, False),
        ({"success": False, "status_code": 503, "error": "no live websocket"}, True),
        ({"success": False, "status_code": 412, "error": "requires confirmation"}, True),
        ({"success": False, "status_code": 424, "error": "dep failed"}, True),
        # Ten integration modules return no status_code at all, so the
        # phrasing has to carry it.
        ({"success": False, "error": "Not connected to Notion"}, True),
        ({"success": False, "error": "Home Assistant is not configured"}, True),
        ({"success": False, "error": "Sandbox required, Docker unavailable"}, True),
        # Retrying differently may well help with these.
        ({"success": False, "status_code": 429, "error": "rate limited"}, False),
        ({"success": False, "status_code": 500, "error": "server error"}, False),
        ({"success": False, "status_code": 404, "error": "not found"}, False),
        ("not a dict", False),
    ],
)
def test_precondition_classification(result, expected):
    assert bool(precondition_failure_reason(result)) is expected


def test_threshold_is_configurable(monkeypatch):
    assert resolve_unavailable_tool_threshold(settings={}) == (
        DEFAULT_UNAVAILABLE_TOOL_THRESHOLD
    )
    assert resolve_unavailable_tool_threshold(
        settings={"agents": {"unavailable_tool_threshold": 7}},
    ) == 7
    monkeypatch.setenv("FERAL_UNAVAILABLE_TOOL_THRESHOLD", "2")
    assert resolve_unavailable_tool_threshold(
        settings={"agents": {"unavailable_tool_threshold": 7}},
    ) == 2


def test_budget_exposes_the_guard_for_both_tool_loops():
    """``agents/multi_agent.py`` calls SkillExecutor directly and never
    reaches ToolRunner's anti-loop block, so both loops drive this through
    the same IterationBudget rather than each rolling its own."""
    budget = IterationBudget(unavailable_threshold=1)
    tools = [
        {"function": {"name": "cutebot__set_lights"}},
        {"function": {"name": "notes_memory__fused_timeline"}},
    ]

    budget.observe_tool("cutebot__set_lights", {}, False, CUTEBOT_503)

    assert budget.unavailable_tools
    assert [t["function"]["name"] for t in budget.filter_tools(tools)] == [
        "notes_memory__fused_timeline",
    ]
    assert budget.take_unannounced_unavailable()


def test_filter_returns_none_when_nothing_survives():
    """``tools=None`` is the shape every caller already passes when it has
    no tools; an empty list is not."""
    budget = IterationBudget(unavailable_threshold=1)
    budget.observe_tool("cutebot__set_lights", {}, False, CUTEBOT_503)
    assert budget.filter_tools([{"function": {"name": "cutebot__set_lights"}}]) is None


@pytest.mark.asyncio
async def test_the_multi_agent_worker_stops_offering_the_dead_tool(monkeypatch):
    """The path routine-10 actually ran on.

    ``AgentWorker`` calls ``SkillExecutor.execute`` directly and never
    reaches ``ToolRunner``, so ToolRunner's hard anti-loop block never
    saw those 46 calls. The guard has to bite here too.
    """
    monkeypatch.setenv("FERAL_MAX_ITERATIONS", "0")
    monkeypatch.setenv("FERAL_UNAVAILABLE_TOOL_THRESHOLD", "3")

    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={})
    # Six rounds walking the argument, exactly as the routine did, then a
    # text answer so the worker can finish.
    rounds = [
        ("", [_tool_call("cutebot__explore", {"red": red})])
        for red in (255, 254, 253, 252, 251, 250)
    ]
    rounds.append(("the robot is not connected", []))
    responses = iter(rounds)
    llm.extract_response = MagicMock(side_effect=lambda _r: next(responses))

    executor = MagicMock()
    executor.execute = AsyncMock(return_value=CUTEBOT_503)

    worker = _make_worker(llm, executor)
    result = await worker.run("s1", "flash the lights")

    # Three failures, then the tool is gone from the next request AND
    # refused at dispatch if the model names it anyway.
    assert executor.execute.await_count == 3
    tool_lists = [c.kwargs.get("tools") for c in llm.chat.await_args_list]
    assert tool_lists[0], "the first round must still be offered the tool"
    assert tool_lists[-1] is None, (
        "once the only tool is withdrawn the worker asks for a plain answer"
    )
    assert result.text

