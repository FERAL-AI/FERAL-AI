"""Fast post-write checks folded into the tool result.

After ``coding_tools`` writes or edits a file, run whatever cheap checker
that file's extension has and hand the model back only the findings its
write introduced. The point is to close the loop inside the same turn:
without it, the model writes a file with a syntax error, moves on, and
discovers the problem three tool calls later when a test run fails, if at
all.

Baseline diffing is not an enhancement
--------------------------------------
It is the feature. Reporting every finding in the file means that editing
one line of a legacy module dumps a few hundred pre-existing warnings
into the context, and the model, which has no way to know they are not
its fault, starts "fixing" them. So each checker runs twice: once against
the pre-write content and once against the file on disk, and only
findings that are new relative to the baseline are reported.
``_edit_file`` already holds the original string, and step 4's pre-write
blob covers ``write_file``.

No temporary files, ever
------------------------
Both the baseline and the after check feed their content to the checker
on **stdin**. Nothing is written to disk. An earlier version wrote the
pre-write content to a hidden sibling file so that checkers which resolve
configuration by walking up from the file path would resolve the same
config; that was correct but unacceptable, because a stray
``.feral-baseline-*`` survives a SIGKILL (a ``finally`` block does not),
shows up in ``git status``, and trips file watchers and test runners. It
ran on every single edit.

The replacement, per checker:

* **ruff** takes ``--stdin-filename``, which makes it resolve
  ``pyproject.toml`` / ``ruff.toml`` by walking up from *that* path while
  reading the source from stdin. Verified: with a ``ruff.toml`` selecting
  only ``E501``, piping ``import os`` with ``--stdin-filename`` pointing
  inside that directory suppresses ``F401``, and it still fires when the
  path is elsewhere. Config resolution is fully preserved, cwd is
  irrelevant, and no file is created.
* **bash -n** has no configuration to resolve at all. Verified: identical
  exit code and message from a file and from stdin, unaffected by cwd or
  ``BASH_ENV``.
* **node --check** reads stdin but has no ``--stdin-filename``
  equivalent, so it cannot be told where the source came from. See
  :func:`_run_node_check` for what that costs and why it is the right
  trade anyway.

Because before and after are always evaluated through the identical
channel, the diff between them is sound even where that channel sees less
context than the real file would.

Absent output means absent tooling
----------------------------------
When no checker ran, the caller omits the ``diagnostics`` block
completely. An empty ``findings`` list would read to the model as
"checked, and clean", which is a much stronger claim than "there was
nothing installed to check with". No tooling is the common case.

TypeScript is deliberately skipped
----------------------------------
``tsc --noEmit`` on a single file without the project's ``tsconfig.json``
reports a flood of phantom errors (every import unresolved, every ambient
type missing); with the project tsconfig it type-checks the whole program
and blows any timeout worth having on a post-write hook. Half-checking
TypeScript is worse than not checking it, so ``.ts`` / ``.tsx`` return
nothing.

Everything here is advisory. The write already happened by the time a
checker runs, so any exception, timeout or missing binary means the
diagnostics block is dropped, never that the tool call fails.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Optional

logger = logging.getLogger("feral.skills.diagnostics")

__all__ = ["diagnose", "SKIPPED_SUFFIXES"]

#: Extensions we explicitly refuse to check. See the module docstring.
SKIPPED_SUFFIXES = frozenset({".ts", ".tsx", ".mts", ".cts"})

MAX_ITEMS = 10
MAX_MESSAGE_CHARS = 200
# Files this big make even a fast linter a noticeable pause on a write.
MAX_FILE_BYTES = 2_000_000


def _timeout() -> float:
    try:
        return max(0.5, float(os.environ.get("FERAL_DIAGNOSTICS_TIMEOUT", "5")))
    except ValueError:
        return 5.0


def _enabled() -> bool:
    return os.environ.get("FERAL_POST_EDIT_DIAGNOSTICS", "on").strip().lower() not in (
        "off", "0", "false", "no",
    )


async def diagnose(
    path,
    *,
    before_text: Optional[str],
    after_text: Optional[str] = None,
) -> Optional[dict]:
    """Check ``path`` and return the findings the write introduced.

    ``after_text`` is the content the caller just wrote. Passing it skips
    reading the file back, which is what lets the whole of this run
    *outside* the per-path write lock: no file access means no race with
    another writer, and a linter subprocess with a five second timeout
    cannot keep another subagent waiting. Omit it and the file is read in
    a worker thread instead.

    Returns ``None`` when nothing ran: unknown extension, checker not
    installed, file too large, timeout, or any error at all. The caller
    must omit the diagnostics key entirely in that case.
    """
    if not _enabled():
        return None
    try:
        return await _diagnose_inner(Path(path), before_text, after_text)
    except Exception as exc:  # noqa: BLE001 - advisory only, never fatal
        logger.debug("post-write diagnostics failed for %s: %s", path, exc)
        return None


async def _diagnose_inner(
    target: Path, before_text: Optional[str], after_text: Optional[str],
) -> Optional[dict]:
    suffix = target.suffix.lower()
    if suffix in SKIPPED_SUFFIXES:
        return None

    if after_text is None:
        # Blocking stat + read, so it goes to a thread like every other
        # file touch in this layer.
        def _stat_and_read() -> Optional[str]:
            if not target.is_file():
                return None
            if target.stat().st_size > MAX_FILE_BYTES:
                return None
            return target.read_text(errors="replace")

        try:
            after_text = await asyncio.to_thread(_stat_and_read)
        except OSError:
            return None
        if after_text is None:
            return None
    elif len(after_text) > MAX_FILE_BYTES:
        return None

    checker = _pick_checker(suffix)
    if checker is None:
        return None
    name, runner = checker

    # Every checker is content-in / findings-out. The path is passed only
    # so config-resolving checkers can be told where the content came
    # from; nothing is ever written to it.
    after = await runner(after_text, target)
    if after is None:
        return None
    before = await runner(before_text, target) if before_text is not None else []

    findings = _new_findings(before or [], after)
    total = len(findings)
    findings.sort(key=lambda f: (0 if f["severity"] == "error" else 1, f["line"]))
    shown = findings[:MAX_ITEMS]
    out = {
        "checker": name,
        "findings": shown,
        "new_count": total,
    }
    if total > len(shown):
        out["truncated"] = True
    if before_text is None:
        # Say so: without a baseline these are all findings in the file,
        # not necessarily ones this write introduced.
        out["baseline"] = "unavailable"
    return out


# ── checker selection ─────────────────────────────────────────────────


def _pick_checker(suffix: str):
    """Return ``(display_name, runner)`` or ``None``.

    Every runner has the same shape, ``(content, path) -> findings | None``,
    so the caller can drive baseline and after identically.
    """
    if suffix == ".py":
        if shutil.which("ruff"):
            return "ruff", _run_ruff
        # ruff is not a declared dependency of feral-core, so the common
        # case is that it is simply absent. `ast.parse` costs nothing and
        # still catches the failure that actually matters after a bad
        # edit: the file no longer parses.
        return "python-ast", _check_python_ast
    if suffix in (".js", ".mjs", ".cjs", ".jsx"):
        if shutil.which("node"):
            return "node --check", _run_node_check
        return None
    if suffix == ".json":
        return "json", _check_json
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # noqa: F401
        except ImportError:
            return None
        return "yaml", _check_yaml
    if suffix in (".sh", ".bash"):
        if shutil.which("bash"):
            return "bash -n", _run_bash_n
        return None
    return None


# ── in-process checkers ───────────────────────────────────────────────


# These parse the whole file, which is CPU-bound work with no await in
# it. `ast.parse` and `yaml.safe_load` on a large file are tens of
# milliseconds of straight-line C and Python, so they go to a thread like
# every other blocking call here rather than stalling the loop. The
# subprocess checkers need no such treatment: they are already async all
# the way down.


async def _check_python_ast(text: str, _path: Path) -> list[dict]:
    return await asyncio.to_thread(_check_python_ast_sync, text)


def _check_python_ast_sync(text: str) -> list[dict]:
    try:
        ast.parse(text)
    except SyntaxError as exc:
        return [_finding("error", "SyntaxError", exc.lineno or 1, exc.msg or str(exc))]
    return []


async def _check_json(text: str, _path: Path) -> list[dict]:
    return await asyncio.to_thread(_check_json_sync, text)


def _check_json_sync(text: str) -> list[dict]:
    try:
        json.loads(text)
    except ValueError as exc:
        line = getattr(exc, "lineno", 1) or 1
        return [_finding("error", "JSONDecodeError", line, str(exc))]
    return []


async def _check_yaml(text: str, _path: Path) -> list[dict]:
    return await asyncio.to_thread(_check_yaml_sync, text)


def _check_yaml_sync(text: str) -> list[dict]:
    import yaml

    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = (mark.line + 1) if mark is not None else 1
        return [_finding("error", "YAMLError", line, str(exc))]
    return []


# ── subprocess checkers ───────────────────────────────────────────────


async def _run(
    argv: list[str], cwd: Path, stdin_text: str,
) -> Optional[tuple[int, str, str]]:
    """Run a checker with its input on stdin. Nothing touches the disk."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )
    except (OSError, ValueError):
        return None
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin_text.encode()), timeout=_timeout(),
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None
    except (BrokenPipeError, ConnectionResetError):
        return None
    return (
        proc.returncode or 0,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


def _cwd_for(target: Path) -> Path:
    """A real directory to run in. The file's own directory when it
    exists, so nothing resolves against FERAL's checkout by accident."""
    parent = target.parent
    return parent if parent.is_dir() else Path.cwd()


async def _run_ruff(content: str, target: Path) -> Optional[list[dict]]:
    """Lint ``content`` as if it were the file at ``target``.

    ``--stdin-filename`` is what makes this work: ruff resolves
    ``pyproject.toml`` / ``ruff.toml`` by walking up from that path, so
    the model is told about the *project's* rules rather than FERAL's,
    while the source itself never reaches the filesystem. Confirmed
    against ruff 0.15: a ``ruff.toml`` selecting only ``E501`` suppresses
    ``F401`` for a stdin-filename inside its directory and not for one
    outside it, regardless of cwd.
    """
    res = await _run(
        [
            "ruff", "check", "--output-format", "json", "--quiet",
            # Belt and braces on "creates nothing". Reading from stdin
            # already gives ruff nothing to cache, but the file-based
            # version this replaced dropped a `.ruff_cache/` directory
            # into whatever directory it ran in, which is the user's
            # source tree. `--no-cache` makes that impossible to
            # reintroduce by a future ruff behaviour change.
            "--no-cache",
            "--stdin-filename", str(target), "-",
        ],
        _cwd_for(target),
        content,
    )
    if res is None:
        return None
    _code, stdout, _stderr = res
    try:
        rows = json.loads(stdout or "[]")
    except ValueError:
        return None
    findings = []
    for row in rows if isinstance(rows, list) else []:
        code = row.get("code") or "invalid-syntax"
        location = row.get("location") or {}
        findings.append(_finding(
            _ruff_severity(code),
            code,
            int(location.get("row") or 1),
            str(row.get("message") or ""),
        ))
    return findings


_SYNTAX_CODES = frozenset({"invalid-syntax", "syntax", "E999"})


def _ruff_severity(code: str) -> str:
    """Only unparseable code and undefined names are "error" here.

    Ruff's own JSON marks every diagnostic ``severity: error``, which is
    useless for ranking. What the model needs first is the class of
    finding that means the file is broken, not the class that means a
    style rule fired, so the split is made on the rule code.
    """
    if code in _SYNTAX_CODES or code.startswith(("E9", "F8", "F6")):
        return "error"
    return "warning"


async def _run_node_check(content: str, target: Path) -> Optional[list[dict]]:
    """Syntax-check ``content`` with ``node --check`` over stdin.

    Known and accepted limitation: ``node --check`` reads stdin but has
    no ``--stdin-filename``, so it cannot resolve the nearest
    ``package.json``. That matters, because module type is
    location-dependent: the same ``import`` statement passes inside a
    ``"type": "module"`` package and fails with "Cannot use import
    statement outside a module" inside a ``"type": "commonjs"`` one
    (verified on node 25).

    So this misses that one class of error. It is the right trade:

    * The alternative that preserves module type is a temp file beside
      the real one, which is exactly what we are removing.
    * Evaluating baseline and after through the same stdin channel keeps
      the *diff* sound. We can under-report, never over-report, and a
      false "you broke this" is far more expensive than a miss, because
      it makes the model edit code it did not touch.
    * A file placed in the system temp directory would be worse than
      either: it silently changes the module type, so a pre-existing
      ESM-in-CJS error would come back as newly introduced.
    * Ordinary syntax errors, the reason this checker exists, are caught
      identically over stdin.
    """
    res = await _run(["node", "--check"], _cwd_for(target), content)
    if res is None:
        return None
    code, _stdout, stderr = res
    if code == 0:
        return []
    return [_finding("error", "SyntaxError", _line_from(stderr), _stable_message(stderr))]


async def _run_bash_n(content: str, target: Path) -> Optional[list[dict]]:
    """Syntax-check ``content`` with ``bash -n`` over stdin.

    Lossless: ``bash -n`` resolves no configuration from the script's
    location. Verified that a file and the same bytes on stdin produce
    the same exit code and message, and that neither cwd nor ``BASH_ENV``
    changes the verdict.
    """
    res = await _run(["bash", "-n"], _cwd_for(target), content)
    if res is None:
        return None
    code, _stdout, stderr = res
    if code == 0:
        return []
    return [_finding("error", "SyntaxError", _line_from(stderr), _stable_message(stderr))]


# ── shared ────────────────────────────────────────────────────────────


#: Interpreter chatter that varies between two runs of the *same* input.
#: `(node:97599)` is a process id, and leaving it in the message means
#: the baseline key never matches the after key, so an untouched
#: pre-existing error gets reported as newly introduced on every edit.
_PID_NOISE = re.compile(r"\(node:\d+\)\s*")
_ERROR_LINE = re.compile(r"^[A-Za-z_$][\w$.]*(?:Error|Exception)\b.*$", re.M)


def _stable_message(stderr: str) -> str:
    """Reduce interpreter stderr to the part that depends only on the
    source, so before and after are comparable.

    Prefers the actual ``SomethingError: ...`` line, which is stable
    across runs, and falls back to the whole stream with per-process
    noise stripped.
    """
    matches = [m.group(0).strip() for m in _ERROR_LINE.finditer(stderr)]
    if matches:
        return matches[-1]
    return _PID_NOISE.sub("", stderr).strip()


_LINE_WORD = re.compile(r"\bline\s+(\d+)")


def _line_from(stderr: str) -> int:
    """Best-effort line number out of an interpreter's error message.

    Handles both shapes we actually see: node's ``[stdin]:2`` and bash's
    ``bash: line 2: syntax error``.
    """
    flat = stderr.replace("\n", " ")
    word = _LINE_WORD.search(flat)
    if word:
        return int(word.group(1))
    for token in flat.split():
        parts = token.rstrip(":").rsplit(":", 1)
        if len(parts) == 2 and parts[1].isdigit():
            return int(parts[1])
    return 1


def _finding(severity: str, code: str, line: int, message: str) -> dict:
    return {
        "severity": severity,
        "code": str(code)[:32],
        "line": int(line),
        "message": " ".join(message.split())[:MAX_MESSAGE_CHARS],
    }


def _new_findings(before: list[dict], after: list[dict]) -> list[dict]:
    """Findings present in ``after`` beyond what ``before`` already had.

    Keyed on (code, message) and not on line number: an edit shifts every
    line below it, so a line-sensitive diff would report the whole tail of
    the file as new.
    """
    baseline = Counter((f["code"], f["message"]) for f in before)
    out: list[dict] = []
    for finding in after:
        key = (finding["code"], finding["message"])
        if baseline[key] > 0:
            baseline[key] -= 1
            continue
        out.append(finding)
    return out
