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
import shutil
from collections import Counter
from pathlib import Path
from typing import Optional
from uuid import uuid4

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


async def diagnose(path, *, before_text: Optional[str]) -> Optional[dict]:
    """Check ``path`` and return the findings the write introduced.

    Returns ``None`` when nothing ran: unknown extension, checker not
    installed, file too large, timeout, or any error at all. The caller
    must omit the diagnostics key entirely in that case.
    """
    if not _enabled():
        return None
    try:
        return await _diagnose_inner(Path(path), before_text)
    except Exception as exc:  # noqa: BLE001 - advisory only, never fatal
        logger.debug("post-write diagnostics failed for %s: %s", path, exc)
        return None


async def _diagnose_inner(target: Path, before_text: Optional[str]) -> Optional[dict]:
    suffix = target.suffix.lower()
    if suffix in SKIPPED_SUFFIXES or not target.is_file():
        return None
    try:
        if target.stat().st_size > MAX_FILE_BYTES:
            return None
    except OSError:
        return None

    checker = _pick_checker(suffix)
    if checker is None:
        return None
    name, runner, needs_subprocess = checker

    after_text = target.read_text(errors="replace")
    if needs_subprocess:
        after = await runner(target)
        before = (
            await _run_on_snapshot(runner, target, before_text)
            if before_text is not None else []
        )
    else:
        after = runner(after_text)
        before = runner(before_text) if before_text is not None else []
    if after is None:
        return None

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
    if suffix == ".py":
        if shutil.which("ruff"):
            return "ruff", _run_ruff, True
        # ruff is not a declared dependency of feral-core, so the common
        # case is that it is simply absent. `ast.parse` costs nothing and
        # still catches the failure that actually matters after a bad
        # edit: the file no longer parses.
        return "python-ast", _check_python_ast, False
    if suffix in (".js", ".mjs", ".cjs", ".jsx"):
        if shutil.which("node"):
            return "node --check", _run_node_check, True
        return None
    if suffix == ".json":
        return "json", _check_json, False
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # noqa: F401
        except ImportError:
            return None
        return "yaml", _check_yaml, False
    if suffix in (".sh", ".bash"):
        if shutil.which("bash"):
            return "bash -n", _run_bash_n, True
        return None
    return None


# ── in-process checkers ───────────────────────────────────────────────


def _check_python_ast(text: str) -> list[dict]:
    try:
        ast.parse(text)
    except SyntaxError as exc:
        return [_finding("error", "SyntaxError", exc.lineno or 1, exc.msg or str(exc))]
    return []


def _check_json(text: str) -> list[dict]:
    try:
        json.loads(text)
    except ValueError as exc:
        line = getattr(exc, "lineno", 1) or 1
        return [_finding("error", "JSONDecodeError", line, str(exc))]
    return []


def _check_yaml(text: str) -> list[dict]:
    import yaml

    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = (mark.line + 1) if mark is not None else 1
        return [_finding("error", "YAMLError", line, str(exc))]
    return []


# ── subprocess checkers ───────────────────────────────────────────────


async def _run(argv: list[str], cwd: Path) -> Optional[tuple[int, str, str]]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )
    except (OSError, ValueError):
        return None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_timeout())
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None
    return (
        proc.returncode or 0,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


async def _run_ruff(target: Path) -> Optional[list[dict]]:
    # cwd is the file's own directory so ruff discovers the *project's*
    # pyproject.toml / ruff.toml rather than FERAL's. A model editing a
    # user's repo should be told about that repo's rules, not ours.
    res = await _run(
        ["ruff", "check", "--output-format", "json", "--quiet", target.name],
        target.parent,
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


async def _run_node_check(target: Path) -> Optional[list[dict]]:
    res = await _run(["node", "--check", target.name], target.parent)
    if res is None:
        return None
    code, _stdout, stderr = res
    if code == 0:
        return []
    return [_finding("error", "SyntaxError", _line_from(stderr), stderr.strip())]


async def _run_bash_n(target: Path) -> Optional[list[dict]]:
    res = await _run(["bash", "-n", target.name], target.parent)
    if res is None:
        return None
    code, _stdout, stderr = res
    if code == 0:
        return []
    return [_finding("error", "SyntaxError", _line_from(stderr), stderr.strip())]


async def _run_on_snapshot(runner, target: Path, before_text: str) -> list[dict]:
    """Run a subprocess checker against the pre-write content.

    The snapshot lands next to the real file rather than in the system
    temp directory, because ruff and node both resolve configuration by
    walking up from the file's own path. A baseline computed under
    different configuration produces different findings, and the diff
    against it would be noise.
    """
    snapshot = target.with_name(f".feral-baseline-{uuid4().hex[:8]}{target.suffix}")
    try:
        snapshot.write_text(before_text)
    except OSError:
        return []
    try:
        return await runner(snapshot) or []
    finally:
        try:
            snapshot.unlink()
        except OSError:
            pass


# ── shared ────────────────────────────────────────────────────────────


def _line_from(stderr: str) -> int:
    """Best-effort line number out of a ``file:LINE`` compiler message."""
    for token in stderr.replace("\n", " ").split():
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
