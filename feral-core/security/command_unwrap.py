"""Recursive shell-command unwrapper: normalise before you pattern-match.

The gap this closes
===================
``security/sandbox_policy.validate_shell_command`` is an allowlist: it
rejects shell metacharacters outright and then requires ``argv[0]`` to
be a named program. That is a stronger posture than a denylist and
nothing here weakens it.

But FERAL grew two paths that accept a *shell string* rather than an
argv vector:

* ``coding_tools__bash`` builds an ``asyncio.create_subprocess_shell``
  call from a model-authored command, gated by a denylist regex and
  ``security.exec_mode.resolve_execution_mode``.
* ``bridges/acp.py`` carries agent-authored commands from an external
  ACP client.

Against a shell string, any check that reads the raw text is a text
match against an attacker-chosen encoding. All of these run ``rm -rf /``
and none of them contain the substring ``rm -rf /``::

    echo cm0gLXJmIC8K | base64 -d | sh
    $'\\x72\\x6d' -rf /
    echo $(echo cm0gLXJmIC8K) | base64 --decode | bash
    sh <<'EOF'
    rm -rf /
    EOF
    bash -c "bash -c 'rm -rf /'"

:func:`scannable_command` rewrites a command into a normalised form that
contains the text a shell would actually execute, so the existing
pattern checks keep their wording and gain sight. It is a
*pre-normalisation* step: callers match against both the raw command and
the scannable form, and a hit on either is a hit. It never turns a
refusal into an execution.

What it does, per recursion level
=================================
1. Strip heredoc bodies that are being *written* to a file, because a
   file's contents are not a command. Keep them when the heredoc feeds a
   shell, because then they are.
2. Decode ANSI-C ``$'...'`` quoting: hex (``\\x41``), octal (``\\101``),
   unicode (``\\u0041``, ``\\U00000041``) and the C escape letters.
3. Unwrap single and double quotes where removing them cannot join two
   unrelated tokens (the "bare word" test), so ``'rm'`` reads as ``rm``
   but ``"some long sentence"`` does not dissolve into loose words.
4. Collect every payload the shell would hand to another shell:
   ``$(...)`` and backtick substitutions, ``sh -c <word>`` / ``eval
   <word>`` arguments, heredocs and herestrings feeding a shell, and the
   *decoded output* of a producer pipeline that ends in a shell
   (``echo <base64> | base64 -d | sh``).
5. Recurse on each payload, to :data:`MAX_DEPTH`.

Deliberate non-goals
====================
This is not a shell. It does not expand variables (``$CMD``), does not
resolve ``$IFS`` tricks into whitespace, and does not evaluate a
producer it cannot decode statically (``curl url | sh`` yields no
payload, only a finding). Anything it cannot resolve stays unresolved
rather than being guessed at, and the load-bearing boundaries remain the
argv allowlist on the daemon path and the workspace containment check in
``security.exec_mode``.

The technique, the recursion bound, and the "bare word" unquoting rule
are adapted from ``scannableCommand`` in ``src/policy/command-policy.ts``
of yc-software/qm, MIT licensed:

    Copyright (c) yc-software. Licensed under the MIT License.

The implementation below is an independent Python one; the pipeline
decoder and the finding model have no counterpart there.
"""

from __future__ import annotations

import base64
import binascii
import re
import shlex
import string
from dataclasses import dataclass
from typing import Final, Iterable, Optional

__all__ = [
    "MAX_DEPTH",
    "Finding",
    "KIND_ANSI_C_QUOTING",
    "KIND_ENCODED_PAYLOAD_TO_SHELL",
    "KIND_HEREDOC_TO_SHELL",
    "KIND_NESTED_SHELL",
    "KIND_OPAQUE_PIPE_TO_SHELL",
    "KIND_SUBSTITUTION",
    "decode_ansi_c",
    "obfuscation_findings",
    "scannable_command",
]

# Recursion ceiling. Eight levels of "a shell running a shell" is far
# past anything legitimate; the bound exists so a self-referential
# payload cannot spin the checker.
MAX_DEPTH: Final[int] = 8

# Longest command this module will normalise. Beyond it the input is
# returned unchanged, because the quadratic-ish scans below are only
# free on realistic commands and a megabyte of text is not one.
MAX_COMMAND_CHARS: Final[int] = 64_000

# Longest decoded payload we will carry forward from a pipeline.
MAX_DECODED_CHARS: Final[int] = 16_000

