"""The `cron` surface must have a deny list, and it must not eat the robot.

Why this file exists
--------------------
`SURFACE_DENY_LISTS` had entries for http_api, mcp, websocket, phone_actuator,
local_cli and brain_host, and no entry at all for `cron`. `is_tool_allowed`
resolves an unlisted surface to `None` and returns True:

    denied = SURFACE_DENY_LISTS.get(surface)
    if denied is None:
        return True

So `is_tool_allowed("shell__exec", "cron")` was True. `resolve_policy` runs
`is_tool_allowed` as step 1 and `execute_routine_job` (api/server.py) runs
`resolve_policy(..., surface="cron")` as the ONLY gate on a routine's direct
skill invoke. A scheduled routine could therefore run shell, docker exec,
browser eval and the VLM desktop loop at 3am, all of which are hard-denied on
every other remote surface.

An ABSENT key is worse than a WRONG entry, and that is the point of test (c)
below. A wrong entry is loud: the tool is denied when it should not be, or a
sibling entry next to it shows the intended shape, and someone reading the
policy sees `cron` listed and can reason about it. An absent key is silent in
both directions. The lookup succeeds, returns "unrestricted", produces no
warning, no log line and no refusal, and the policy file reads as complete
because every OTHER surface is there. Nothing fails, so nothing gets noticed.
It fails open, quietly, and the first evidence would be an executed command.
So we pin the KEY's existence separately from its contents: a future refactor
that drops the key must break a test, not just weaken one.

What must keep working
----------------------
The operator runs nightly CuteBot routines (line-follow, spin, lights) through
the cron path. A deny list that blocks those is a wrong deny list, so the
CuteBot endpoints are pinned as allowed here with the same force as the
dangerous tools are pinned as denied.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from security.dangerous_tools import (  # noqa: E402
    SURFACE_DENY_LISTS,
    denied_tools_for_surface,
    is_tool_allowed,
    known_surfaces,
)

CRON = "cron"

# Tools that must never run with no human present. Both naming shapes are
# listed because the codebase carries both (dotted legacy / MCP, and modern
# `skill__endpoint` LLM tool ids) and the deny matcher is supposed to be
# naming-agnostic.
DANGEROUS_ON_CRON = (
    # shell / process execution
    "shell.exec",
    "shell__exec",
    "system.run",
    "system__run",
    "process.spawn",
    "process__spawn",
    "desktop_control__shell_command",
    "desktop_control.shell_command",
    "desktop_control__shell",
    "computer_use__bash",
    "coding_tools__bash",
    "coding_tools.bash",
    # filesystem destruction
    "fs.delete",
    "fs__delete",
    "fs.remove",
    "fs__remove",
    "filesystem.delete",
    "filesystem__delete",
    "file.delete",
    "file__delete",
    # container escape surface
    "docker.exec",
    "docker__exec",
    # arbitrary script evaluation in a browser context
    "browser.evaluate",
    "browser__evaluate",
    # arbitrary code eval
    "code_interpreter__execute",
    "code_interpreter__run_python",
    "code_interpreter__run_node",
    "workspace_scripts__run",
    "workspace_scripts__rerun",
    # unattended file writes
    "coding_tools__write_file",
    "coding_tools__edit_file",
    "computer_use__write_file",
    "computer_use__edit_file",
    # VLM-driven autonomous desktop loop
    "agentic_computer_use__execute_task",
)

# The routines the operator actually runs nightly: CuteBot line-follow (job 7),
# spin (job 9) and lights (job 10) in ~/.feral/scheduled_jobs.db. Every
# endpoint in skills/manifests/cutebot.json is pinned, not just the three in
# use, because the LLM picks the endpoint at fire time (a "spin then halt"
# prompt reaches drive AND halt).
CUTEBOT_ENDPOINTS_ON_CRON = (
    "cutebot__follow_line",
    "cutebot__explore",
    "cutebot__drive",
    "cutebot__halt",
    "cutebot__set_lights",
    "cutebot__status",
)

# Other enabled routines on this install that dispatch via the
# skill+endpoint branch, i.e. the branch that runs the cron pre-flight.
LIVE_ROUTINE_TOOLS_ON_CRON = (
    "digital_twin__daily_reflection",
    "calendar_google__get_today",
    "smart_home_hue__get_states",
    "messaging_sms__telegram_send",
    "health_data__health_summary",
)


class TestCronSurfaceIsRegisteredAtAll:
    """(c) The bug was an absent key, so pin the key itself."""

    def test_cron_key_exists_in_surface_deny_lists(self):
        assert CRON in SURFACE_DENY_LISTS, (
            "SURFACE_DENY_LISTS has no 'cron' key. is_tool_allowed() returns "
            "True for any surface it cannot find, so a missing key does not "
            "raise or warn: it silently allows every tool on the unattended "
            "scheduled-routine path."
        )

    def test_cron_is_a_known_surface(self):
        assert CRON in known_surfaces()

    def test_cron_deny_list_is_not_empty(self):
        """An empty set is the same fail-open outcome as a missing key.

        `is_tool_allowed` short-circuits on a falsy deny set too, so
        `"cron": set()` would restore the exact bug while looking present.
        """
        assert denied_tools_for_surface(CRON), "cron deny list is empty"


class TestDangerousToolsAreDeniedOnCron:
    """(a) Nothing that needs a human may run on a timer."""

    @pytest.mark.parametrize("tool", DANGEROUS_ON_CRON)
    def test_denied(self, tool):
        assert not is_tool_allowed(tool, CRON), (
            f"{tool} is allowed on surface 'cron'. It would run unattended."
        )

    def test_cron_is_at_least_as_strict_as_http_api(self):
        """http_api is the floor.

        http_api is a remote caller with an operator at the keyboard. cron is
        the same reach with nobody there, so anything denied to http_api must
        be denied to cron. Written as a set relation, not a copy of the list,
        so a tool added to http_api later cannot be forgotten here.
        """
        missing = denied_tools_for_surface("http_api") - denied_tools_for_surface(CRON)
        assert not missing, f"denied on http_api but allowed on cron: {sorted(missing)}"


class TestCuteBotRoutinesStillRunOnCron:
    """(b) The operator's nightly robot routines must not regress."""

    @pytest.mark.parametrize("tool", CUTEBOT_ENDPOINTS_ON_CRON)
    def test_allowed(self, tool):
        assert is_tool_allowed(tool, CRON), (
            f"{tool} is denied on surface 'cron'. This breaks the nightly "
            f"CuteBot routines (jobs 7, 9, 10), which is a wrong deny list."
        )

    @pytest.mark.parametrize("tool", LIVE_ROUTINE_TOOLS_ON_CRON)
    def test_other_enabled_routines_still_allowed(self, tool):
        assert is_tool_allowed(tool, CRON)

    def test_no_cutebot_entry_leaked_into_any_deny_list(self):
        """Robot control is the reason routines exist; no surface denies it."""
        for surface, denied in SURFACE_DENY_LISTS.items():
            leaked = {t for t in denied if t.startswith(("cutebot__", "cutebot."))}
            assert not leaked, f"surface {surface!r} denies CuteBot: {sorted(leaked)}"


