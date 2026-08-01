"""ReDoS-safe compilation for patterns FERAL did not write.

The defect this pins: Python's ``re`` is a backtracking engine with no
match timeout. ``re.compile("(a+)+$").search("a" * 30 + "b")`` does not
return in any useful amount of time, and there is no way to interrupt it
once it starts. A pattern that arrives from a policy file or a tool
argument therefore has to be refused *before* compilation or not at all.

``SandboxPolicy.denied_command_patterns`` is the in-repo consumer:
operator rules from ``execution.denied_command_patterns`` go through
:func:`compile_safe_regex`, the reviewed floor does not.
"""

from __future__ import annotations

import re
import time

import pytest

from security.safe_regex import (
    MAX_PATTERN_CHARS,
    UnsafePatternError,
    compile_safe_regex,
    is_pattern_safe,
)
from security.sandbox_policy import SandboxPolicy


# ── the shapes that actually blow up ──────────────────────────────


@pytest.mark.parametrize(
    "pattern",
    [
        r"(a+)+$",          # the canonical nested quantifier
        r"(a*)*b",
        r"(\d+)*x",
        r"(a|ab)+c",        # quantified alternation
        r"(x|y|z)*w",
        r"([a-z]+)+@",
        r"(a{2,})+",
    ],
)
def test_catastrophic_shapes_are_refused(pattern):
    with pytest.raises(UnsafePatternError):
        compile_safe_regex(pattern)
    assert is_pattern_safe(pattern) is False


def test_the_refused_pattern_really_is_catastrophic():
    """Proof the refusal is earning its keep, not superstition.

    Compiled directly (bypassing the safe compiler) and matched against
    a short adversarial subject, this pattern takes far longer than any
    regex has business taking. The point of the test is that the number
    is enormous even at n=24; the safe compiler never lets it run.
    """
    evil = re.compile(r"(a+)+$")
    subject = "a" * 24 + "b"

    start = time.monotonic()
    evil.search(subject)
    elapsed = time.monotonic() - start

    assert elapsed > 0.1, (
        "expected catastrophic backtracking; if this got fast, Python's "
        "engine changed and the refusal set deserves a fresh look"
    )


@pytest.mark.parametrize(
    "pattern",
    [
        r"(\w)\1",          # backreference
        r"(?P<x>a)(?P=x)",  # named backreference
        r"a(?=b)",          # lookahead
        r"a(?!b)",          # negative lookahead
        r"(?<=a)b",         # lookbehind
        r"(?<!a)b",         # negative lookbehind
    ],
)
def test_backreferences_and_lookarounds_are_refused(pattern):
    with pytest.raises(UnsafePatternError):
        compile_safe_regex(pattern)


def test_named_groups_are_not_mistaken_for_lookbehind():
    """``(?P<name>`` and ``(?<=`` both start ``(?<``-ish.

    A naive check written for JavaScript refuses named groups outright,
    because in JS the syntax IS ``(?<name>``. Python spells it
    ``(?P<name>``, so the checker must key on the ``=``/``!``.
    """
    compiled = compile_safe_regex(r"(?P<who>\w+) sent")
    assert compiled.search("alice sent").group("who") == "alice"


# ── ordinary patterns must still compile ──────────────────────────


@pytest.mark.parametrize(
    "pattern",
    [
        r"\brm\b.*-rf",
        r"^git\s+push\s+--force",
        r"curl\s+\S+\s*\|\s*sh",
        r"[a-z]+@[a-z]+\.com",
        r"(?:sudo|doas)\s+",
        r"\d{4}-\d{2}-\d{2}",
        r"foo|bar|baz",
        r"a?b*c+",
        r"(abc)+",
        r"x+?y",
    ],
)
def test_reasonable_patterns_compile(pattern):
    assert compile_safe_regex(pattern) is not None
    assert is_pattern_safe(pattern) is True


def test_lazy_quantifier_is_not_a_stacked_quantifier():
    """``a+?`` is one lazy quantifier, not ``+`` followed by ``?``."""
    assert compile_safe_regex(r"<.+?>").findall("<a><b>") == ["<a>", "<b>"]


