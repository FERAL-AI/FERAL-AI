"""``execution.full_authority`` governs shell and files, not actuators.

The two keys were added in the same release by separate branches that
did not know about each other: ``full_authority`` lifts the command deny
floor, the Docker requirement, workspace confinement and the filesystem
path check; the hardware allowlist gates physical actuation at
``DeviceRegistry.execute_action``.

They do not interact today, which is a decision rather than an accident,
and SECURITY.md now says so. An actuator moves something in the world --
a lock opens, a motor turns -- and that is a different category of
consequence from a shell command on a machine the operator already owns.
It is also not undoable by restoring a file, which is the boundary the
rest of this codebase draws.

This file exists because that separation is currently enforced by the
*absence* of a code path, and an absence is exactly the thing a later
refactor removes without noticing. If someone decides the keys should
compose, that should be a deliberate change that fails here first.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from security.sandbox_policy import SandboxPolicy  # noqa: E402


def _granting_policy(**filesystem) -> SandboxPolicy:
    policy = SandboxPolicy()
    policy._data["execution"] = {
        **policy._data.get("execution", {}),
        "allow_shell_commands": True,
        "full_authority": True,
    }
    if filesystem:
        policy._data["filesystem"] = {
            **policy._data.get("filesystem", {}), **filesystem,
        }
    return policy


def test_the_key_is_on_for_these_tests():
    assert _granting_policy().full_authority() is True


def test_it_lifts_the_command_deny_floor():
    """The thing it IS for, so a failure here means the key broke, not
    that the hardware separation broke."""
    assert _granting_policy().denied_command_patterns() == []


def test_the_hardware_policy_module_never_consults_it():
    """The separation, asserted where it lives.

    ``security/hardware_policy.py`` decides whether a capability may run
    and whether it may run unattended. If it ever reads
    ``full_authority``, granting shell latitude would silently grant
    physical actuation too.
    """
    src = (ROOT / "security" / "hardware_policy.py").read_text()
    assert "full_authority" not in src, (
        "hardware_policy now reads full_authority, so granting shell and "
        "filesystem latitude also grants unattended physical actuation. "
        "If that is intended, update SECURITY.md's 'It does not reach "
        "hardware' section in the same change."
    )


def test_the_actuator_allowlist_still_applies_under_the_key():
    """Behavioural half of the same claim.

    A capability the operator has not named must stay refused even with
    full authority granted, because the hardware allowlist is a separate
    decision.
    """
    from security.hardware_policy import permits_unattended

    policy = _granting_policy()
    # A capability id no shipped device declares and no operator named.
    assert permits_unattended(policy, "unlock_front_door", "actuator") is False


def test_a_named_actuator_is_still_permitted():
    """The other direction, so the test above cannot pass by the
    allowlist being broken outright."""
    from security.hardware_policy import permits_unattended

    policy = _granting_policy()
    allowed = (
        policy._data.get("hardware", {}).get("actuators", {}).get("allowed", [])
    )
    if not allowed:
        pytest.skip("shipped policy declares no allowed actuators")
    assert permits_unattended(policy, allowed[0], "actuator") is True


def test_security_md_documents_the_separation():
    """The interaction is enforced by an absence; the doc is the only
    place a reader learns it is deliberate."""
    doc = (REPO / "SECURITY.md").read_text()
    assert "It does not reach hardware" in doc
    assert "hardware.actuators.allowed" in doc


def test_full_authority_is_read_only_where_it_is_meant_to_be():
    """Enumerate every reader, so a new one is a decision.

    ``full_authority`` is a break-glass key. Every place that consults it
    widens what the model may do, so the set of readers is worth pinning
    the way the undoable-tool set is pinned for earned autonomy.
    """
    readers = set()
    for d in ("api", "agents", "hardware", "memory", "security", "skills"):
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if "build/" in str(p.relative_to(ROOT)):
                continue
            try:
                tree = ast.parse(p.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "full_authority":
                    readers.add(str(p.relative_to(ROOT)))
                elif isinstance(node, ast.Attribute) and node.attr == "full_authority":
                    readers.add(str(p.relative_to(ROOT)))

    expected = {"security/sandbox_policy.py", "security/exec_mode.py",
                "api/routes/security_and_hardware.py"}
    unexpected = readers - expected
    assert not unexpected, (
        f"new readers of full_authority: {sorted(unexpected)}. Each one "
        "widens a break-glass key; confirm the widening is intended and "
        "add it to `expected` in the same change."
    )
