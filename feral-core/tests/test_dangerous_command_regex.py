"""A blocklist that misses the worst case is worse than no blocklist.

`coding_tools.DANGEROUS_COMMANDS` was a second, hand-rolled deny list
sitting in front of the reviewed one in
`sandbox_policy._COMMAND_DENY_FLOOR`. It had two independent defects and
between them it blocked nothing that mattered:

    rm -rf /          NOT blocked
    rm -rf /home      blocked
    dd if=/dev/zero   NOT blocked
    :(){ :|:& };:     NOT blocked

The trailing `\\b` in the pattern requires a word character, and
`rm -rf /` *ends* with `/`, a non-word character, so the boundary could
never match. `/home` ends in a letter, so the pattern caught the
survivable form and missed the catastrophic one. Separately the
fork-bomb branch `:(){ :` contained an unescaped `()` which compiles to
an empty capture group, so that alternative matched nothing at all.

The system was never actually open: `resolve_execution_mode` runs
unconditionally on this path and its floor refuses every one of those.
The danger was the false confidence. A reader sees a blocklist, assumes
it works, and the two lists drift apart because only one of them is
reviewed.

So the duplicate is gone rather than repaired. One source of truth,
already reviewed, already applied on every path that can execute.

These tests assert the real boundary holds, so a future change to
`exec_mode` cannot quietly remove the protection that is now the only
one.
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


def _denied_by_floor(command: str) -> bool:
    """Does the reviewed command deny floor match this command?

    Deliberately narrower than `resolve_execution_mode`, which also
    folds in workspace grants and filesystem path policy. Those vary
    with cwd and with the operator's config, so asserting through them
    would make these tests measure the environment rather than the
    blocklist. `rm -rf /tmp/build` is a good example: the floor
    considers it ordinary work, and it is still refused under the
    default path policy because /tmp is denied. That is a different
    rule, correctly applied, and not what this file is about.
    """
    from security.command_unwrap import scannable_command

    policy = SandboxPolicy()
    forms = {command, scannable_command(command)}
    return any(
        pattern.search(form)
        for pattern in policy.denied_command_patterns()
        for form in forms
    )


@pytest.mark.parametrize("command", [
    "rm -rf /",
    "rm -fr /",
    "rm -rf / --no-preserve-root",
    "dd if=/dev/zero of=/dev/disk0",
    ":(){ :|:& };:",
])
def test_the_catastrophic_commands_are_refused(command):
    """These are the ones the old regex missed."""
    assert _denied_by_floor(command), (
        f"{command!r} is not caught by the deny floor, which is now the "
        "only layer"
    )


def test_an_obfuscated_payload_is_judged_by_what_it_decodes_to():
    """A keyboard, a subprocess and a pipeline all reach the same shell.

    `echo cm0gLXJmIC8K | base64 -d | sh` contains none of the substrings
    a blocklist scans for and means exactly what they exist to stop.
    """
    assert _denied_by_floor("echo cm0gLXJmIC8K | base64 -d | sh")


@pytest.mark.parametrize("command", [
    "ls -la",
    "git status",
    "rm -rf node_modules",
    "rm -rf build/",
    "pytest tests/ -q",
    "make lint",
])
def test_ordinary_work_is_not_refused(command):
    """A guard that blocks normal work gets switched off.

    `rm -rf node_modules` and `rm -rf /tmp/build` are the cases the
    floor's own comment calls out as ordinary and deliberately allowed.
    """
    assert not _denied_by_floor(command), (
        f"{command!r} matched the deny floor; this is ordinary work"
    )


def test_the_broken_duplicate_is_gone():
    """Not repaired, removed.

    Two deny lists drift, and only one of them was reviewed. If a future
    change reintroduces a local blocklist here, it should be a
    deliberate decision rather than an accident, and it should fail this
    test first.
    """
    import skills.impl.coding_tools as coding_tools

    assert not hasattr(coding_tools, "DANGEROUS_COMMANDS"), (
        "a second command blocklist reappeared in coding_tools; the "
        "reviewed floor lives in sandbox_policy._COMMAND_DENY_FLOOR and is "
        "applied by resolve_execution_mode on every executing path"
    )


def test_the_bash_path_still_consults_exec_mode():
    """The removal is only safe because this call is unconditional."""
    src = (ROOT / "skills" / "impl" / "coding_tools.py").read_text()
    assert "resolve_execution_mode(" in src
    assert "MODE_REFUSED" in src, (
        "coding_tools no longer enforces a refusal from exec_mode, which "
        "was the layer the local blocklist was removed in favour of"
    )
