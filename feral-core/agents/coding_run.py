"""
CodingRun — durable, repairable coding loop
=============================================

A *CodingRun* is FERAL's first-class long-horizon coding task. It is to
"the agent edited a file and reran the tests" what ``TaskFlow`` is to
"the agent ran a few sequenced skill calls": a durable, inspectable,
resumable record of work.

Design constraints (from the recovery plan):

* **Truthful.** We do not optimistically mark a run as "completed"
  until the verify phase actually passes. If the test command exits
  non-zero we say so and store the exit code, the stdout/stderr tail,
  and the failing-file hint we parsed out.
* **No destructive git.** This module never shells out to ``git
  reset --hard`` / ``git push -f`` / ``rm -rf`` / ``gh pr create``.
  The user can resolve commits and PRs themselves.
* **No vendor lock.** The loop accepts a pluggable ``Planner`` so
  tests use a deterministic planner and production wires an LLM.

.. warning::

   **This module is unwired and its write path is NOT policy-gated.**

   Nothing constructs a ``CodingRun`` in production; the only caller is
   ``cli/main.py``'s doctor check, which constructs the store. Do not
   treat the design constraints above as properties the running system
   has.

   In particular, an earlier version of this docstring claimed writes
   "go through the ``computer_use``/``coding_tools`` skill surface so
   the existing SandboxPolicy + workspace_grants gate every edit", and
   that a ``permission_needed`` response parks the run in
   ``waiting_grant``. Neither is true. ``_run_edit_phase`` calls
   ``target.write_text(...)`` directly (see the EDIT phase below), so
   SandboxPolicy never sees the write, no grant is consulted, and the
   ``WAITING_GRANT`` status is unreachable from the edit path.
   :func:`_ensure_inside_workspace` is the only check that actually
   runs, and it only proves the path is under ``workspace_root``.

   The reliability layer that IS wired, and that does go through the
   skill surface (read-before-edit, per-turn checkpoints, post-edit
   diagnostics, fallback matching), lives in ``skills/impl/coding_tools.py``
   and the modules under ``skills/``. Anything reused from here should
   be routed through that instead of this loop's direct write.

The schema (`coding_runs` table) intentionally mirrors `taskflows` so
operators can reason about both with the same mental model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional


logger = logging.getLogger(__name__)


class CodingRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_GRANT = "waiting_grant"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CodingRunPhase(str, Enum):
    INSPECT = "inspect"
    PLAN = "plan"
    EDIT = "edit"
    RUN_TESTS = "run_tests"
    PARSE_ERRORS = "parse_errors"
    REPAIR = "repair"
    VERIFY = "verify"


_TERMINAL_STATUSES = {
    CodingRunStatus.COMPLETED,
    CodingRunStatus.FAILED,
    CodingRunStatus.CANCELLED,
}


@dataclass
class CodingRunStep:
    """A single iteration of the loop, persisted alongside the run for
    inspection. We keep stdout/stderr tails (not full output) so the
    table stays small even when a noisy test runner is involved."""

    index: int
    phase: str
    status: str  # "ok" | "fail" | "skipped"
    summary: str = ""
    command: str = ""
    exit_code: Optional[int] = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    touched_paths: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = field(default_factory=time.time)


@dataclass
class CodingRun:
    """The durable shape of a coding task. Persisted as a single row in
    `coding_runs.db`; steps live in `coding_run_steps`."""

    id: str
    parent_session_id: str
    workspace_root: str
    goal: str
    test_command: str
    status: str = CodingRunStatus.QUEUED.value
    phase: str = ""
    iterations_used: int = 0
    max_iterations: int = 5
    last_exit_code: Optional[int] = None
    last_stdout_tail: str = ""
    last_stderr_tail: str = ""
    failing_test_hint: str = ""
    branch_name: str = ""
    pr_url: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None


# ── Persistence ───────────────────────────────────────────────────────


class CodingRunStore:
    """Thin SQLite-backed store. Mirrors the TaskFlow schema patterns so
    operators reading the two side-by-side find the same column names
    in the same places."""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coding_runs (
                    id TEXT PRIMARY KEY,
                    parent_session_id TEXT NOT NULL DEFAULT '',
                    workspace_root TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    test_command TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT '',
                    iterations_used INTEGER NOT NULL DEFAULT 0,
                    max_iterations INTEGER NOT NULL DEFAULT 5,
                    last_exit_code INTEGER,
                    last_stdout_tail TEXT NOT NULL DEFAULT '',
                    last_stderr_tail TEXT NOT NULL DEFAULT '',
                    failing_test_hint TEXT NOT NULL DEFAULT '',
                    branch_name TEXT NOT NULL DEFAULT '',
                    pr_url TEXT NOT NULL DEFAULT '',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coding_run_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    command TEXT NOT NULL DEFAULT '',
                    exit_code INTEGER,
                    stdout_tail TEXT NOT NULL DEFAULT '',
                    stderr_tail TEXT NOT NULL DEFAULT '',
                    touched_paths_json TEXT NOT NULL DEFAULT '[]',
                    started_at REAL NOT NULL,
                    finished_at REAL NOT NULL,
                    UNIQUE(run_id, step_index)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_coding_runs_status ON coding_runs(status)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_coding_run_steps_run ON coding_run_steps(run_id, step_index)"
            )

    def insert(self, run: CodingRun) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO coding_runs (
                    id, parent_session_id, workspace_root, goal, test_command,
                    status, phase, iterations_used, max_iterations,
                    last_exit_code, last_stdout_tail, last_stderr_tail,
                    failing_test_hint, branch_name, pr_url, context_json,
                    error, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id, run.parent_session_id, run.workspace_root, run.goal,
                    run.test_command, run.status, run.phase, run.iterations_used,
                    run.max_iterations, run.last_exit_code, run.last_stdout_tail,
                    run.last_stderr_tail, run.failing_test_hint, run.branch_name,
                    run.pr_url, json.dumps(run.context), run.error, run.created_at,
                    run.updated_at, run.completed_at,
                ),
            )

    def update(self, run: CodingRun) -> None:
        run.updated_at = time.time()
        with self._conn:
            self._conn.execute(
                """
                UPDATE coding_runs SET
                    status = ?, phase = ?, iterations_used = ?,
                    last_exit_code = ?, last_stdout_tail = ?,
                    last_stderr_tail = ?, failing_test_hint = ?,
                    branch_name = ?, pr_url = ?, context_json = ?,
                    error = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    run.status, run.phase, run.iterations_used,
                    run.last_exit_code, run.last_stdout_tail,
                    run.last_stderr_tail, run.failing_test_hint,
                    run.branch_name, run.pr_url, json.dumps(run.context),
                    run.error, run.updated_at, run.completed_at, run.id,
                ),
            )

    def get(self, run_id: str) -> Optional[CodingRun]:
        cur = self._conn.execute(
            "SELECT * FROM coding_runs WHERE id = ?", (run_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_run(row)

    def list_recent(self, limit: int = 50) -> List[CodingRun]:
        cur = self._conn.execute(
            "SELECT * FROM coding_runs ORDER BY updated_at DESC LIMIT ?",
            (int(limit),),
        )
        return [self._row_to_run(r) for r in cur.fetchall()]

    def append_step(self, run_id: str, step: CodingRunStep) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO coding_run_steps (
                    run_id, step_index, phase, status, summary, command,
                    exit_code, stdout_tail, stderr_tail, touched_paths_json,
                    started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, step.index, step.phase, step.status, step.summary,
                    step.command, step.exit_code, step.stdout_tail,
                    step.stderr_tail, json.dumps(step.touched_paths),
                    step.started_at, step.finished_at,
                ),
            )

    def list_steps(self, run_id: str) -> List[CodingRunStep]:
        cur = self._conn.execute(
            """
            SELECT step_index, phase, status, summary, command, exit_code,
                   stdout_tail, stderr_tail, touched_paths_json,
                   started_at, finished_at
            FROM coding_run_steps WHERE run_id = ? ORDER BY step_index
            """,
            (run_id,),
        )
        out: List[CodingRunStep] = []
        for row in cur.fetchall():
            out.append(
                CodingRunStep(
                    index=row[0], phase=row[1], status=row[2], summary=row[3],
                    command=row[4], exit_code=row[5], stdout_tail=row[6],
                    stderr_tail=row[7],
                    touched_paths=json.loads(row[8] or "[]"),
                    started_at=row[9], finished_at=row[10],
                )
            )
        return out

    @staticmethod
    def _row_to_run(row) -> CodingRun:
        return CodingRun(
            id=row[0], parent_session_id=row[1], workspace_root=row[2],
            goal=row[3], test_command=row[4], status=row[5], phase=row[6],
            iterations_used=row[7], max_iterations=row[8],
            last_exit_code=row[9], last_stdout_tail=row[10],
            last_stderr_tail=row[11], failing_test_hint=row[12],
            branch_name=row[13], pr_url=row[14],
            context=json.loads(row[15] or "{}"), error=row[16],
            created_at=row[17], updated_at=row[18], completed_at=row[19],
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ── Planner contract ──────────────────────────────────────────────────


@dataclass
class EditProposal:
    """A single proposed edit produced by a planner.

    * ``path`` — absolute filesystem path; the loop refuses writes
      outside the run's `workspace_root` so a buggy planner can't
      escape the sandbox.
    * ``find`` / ``replace`` — exact-string edit. `find` MUST exist
      in the file or the loop refuses; this is the same contract
      ``coding_tools.edit_file`` already enforces.
    * ``rationale`` — human string for the step log.
    """

    path: str
    find: str
    replace: str
    rationale: str = ""


@dataclass
class PlanResult:
    edits: List[EditProposal] = field(default_factory=list)
    summary: str = ""
    give_up: bool = False
    give_up_reason: str = ""


Planner = Callable[[CodingRun, str, str, str], Awaitable[PlanResult]]
"""Planner contract: ``(run, stdout_tail, stderr_tail, failing_hint) -> PlanResult``.

The runtime passes the failing test output (truncated to the same
tail it stored) so the planner can decide what to edit. Returning
``give_up=True`` ends the run as ``failed`` with the supplied reason."""


# ── Test execution + error parsing ────────────────────────────────────


_PYTEST_FAILURE_HINT = re.compile(r"FAILED\s+(\S+)::([^\s]+)")
_PYTEST_ERROR_FILE = re.compile(r"^(?P<file>\S+\.py):\d+:\s", re.MULTILINE)


def _parse_failing_test_hint(stdout: str, stderr: str) -> str:
    """Extract a short hint of the first failing test from pytest output.

    We only look at standard pytest summary lines (`FAILED foo.py::test`)
    and traceback file headers. If nothing parses we return the empty
    string rather than guessing — see the truthfulness rule.
    """
    for blob in (stdout or "", stderr or ""):
        m = _PYTEST_FAILURE_HINT.search(blob)
        if m:
            return f"{m.group(1)}::{m.group(2)}"
        m2 = _PYTEST_ERROR_FILE.search(blob)
        if m2:
            return m2.group("file")
    return ""


def _tail(s: str, limit: int = 2000) -> str:
    """Keep the last `limit` characters of `s`. The persisted blob in
    the run row is intentionally bounded so an ill-behaved test runner
    cannot inflate the DB."""
    s = s or ""
    if len(s) <= limit:
        return s
    return s[-limit:]


async def _run_command(
    command: List[str],
    *,
    cwd: Path,
    timeout: float = 120.0,
) -> tuple[int, str, str]:
    """Run a command in `cwd` without using a shell. Returns
    (exit_code, stdout, stderr). On timeout returns exit_code = -1.

    We force ``PYTHONDONTWRITEBYTECODE=1`` in the child env to avoid a
    classic CodingRun foot-gun: if the verifier is a Python test runner
    and two iterations happen inside the same filesystem-mtime second,
    the second run can pick up a stale ``__pycache__`` ``.pyc`` whose
    source-mtime header still validates against the un-edited file.
    Disabling bytecode writing keeps every iteration honest.
    """
    import os as _os
    env = _os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        finally:
            await proc.communicate()
        return -1, "", f"command timed out after {timeout}s"
    return proc.returncode if proc.returncode is not None else -1, (stdout or b"").decode(errors="replace"), (stderr or b"").decode(errors="replace")


# ── The loop ──────────────────────────────────────────────────────────


_FORBIDDEN_TEST_COMMANDS = {"rm", "sudo", "git"}
"""Commands a test runner must never execute through CodingRun. Git
operations stay manual: PR 5's mission is repair-and-verify, not
auto-commit. The runner is also blocked from `rm`-style cleanup —
that belongs in the test runner's own teardown."""


def _ensure_inside_workspace(workspace_root: Path, target: Path) -> None:
    """Raise PermissionError if `target` escapes `workspace_root`.

    We resolve both sides so symlinks don't smuggle writes out.

    This was written as defense in depth on the assumption that the
    SandboxPolicy would stop the skill too. It does not, because the
    edit phase never invokes a skill: it calls ``target.write_text``
    directly. So this containment check is the ONLY thing standing
    between a planner's proposed path and the filesystem on this code
    path. See the module docstring."""
    root = workspace_root.resolve()
    abs_target = target.resolve()
    try:
        abs_target.relative_to(root)
    except ValueError as exc:
        raise PermissionError(
            f"Edit target {abs_target} is outside CodingRun workspace {root}"
        ) from exc


class CodingRunner:
    """Drives a single CodingRun to a terminal state.

    The class is split out from the dataclass so tests can run the loop
    against an in-memory `CodingRunStore` without standing up the full
    FERAL service registry.
    """

    def __init__(self, store: CodingRunStore):
        self._store = store

    async def execute(
        self,
        *,
        run: CodingRun,
        planner: Planner,
        verify_command: Optional[List[str]] = None,
    ) -> CodingRun:
        """Drive a CodingRun: inspect → plan → edit → verify, repeating
        repair iterations until the verify command exits 0 or the
        planner gives up or we hit ``max_iterations``."""
        if not run.test_command:
            raise ValueError("CodingRun.test_command is required")

        cmd = run.test_command.split() if not verify_command else verify_command
        if cmd[0] in _FORBIDDEN_TEST_COMMANDS:
            run.status = CodingRunStatus.FAILED.value
            run.error = (
                f"Refusing CodingRun: test_command starts with {cmd[0]!r}; "
                "git/rm/sudo are not valid verifiers for the repair loop."
            )
            self._store.update(run)
            return run

        workspace = Path(run.workspace_root).expanduser().resolve()
        if not workspace.is_dir():
            run.status = CodingRunStatus.FAILED.value
            run.error = f"workspace_root {workspace} is not a directory"
            self._store.update(run)
            return run

        run.status = CodingRunStatus.RUNNING.value
        self._store.update(run)

        step_index = 0
        for iteration in range(run.max_iterations):
            run.iterations_used = iteration + 1

            # Verify phase first — if tests already pass, we are done
            # before touching a file. This is the truthful equivalent
            # of "is there anything to fix?".
            run.phase = CodingRunPhase.VERIFY.value if iteration == 0 else CodingRunPhase.RUN_TESTS.value
            self._store.update(run)
            t0 = time.time()
            exit_code, stdout, stderr = await _run_command(cmd, cwd=workspace)
            t1 = time.time()
            run.last_exit_code = exit_code
            run.last_stdout_tail = _tail(stdout)
            run.last_stderr_tail = _tail(stderr)
            run.failing_test_hint = _parse_failing_test_hint(stdout, stderr)
            step_index += 1
            self._store.append_step(
                run.id,
                CodingRunStep(
                    index=step_index, phase=run.phase,
                    status="ok" if exit_code == 0 else "fail",
                    summary=f"{' '.join(cmd)} -> exit {exit_code}",
                    command=" ".join(cmd), exit_code=exit_code,
                    stdout_tail=run.last_stdout_tail,
                    stderr_tail=run.last_stderr_tail,
                    started_at=t0, finished_at=t1,
                ),
            )

            if exit_code == 0:
                run.status = CodingRunStatus.COMPLETED.value
                run.phase = ""
                run.completed_at = time.time()
                run.error = ""
                self._store.update(run)
                return run

            # Planner decides the next edit.
            run.phase = CodingRunPhase.PLAN.value
            self._store.update(run)
            plan = await planner(run, run.last_stdout_tail, run.last_stderr_tail, run.failing_test_hint)
            if plan.give_up:
                run.status = CodingRunStatus.FAILED.value
                run.phase = ""
                run.completed_at = time.time()
                run.error = plan.give_up_reason or "planner gave up"
                step_index += 1
                self._store.append_step(
                    run.id,
                    CodingRunStep(
                        index=step_index, phase=CodingRunPhase.PLAN.value,
                        status="fail",
                        summary=f"planner gave up: {plan.give_up_reason}",
                    ),
                )
                self._store.update(run)
                return run

            if not plan.edits:
                run.status = CodingRunStatus.FAILED.value
                run.phase = ""
                run.completed_at = time.time()
                run.error = "planner returned no edits and did not give up"
                self._store.update(run)
                return run

            run.phase = CodingRunPhase.EDIT.value
            self._store.update(run)
            applied: List[str] = []
            for edit in plan.edits:
                t0 = time.time()
                target = Path(edit.path).expanduser()
                if not target.is_absolute():
                    target = (workspace / target).resolve()
                try:
                    _ensure_inside_workspace(workspace, target)
                except PermissionError as exc:
                    step_index += 1
                    self._store.append_step(
                        run.id,
                        CodingRunStep(
                            index=step_index, phase=CodingRunPhase.EDIT.value,
                            status="fail",
                            summary=f"refused edit outside workspace: {edit.path}",
                            stderr_tail=str(exc),
                            started_at=t0, finished_at=time.time(),
                        ),
                    )
                    run.status = CodingRunStatus.FAILED.value
                    run.phase = ""
                    run.completed_at = time.time()
                    run.error = str(exc)
                    self._store.update(run)
                    return run

                if not target.exists():
                    step_index += 1
                    self._store.append_step(
                        run.id,
                        CodingRunStep(
                            index=step_index, phase=CodingRunPhase.EDIT.value,
                            status="fail",
                            summary=f"missing file: {target}",
                            started_at=t0, finished_at=time.time(),
                        ),
                    )
                    run.status = CodingRunStatus.FAILED.value
                    run.phase = ""
                    run.completed_at = time.time()
                    run.error = f"missing file: {target}"
                    self._store.update(run)
                    return run

                original = target.read_text()
                if edit.find not in original:
                    step_index += 1
                    self._store.append_step(
                        run.id,
                        CodingRunStep(
                            index=step_index, phase=CodingRunPhase.EDIT.value,
                            status="fail",
                            summary=f"find-string absent in {target}: {edit.find!r}",
                            started_at=t0, finished_at=time.time(),
                        ),
                    )
                    run.status = CodingRunStatus.FAILED.value
                    run.phase = ""
                    run.completed_at = time.time()
                    run.error = f"edit find-string absent in {target}"
                    self._store.update(run)
                    return run

                target.write_text(original.replace(edit.find, edit.replace, 1))
                applied.append(str(target))
                step_index += 1
                self._store.append_step(
                    run.id,
                    CodingRunStep(
                        index=step_index, phase=CodingRunPhase.EDIT.value,
                        status="ok",
                        summary=edit.rationale or f"edit {target.name}",
                        touched_paths=[str(target)],
                        started_at=t0, finished_at=time.time(),
                    ),
                )
            run.context.setdefault("touched_paths", []).extend(applied)
            self._store.update(run)

        # Out of iterations without success.
        run.status = CodingRunStatus.FAILED.value
        run.phase = ""
        run.completed_at = time.time()
        if not run.error:
            run.error = f"max_iterations ({run.max_iterations}) exhausted"
        self._store.update(run)
        return run


def new_run(
    *,
    workspace_root: str,
    goal: str,
    test_command: str,
    parent_session_id: str = "",
    max_iterations: int = 5,
) -> CodingRun:
    """Factory that produces a fresh CodingRun with a short uuid id and
    sane defaults. Separated from the dataclass so callers don't have
    to remember to set timestamps."""
    return CodingRun(
        id=uuid.uuid4().hex[:12],
        parent_session_id=parent_session_id,
        workspace_root=workspace_root,
        goal=goal,
        test_command=test_command,
        max_iterations=max_iterations,
    )


__all__ = [
    "CodingRun",
    "CodingRunner",
    "CodingRunStatus",
    "CodingRunPhase",
    "CodingRunStep",
    "CodingRunStore",
    "EditProposal",
    "PlanResult",
    "Planner",
    "new_run",
    "_parse_failing_test_hint",
    "_FORBIDDEN_TEST_COMMANDS",
]


# Convenience: expose a tiny dict form for REST / debug routes without
# leaking the dataclass type.
def run_to_dict(run: CodingRun) -> Dict[str, Any]:
    return asdict(run)