_SHELL_BASENAMES: Final[frozenset[str]] = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "ash", "csh", "tcsh", "busybox"}
)

# Programs that take a command *string* as an argument rather than a
# path. ``eval`` and ``source`` are builtins, not files, but they read
# the same way here.
_EVAL_BUILTINS: Final[frozenset[str]] = frozenset({"eval", "source", "."})

# A token that "unquotes cleanly": removing its quotes cannot merge it
# with a neighbouring token, because it holds no whitespace or shell
# operator. Taken from qm; it is what keeps quote-stripping from turning
# an English sentence into a pile of would-be commands.
#
# ``~`` is added to qm's set. FERAL's downstream consumer is
# ``exec_mode.command_argument_paths``, whose whole job is to notice
# ``~/.ssh/id_rsa``; dropping the tilde would hand it a path it cannot
# recognise. A tilde starts no word and ends none, so it is as safe to
# unquote as the rest.
_BARE_WORD: Final[re.Pattern[str]] = re.compile(r"^[\w@%+=:,./~-]*$")

_ANSI_C_LETTERS: Final[dict[str, str]] = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}

KIND_ENCODED_PAYLOAD_TO_SHELL: Final[str] = "encoded_payload_to_shell"
KIND_OPAQUE_PIPE_TO_SHELL: Final[str] = "opaque_pipe_to_shell"
KIND_ANSI_C_QUOTING: Final[str] = "ansi_c_quoting"
KIND_SUBSTITUTION: Final[str] = "command_substitution"
KIND_NESTED_SHELL: Final[str] = "nested_shell_invocation"
KIND_HEREDOC_TO_SHELL: Final[str] = "heredoc_to_shell"


@dataclass(frozen=True)
class Finding:
    """One structural observation about a command.

    ``kind`` is one of the ``KIND_*`` constants. ``detail`` is a short
    operator-facing phrase. ``revealed`` is the text the layer decoded
    to, when there is one, so a refusal can quote what it actually saw
    instead of the encoded form the operator would have to decode by
    hand.
    """

    kind: str
    detail: str
    revealed: str = ""
    depth: int = 0


# ──────────────────────────────────────────────────────────────────
# ANSI-C quoting
# ──────────────────────────────────────────────────────────────────


def decode_ansi_c(value: str) -> str:
    """Decode the body of a bash ``$'...'`` string.

    Single left-to-right pass rather than a sequence of independent
    substitutions, so a decoded byte can never be re-read as an escape
    introducer: ``$'\\\\x5cx41'`` decodes to the six characters
    ``\\x41`` and stops, instead of collapsing to ``A``.
    """
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue

        nxt = value[i + 1]

        if nxt == "x":
            digits = _take(value, i + 2, 2, string.hexdigits)
            if digits:
                out.append(chr(int(digits, 16)))
                i += 2 + len(digits)
                continue
        elif nxt in ("u", "U"):
            width = 4 if nxt == "u" else 8
            digits = _take(value, i + 2, width, string.hexdigits)
            if digits:
                code = int(digits, 16)
                out.append(chr(code) if code <= 0x10FFFF else "")
                i += 2 + len(digits)
                continue
        elif nxt in "01234567":
            digits = _take(value, i + 1, 3, "01234567")
            out.append(chr(int(digits, 8) & 0xFF))
            i += 1 + len(digits)
            continue

        out.append(_ANSI_C_LETTERS.get(nxt, nxt))
        i += 2
    return "".join(out)


def _take(value: str, start: int, limit: int, alphabet: str) -> str:
    end = start
    stop = min(len(value), start + limit)
    while end < stop and value[end] in alphabet:
        end += 1
    return value[start:end]


# ──────────────────────────────────────────────────────────────────
# Quote-aware scanning primitives
# ──────────────────────────────────────────────────────────────────


