"""
FERAL TaskFlow Runtime
=======================
Persistent multi-step background flows with restart-safe state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Optional, Any
from uuid import uuid4

import httpx

from config.loader import feral_data_home
from skills.call_context import bind_context

logger = logging.getLogger("feral.taskflow")


class TaskFlowStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskFlowRuntime:
    """SQLite-backed taskflow runner with resumable state."""

    def __init__(self, db_path: Optional[str] = None, memory_store=None,
                 skill_registry=None, orchestrator=None):
        base = feral_data_home()
        base.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path or str(base / "taskflows.db")
        self._memory = memory_store
        self._skill_registry = skill_registry
        self._orchestrator = orchestrator
        self._supervisor = None
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._runner_task: Optional[asyncio.Task] = None
        self._step_task: Optional[asyncio.Task] = None
        self._active_flow_id = ""
        self._approved_tasks: dict[str, asyncio.Task] = {}
        self._stop_event = asyncio.Event()
        self._http = httpx.AsyncClient(timeout=20.0)
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = self._conn
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS taskflows (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step INTEGER NOT NULL DEFAULT 0,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    wait_until REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS taskflow_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    flow_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    step_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    result_json TEXT,
                    error TEXT,
                    started_at REAL,
                    finished_at REAL,
                    UNIQUE(flow_id, step_index)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_taskflows_status ON taskflows(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_taskflows_updated ON taskflows(updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_taskflow_steps_flow ON taskflow_steps(flow_id, step_index)")
            conn.commit()

    async def start(self):
        if self._runner_task and not self._runner_task.done():
            return
        self._recover_after_restart()
        self._stop_event.clear()
        self._runner_task = asyncio.create_task(self._runner_loop())
        logger.info("TaskFlow runtime started")

    async def stop(self):
        self._stop_event.set()
        active = [task for task in self._approved_tasks.values()
                  if task is not asyncio.current_task() and not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        if self._runner_task:
            self._runner_task.cancel()
            try:
                await self._runner_task
            except asyncio.CancelledError:
                pass
            self._runner_task = None
        await self._http.aclose()

    def _recover_after_restart(self):
        # A persisted running step may have committed before the process
        # disappeared. Only proven read-only work may be replayed.
        with self._lock:
            identifiers = [r[0] for r in self._conn.execute("SELECT id FROM taskflows").fetchall()]
        for flow_id in identifiers:
            full = self.get_flow(flow_id)
            if not full:
                continue
            for step in full["steps"]:
                if step["status"] == "running":
                    if self._restart_safe(step):
                        with self._lock:
                            self._conn.execute("UPDATE taskflow_steps SET status = 'pending' WHERE id = ?", (step["id"],))
                            self._conn.commit()
                    else:
                        self._mark_unknown(full, step, "Process stopped during a potentially effectful step")
        now = time.time()
        with self._lock:
            conn = self._conn
            conn.execute("UPDATE taskflows SET status = ?, updated_at = ? WHERE status = ?",
                         (TaskFlowStatus.QUEUED.value, now, TaskFlowStatus.RUNNING.value))
            conn.execute(
                """
                UPDATE taskflows
                SET status = ?, updated_at = ?
                WHERE status = ? AND wait_until IS NOT NULL AND wait_until <= ?
                """,
                (TaskFlowStatus.QUEUED.value, now, TaskFlowStatus.WAITING.value, now),
            )
            conn.commit()

    def create_flow(
        self,
        *,
        session_id: str,
        title: str,
        steps: list[dict],
        context: Optional[dict] = None,
    ) -> dict:
        if not steps:
            raise ValueError("TaskFlow requires at least one step")
        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"Invalid step at index {idx}")
            if not step.get("type"):
                raise ValueError(f"Missing step.type at index {idx}")

        now = time.time()
        flow_id = str(uuid4())[:12]
        with self._lock:
            conn = self._conn
            conn.execute(
                """
                INSERT INTO taskflows
                (id, session_id, title, status, current_step, context_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    flow_id,
                    session_id,
                    title or f"TaskFlow {flow_id}",
                    TaskFlowStatus.QUEUED.value,
                    json.dumps(context or {}),
                    now,
                    now,
                ),
            )
            for i, step in enumerate(steps):
                payload = dict(step)
                payload.pop("type", None)
                conn.execute(
                    """
                    INSERT INTO taskflow_steps
                    (flow_id, step_index, step_type, payload_json, status)
                    VALUES (?, ?, ?, ?, 'pending')
                    """,
                    (flow_id, i, step["type"], json.dumps(payload)),
                )
            conn.commit()
        flow = self.get_flow(flow_id)
        # Consciousness-layer write: record the flow as an in-flight
        # entity so "where did I leave off" queries surface it across
        # a Brain restart. Wrapped in try/except so a missing store
        # never blocks flow creation.
        try:
            from api.state import state as _state
            store = getattr(_state, "consciousness", None)
            if store is not None:
                store.record_flow(
                    flow_id=flow_id,
                    title=title or f"TaskFlow {flow_id}",
                    step=0,
                    steps=len(steps),
                    session_id=session_id,
                )
        except Exception:
            pass
        return flow

    def list_flows(
        self,
        *,
        session_id: str = "",
        status: str = "",
        limit: int = 50,
    ) -> list[dict]:
        lim = max(1, min(limit, 200))
        with self._lock:
            conn = self._conn
            if session_id and status:
                rows = conn.execute(
                    "SELECT * FROM taskflows WHERE session_id = ? AND status = ? ORDER BY updated_at DESC LIMIT ?",
                    (session_id, status, lim),
                ).fetchall()
            elif session_id:
                rows = conn.execute(
                    "SELECT * FROM taskflows WHERE session_id = ? ORDER BY updated_at DESC LIMIT ?",
                    (session_id, lim),
                ).fetchall()
            elif status:
                rows = conn.execute(
                    "SELECT * FROM taskflows WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                    (status, lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM taskflows ORDER BY updated_at DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        return [self._flow_row_to_dict(r) for r in rows]

    def get_flow(self, flow_id: str) -> Optional[dict]:
        with self._lock:
            conn = self._conn
            row = conn.execute("SELECT * FROM taskflows WHERE id = ?", (flow_id,)).fetchone()
            if not row:
                return None
            step_rows = conn.execute(
                "SELECT * FROM taskflow_steps WHERE flow_id = ? ORDER BY step_index ASC",
                (flow_id,),
            ).fetchall()
        flow = self._flow_row_to_dict(row)
        flow["steps"] = [self._step_row_to_dict(s) for s in step_rows]
        return flow

    def resume_flow(self, flow_id: str) -> Optional[dict]:
        now = time.time()
        flow = self.get_flow(flow_id)
        if not flow:
            return None
        # Review completion belongs to the existing approval dispatcher.
        # Manual resume is never a reconciliation of an uncertain effect.
        if flow["status"] in (TaskFlowStatus.COMPLETED.value, TaskFlowStatus.CANCELLED.value):
            return flow
        if any(s["status"] == "outcome_unknown" or
               (s.get("result") or {}).get("reason") == "approval_required"
               for s in flow["steps"]):
            return flow
        if any(s["status"] == "failed" and
               (s.get("result") or {}).get("dispatch_started") is not False
               and not self._restart_safe(s) for s in flow["steps"]):
            return flow
        with self._lock:
            conn = self._conn
            row = conn.execute("SELECT status FROM taskflows WHERE id = ?", (flow_id,)).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE taskflows SET status = ?, error = NULL, wait_until = NULL, updated_at = ? WHERE id = ?",
                (TaskFlowStatus.QUEUED.value, now, flow_id),
            )
            conn.execute(
                """
                UPDATE taskflow_steps
                SET status = 'pending', error = NULL, started_at = NULL, finished_at = NULL
                WHERE flow_id = ? AND status IN ('failed', 'waiting')
                """,
                (flow_id,),
            )
            conn.commit()
        return self.get_flow(flow_id)

    def cancel_flow(self, flow_id: str) -> Optional[dict]:
        now = time.time()
        with self._lock:
            conn = self._conn
            row = conn.execute("SELECT id FROM taskflows WHERE id = ?", (flow_id,)).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE taskflows SET status = ?, updated_at = ? WHERE id = ?",
                (TaskFlowStatus.CANCELLED.value, now, flow_id),
            )
            conn.commit()
        if self._active_flow_id == flow_id and self._step_task and not self._step_task.done():
            self._step_task.cancel()
        approved_task = self._approved_tasks.get(flow_id)
        if approved_task and approved_task is not asyncio.current_task() and not approved_task.done():
            approved_task.cancel()
        flow = self.get_flow(flow_id)
        if flow:
            for step in flow["steps"]:
                approval = (step.get("result") or {}).get("approval") or {}
                runner = getattr(self._orchestrator, "tool_runner", None)
                if approval.get("request_id") and runner is not None:
                    runner.deny_pending(approval["request_id"], session_id=approval["session_id"])
        return self.get_flow(flow_id)

    def _restart_safe(self, step: dict) -> bool:
        if step["step_type"] in {"noop", "condition", "sleep", "memory.search"}:
            return True
        if step["step_type"] != "skill.invoke":
            return False
        from security.safety_resolver import is_read_only
        raw = step.get("payload") or {}
        payload = raw.get("config", raw)
        try:
            manifest = self._skill_registry.skills.get(payload.get("skill_id", "")) if self._skill_registry else None
            endpoint = next((ep for ep in manifest.endpoints if ep.id == payload.get("endpoint")), None) if manifest else None
            return bool(endpoint and endpoint.read_only_hint is True and
                        is_read_only(f"{payload.get('skill_id', '')}__{payload.get('endpoint', '')}",
                                     registry=self._skill_registry, strict=True))
        except Exception:
            logger.warning("Could not classify interrupted workflow step; requiring reconciliation")
            return False

    def _mark_unknown(self, flow: dict, step: dict, reason: str):
        outcome = {"status": "outcome_unknown", "error": reason,
                   "tool_outcome_verified": False, "replay_safe": False}
        with self._lock:
            self._conn.execute("UPDATE taskflow_steps SET status = 'outcome_unknown', error = ?, result_json = ?, finished_at = ? WHERE id = ?",
                               (reason, json.dumps(outcome), time.time(), step["id"]))
            self._conn.execute("UPDATE taskflows SET status = CASE WHEN status = 'cancelled' THEN status ELSE 'waiting' END, error = ?, wait_until = NULL, updated_at = ? WHERE id = ?",
                               (reason, time.time(), flow["id"]))
            self._conn.commit()

    def _approval_step(self, pending: dict):
        binding = pending.get("taskflow") or {}
        flow = self.get_flow(binding.get("flow_id", ""))
        if not flow:
            return None, None
        step = next((s for s in flow["steps"] if s["id"] == binding.get("step_id")), None)
        approval = (step.get("result") or {}).get("approval") if step else None
        exact = {key: pending.get(key) for key in ("request_id", "session_id", "tool_name", "args")}
        if not approval or approval != exact:
            return None, None
        return flow, step

    def prepare_approved_dispatch(self, pending: dict) -> bool:
        """Claim the existing review once, before its central execution."""
        flow, step = self._approval_step(pending)
        if (not flow or flow["status"] != "waiting" or step["status"] != "waiting"
                or (self._supervisor is not None and self._supervisor.paused)):
            return False
        runner = getattr(self._orchestrator, "tool_runner", None)
        if runner is None or runner.enforce_plan_mode(pending["tool_name"], pending["session_id"]):
            return False
        with self._lock:
            changed = self._conn.execute("UPDATE taskflow_steps SET status = 'running', started_at = ?, finished_at = NULL WHERE id = ? AND status = 'waiting'",
                                         (time.time(), step["id"])).rowcount
            if not changed:
                return False
            self._conn.execute("UPDATE taskflows SET status = 'running', updated_at = ? WHERE id = ? AND status = 'waiting'",
                               (time.time(), flow["id"]))
            self._conn.commit()
        task = asyncio.current_task()
        if task is not None:
            self._approved_tasks[flow["id"]] = task
        return True

    def approved_dispatch_allowed(self, pending: dict) -> bool:
        flow, step = self._approval_step(pending)
        return bool(flow and flow["status"] == "running" and step["status"] == "running"
                    and not (self._supervisor is not None and self._supervisor.paused))

    async def finish_approved_dispatch(self, pending: dict, result: Optional[dict] = None,
                                       *, rejected: bool = False, uncertain: bool = False):
        """Persist the actual dispatcher response, never dispatch again."""
        flow, step = self._approval_step(pending)
        binding = pending.get("taskflow") or {}
        self._approved_tasks.pop(binding.get("flow_id", ""), None)
        if not flow or step["status"] not in {"waiting", "running"}:
            return False
        if uncertain or flow["status"] == "cancelled":
            self._mark_unknown(flow, step, "Approval dispatch interrupted; external effect outcome is unknown")
            return True
        result = result or {}
        success = not rejected and result.get("success") is True
        error = None if success else ("Approval rejected" if rejected else str(result.get("error") or result.get("note") or "Dispatcher refused the workflow action"))
        outcome = {"status": "completed" if success else "failed", "result": result,
                   "error": error, "dispatch_started": not rejected,
                   "tool_outcome_verified": result.get("tool_outcome_verified", False)}
        context = dict(flow.get("context") or {})
        if success:
            output = self._step_output_text(outcome)
            records = dict(context.get("step_results") or {})
            records[str(step["step_index"])] = {"type": step["step_type"], "status": "completed", "output": output}
            context["step_results"] = records
            if output is not None:
                context["previous_output"] = output
        now = time.time()
        with self._lock:
            changed = self._conn.execute("UPDATE taskflow_steps SET status = ?, result_json = ?, error = ?, finished_at = ? WHERE id = ? AND status IN ('waiting', 'running')",
                                         (outcome["status"], json.dumps(outcome, default=str), error, now, step["id"])).rowcount
            if not changed:
                return False
            self._conn.execute("UPDATE taskflows SET status = ?, current_step = ?, context_json = ?, error = ?, wait_until = NULL, updated_at = ? WHERE id = ? AND status != 'cancelled'",
                               ("queued" if success else "failed", step["step_index"] + int(success),
                                json.dumps(context, default=str), error, now, flow["id"]))
            self._conn.commit()
        return True

    def stats(self) -> dict:
        with self._lock:
            conn = self._conn
            total = conn.execute("SELECT COUNT(*) FROM taskflows").fetchone()[0]
            grouped = conn.execute(
                "SELECT status, COUNT(*) AS c FROM taskflows GROUP BY status ORDER BY c DESC"
            ).fetchall()
        return {
            "flows_total": total,
            "by_status": [{"status": r["status"], "count": r["c"]} for r in grouped],
        }

    async def _runner_loop(self):
        while not self._stop_event.is_set():
            flow_id = self._next_ready_flow_id()
            if not flow_id:
                await asyncio.sleep(1.0)
                continue
            try:
                await self._run_flow(flow_id)
            except Exception as e:
                logger.error(f"TaskFlow runner error ({flow_id}): {e}", exc_info=True)

    def _next_ready_flow_id(self) -> Optional[str]:
        now = time.time()
        with self._lock:
            conn = self._conn
            row = conn.execute(
                """
                SELECT id
                FROM taskflows
                WHERE status = ?
                   OR (status = ? AND wait_until IS NOT NULL AND wait_until <= ?)
                ORDER BY updated_at ASC
                LIMIT 1
                """,
                (TaskFlowStatus.QUEUED.value, TaskFlowStatus.WAITING.value, now),
            ).fetchone()
        return row["id"] if row else None

    async def _run_flow(self, flow_id: str):
        flow = self.get_flow(flow_id)
        if not flow:
            return
        if flow["status"] == TaskFlowStatus.CANCELLED.value:
            return
        if any(s["status"] == "outcome_unknown" or
               (s.get("result") or {}).get("reason") == "approval_required"
               for s in flow["steps"]):
            return

        now = time.time()
        with self._lock:
            conn = self._conn
            conn.execute(
                """
                UPDATE taskflows
                SET status = ?, started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (TaskFlowStatus.RUNNING.value, now, now, flow_id),
            )
            conn.commit()

        current_step = int(flow.get("current_step", 0))
        while True:
            flow = self.get_flow(flow_id)
            if not flow:
                return
            if flow["status"] == TaskFlowStatus.CANCELLED.value:
                return
            steps = flow.get("steps", [])
            if current_step >= len(steps):
                done = time.time()
                with self._lock:
                    conn = self._conn
                    conn.execute(
                        """
                        UPDATE taskflows
                        SET status = ?, completed_at = ?, updated_at = ?, wait_until = NULL
                        WHERE id = ?
                        """,
                        (TaskFlowStatus.COMPLETED.value, done, done, flow_id),
                    )
                    conn.commit()
                return

            step = steps[current_step]
            step_id = step["id"]
            with self._lock:
                conn = self._conn
                conn.execute(
                    """
                    UPDATE taskflow_steps
                    SET status = 'running', started_at = COALESCE(started_at, ?), error = NULL
                    WHERE id = ?
                    """,
                    (time.time(), step_id),
                )
                conn.commit()

            self._active_flow_id = flow_id
            self._step_task = asyncio.create_task(self._execute_step(flow, step))
            try:
                outcome = await self._step_task
            except asyncio.CancelledError:
                latest = self.get_flow(flow_id)
                finished = next((s for s in latest["steps"] if s["id"] == step_id), None) if latest else None
                if not finished or finished["status"] != "completed":
                    self._mark_unknown(flow, step, "Step cancelled; already-started effects may have an unknown outcome")
                if self._stop_event.is_set():
                    raise
                return
            except Exception as exc:
                logger.error("Workflow step dispatch interrupted (%s); no automatic replay", type(exc).__name__)
                latest = self.get_flow(flow_id)
                finished = next((s for s in latest["steps"] if s["id"] == step_id), None) if latest else None
                if not finished or finished["status"] != "completed":
                    self._mark_unknown(flow, step, "Step interrupted by an execution error; reconcile before continuing")
                return
            finally:
                self._step_task = None
                self._active_flow_id = ""
            if outcome.get("status") == "deferred":
                return  # The persisted review/callback owns its state.
            latest = self.get_flow(flow_id)
            if latest and latest["status"] == TaskFlowStatus.CANCELLED.value:
                if outcome.get("status") == "completed" and self._restart_safe(step):
                    # Cancellation stops the flow, but cannot make an
                    # already returned read result uncertain.
                    with self._lock:
                        self._conn.execute("UPDATE taskflow_steps SET status = 'completed', result_json = ?, error = NULL, finished_at = ? WHERE id = ?",
                                           (json.dumps(outcome, default=str), time.time(), step_id))
                        self._conn.commit()
                else:
                    self._mark_unknown(flow, step, "Flow cancelled during dispatch; result requires reconciliation")
                return
            status = outcome.get("status", "failed")
            if status == "waiting":
                wait_until = float(outcome.get("wait_until", time.time() + 1))
                with self._lock:
                    conn = self._conn
                    conn.execute(
                        """
                        UPDATE taskflow_steps
                        SET status = 'waiting', result_json = ?, finished_at = NULL
                        WHERE id = ?
                        """,
                        (json.dumps(outcome), step_id),
                    )
                    conn.execute(
                        """
                        UPDATE taskflows
                        SET status = ?, wait_until = ?, current_step = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (TaskFlowStatus.WAITING.value, wait_until, current_step, time.time(), flow_id),
                    )
                    conn.commit()
                return

            if status == "failed":
                err = outcome.get("error", "step failed")
                with self._lock:
                    conn = self._conn
                    conn.execute(
                        """
                        UPDATE taskflow_steps
                        SET status = 'failed', error = ?, result_json = ?, finished_at = ?
                        WHERE id = ?
                        """,
                        (err, json.dumps(outcome), time.time(), step_id),
                    )
                    conn.execute(
                        """
                        UPDATE taskflows
                        SET status = ?, error = ?, wait_until = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (TaskFlowStatus.FAILED.value, err, time.time(), flow_id),
                    )
                    conn.commit()
                return

            # L4 — accumulate step output into the flow context so later
            # steps (and {{ step_N }} / {{ previous_output }} templating) can
            # consume it. The next loop iteration reloads the flow, so the
            # persisted context is what downstream steps see.
            context = dict(flow.get("context") or {})
            step_results = dict(context.get("step_results") or {})
            output_text = self._step_output_text(outcome)
            step_results[str(current_step)] = {
                "type": step.get("step_type"),
                "status": outcome.get("status"),
                "output": output_text,
            }
            context["step_results"] = step_results
            if output_text is not None:
                context["previous_output"] = output_text

            # L4 — condition steps perform a real branch-jump. ``branch`` is
            # the then/else *index* computed by _execute_step; when present
            # and valid we jump there instead of falling through.
            next_step = current_step + 1
            if step.get("step_type") == "condition":
                branch = outcome.get("branch")
                if isinstance(branch, bool):
                    branch = None  # guard: bools are ints in Python
                if isinstance(branch, int) and branch >= 0:
                    next_step = branch

            with self._lock:
                conn = self._conn
                conn.execute(
                    """
                    UPDATE taskflow_steps
                    SET status = 'completed', result_json = ?, error = NULL, finished_at = ?
                    WHERE id = ?
                    """,
                    (json.dumps(outcome), time.time(), step_id),
                )
                conn.execute(
                    """
                    UPDATE taskflows
                    SET current_step = ?, status = ?, context_json = ?, wait_until = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (next_step, TaskFlowStatus.RUNNING.value, json.dumps(context, default=str), time.time(), flow_id),
                )
                conn.commit()
            current_step = next_step
            self._record_flow_progress(flow, next_step)

    async def _execute_step(self, flow: dict, step: dict) -> dict:
        step_type = step.get("step_type", "")
        raw_payload = step.get("payload", {})
        payload = raw_payload.get("config", raw_payload) if isinstance(raw_payload.get("config"), dict) else raw_payload

        # L4 — render {{ step_N }} / {{ previous_output }} against the
        # accumulated flow context, then apply convenience aliases so the LLM
        # can author steps loosely (prompt_template→prompt, q→query).
        context = flow.get("context", {}) or {}
        payload = self._render_templates(payload, context)
        if isinstance(payload, dict):
            if not payload.get("prompt") and payload.get("prompt_template"):
                payload["prompt"] = payload.get("prompt_template")
            if not payload.get("query") and payload.get("q") is not None:
                payload["query"] = payload.get("q")

        if step_type == "noop":
            return {"status": "completed", "message": "noop"}

        if step_type == "sleep":
            seconds = max(1, int(payload.get("seconds", 1)))
            now = time.time()
            wait_until = flow.get("wait_until")
            if not wait_until:
                return {"status": "waiting", "wait_until": now + seconds, "seconds": seconds}
            if now < float(wait_until):
                return {"status": "waiting", "wait_until": float(wait_until), "seconds": seconds}
            return {"status": "completed", "slept_seconds": seconds}

        if step_type == "note.save":
            if not self._memory:
                return {"status": "failed", "error": "memory store not available"}
            content = str(payload.get("content", "")).strip()
            if not content:
                return {"status": "failed", "error": "note.save requires content"}
            note = await self._memory.save(
                content=content,
                tags=payload.get("tags", []),
                importance=payload.get("importance", "normal"),
                source=f"taskflow:{flow['id']}",
            )
            return {"status": "completed", "note": note}

        if step_type == "wiki.compile":
            if not self._memory:
                return {"status": "failed", "error": "memory store not available"}
            result = await self._memory.wiki_compile(
                notes_limit=int(payload.get("notes_limit", 200)),
                episodes_limit=int(payload.get("episodes_limit", 200)),
                knowledge_limit=int(payload.get("knowledge_limit", 400)),
            )
            return {"status": "completed", "wiki": result}

        if step_type == "memory.search":
            if not self._memory:
                return {"status": "failed", "error": "memory store not available"}
            query = str(payload.get("query", "")).strip()
            if not query:
                return {"status": "failed", "error": "memory.search requires query"}
            results = await self._memory.search_all(query, limit=int(payload.get("limit", 8)))
            return {"status": "completed", "results": results}

        if step_type == "http.get":
            url = str(payload.get("url", "")).strip()
            if not url:
                return {"status": "failed", "error": "http.get requires url"}
            resp = await self._http.get(url)
            preview_chars = max(100, min(int(payload.get("preview_chars", 3000)), 15000))
            return {
                "status": "completed",
                "status_code": resp.status_code,
                "body_preview": (resp.text or "")[:preview_chars],
            }

        if step_type == "skill.invoke":
            skill_id = str(payload.get("skill_id", "")).strip()
            endpoint = str(payload.get("endpoint", "")).strip()
            if not skill_id or not endpoint:
                return {"status": "failed", "error": "skill.invoke requires skill_id and endpoint"}
            if not self._skill_registry:
                return {"status": "failed", "error": "No skill registry available"}
            skill = self._skill_registry.skills.get(skill_id)
            if not skill:
                return {"status": "failed", "error": f"Skill '{skill_id}' not found"}
            args = payload.get("args", {})
            if not isinstance(args, dict):
                return {"status": "failed", "error": "skill.invoke args must be an object", "dispatch_started": False}
            runner = getattr(self._orchestrator, "tool_runner", None)
            tool_name = f"{skill_id}__{endpoint}"
            if runner is None:
                # Explain an explicit deny for legacy headless callers, but
                # never substitute direct skill execution for full authority.
                from security.safety_resolver import resolve_policy, LEVEL_DENY
                policy = resolve_policy(tool_name, args, surface="taskflow", registry=self._skill_registry)
                error = (f"denied by safety policy: {policy.deny_reason}" if policy.level == LEVEL_DENY
                         else "Full policy dispatcher unavailable; no skill executed")
                return {"status": "failed", "error": error, "dispatch_started": False}
            if self._supervisor is not None and self._supervisor.paused:
                return {"status": "failed", "error": "Agent supervisor is paused", "dispatch_started": False}
            session_id = flow.get("session_id") or f"taskflow-{flow['id']}"
            call_id = f"taskflow:{flow['id']}:{step['id']}"
            with bind_context(session_id=session_id, surface="taskflow", call_id=call_id):
                refusal = runner.enforce_plan_mode(tool_name, session_id)
                if refusal is None:
                    refusal = runner.enforce_safety(tool_name, args, session_id=session_id, surface="taskflow")
                if refusal is not None:
                    if refusal.get("status") != "pending_approval":
                        return {"status": "failed", "error": str(refusal.get("note") or refusal.get("reason") or refusal.get("error") or "Policy refused workflow action"),
                                "result": refusal, "dispatch_started": False}
                    binding = {"flow_id": flow["id"], "step_id": step["id"]}
                    if refusal.get("taskflow") not in (None, binding):
                        return {"status": "failed", "error": "Approval is owned by another workflow step", "dispatch_started": False}
                    refusal["taskflow"] = binding
                    approval = json.loads(json.dumps({key: refusal[key] for key in
                                                      ("request_id", "session_id", "tool_name", "args")}))
                    review = {"status": "waiting", "reason": "approval_required", "approval": approval}
                    with self._lock:
                        self._conn.execute("UPDATE taskflow_steps SET status = 'waiting', result_json = ?, finished_at = NULL WHERE id = ?", (json.dumps(review), step["id"]))
                        self._conn.execute("UPDATE taskflows SET status = 'waiting', wait_until = NULL, updated_at = ? WHERE id = ? AND status != 'cancelled'", (time.time(), flow["id"]))
                        self._conn.commit()
                    # Persist identity/action binding before publishing the
                    # existing review, including an immediate user response.
                    await runner._notify_user_of_pending_approval(session_id, tool_name, refusal)
                    return {"status": "deferred"}
                result = await runner.execute_tool_call_for_llm(
                    session_id, {"id": call_id, "name": tool_name, "args": args}, [], surface="taskflow"
                )
            ok = result.get("success", False)
            return {
                "status": "completed" if ok else "failed",
                "result": result,
                "error": result.get("error") if not ok else None,
                "dispatch_started": True,
            }

        if step_type == "llm.chat":
            prompt = str(payload.get("prompt", "")).strip()
            if not prompt:
                return {"status": "failed", "error": "llm.chat requires prompt"}
            if not self._orchestrator:
                return {"status": "failed", "error": "No orchestrator available"}
            session_id = flow.get("session_id") or f"taskflow-{flow['id']}"
            try:
                # L4 — capture the reply text so downstream steps can consume
                # it via {{ previous_output }} / {{ step_N }}.
                reply = await self._orchestrator.handle_command(session_id, prompt)
                reply_text = "" if reply is None else str(reply)
                return {"status": "completed", "prompt": prompt, "output": reply_text, "reply": reply_text}
            except Exception as exc:
                return {"status": "failed", "error": str(exc)}

        if step_type == "condition":
            field = str(payload.get("field", "")).strip()
            op = str(payload.get("op", "eq")).strip()
            expected = payload.get("value")
            then_step = payload.get("then")
            else_step = payload.get("else")

            context = flow.get("context", {})
            actual = context.get(field)

            match = False
            if op == "eq":
                match = actual == expected
            elif op == "ne":
                match = actual != expected
            elif op == "gt":
                match = (actual or 0) > (expected or 0)
            elif op == "lt":
                match = (actual or 0) < (expected or 0)
            elif op == "contains":
                match = str(expected) in str(actual)
            elif op == "truthy":
                match = bool(actual)

            branch = then_step if match else else_step
            return {
                "status": "completed",
                "match": match,
                "branch": branch,
                "field": field,
                "op": op,
            }

        return {"status": "failed", "error": f"Unsupported step type: {step_type}"}

    # ── L4 composition helpers ───────────────────────────────────────────

    # ``context.a.b`` is accepted alongside the two original tokens.
    # Without it, a workflow could only reference the immediately
    # preceding step or a step by index, so anything needing a value the
    # trigger supplied had nowhere to read it from. That is why
    # ``workflows/meeting_recap.json`` never worked: its templates
    # reference dotted context paths, they matched nothing, and the
    # literal ``{{ ... }}`` text was passed through to the step.
    _TEMPLATE_RE = re.compile(
        r"\{\{\s*(previous_output|step_(\d+)|context\.([A-Za-z0-9_.]+))\s*\}\}"
    )

    def _render_templates(self, value: Any, context: dict) -> Any:
        """Recursively render template tokens in strings of a payload."""
        if isinstance(value, str):
            return self._render_string(value, context)
        if isinstance(value, dict):
            return {k: self._render_templates(v, context) for k, v in value.items()}
        if isinstance(value, list):
            return [self._render_templates(v, context) for v in value]
        return value

    def _render_string(self, text: str, context: dict) -> str:
        if "{{" not in text:
            return text
        step_results = context.get("step_results") or {}

        def _repl(match: "re.Match") -> str:
            token = match.group(1)
            if token == "previous_output":
                return str(context.get("previous_output", "") or "")

            dotted = match.group(3)
            if dotted:
                return self._resolve_context_path(context, dotted)

            idx = match.group(2)
            entry = step_results.get(str(idx))
            if isinstance(entry, dict):
                return str(entry.get("output", "") or "")
            return str(entry or "")

        return self._TEMPLATE_RE.sub(_repl, text)

    @staticmethod
    def _resolve_context_path(context: dict, dotted: str) -> str:
        """Walk a dotted path through the run context, or return "".

        Missing keys resolve to the empty string rather than raising or
        leaving the literal token in place. A workflow that references
        something the trigger did not supply should degrade to a blank,
        not hand the model a ``{{ context.foo }}`` string to interpret.

        Mappings only. Attribute access would let a template reach into
        arbitrary Python objects reachable from the context, which is a
        much larger surface than a workflow author is asking for.
        """
        current: Any = context
        for part in dotted.split("."):
            if not isinstance(current, dict) or part not in current:
                return ""
            current = current[part]
        if current is None:
            return ""
        return current if isinstance(current, str) else json.dumps(current, default=str)

    @staticmethod
    def _step_output_text(outcome: dict) -> Optional[str]:
        """Best-effort text representation of a step's result, used as
        ``previous_output`` and the ``{{ step_N }}`` substitution value."""
        if not isinstance(outcome, dict):
            return None
        if outcome.get("output") is not None:
            return str(outcome["output"])
        for key in ("reply", "body_preview", "text"):
            if outcome.get(key) is not None:
                return str(outcome[key])
        result = outcome.get("result")
        if isinstance(result, dict) and result.get("data") is not None:
            data = result["data"]
            return data if isinstance(data, str) else json.dumps(data, default=str)
        if outcome.get("results") is not None:
            return json.dumps(outcome["results"], default=str)
        if outcome.get("note") is not None:
            return json.dumps(outcome["note"], default=str)
        return None

    def _record_flow_progress(self, flow: dict, step: int) -> None:
        """Advance the consciousness flow entity as the workflow progresses so
        'where did I leave off' surfaces the live step, not step 0."""
        try:
            from api.state import state as _state
            store = getattr(_state, "consciousness", None)
            if store is None:
                return
            store.record_flow(
                flow_id=flow["id"],
                title=flow.get("title", "") or "",
                step=step,
                steps=len(flow.get("steps", []) or []),
                session_id=flow.get("session_id", "") or "",
            )
        except Exception:
            pass

    @staticmethod
    def _flow_row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "title": row["title"],
            "status": row["status"],
            "current_step": row["current_step"],
            "context": json.loads(row["context_json"] or "{}"),
            "error": row["error"],
            "wait_until": row["wait_until"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _step_row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "flow_id": row["flow_id"],
            "step_index": row["step_index"],
            "step_type": row["step_type"],
            "payload": json.loads(row["payload_json"] or "{}"),
            "status": row["status"],
            "result": json.loads(row["result_json"] or "{}") if row["result_json"] else None,
            "error": row["error"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }
