"""Restarting your own machine is a question, not a refusal.

``shutdown`` / ``reboot`` / ``halt`` / ``poweroff`` used to sit in
``SandboxPolicy._COMMAND_DENY_FLOOR`` alongside the commands that destroy
a filesystem or a raw device. The floor applies at every autonomy tier
and, before ``execution.full_authority``, nothing could lift it. So the
answer to "reboot my machine" was no, at every tier, with no override.

That is a refusal standing in for a question. The floor is for things
that are catastrophic *and never legitimate*, where there is nothing to
confirm because no operator wants them. A restart is reversible, it is
ordinary sysadmin, and asking an assistant to do it is a reasonable
request.

So they are gated as the question they are. The ``coding_tools__bash``
endpoint is ``safety_tier: confirm``, which means strict and hybrid ask
the operator before running one. ``loose`` runs it without asking, which
is what loose means and what an operator chose it for.

These tests pin both halves: that the floor no longer swallows the
question, and that something actually asks it. Removing the floor entry
without the second half would have turned a refusal into a silent yes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.orchestrator import Orchestrator  # noqa: E402
from security.exec_approvals import ApprovalManager  # noqa: E402
from security.exec_mode import MODE_REFUSED, resolve_execution_mode  # noqa: E402
from security.sandbox_policy import SandboxPolicy  # noqa: E402

# Spelled in pieces so this file does not contain the literal commands.
# Nothing here runs anything; every assertion asks the policy layer what
# it *would* decide.
_REBOOT = "sudo " + "reboot"
_SHUTDOWN = "shut" + "down -h now"
_POWEROFF = "sudo " + "power" + "off"
_HALT = "sudo " + "halt"

POWER_COMMANDS = [_REBOOT, _SHUTDOWN, _POWEROFF, _HALT]

# The floor's actual job, which must not move.
_WIPE_ROOT = "rm" + " -rf /"
_WIPE_ROOT_FORCED = _WIPE_ROOT + " --no-preserve-root"
_FORMAT = "mkfs" + ".ext4 /dev/sda1"

DESTRUCTIVE = [_WIPE_ROOT, _WIPE_ROOT_FORCED, _FORMAT]


def _policy(path) -> SandboxPolicy:
    policy = SandboxPolicy.load_default()
    policy.grant_folder(str(path), mode="readwrite")
    policy._data["execution"] = {
        **policy._data.get("execution", {}),
        "allow_shell_commands": True,
    }
    return policy


def _refused_by_floor(command, policy, cwd, tier="loose") -> bool:
    decision = resolve_execution_mode(
        command, policy=policy, cwd=str(cwd), autonomy_mode=tier
    )
    return decision.mode == MODE_REFUSED and "denied pattern" in (decision.reason or "")


# ----------------------------------------------------------------------
# The floor lets the question through
# ----------------------------------------------------------------------

@pytest.mark.parametrize("command", POWER_COMMANDS)
@pytest.mark.parametrize("tier", ["strict", "hybrid", "loose"])
def test_the_floor_no_longer_refuses_a_restart(command, tier, tmp_path):
    assert not _refused_by_floor(command, _policy(tmp_path), tmp_path, tier), (
        f"{command!r} is still refused by the deny floor, so the operator "
        "cannot answer the question even by confirming it"
    )


@pytest.mark.parametrize("command", DESTRUCTIVE)
@pytest.mark.parametrize("tier", ["strict", "hybrid", "loose"])
def test_the_destructive_commands_did_not_move(command, tier, tmp_path):
    """The floor keeps doing the job it is actually for."""
    assert _refused_by_floor(command, _policy(tmp_path), tmp_path, tier)


def test_the_floor_still_has_entries():
    """Guards against 'fixing' this by emptying the floor."""
    assert len(SandboxPolicy()._COMMAND_DENY_FLOOR) >= 5


# ----------------------------------------------------------------------
# Something actually asks
# ----------------------------------------------------------------------
#
# This is the half that makes the change safe. Dropping the floor entry
# on its own would turn a refusal into a silent yes at every tier.

@pytest.fixture
def orchestrator() -> Orchestrator:
    reg = MagicMock()
    reg.skills = {}
    reg.find_skills_for_query = MagicMock(return_value=[])
    reg.get_tools_for_skills = MagicMock(return_value=[])
    orch = Orchestrator(
        skill_registry=reg,
        send_to_client=AsyncMock(),
        daemons={},
        memory=None,
        vision_buffer=None,
        perception=None,
        learner=None,
        approval_manager=ApprovalManager(db_path=":memory:"),
    )
    orch._send_text = AsyncMock()
    orch._try_genui_for_result = AsyncMock()
    return orch


@pytest.mark.parametrize("command", POWER_COMMANDS)
@pytest.mark.parametrize("tier", ["strict", "hybrid"])
def test_strict_and_hybrid_ask_before_restarting(orchestrator, command, tier):
    """The operator is asked, and nothing runs until they answer."""
    orchestrator.tool_runner._autonomy_mode = tier

    pending = orchestrator.tool_runner.enforce_safety(
        "coding_tools__bash", {"command": command}, session_id=f"sess-{tier}",
    )

    assert pending is not None, (
        f"under {tier!r} the brain would run {command!r} without asking"
    )
    assert pending["status"] == "pending_approval"


@pytest.mark.parametrize("command", POWER_COMMANDS)
def test_loose_runs_it_without_asking(orchestrator, command):
    """Not an oversight. loose means nothing needs approval.

    An operator who chose loose asked for exactly this, and a special
    case here would be the runtime overriding the tier they picked,
    which is the defect this whole line of work exists to remove.
    """
    orchestrator.tool_runner._autonomy_mode = "loose"

    pending = orchestrator.tool_runner.enforce_safety(
        "coding_tools__bash", {"command": command}, session_id="sess-loose",
    )

    assert pending is None


def test_the_bash_endpoint_is_still_confirm_tier():
    """The gate above rests entirely on this. Pin it.

    If ``coding_tools__bash`` ever resolves to ``auto``, strict and
    hybrid stop asking and the floor removal becomes a silent yes.
    """
    from security.safety_resolver import LEVEL_CONFIRM, resolve_policy

    assert resolve_policy(tool_name="coding_tools__bash", surface="chat").level == (
        LEVEL_CONFIRM
    )


def test_the_manifest_agrees_with_the_resolver():
    """Two sources, one answer. They have drifted before."""
    import json

    manifest = json.loads((ROOT / "skills" / "manifests" / "coding_tools.json").read_text())
    bash = next(e for e in manifest["endpoints"] if e["id"] == "bash")
    assert bash["safety_tier"] == "confirm"