def test_literal_brace_is_not_treated_as_a_quantifier():
    """``a{b`` is a literal brace in Python and must not be refused."""
    assert compile_safe_regex(r"func\{b").search("func{b") is not None


def test_flags_are_passed_through():
    assert compile_safe_regex(r"abc", re.IGNORECASE).search("ABC") is not None


# ── input validation ──────────────────────────────────────────────


def test_empty_pattern_is_refused():
    with pytest.raises(UnsafePatternError):
        compile_safe_regex("")


def test_oversized_pattern_is_refused():
    with pytest.raises(UnsafePatternError):
        compile_safe_regex("a" * (MAX_PATTERN_CHARS + 1))


def test_non_string_is_refused():
    with pytest.raises(UnsafePatternError):
        compile_safe_regex(None)  # type: ignore[arg-type]


def test_invalid_regex_raises_re_error_not_unsafe():
    """"You wrote it wrong" must be distinguishable from "you may not".

    A caller reporting config errors wants to say "unbalanced bracket",
    not "unsafe pattern", for a plain typo.
    """
    with pytest.raises(re.error):
        compile_safe_regex(r"[unclosed")


# ── the in-repo consumer ──────────────────────────────────────────


def test_operator_deny_patterns_are_compiled_safely():
    policy = SandboxPolicy({
        "execution": {"denied_command_patterns": [r"\bterraform\s+destroy\b"]},
    })
    patterns = policy.denied_command_patterns()
    assert any(p.search("terraform destroy -auto-approve") for p in patterns)


def test_an_unsafe_operator_pattern_is_dropped_not_fatal(caplog):
    """One bad line in a policy file must not take the brain down.

    Dropping it keeps the floor in force and leaves a log line; raising
    would mean a typo in an optional config key becomes a boot failure.
    """
    policy = SandboxPolicy({
        "execution": {"denied_command_patterns": [r"(a+)+$", r"\bok\b"]},
    })
    with caplog.at_level("WARNING"):
        patterns = policy.denied_command_patterns()

    sources = [p.pattern for p in patterns]
    assert r"(a+)+$" not in sources
    assert r"\bok\b" in sources


def test_the_reviewed_floor_survives_a_bad_operator_pattern():
    policy = SandboxPolicy({
        "execution": {"denied_command_patterns": [r"(a|ab)+"]},
    })
    patterns = policy.denied_command_patterns()
    assert any(p.search("mkfs.ext4 /dev/sda1") for p in patterns)


def _floor_hits(command: str) -> bool:
    return any(p.search(command) for p in SandboxPolicy().denied_command_patterns())


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -fr ~",
        "sudo rm -rf /",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        "shutdown -h now",
        "sudo reboot",
        "ls; shutdown now",
        "echo hi && sudo poweroff",
        "make build\nreboot",          # the unwrapped form is multi-line
        "cat payload > /dev/sda",
    ],
)
def test_floor_catches_destructive_commands(command):
    assert _floor_hits(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /tmp/build",           # scoped delete is ordinary work
        "rm -rf node_modules",
        "rm file.txt",
        "git status",
        "npm run build",
        "dd if=in.iso of=out.img",
        "ls /dev/",
        "grep -r reboot .",            # merely mentions the word
        "grep -rn 'shutdown' src/",
        "cat docs/shutdown-procedure.md",
        "python manage.py shutdown_worker",
        "./scripts/reboot-helper.sh",
    ],
)
def test_floor_does_not_fire_on_ordinary_work(command):
    """False positives here are worse than the misses they prevent.

    An agent refused for ``grep -r reboot .`` learns to route around the
    check. The rules are anchored to command position for exactly this
    reason.
    """
    assert _floor_hits(command) is False


def test_malformed_deny_config_is_tolerated():
    policy = SandboxPolicy({"execution": {"denied_command_patterns": "not-a-list"}})
    assert policy.denied_command_patterns()  # the floor, at least
    policy = SandboxPolicy({"execution": {"denied_command_patterns": [None, "", 42]}})
    assert policy.denied_command_patterns()
