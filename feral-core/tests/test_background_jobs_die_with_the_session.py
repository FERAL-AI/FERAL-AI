"""A detached shell job must not outlive the session that started it.

``coding_tools__bash`` with ``run_in_background`` starts a real detached
process. ``CodingToolsSkill.clear_session`` was written to reap those
jobs, and nothing called it: the orchestrator's disconnect fan-out lives
in a different file than the skill.

The consequence is not theoretical. ``kill_bash`` is scoped to the owning
session, so once the session is gone nobody can read the job's output and
nobody can stop it. A ``sleep 120`` (or a runaway build) kept a process
and its whole group alive with no reachable owner, bounded only by its
wall-clock timeout and an atexit reaper that only fires when the brain
itself exits.

``ToolRunner.clear_session`` now reaps them, which is the point at which
every surface (websocket disconnect, CLI exit, channel teardown) already
converges.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from agents.tool_runner import ToolRunner
from security.sandbox_policy import SandboxPolicy
from skills.call_context import bind_context
from skills.registry import SkillRegistry


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _Orch:
    def __init__(self, registry):
        self.skills = registry


class _PlanMode:
    def clear_session(self, session_id):  # pragma: no cover - trivial stub
        pass


def _runner(registry) -> ToolRunner:
    runner = ToolRunner.__new__(ToolRunner)
    runner._orch = _Orch(registry)
    runner._tool_repeat_state = {}
    runner._reaper_tasks = set()
    runner.plan_mode = _PlanMode()
    return runner


@pytest.fixture(scope="module")
def registry():
    reg = SkillRegistry()
    reg.load_builtin_skills()
    return reg


@pytest.fixture
def granted_cwd(tmp_path):
    """A folder bash may run in.

    The suite runs under an isolated FERAL_HOME, so the developer's real
    workspace grants do not apply and an ungranted cwd is a correct 403.
    """
    d = tmp_path / "work"
    d.mkdir()
    SandboxPolicy.load_default().grant_folder(str(d), mode="readwrite")
    return str(d)


class TestTheJobDiesWithTheSession:
    def test_the_os_process_is_actually_gone(self, registry, granted_cwd):
        """Asserts on the OS, not on FERAL's own bookkeeping.

        Dropping the record while the process keeps running would satisfy
        a test written against the job table and would be exactly the
        bug: an unowned process nobody can reach.
        """
        skill = registry.get_skill("coding_tools")

        async def scenario():
            with bind_context(session_id="reap-test", tool_name="coding_tools__bash"):
                started = await skill.execute(
                    "bash",
                    {"command": "sleep 120", "run_in_background": True, "cwd": granted_cwd},
                    {},
                )
            data = started.get("data") or {}
            pid = data.get("pid")
            assert started.get("status_code") == 202, started
            assert pid, "no pid reported for a background job"
            assert _pid_alive(pid), "the job was never actually running"

            _runner(registry).clear_session("reap-test")
            for _ in range(40):  # up to ~4s for TERM to land
                await asyncio.sleep(0.1)
                if not _pid_alive(pid):
                    return pid
            return pid

        pid = asyncio.run(scenario())
        assert not _pid_alive(pid), (
            f"pid {pid} survived the session that started it; nothing can reach it now"
        )

    def test_another_sessions_job_is_left_alone(self, registry, granted_cwd):
        """Reaping must be scoped, or one disconnect kills everyone's work."""
        skill = registry.get_skill("coding_tools")

        async def scenario():
            with bind_context(session_id="keeper", tool_name="coding_tools__bash"):
                keep = await skill.execute(
                    "bash",
                    {"command": "sleep 30", "run_in_background": True, "cwd": granted_cwd},
                    {},
                )
            keep_pid = (keep.get("data") or {}).get("pid")
            _runner(registry).clear_session("some-other-session")
            await asyncio.sleep(0.6)
            still_alive = _pid_alive(keep_pid)
            with bind_context(session_id="keeper"):
                await skill.execute(
                    "kill_bash", {"job_id": (keep.get("data") or {}).get("job_id")}, {},
                )
            return still_alive

        assert asyncio.run(scenario()) is True, (
            "clearing one session killed another session's job"
        )


class TestTheReaperIsSafe:
    def test_a_disconnect_never_raises(self, registry):
        """A teardown path that can raise turns a disconnect into an error."""
        runner = _runner(registry)
        runner._orch = _Orch(None)          # registry unavailable
        runner.clear_session("whatever")     # must not raise

    def test_it_still_clears_the_ordinary_state(self, registry):
        runner = _runner(registry)
        runner._tool_repeat_state["s1"] = {"x": 1}
        runner.clear_session("s1")
        assert "s1" not in runner._tool_repeat_state

    def test_the_reaper_task_is_held(self, registry):
        """A bare create_task can be collected mid-kill; the loop holds
        tasks only weakly. CLAUDE.md calls this out explicitly."""
        skill = registry.get_skill("coding_tools")
        assert skill is not None

        async def scenario():
            runner = _runner(registry)
            runner.clear_session("no-jobs-here")
            # The set exists and the callback discards on completion, so it
            # neither leaks nor drops the reference while in flight.
            assert isinstance(runner._reaper_tasks, set)
            await asyncio.sleep(0.2)
            return runner._reaper_tasks

        assert asyncio.run(scenario()) == set()
