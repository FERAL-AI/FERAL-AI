"""A human can take the floor off. It is their computer.

``SandboxPolicy._COMMAND_DENY_FLOOR`` refuses six command shapes, and it
refused them at every autonomy tier including ``loose``, with nothing in
the policy file able to remove them: ``denied_command_patterns`` let an
operator *add* rules and never drop the built-in ones.

That made ``loose`` a promise the runtime did not keep. ``loose`` is
documented as "nothing needs approval", and the tier system is meant to
be the single authority on what FERAL is allowed to do. A floor that
outranks the tier is a second authority, and an operator who chose loose
was not told about it.

The sharpest case was not a destructive one. ``sudo reboot`` sat in the
floor next to ``mkfs``: rebooting your own machine is ordinary work, it
is reversible, and FERAL refused it at every tier with no override.

``execution.full_authority: true`` is the answer. Off by default, so the
floor still stands for everybody who has not thought about it, and the
model's ability to spell ``mkfs`` for its own reasons is still contained.
Set, it means the human sat down and said which side of that line they
are on.

Policy file only. Not an environment variable: an env var is something
you can be handed by a script or a parent process, and this is something
you have to mean.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from security.exec_mode import MODE_REFUSED, resolve_execution_mode  # noqa: E402
from security.sandbox_policy import SandboxPolicy  # noqa: E402

# Spelled in pieces so this file does not contain the literal command
# text. Nothing here executes anything: every assertion below asks the
# policy layer what it *would* decide.
_WIPE_ROOT = "rm" + " -rf /"
_WIPE_ROOT_FORCED = _WIPE_ROOT + " --no-preserve-root"
_FORMAT = "mkfs" + ".ext4 /dev/sda1"
_REBOOT = "sudo " + "reboot"

FLOOR_COMMANDS = [_WIPE_ROOT, _WIPE_ROOT_FORCED, _FORMAT, _REBOOT]


def _policy(path, **execution) -> SandboxPolicy:
    """A policy that reaches the floor.

    The workspace grant is load-bearing, not scenery. A bare
    ``SandboxPolicy()`` grants no workspace, so ``resolve_execution_mode``
    refuses every command with ``needs=workspace_grant`` long before the
    deny floor is consulted. A test built on one passes green while the
    floor does nothing, which is precisely the failure this file exists
    to detect. ``grant_folder`` also satisfies strict autonomy, which
    demands an explicit grant rather than a policy read path, so the same
    fixture is honest at all three tiers.
    """
    policy = SandboxPolicy.load_default()
    policy.grant_folder(str(path), mode="readwrite")
    policy._data["execution"] = {
        **policy._data.get("execution", {}),
        "allow_shell_commands": True,
        **execution,
    }
    return policy


def _refused_by_floor(command: str, policy: SandboxPolicy, cwd, tier="loose") -> bool:
    """Did the *deny floor* refuse, as opposed to some other rule?

    ``resolve_execution_mode`` refuses for several unrelated reasons.
    Matching on ``MODE_REFUSED`` alone would let this file pass while
    the floor did nothing.
    """
    decision = resolve_execution_mode(
        command, policy=policy, cwd=str(cwd), autonomy_mode=tier
    )
    return decision.mode == MODE_REFUSED and "denied pattern" in (decision.reason or "")


# ----------------------------------------------------------------------
# Default: the floor stands
# ----------------------------------------------------------------------

def test_full_authority_is_off_unless_asked_for():
    assert SandboxPolicy().full_authority() is False


@pytest.mark.parametrize("command", FLOOR_COMMANDS)
@pytest.mark.parametrize("tier", ["strict", "hybrid", "loose"])
def test_by_default_the_floor_holds_at_every_tier(command, tier, tmp_path):
    """Unchanged behaviour for anyone who has not set the key.

    ``loose`` is the one that matters: it is the tier that means
    "nothing needs approval", and the floor outranks it.
    """
    assert _refused_by_floor(command, _policy(tmp_path), tmp_path, tier)


def test_an_ordinary_command_is_not_refused_by_the_default_floor(tmp_path):
    """The fixture reaches the floor rather than failing earlier.

    Without this, every assertion above would still pass if the policy
    refused everything for an unrelated reason.
    """
    decision = resolve_execution_mode(
        "ls -la", policy=_policy(tmp_path), cwd=str(tmp_path), autonomy_mode="loose"
    )
    assert decision.mode != MODE_REFUSED, decision.reason


# ----------------------------------------------------------------------
# Granted: the floor gets out of the way
# ----------------------------------------------------------------------

@pytest.mark.parametrize("command", FLOOR_COMMANDS)
@pytest.mark.parametrize("tier", ["strict", "hybrid", "loose"])
def test_a_granted_human_is_not_refused_by_the_floor(command, tier, tmp_path):
    assert not _refused_by_floor(
        command, _policy(tmp_path, full_authority=True), tmp_path, tier
    )


def test_the_floor_is_empty_under_a_grant(tmp_path):
    """Directly, so this does not depend on how exec_mode composes."""
    assert _policy(tmp_path, full_authority=True).denied_command_patterns() == []


def test_the_operators_own_rules_survive_the_grant(tmp_path):
    """Two separate decisions, both of them the operator's.

    Someone who granted full authority and then wrote their own deny
    list meant both things. Dropping their rules along with the floor
    would be the runtime overriding them a second time.
    """
    policy = _policy(
        tmp_path,
        full_authority=True,
        denied_command_patterns=[r"terraform\s+destroy"],
    )
    assert _refused_by_floor("terraform destroy", policy, tmp_path)
    assert not _refused_by_floor(_WIPE_ROOT, policy, tmp_path)


# ----------------------------------------------------------------------
# The shape of the switch
# ----------------------------------------------------------------------

def test_the_grant_is_not_readable_from_the_environment():
    """An env var can be handed to you. This has to be meant.

    Checked against the source rather than by setting candidate names,
    which would only cover the names someone thought to guess, and would
    leave them behind for the suite's env-leak guard to report.

    If a future change wires this to the environment, that should be a
    decision made on purpose, and it should fail here first.
    """
    names = set(SandboxPolicy.full_authority.__code__.co_names)
    assert not names & {"environ", "getenv"}, (
        f"full_authority reads the environment ({names}); it is meant to "
        "require an explicit edit to the policy file"
    )


@pytest.mark.parametrize("value", [False, None, 0, ""])
def test_only_a_real_yes_counts(value, tmp_path):
    """A key present but falsey is not a grant."""
    assert _policy(tmp_path, full_authority=value).full_authority() is False
    assert _policy(tmp_path, full_authority=value).denied_command_patterns()


def test_granting_authority_is_announced(tmp_path, caplog):
    """A machine with the floor off should say so where it is findable."""
    import logging

    policy = _policy(tmp_path, full_authority=True)
    with caplog.at_level(logging.WARNING):
        policy.denied_command_patterns()
    assert any(
        "full_authority" in r.message and "OFF" in r.message
        for r in caplog.records
    ), "no warning names the state the machine is in"


def test_the_announcement_does_not_repeat_per_command(tmp_path, caplog):
    """Standing state, logged once. A line on every call trains skipping."""
    import logging

    policy = _policy(tmp_path, full_authority=True)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            policy.denied_command_patterns()
    warnings = [r for r in caplog.records if "full_authority" in r.message]
    assert len(warnings) == 1, f"logged {len(warnings)} times, expected once"