class TestCronDenyReachesTheRoutineDispatcher:
    """The deny list is only worth anything through `resolve_policy`.

    `execute_routine_job` skips a run when `resolve_policy(...,
    surface="cron").level == LEVEL_DENY`, so assert the wiring end to end
    rather than trusting `is_tool_allowed` in isolation.
    """

    def test_dangerous_tool_resolves_to_deny(self):
        from security.safety_resolver import LEVEL_DENY, resolve_policy

        decision = resolve_policy(
            "coding_tools__bash", {"command": "rm -rf /"}, surface=CRON,
        )
        assert decision.level == LEVEL_DENY
        assert decision.sources.get("surface_deny") is True

    @pytest.mark.parametrize(
        "tool,args",
        [
            ("cutebot__follow_line", {}),
            ("cutebot__drive", {"left": 50, "right": -50}),
            ("cutebot__set_lights", {"color": "red"}),
            ("cutebot__halt", {}),
        ],
    )
    def test_cutebot_never_resolves_to_surface_deny(self, tool, args):
        """CONFIRM is fine (auto_confirm covers it); DENY skips the run."""
        from security.safety_resolver import LEVEL_DENY, resolve_policy

        decision = resolve_policy(tool, args, surface=CRON)
        assert not (
            decision.level == LEVEL_DENY and decision.sources.get("surface_deny")
        ), f"{tool} is surface-denied on cron: {decision.deny_reason}"