def _split_unquoted(text: str, separators: str) -> list[str]:
    """Split ``text`` on ``separators`` that are outside quotes.

    Backslash escapes and both quote styles are honoured, so
    ``echo "a|b" | sh`` splits into two segments, not three.
    """
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(text[i + 1])
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in separators:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _command_substitutions(text: str) -> list[str]:
    """Bodies of ``$( ... )`` and `` `...` `` that a shell would execute.

    Nesting is tracked so ``$(echo $(id))`` yields the whole outer body,
    which the recursion then re-scans and finds the inner one in. A
    substitution inside single quotes is *not* executed by the shell and
    is therefore not returned.
    """
    found: list[str] = []
    quote = ""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = ""
            elif quote == '"' and ch == "$" and i + 1 < n and text[i + 1] == "(":
                body, end = _balanced_paren(text, i + 1)
                if body is not None:
                    found.append(body)
                    i = end
                    continue
            elif quote == '"' and ch == "`":
                end = text.find("`", i + 1)
                if end != -1:
                    found.append(text[i + 1 : end])
                    i = end + 1
                    continue
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "$" and i + 1 < n and text[i + 1] == "(":
            body, end = _balanced_paren(text, i + 1)
            if body is not None:
                found.append(body)
                i = end
                continue
        if ch == "`":
            end = text.find("`", i + 1)
            if end != -1:
                found.append(text[i + 1 : end])
                i = end + 1
                continue
        i += 1
    return found


def _balanced_paren(text: str, open_index: int) -> tuple[Optional[str], int]:
    """Body and end offset of the parenthesised run starting at ``open_index``."""
    depth = 0
    quote = ""
    i = open_index
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : i], i + 1
        i += 1
    return None, n


def _argv(segment: str) -> list[str]:
    """Best-effort argv for one pipeline segment.

    ``shlex`` is right about quoting; when the segment has unbalanced
    quotes (common in a half-built obfuscation) it raises, and a plain
    whitespace split is a better answer than giving up, because the
    caller only needs ``argv[0]`` and flag shapes.
    """
    try:
        return shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return segment.split()


def _program(argv: Iterable[str]) -> tuple[str, list[str]]:
    """Basename of the program a segment runs, plus its remaining argv.

    Skips ``env`` and leading ``VAR=value`` assignments so
    ``env -i PATH=/bin bash -c '...'`` is still recognised as bash.
    """
    rest = list(argv)
    while rest:
        head = rest[0]
        if "=" in head and not head.startswith("=") and "/" not in head.split("=", 1)[0]:
            rest = rest[1:]
            continue
        base = head.rsplit("/", 1)[-1].lower()
        if base == "env":
            rest = rest[1:]
            while rest and rest[0].startswith("-"):
                rest = rest[1:]
            continue
        return base, rest[1:]
    return "", []


def _command_flag_value(args: list[str]) -> Optional[str]:
    """The argument of a ``-c``-style flag, if the segment has one.

    Accepts bundled short flags (``-lc``, ``-xc``) the way a shell does.
    """
    for index, token in enumerate(args):
        if not token.startswith("-") or token.startswith("--"):
            continue
        if token.endswith("c") and len(token) >= 2 and token[1:].isalpha():
            if index + 1 < len(args):
                return args[index + 1]
            return None
    return None


def _reads_stdin_as_script(program: str, args: list[str]) -> bool:
    """``True`` when this segment is a shell taking its script from stdin.

    ``sh`` and ``bash -s`` read stdin. ``bash -c 'x'`` does not, so a
    pipe into it is data, not code, and must not be reported as a
    pipe-to-shell.
    """
    if program not in _SHELL_BASENAMES:
        return False
    return _command_flag_value(args) is None


# ──────────────────────────────────────────────────────────────────
# Heredocs
# ──────────────────────────────────────────────────────────────────

_HEREDOC = re.compile(
    r"^(?P<pre>[^\n]*?)<<-?\s*(?P<q>[\"']?)(?P<delim>[A-Za-z_]\w*)(?P=q)"
    r"(?P<post>[^\n]*)\n(?P<body>.*?)^[\t ]*(?P=delim)[\t ]*$",
    re.MULTILINE | re.DOTALL,
)


def _heredoc_runs_shell(header: str) -> bool:
    """Does this heredoc header line feed its body to a shell?

    ``sh <<EOF`` and ``ssh host bash <<EOF`` do. ``cat <<EOF > f.txt``
    does not, and neither does ``bash -c 'x' <<EOF``, where the body is
    stdin data for an already-supplied script.
    """
    for segment in _split_unquoted(header, "|;&\n"):
        program, args = _program(_argv(segment))
        if _reads_stdin_as_script(program, args):
            return True
    return False