class TestAutoConfirmCannotOverrideASurfaceDeny:
    """`auto_confirm` in the routine payload must not wave a surface deny through.

    `execute_routine_job` treated ANY deny as overridable by
    `payload["auto_confirm"]`, with one carve-out for the CuteBot speed limit.
    But auto_confirm lives in the same payload that names the tool, so a
    routine could hand itself the override, and the new cron deny list would
    have been decorative. Surface deny now joins physical-safety deny as
    non-overridable. Manifest-tier DENY stays overridable, which is what the
    operator's pre-authorised device routines rely on (see
    tests/test_automation_time_context.py).
    """

    def _run_routine(self, monkeypatch, payload, tmp_path):
        import api.server as server
        from agents.scheduler import CronService, JobType
        from skills.base import BaseSkill
        from skills.impl import SKILL_IMPLEMENTATIONS
        from skills.registry import SkillRegistry

        class _RecordingSkill(BaseSkill):
            def __init__(self, skill_id):
                super().__init__(skill_id=skill_id)
                self.calls = []

            async def execute(self, endpoint_id, args, vault):
                self.calls.append((endpoint_id, args))
                return {
                    "success": True, "status_code": 200,
                    "data": {"ran": endpoint_id}, "error": None,
                }

        cron = CronService(db_path=str(tmp_path / "cron.db"))
        reg = SkillRegistry()
        reg.load_builtin_skills()
        impl = _RecordingSkill(payload["skill"])
        monkeypatch.setitem(SKILL_IMPLEMENTATIONS, payload["skill"], impl)

        saved = {
            k: getattr(server.state, k, None)
            for k in (
                "cron_service", "skill_registry", "orchestrator",
                "cron_cost_guard", "taskflows",
            )
        }
        server.state.cron_service = cron
        server.state.skill_registry = reg
        server.state.orchestrator = None
        server.state.cron_cost_guard = None
        server.state.taskflows = None
        try:
            job = cron.create_job(
                JobType.SCHEDULED, "daily 03:00", "deny-check", payload, "",
                recurring=False,
            )
            server.execute_routine_job(job)
            return impl.calls, cron.get_runs(job.id, limit=1)[0]
        finally:
            for k, v in saved.items():
                setattr(server.state, k, v)
            cron.close()

    def test_shell_routine_with_auto_confirm_is_still_skipped(
        self, monkeypatch, tmp_path,
    ):
        calls, run = self._run_routine(
            monkeypatch,
            {
                "skill": "coding_tools",
                "endpoint": "bash",
                "args": {"command": "echo unattended"},
                "auto_confirm": True,
            },
            tmp_path,
        )
        assert calls == [], "a shell routine executed on the cron surface"
        assert run["status"] == "skipped"

    def test_cutebot_routine_with_auto_confirm_still_runs(
        self, monkeypatch, tmp_path,
    ):
        """The control: the carve-out did not become a blanket block."""
        calls, run = self._run_routine(
            monkeypatch,
            {
                "skill": "cutebot",
                "endpoint": "follow_line",
                "args": {},
                "auto_confirm": True,
            },
            tmp_path,
        )
        assert calls == [("follow_line", {})]
        assert run["status"] == "success"