def _strip_written_heredocs(command: str) -> str:
    """Drop heredoc bodies that are only being written to a file.

    ``cat <<'EOF' > notes.txt`` followed by prose is a file, not a
    program, and scanning it produces nothing but false positives. The
    body is kept whenever the header line has no redirect, or when the
    header runs a shell, because then the body *is* the program.

    The header line itself always survives, unlike the qm original which
    drops the whole construct: ``cat <<'EOF' > ~/.ssh/authorized_keys``
    is a command worth scanning even though its body is not.
    """

    def replace(match: re.Match[str]) -> str:
        header = match.group("pre") + match.group("post")
        if ">" in header and not _heredoc_runs_shell(header):
            return header
        return match.group(0)

    return _HEREDOC.sub(replace, command)


def _heredoc_shell_payloads(command: str) -> list[str]:
    """Heredoc bodies that are handed to a shell."""
    payloads: list[str] = []
    for match in _HEREDOC.finditer(command):
        header = match.group("pre") + match.group("post")
        if _heredoc_runs_shell(header):
            body = match.group("body")
            if body.strip():
                payloads.append(body)
    return payloads


def _herestring_payloads(command: str) -> list[str]:
    """Bodies of ``<<<`` herestrings feeding a shell."""
    payloads: list[str] = []
    for segment in _split_unquoted(command, ";\n"):
        if "<<<" not in segment:
            continue
        head, _, tail = segment.partition("<<<")
        program, args = _program(_argv(head))
        if not _reads_stdin_as_script(program, args):
            continue
        tokens = _argv(tail)
        if tokens:
            payloads.append(tokens[0])
    return payloads


# ──────────────────────────────────────────────────────────────────
# Producer pipelines that end in a shell
# ──────────────────────────────────────────────────────────────────


def _literal_stdout(segment: str) -> Optional[str]:
    """Statically known stdout of a producer segment, or ``None``.

    Only ``echo`` and ``printf`` with literal arguments qualify. A
    ``curl`` or ``cat`` produces bytes we cannot know here, which is the
    honest answer and keeps the decoder from inventing a payload.
    """
    argv = _argv(segment)
    program, args = _program(argv)
    if program not in ("echo", "printf"):
        return None
    payload = [a for a in args if not (a.startswith("-") and set(a[1:]) <= set("neE"))]
    if not payload:
        return ""
    if program == "printf":
        # The format string is the payload for the shapes that matter
        # here (``printf '%s' <data>`` and ``printf <data>``).
        if len(payload) > 1 and "%" in payload[0]:
            return " ".join(payload[1:])
        return payload[0]
    return " ".join(payload)


def _apply_decoder(segment: str, data: str) -> Optional[str]:
    """Run one filter segment over ``data``, or ``None`` if opaque.

    Covers the decoders that actually show up in a pipe-to-shell
    payload. An unrecognised filter returns ``None`` rather than
    passing the data through unchanged, because a wrong guess here would
    make the scannable form claim something the shell will not run.
    """
    argv = _argv(segment)
    program, args = _program(argv)
    flags = " ".join(a for a in args if a.startswith("-"))

    if program == "base64":
        if "d" in flags.replace("--decode", "d").replace("-D", "-d"):
            return _b64(data)
        return None
    if program == "openssl":
        joined = " ".join(args)
        if ("base64" in joined or "enc" in joined) and ("-d" in args or "-decode" in joined):
            return _b64(data)
        return None
    if program == "xxd":
        if "-r" in args or "r" in flags:
            return _hex(data)
        return None
    if program in ("cat", "tee"):
        return data
    if program == "rev":
        return data[::-1]
    if program == "tr":
        if "-d" in args:
            index = args.index("-d")
            if index + 1 < len(args):
                return data.translate({ord(c): None for c in args[index + 1]})
        return None
    return None


def _b64(data: str) -> Optional[str]:
    text = "".join(data.split())
    if not text:
        return None
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded, validate=True).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return None


def _hex(data: str) -> Optional[str]:
    text = "".join(data.split())
    if not text or len(text) % 2 or any(c not in string.hexdigits for c in text):
        return None
    try:
        return bytes.fromhex(text).decode("utf-8", errors="replace")
    except ValueError:
        return None


def _pipeline_shell_payloads(command: str) -> tuple[list[str], list[Finding]]:
    """Decoded payloads for pipelines whose last consumer is a shell.

    ``echo cm0gLXJmIC8K | base64 -d | sh`` returns ``rm -rf /`` and an
    :data:`KIND_ENCODED_PAYLOAD_TO_SHELL` finding.  ``curl x | sh``
    returns no payload and an :data:`KIND_OPAQUE_PIPE_TO_SHELL` finding,
    because the shape is worth reporting even when the bytes are not
    knowable.
    """
    payloads: list[str] = []
    findings: list[Finding] = []

    for statement in _split_unquoted(command, ";\n"):
        segments = [s.strip() for s in _split_unquoted(statement, "|")]
        segments = [s for s in segments if s]
        if len(segments) < 2:
            continue
        for index, segment in enumerate(segments):
            if index == 0:
                continue
            program, args = _program(_argv(segment))
            if not _reads_stdin_as_script(program, args):
                continue

            data = _literal_stdout(segments[0])
            decoded_by: list[str] = []
            if data is not None:
                for filter_segment in segments[1:index]:
                    before = data
                    data = _apply_decoder(filter_segment, before)
                    if data is None:
                        break
                    if data != before:
                        decoded_by.append(_program(_argv(filter_segment))[0])

            if data is None:
                findings.append(
                    Finding(
                        kind=KIND_OPAQUE_PIPE_TO_SHELL,
                        detail=(
                            f"output of '{segments[0][:60]}' is piped into "
                            f"'{program}' and cannot be resolved statically"
                        ),
                    )
                )
                continue

            data = data[:MAX_DECODED_CHARS]
            if data.strip():
                payloads.append(data)
            if decoded_by:
                findings.append(
                    Finding(
                        kind=KIND_ENCODED_PAYLOAD_TO_SHELL,
                        detail=(
                            f"a {'/'.join(decoded_by)}-encoded payload is decoded "
                            f"and piped into '{program}'"
                        ),
                        revealed=data.strip()[:200],
                    )
                )
            break

    return payloads, findings


# ──────────────────────────────────────────────────────────────────
# Explicit ``-c`` / ``eval`` payloads
# ──────────────────────────────────────────────────────────────────


def _nested_shell_payloads(command: str) -> tuple[list[str], list[Finding]]:
    """Command strings handed to ``sh -c`` / ``bash -c`` / ``eval``.

    ``shlex`` removes exactly one quoting layer, which is what makes
    ``bash -c "bash -c 'rm -rf /'"`` peel correctly: the outer call
    yields ``bash -c 'rm -rf /'``, and the recursion yields ``rm -rf /``.
    """
    payloads: list[str] = []
    findings: list[Finding] = []

    for segment in _split_unquoted(command, ";|\n"):
        segment = segment.strip()
        if not segment:
            continue
        program, args = _program(_argv(segment))
        payload: Optional[str] = None
        if program in _SHELL_BASENAMES:
            payload = _command_flag_value(args)
        elif program in _EVAL_BUILTINS:
            positional = [a for a in args if not a.startswith("-")]
            payload = " ".join(positional) if positional else None
        if payload and payload.strip():
            payloads.append(payload)
            findings.append(
                Finding(
                    kind=KIND_NESTED_SHELL,
                    detail=f"'{program}' is handed a command string to interpret",
                    revealed=payload.strip()[:200],
                )
            )
    return payloads, findings


# ──────────────────────────────────────────────────────────────────
# Normalisation
# ──────────────────────────────────────────────────────────────────

_DOUBLE_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')
_ANSI_C_QUOTED = re.compile(r"\$'((?:[^'\\]|\\.)*)'")
_SINGLE_QUOTED = re.compile(r"'[^']*'")
_ESCAPED_BARE = re.compile(r"\\([\w@%+=:,./-])")
_INNER_SUBSTITUTION = re.compile(r"\$\([^)]*\)|`[^`]*`")


def _unquote_bare(inner: str) -> Optional[str]:
    return inner if _BARE_WORD.match(inner) else None


def _normalise(text: str) -> str:
    """Remove one quoting layer where it is safe to do so.

    A double-quoted run that contains a substitution keeps only the
    substitutions, because that is the part the shell executes. A quoted
    run that unquotes to a bare word becomes that word. Anything else
    collapses to an empty quote pair, which is what stops prose in an
    argument from being read as a command line.
    """

    def double(match: re.Match[str]) -> str:
        body = match.group(0)
        subs = _INNER_SUBSTITUTION.findall(body)
        if subs:
            return " ".join(subs)
        return _unquote_bare(body[1:-1]) or '""'

    def ansi_c(match: re.Match[str]) -> str:
        return _unquote_bare(decode_ansi_c(match.group(1))) or "''"

    def single(match: re.Match[str]) -> str:
        return _unquote_bare(match.group(0)[1:-1]) or "''"

    text = _DOUBLE_QUOTED.sub(double, text)
    text = _ANSI_C_QUOTED.sub(ansi_c, text)
    text = _SINGLE_QUOTED.sub(single, text)
    return _ESCAPED_BARE.sub(r"\1", text)


def _executed_payloads(command: str) -> tuple[list[str], list[Finding]]:
    payloads: list[str] = []
    findings: list[Finding] = []

    for body in _command_substitutions(command):
        if body.strip():
            payloads.append(body)
            findings.append(
                Finding(
                    kind=KIND_SUBSTITUTION,
                    detail="a command substitution runs a nested command",
                    revealed=body.strip()[:200],
                )
            )

    nested, nested_findings = _nested_shell_payloads(command)
    payloads.extend(nested)
    findings.extend(nested_findings)

    piped, pipe_findings = _pipeline_shell_payloads(command)
    payloads.extend(piped)
    findings.extend(pipe_findings)

    for body in _heredoc_shell_payloads(command):
        payloads.append(body)
        findings.append(
            Finding(
                kind=KIND_HEREDOC_TO_SHELL,
                detail="a heredoc body is fed to a shell as a script",
                revealed=body.strip()[:200],
            )
        )

    for body in _herestring_payloads(command):
        if body.strip():
            payloads.append(body)
            findings.append(
                Finding(
                    kind=KIND_HEREDOC_TO_SHELL,
                    detail="a herestring is fed to a shell as a script",
                    revealed=body.strip()[:200],
                )
            )

    return payloads, findings


def scannable_command(command: str, *, max_depth: int = MAX_DEPTH) -> str:
    """Normalised form of ``command``, for pattern matching only.

    The result is the original command with one quoting layer removed,
    followed by one line per payload the command would hand to another
    shell, recursively. It is NOT a rewritten command and must never be
    executed: quotes have been dropped and substitutions inlined, so its
    meaning as a shell program is undefined.

    Callers should match their patterns against the raw command *and*
    this form, and treat a hit on either as a hit.
    """
    if not isinstance(command, str) or not command.strip():
        return ""
    if len(command) > MAX_COMMAND_CHARS:
        return command
    findings: list[Finding] = []
    seen: set[str] = {command.strip()}
    lines = _walk_bounded(command, max_depth, findings, seen)
    return "\n".join(line for line in lines if line.strip())


def _walk_bounded(command: str, max_depth: int, findings: list[Finding], seen: set[str]) -> list[str]:
    limit = max(0, min(int(max_depth), MAX_DEPTH))

    def walk(text: str, depth: int) -> list[str]:
        stripped = _strip_written_heredocs(text)
        base = _normalise(stripped)
        out = [base]

        if _ANSI_C_QUOTED.search(stripped):
            findings.append(
                Finding(
                    kind=KIND_ANSI_C_QUOTING,
                    detail="ANSI-C quoting hides literal bytes from a text match",
                    revealed=base.strip()[:200],
                    depth=depth,
                )
            )

        if depth >= limit:
            return out

        payloads, layer_findings = _executed_payloads(stripped)
        for finding in layer_findings:
            findings.append(
                Finding(
                    kind=finding.kind,
                    detail=finding.detail,
                    revealed=finding.revealed,
                    depth=depth,
                )
            )
        for payload in payloads:
            key = payload.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.extend(walk(payload, depth + 1))
        return out

    return walk(command, 0)


def obfuscation_findings(command: str, *, max_depth: int = MAX_DEPTH) -> list[Finding]:
    """Structural observations about ``command``, deduplicated.

    Empty for a plain command. A non-empty list does not by itself mean
    "malicious": ``$(git rev-parse HEAD)`` produces a substitution
    finding and is perfectly ordinary. Callers decide which kinds are
    disqualifying; ``security.exec_mode`` refuses only on
    :data:`KIND_ENCODED_PAYLOAD_TO_SHELL`.
    """
    if not isinstance(command, str) or not command.strip():
        return []
    if len(command) > MAX_COMMAND_CHARS:
        return []
    findings: list[Finding] = []
    seen: set[str] = {command.strip()}
    _walk_bounded(command, max_depth, findings, seen)

    unique: list[Finding] = []
    keys: set[tuple[str, str]] = set()
    for finding in findings:
        key = (finding.kind, finding.revealed)
        if key in keys:
            continue
        keys.add(key)
        unique.append(finding)
    return unique
