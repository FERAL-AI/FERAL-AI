"""
FERAL TaskFlow Runtime
=======================
Persistent multi-step background flows with restart-safe state.
"""

from __future__ import annotations

import asyncio
import copy
from contextvars import ContextVar
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from enum import Enum
from typing import Optional, Any, Callable
from uuid import UUID, uuid4

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


class TaskFlowHandoffConflict(ValueError):
    """An accepted creation identity cannot be reused with changed terms."""


class TaskFlowReceiptError(ValueError):
    """A persisted origin or bounded read could not be verified."""


_model_step: ContextVar["TaskFlowModelStep | None"] = ContextVar(
    "feral_taskflow_model_step", default=None
)


def model_step_for(session_id, orchestrator):
    scope = _model_step.get()
    return (
        scope
        if scope is not None
        and scope.session_id == session_id
        and scope.runtime._orchestrator is orchestrator
        else None
    )


def redact_model_task_result(value, depth=0):
    """Exclude recognized credentials from model and durable result projections."""
    from observability.log_redaction import redact
    if depth > 32:
        return {"_truncated": True, "reason": "result_depth_limit"}
    if isinstance(value, dict):
        sensitive = {"token", "access_token", "refresh_token", "id_token", "api_key", "apikey",
                     "authorization", "password", "secret", "client_secret", "card_number",
                     "cvc", "cvv", "cookie", "cookies", "set_cookie", "session_cookie"}
        return {key: "<redacted>" if str(key).lower().replace("-", "_") in sensitive
                else redact_model_task_result(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_model_task_result(item, depth + 1) for item in value]
    return redact(value) if isinstance(value, str) else value


def _model_receipt_result(tool_name, result, registry):
    """Bound durable evidence without altering the original live model result."""
    result = redact_model_task_result(result)
    if len(json.dumps(result, allow_nan=False).encode()) <= 32768:
        return copy.deepcopy(result)
    from dataclasses import replace
    from skills.result_budget import budget_for_tool, serialize_tool_result_with_images
    budget = replace(budget_for_tool(tool_name, registry), max_result_chars=32768)
    blob, _ = serialize_tool_result_with_images(tool_name, result, registry=registry,
                                               budget=budget, allow_images=False)
    projection = json.loads(blob)
    if not isinstance(projection, dict):
        projection = {"data": projection}
    for key in ("success", "tool_outcome_verified"):
        if type(result.get(key)) is bool:
            projection[key] = result[key]
    projection["_receipt_projection"] = True
    return projection


class TaskFlowModelStep:
    """Typed observation of the existing runner; this grants no permission."""

    def __init__(self, runtime, flow, step, session_id):
        self.runtime, self.flow, self.step, self.session_id = (
            runtime,
            flow,
            step,
            session_id,
        )
        prior = step.get("result") or {}
        self.actions = copy.deepcopy(prior.get("model_actions", {}))
        self.calls = {}
        self.pending = None
        self.review_issued = False
        self.error = False
        self.unknown = False
        self.text = ""

    def _guard(self):
        flow = self.runtime.get_flow(self.flow["id"])
        step = (
            next((s for s in flow["steps"] if s["id"] == self.step["id"]), None)
            if flow
            else None
        )
        if (
            not flow
            or flow["status"] == "cancelled"
            or not step
            or step["status"] not in {"running", "waiting"}
            or self.runtime._stop_event.is_set()
        ):
            raise RuntimeError("Task model-step authority is no longer active")

    def admit_call(self, runner, call, surface):
        self._guard()
        from security.dangerous_tools import known_surfaces
        if surface not in known_surfaces() or surface != self.runtime.execution_surface_for_flow(self.flow["id"]):
            raise ValueError("Model action surface is unavailable or changed")
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 256:
            raise ValueError("A stable model action identity is required")
        if call.get("name", "").startswith("subagent__") or call.get("name") == "background_task__start":
            raise ValueError("Durable child model action ownership is unavailable")
        resource = (
            runner._capture_browser_resource(call["name"], self.session_id)
            if runner._browser_tool(call["name"])
            else None
        )
        terms = {
            "call_id": call_id,
            "session_id": self.session_id,
            "flow_id": self.flow["id"],
            "step_id": self.step["id"],
            "tool_name": call["name"],
            "args": call.get("args", {}),
            "resource": resource,
            "surface": surface,
        }
        encoded = json.dumps(
            terms, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if len(encoded.encode()) > 65536:
            raise ValueError("Model action terms exceed the checkpoint budget")
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        existing = self.actions.get(call_id)
        if existing is not None:
            if existing.get("terms_digest") != digest or not isinstance(
                existing.get("result"), dict
            ):
                raise ValueError("Recorded model action identity changed")
            return copy.deepcopy(existing["result"])
        if self.pending is not None:
            return {
                "success": False,
                "error_code": "task_waiting_approval",
                "status": "pending_approval",
                "request_id": self.pending["request_id"],
                "tool_name": self.pending["tool_name"],
                "session_id": self.session_id,
                "args": self.pending["args"],
            }
        if call_id in self.calls or len(self.actions) + len(self.calls) >= 32:
            raise ValueError("Model action identity is duplicated or at capacity")
        self.calls[call_id] = {"terms_digest": digest, "terms": terms}
        with self.runtime._lock:
            row = self.runtime._conn.execute(
                "SELECT result_json FROM taskflow_steps WHERE id=?", (self.step["id"],)
            ).fetchone()
            prior = json.loads(row[0]) if row and row[0] else {}
            prior["model_intents"] = self.calls
            self.runtime._conn.execute(
                "UPDATE taskflow_steps SET result_json=? WHERE id=? AND status='running'",
                (json.dumps(prior, allow_nan=False), self.step["id"]),
            )
            self.runtime._conn.commit()
        return None

    def bind_approval(self, runner, pending):
        self._guard()
        from skills.call_context import current_context

        call_id = current_context().call_id
        if call_id not in self.calls or self.pending is not None:
            raise RuntimeError("Model review is not bound to an admitted action")
        binding = {
            "flow_id": self.flow["id"],
            "step_id": self.step["id"],
            "model_call_id": call_id,
        }
        if pending.get("taskflow") not in (None, binding):
            raise RuntimeError("Model review belongs to another task")
        exact = copy.deepcopy(
            {
                key: pending[key]
                for key in ("request_id", "session_id", "tool_name", "args")
            }
        )
        review = {
            "status": "waiting",
            "reason": "approval_required",
            "approval": exact,
            "model_action": self.calls[call_id],
            "model_actions": self.actions,
        }
        encoded = json.dumps(review, allow_nan=False)
        with self.runtime._lock:
            changed = self.runtime._conn.execute(
                "UPDATE taskflow_steps SET status='waiting',result_json=?,finished_at=NULL WHERE id=? AND status='running' AND EXISTS(SELECT 1 FROM taskflows WHERE id=? AND status='running')",
                (encoded, self.step["id"], self.flow["id"]),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Model review checkpoint was superseded")
            self.runtime._conn.execute(
                "UPDATE taskflows SET status='waiting',wait_until=NULL,updated_at=? WHERE id=? AND status!='cancelled'",
                (time.time(), self.flow["id"]),
            )
            self.runtime._conn.commit()
        pending["taskflow"] = binding
        self.pending, self.review_issued = exact, True

    def observe_result(self, call, result):
        if not isinstance(result, dict):
            self.error = True
            return
        if result.get("status") == "pending_approval":
            if (
                self.pending is None
                or result.get("request_id") != self.pending["request_id"]
            ):
                self.error = True
            return
        if result.get("error_code") == "task_waiting_approval":
            return
        if (
            result.get("status") == "outcome_unknown"
            or result.get("outcome") == "unknown"
        ):
            self.unknown = True
        elif result.get("success") is not True:
            self.error = True
        call_id = call.get("id")
        if call_id not in self.calls:
            return  # An exact cached receipt, not another dispatch.
        record = {**self.calls[call_id], "result": _model_receipt_result(call["name"], result, self.runtime._skill_registry)}
        if len(json.dumps(record, allow_nan=False).encode()) > 65536:
            self.unknown = True
            return
        self.actions[call_id] = record
        self.calls.pop(call_id)
        # Keep verified returns before any further model computation. A crash
        # while the step is running still requires reconciliation, never replay.
        with self.runtime._lock:
            row = self.runtime._conn.execute(
                "SELECT result_json FROM taskflow_steps WHERE id=?", (self.step["id"],)
            ).fetchone()
            prior = json.loads(row[0]) if row and row[0] else {}
            prior["model_actions"] = self.actions
            self.runtime._conn.execute(
                "UPDATE taskflow_steps SET result_json=? WHERE id=?",
                (json.dumps(prior, allow_nan=False), self.step["id"]),
            )
            self.runtime._conn.commit()


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
        self._flow_tasks: dict[str, asyncio.Task] = {}
        self._step_tasks: dict[str, asyncio.Task] = {}
        self._max_active_flows = 4
        self._ready_scan_offset = 0
        self._wake_event = asyncio.Event()
        self._effect_owner: Optional[tuple[str, int]] = None
        self._effect_depth = 0
        self._lane_changed = asyncio.Event()
        self._lane_changed.set()
        self._approved_lane_owners: dict[str, tuple[str, int]] = {}
        self._approved_tasks: dict[str, asyncio.Task] = {}
        self._review_publications: set[asyncio.Task] = set()
        self._stop_event = asyncio.Event()
        self._http = httpx.AsyncClient(timeout=20.0)
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
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
            # Optional creation identity lives with the flow, not a second runner.
            columns = {r[1] for r in conn.execute("PRAGMA table_info(taskflows)")}
            for name in ("handoff_key", "terms_digest", "origin_session_id", "origin_surface"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE taskflows ADD COLUMN {name} TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_taskflows_handoff ON taskflows(handoff_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_taskflows_origin ON taskflows(origin_session_id, updated_at)")
            conn.commit()

    async def start(self):
        if self._runner_task and not self._runner_task.done():
            return
        if any(not task.done() for task in
               [*self._flow_tasks.values(), *self._step_tasks.values(), *self._approved_tasks.values()]):
            raise RuntimeError("Cannot recover TaskFlow while an owned dispatch is active")
        self._recover_after_restart()
        self._stop_event.clear()
        if self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=20.0)
        self._runner_task = asyncio.create_task(self._runner_loop())
        logger.info("TaskFlow runtime started")

    async def stop(self):
        self._stop_event.set()
        self._wake_event.set()
        if self._runner_task:
            self._runner_task.cancel()
            try:
                await self._runner_task
            except asyncio.CancelledError:
                pass
            self._runner_task = None
        active = [task for task in {*self._flow_tasks.values(), *self._step_tasks.values(),
                                    *self._approved_tasks.values(), *self._review_publications}
                  if task is not asyncio.current_task() and not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
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
        handoff_key: Optional[str] = None,
        terms_digest: Optional[str] = None,
        origin_session_id: Optional[str] = None,
        origin_surface: Optional[str] = None,
        creation_guard: Optional[Callable[[], None]] = None,
    ) -> dict:
        if not steps:
            raise ValueError("TaskFlow requires at least one step")
        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"Invalid step at index {idx}")
            if not step.get("type"):
                raise ValueError(f"Missing step.type at index {idx}")
        if (handoff_key is None) != (terms_digest is None):
            raise ValueError("TaskFlow handoff requires identity and terms digest")
        if handoff_key is not None and any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in (handoff_key, terms_digest)
        ):
            raise ValueError("Invalid TaskFlow handoff identity")
        if origin_session_id is not None:
            from security.session_identity import validate_session_id
            validate_session_id(origin_session_id)
        if origin_surface is not None:
            from security.dangerous_tools import known_surfaces
            if origin_surface not in known_surfaces():
                raise ValueError("Invalid TaskFlow origin surface")

        now = time.time()
        flow_id = str(uuid4())[:12]
        replayed = False
        retry_deadline = time.monotonic() + 5.0
        while True:
            busy = False
            if creation_guard is not None:
                creation_guard()
            with self._lock:
                conn = self._conn
                timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
                conn.execute("PRAGMA busy_timeout = 0")
                try:
                    # A competing SQLite writer must not hold the mutex while
                    # the active supervisor is trying to inspect ready flows.
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                    except sqlite3.OperationalError as exc:
                        if (getattr(exc, "sqlite_errorcode", 0) & 255) != sqlite3.SQLITE_BUSY or time.monotonic() >= retry_deadline:
                            raise
                        busy = True
                    if not busy:
                        if creation_guard is not None:
                            creation_guard()
                        existing = conn.execute(
                            "SELECT id, terms_digest, origin_session_id, origin_surface FROM taskflows WHERE handoff_key = ?", (handoff_key,),
                        ).fetchone() if handoff_key is not None else None
                        if existing is not None:
                            if (existing["terms_digest"] != terms_digest or existing["origin_session_id"] != origin_session_id
                                    or existing["origin_surface"] != origin_surface):
                                raise TaskFlowHandoffConflict("TaskFlow handoff terms changed")
                            flow_id, replayed = existing["id"], True
                        else:
                            conn.execute(
                                """INSERT INTO taskflows
                                (id, session_id, title, status, current_step, context_json, created_at, updated_at, handoff_key, terms_digest, origin_session_id, origin_surface)
                                VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)""",
                                (flow_id, session_id, title or f"TaskFlow {flow_id}", TaskFlowStatus.QUEUED.value,
                                 json.dumps(context or {}), now, now, handoff_key, terms_digest, origin_session_id, origin_surface),
                            )
                            for i, step in enumerate(steps):
                                payload = dict(step)
                                payload.pop("type", None)
                                conn.execute(
                                    """INSERT INTO taskflow_steps
                                    (flow_id, step_index, step_type, payload_json, status)
                                    VALUES (?, ?, ?, ?, 'pending')""",
                                    (flow_id, i, step["type"], json.dumps(payload)),
                                )
                        conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                finally:
                    conn.execute(f"PRAGMA busy_timeout = {timeout}")
            if not busy:
                break
            # This adapter runs on a retained worker. Never sleep with the
            # runtime mutex held; cancellation is rechecked before every retry.
            if creation_guard is not None:
                creation_guard()
            time.sleep(0.025)
        # The adapter may create on a worker thread. Wake only on a live loop.
        owner_loop = self._runner_task.get_loop() if self._runner_task is not None else None
        if owner_loop is not None and not owner_loop.is_closed():
            owner_loop.call_soon_threadsafe(self._wake_event.set)
        else:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass  # No running supervisor has a wake waiter yet.
            else:
                self._wake_event.set()
        flow = self.get_flow(flow_id)
        if flow is None:
            raise RuntimeError("TaskFlow creation readback unavailable")
        if handoff_key is not None:
            flow["handoff_replayed"] = replayed
        if replayed:
            return flow
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

    def origin_session_for_flow(self, flow_id: str) -> Optional[str]:
        """Read creation-owned attribution, never a caller's context claim."""
        with self._lock:
            row = self._conn.execute("SELECT origin_session_id FROM taskflows WHERE id = ?", (flow_id,)).fetchone()
        return row[0] if row is not None else None

    def origin_surface_for_flow(self, flow_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT origin_surface FROM taskflows WHERE id = ?", (flow_id,)).fetchone()
        return row[0] if row is not None else None

    def execution_surface_for_flow(self, flow_id: str) -> str:
        """Use creation-owned scope; historical unowned work has a cron floor.

        Context/body claims and remembered session permissions cannot upgrade
        a persisted workflow. Legacy safe reads remain available; interactive
        effects require a newly admitted known originating surface.
        """
        from security.dangerous_tools import known_surfaces
        surface = self.origin_surface_for_flow(flow_id)
        if surface is None:
            return "cron"
        if surface not in known_surfaces():
            raise RuntimeError("Workflow execution surface is unavailable")
        return surface

    def list_origin_flows(self, origin_session_id: str, *, limit: int = 50) -> list[dict]:
        from security.session_identity import validate_session_id
        validate_session_id(origin_session_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM taskflows WHERE origin_session_id = ? ORDER BY updated_at DESC LIMIT ?",
                (origin_session_id, max(1, min(limit, 100))),
            ).fetchall()
        return [self._flow_row_to_dict(row) for row in rows]

    def _origin_receipt_snapshot(self, origin_session_id: str, *, flow_id=None,
                                 limit=20, cursor=None, guard=lambda: None):
        """Use a separate read-only snapshot; never wait with the runner mutex.

        The gateway retains the worker and supplies its current-reader guard.
        Busy retries are bounded and cannot dispatch or mutate task state.
        """
        from security.session_identity import validate_session_id
        validate_session_id(origin_session_id)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise TaskFlowReceiptError("Task receipt limit unavailable")
        if flow_id is not None and (not isinstance(flow_id, str) or not 1 <= len(flow_id) <= 128
                                    or any(ord(c) < 32 or ord(c) == 127 for c in flow_id)):
            raise TaskFlowReceiptError("Task identity unavailable")
        if cursor is not None and (not isinstance(cursor, dict) or set(cursor) != {"created_at", "flow_id"}
                or type(cursor.get("created_at")) not in (int, float)
                or not 0 <= cursor["created_at"] <= 253402300799 or not math.isfinite(cursor["created_at"])
                or not isinstance(cursor.get("flow_id"), str) or not 1 <= len(cursor["flow_id"]) <= 128
                or any(ord(c) < 32 or ord(c) == 127 for c in cursor["flow_id"])):
            raise TaskFlowReceiptError("Task discovery cursor unavailable")
        deadline = time.monotonic() + 2.0
        while True:
            guard()
            conn = sqlite3.connect(Path(self._db_path).resolve().as_uri() + "?mode=ro",
                                   uri=True, timeout=0)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("BEGIN")
                params: list[Any] = [origin_session_id]
                query = "SELECT * FROM taskflows WHERE origin_session_id = ? AND handoff_key IS NOT NULL"
                if flow_id is not None:
                    query += " AND id = ?"
                    params.append(flow_id)
                elif cursor is not None:
                    query += " AND (created_at > ? OR (created_at = ? AND id > ?))"
                    params.extend([cursor["created_at"], cursor["created_at"], cursor["flow_id"]])
                query += " ORDER BY created_at ASC, id ASC LIMIT ?"
                params.append(1 if flow_id is not None else limit + 1)
                rows = conn.execute(query, params).fetchall()
                receipts = []
                for row in rows[:limit]:
                    guard()
                    # A malformed ordering key cannot produce a cursor the
                    # reader would reject or safely advance on reconnect.
                    if (type(row["created_at"]) not in (int, float) or not 0 <= row["created_at"] <= 253402300799
                            or not math.isfinite(row["created_at"]) or not isinstance(row["id"], str)
                            or not 1 <= len(row["id"]) <= 128 or any(ord(c) < 32 or ord(c) == 127 for c in row["id"])):
                        raise TaskFlowReceiptError("Task discovery ordering unavailable")
                    steps = conn.execute("SELECT * FROM taskflow_steps WHERE flow_id = ? ORDER BY step_index",
                                         (row["id"],)).fetchall()
                    try:
                        receipts.append(self._origin_receipt_projection(row, steps))
                    except TaskFlowReceiptError:
                        # Invalid rows supply no authority or private output.
                        # Discovery still advances over the scanned SQL page.
                        continue
                guard()
                scanned = rows[:limit]
                next_cursor = ({"created_at": scanned[-1]["created_at"], "flow_id": scanned[-1]["id"]}
                               if scanned else cursor)
                return receipts, len(rows) > limit, next_cursor
            except sqlite3.OperationalError as exc:
                if ((getattr(exc, "sqlite_errorcode", 0) & 255) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                        or time.monotonic() >= deadline):
                    raise TaskFlowReceiptError("Task receipts unavailable") from None
            finally:
                conn.close()
            guard()
            time.sleep(0.025)

    def read_origin_receipt(self, origin_session_id: str, flow_id: str, *, guard=lambda: None):
        rows, _, _ = self._origin_receipt_snapshot(origin_session_id, flow_id=flow_id, guard=guard)
        return rows[0] if rows else None

    def read_origin_receipts(self, origin_session_id: str, *, limit=20, cursor=None, guard=lambda: None):
        rows, more, next_cursor = self._origin_receipt_snapshot(origin_session_id, limit=limit, cursor=cursor, guard=guard)
        return {"receipts": rows, "next_cursor": next_cursor, "has_more": more}

    @staticmethod
    def _origin_receipt_projection(row, steps):
        """Project recorded processing, never an inferred external outcome."""
        from security.dangerous_tools import known_surfaces
        try:
            context = json.loads(row["context_json"])
            origin = context["task_origin"]
            ids = [origin[k] for k in ("session_id", "request_id", "turn_id", "tool_call_id")]
            if (type(origin.get("contract_version")) is not int or origin["contract_version"] != 1
                    or origin.get("source") != "tracked_chat_turn" or origin.get("owner_verified") is not True
                    or ids[0] != row["origin_session_id"] or origin.get("surface") != row["origin_surface"]
                    or origin["surface"] not in known_surfaces() or row["session_id"] != ""
                    or any(not isinstance(v, str) or not v or len(v) > 256 for v in ids)
                    or str(UUID(ids[1])) != ids[1] or str(UUID(ids[2])) != ids[2]
                    or origin.get("handoff_key") != row["handoff_key"]
                    or origin.get("terms_digest") != row["terms_digest"]):
                raise ValueError()
            identity = hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest()
            if identity != row["handoff_key"]:
                raise ValueError()
            # The accepted adapter can substitute title for an empty goal in
            # context. Check both original possibilities, not a guessed goal.
            creation_steps = []
            if (not 1 <= len(steps) <= 32 or type(row["current_step"]) is not int
                    or not 0 <= row["current_step"] <= len(steps)):
                raise ValueError()
            for index, step in enumerate(steps):
                payload = json.loads(step["payload_json"])
                if (step["step_type"] != "llm.chat" or set(payload) != {"prompt"}
                        or step["step_index"] != index or not isinstance(payload["prompt"], str)
                        or not 1 <= len(payload["prompt"]) <= 16384
                        or step["status"] not in {"pending", "running", "waiting", "completed", "failed", "outcome_unknown"}):
                    raise ValueError()
                creation_steps.append({"type": "llm.chat", **payload})
            terms = {"goal": context["goal"], "title": row["title"], "session_id": "",
                     "steps": creation_steps, "origin_surface": row["origin_surface"]}
            possible = []
            for goal in (terms["goal"], ""):
                possible.append(hashlib.sha256(json.dumps({**terms, "goal": goal}, sort_keys=True,
                    separators=(",", ":"), ensure_ascii=False).encode()).hexdigest())
            if row["terms_digest"] not in possible or not steps:
                raise ValueError()
            if row["status"] not in {s.value for s in TaskFlowStatus}:
                raise ValueError()
            for field in ("created_at", "updated_at"):
                if (type(row[field]) not in (int, float) or not math.isfinite(row[field])
                        or not 0 <= row[field] <= 253402300799):
                    raise ValueError()
            result_present, text, result_index = False, None, None
            recorded = [s for s in steps if s["result_json"] is not None and s["status"] == "completed"]
            if recorded:
                latest = max(recorded, key=lambda s: (s["finished_at"] or 0, s["id"]))
                outcome = json.loads(latest["result_json"])
                if not isinstance(outcome, dict):
                    raise ValueError()
                for key in ("output", "reply", "text"):
                    if key in outcome:
                        value = outcome[key]
                        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                        result_present, result_index = True, latest["step_index"]
                        break
            truncated = False
            if text is not None:
                encoded = text.encode("utf-8")
                truncated = len(encoded) > 4096
                text = encoded[:4096].decode("utf-8", errors="ignore")
            outcomes = [json.loads(s["result_json"]) if s["result_json"] else {} for s in steps]
            unknown = any(s["status"] == "outcome_unknown" for s in steps)
            awaiting = any(isinstance(o, dict) and o.get("reason") == "approval_required" for o in outcomes)
            processing = ("outcome_unknown" if unknown else "awaiting_approval" if awaiting else
                          "in_progress" if row["status"] in ("queued", "running") else row["status"])
            projection = {"contract_version": 1, "flow_id": row["id"], "origin_session_id": row["origin_session_id"],
                "title": row["title"][:120], "status": row["status"], "current_step": row["current_step"],
                "steps_total": len(steps), "steps_completed": sum(s["status"] == "completed" for s in steps),
                "processing_outcome": processing, "action_outcome": "not_asserted", "result_present": result_present,
                "result_text": text, "result_truncated": truncated, "result_step_index": result_index,
                "error_code": "task_outcome_unknown" if unknown else "task_processing_failed" if row["status"] == "failed" else None,
                "created_at": row["created_at"], "updated_at": row["updated_at"]}
            projection["result_digest"] = hashlib.sha256(json.dumps(projection, sort_keys=True,
                separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            return projection
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise TaskFlowReceiptError("Task receipt origin or result unavailable") from None

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
        if flow["status"] in (TaskFlowStatus.COMPLETED.value, TaskFlowStatus.CANCELLED.value,
                              TaskFlowStatus.RUNNING.value):
            return flow
        if flow["status"] == "waiting" and 0 <= flow["current_step"] < len(flow["steps"]):
            step = flow["steps"][flow["current_step"]]
            if step["step_type"] == "llm.chat" and step["status"] == "waiting" and (step.get("result") or {}).get("reason") == "approval_required":
                return self._renew_model_review(flow, step)
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
        self._wake_event.set()
        return self.get_flow(flow_id)

    def _renew_model_review(self, flow, step):
        """Explicit Resume renews a lost review, never an action or model goal."""
        runner = getattr(self._orchestrator, "tool_runner", None)
        prior = step.get("result") or {}
        exact = prior.get("approval") or {}
        action = prior.get("model_action") or {}
        terms = action.get("terms") or {}
        binding = {"flow_id": flow["id"], "step_id": step["id"], "model_call_id": terms.get("call_id")}
        candidate = {**exact, "taskflow": binding, "browser_resource": terms.get("resource")}
        def refused(code):
            current = self.get_flow(flow["id"])
            return {**(current or flow), "review_renewal": {"status": "refused", "error_code": code}}
        if runner is None or runner._orch is not self._orchestrator or self._stop_event.is_set() or len(self._review_publications) >= 32:
            return refused("task_review_runtime_unavailable")
        if self._approval_step(candidate)[1] is None or any(s["status"] in {"running", "outcome_unknown"} for s in flow["steps"]):
            return refused("task_review_checkpoint_invalid")
        current = runner.get_pending(exact.get("request_id", ""))
        if current is not None and runner.approval_scope_for(current) is not None:
            return flow
        from security.dangerous_tools import known_surfaces
        if terms.get("surface") not in known_surfaces():
            return refused("task_review_surface_unavailable")
        pending = None
        committed = False
        try:
            loop = asyncio.get_running_loop()
            with self._lock:
                raw = self._conn.execute("SELECT result_json FROM taskflow_steps WHERE id=?", (step["id"],)).fetchone()[0]
            resource = runner._capture_browser_resource(exact["tool_name"], exact["session_id"]) if runner._browser_tool(exact["tool_name"]) else None
            if resource != terms.get("resource"):
                return refused("task_review_resource_changed")
            if runner.enforce_plan_mode(exact["tool_name"], exact["session_id"]):
                return refused("task_review_policy_denied")
            runner.deny_pending(exact["request_id"], session_id=exact["session_id"])
            with bind_context(session_id=exact["session_id"], surface=terms["surface"], tool_name=exact["tool_name"], call_id=terms["call_id"]):
                pending = runner.enforce_safety(exact["tool_name"], exact["args"], session_id=exact["session_id"], surface=terms["surface"], _require_review=True)
            if not pending or pending.get("status") != "pending_approval" or pending["request_id"] == exact["request_id"]:
                return refused("task_review_policy_denied")
            pending["taskflow"] = binding
            new_review = {**prior, "approval": {k: copy.deepcopy(pending[k]) for k in ("request_id", "session_id", "tool_name", "args")}}
            with self._lock:
                changed = self._conn.execute("UPDATE taskflow_steps SET result_json=? WHERE id=? AND status='waiting' AND result_json=? AND EXISTS(SELECT 1 FROM taskflows WHERE id=? AND status='waiting')",
                    (json.dumps(new_review, allow_nan=False), step["id"], raw, flow["id"])).rowcount
                self._conn.commit()
            if not changed:
                return refused("task_review_superseded")
            committed = True
            publication = loop.create_task(runner._notify_user_of_pending_approval(pending["session_id"], pending["tool_name"], pending))
            self._review_publications.add(publication)
            def settled(task):
                self._review_publications.discard(task)
                if not task.cancelled() and task.exception() is not None:
                    logger.warning("Renewed task review publication failed")
            publication.add_done_callback(settled)
            return {**self.get_flow(flow["id"]), "review_renewal": {"status": "waiting", "request_id": pending["request_id"]}}
        except Exception:
            logger.warning("Task review renewal refused; no action dispatched")
            return refused("task_review_unavailable")
        finally:
            if not committed and isinstance(pending, dict) and pending.get("status") == "pending_approval":
                runner.deny_pending(pending["request_id"], session_id=pending["session_id"])

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
        step_task = self._step_tasks.get(flow_id)
        if step_task and not step_task.done():
            step_task.cancel()
        elif flow_id in self._flow_tasks:
            self._flow_tasks[flow_id].cancel()
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

    def _independent_step(self, step: dict) -> bool:
        """Only proven local reads/computation bypass the shared resource lane.

        Browser, desktop, device, HTTP and model turns remain serialized even
        when a manifest calls them read-only. Their resource ownership is not
        yet a durable workflow contract. A skill name alone grants no lane.
        """
        if step["step_type"] in {"noop", "condition", "sleep", "memory.search"}:
            return True
        raw = step.get("payload") or {}
        if not isinstance(raw, dict):
            return False
        payload = raw.get("config", raw)
        return bool(step["step_type"] == "skill.invoke" and isinstance(payload, dict)
                    and payload.get("skill_id") == "notes_memory"
                    and isinstance(payload.get("args", {}), dict)
                    and self._restart_safe(step))

    def _claim_effect_lane(self, owner: tuple[str, int]) -> bool:
        if self._effect_owner not in (None, owner):
            return False
        self._effect_owner = owner
        self._effect_depth += 1
        self._lane_changed.clear()
        return True

    def _release_effect_lane(self, owner: tuple[str, int]) -> None:
        if self._effect_owner != owner:
            return
        self._effect_depth -= 1
        if self._effect_depth == 0:
            self._effect_owner = None
            self._lane_changed.set()
            self._wake_event.set()

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
            row = self._conn.execute("SELECT result_json FROM taskflow_steps WHERE id=?", (step["id"],)).fetchone()
            prior = json.loads(row[0]) if row and row[0] else {}
            for key in ("model_actions", "model_intents", "model_action"):
                if key in prior:
                    outcome[key] = prior[key]
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
        if step is None or not approval or approval != exact:
            return None, None
        if step["step_type"] == "llm.chat":
            action = (step.get("result") or {}).get("model_action") or {}
            terms = action.get("terms") or {}
            try:
                digest = hashlib.sha256(json.dumps(terms, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            except (TypeError, ValueError):
                return None, None
            if (terms.get("flow_id") != flow["id"] or terms.get("step_id") != step["id"]
                    or terms.get("call_id") != binding.get("model_call_id")
                    or terms.get("session_id") != pending.get("session_id")
                    or terms.get("tool_name") != pending.get("tool_name") or terms.get("args") != pending.get("args")
                    or terms.get("resource") != pending.get("browser_resource")
                    or action.get("terms_digest") != digest):
                return None, None
            from security.dangerous_tools import known_surfaces
            try:
                if terms.get("surface") not in known_surfaces() or terms["surface"] != self.execution_surface_for_flow(flow["id"]):
                    return None, None
            except RuntimeError:
                return None, None
        elif step["step_type"] == "skill.invoke":
            try:
                if step["result"].get("dispatch_surface") != self.execution_surface_for_flow(flow["id"]):
                    return None, None
            except RuntimeError:
                return None, None
        return flow, step

    def approved_dispatch_surface(self, pending):
        flow, step = self._approval_step(pending)
        if not flow or not step:
            raise RuntimeError("Workflow action surface is unavailable")
        return step["result"]["model_action"]["terms"]["surface"] if step["step_type"] == "llm.chat" else self.execution_surface_for_flow(flow["id"])

    def prepare_approved_dispatch(self, pending: dict) -> bool:
        """Claim the existing review once, before its central execution."""
        flow, step = self._approval_step(pending)
        if (not flow or flow["status"] != "waiting" or step["status"] != "waiting"
                or self._stop_event.is_set()
                or (self._supervisor is not None and self._supervisor.paused)):
            return False
        runner = getattr(self._orchestrator, "tool_runner", None)
        if runner is None or runner.enforce_plan_mode(pending["tool_name"], pending["session_id"]):
            return False
        retained = set(self._flow_tasks) | set(self._step_tasks) | set(self._approved_tasks)
        if flow["id"] not in retained and len(retained) >= self._max_active_flows:
            return False
        owner = (flow["id"], step["id"])
        if not self._claim_effect_lane(owner):
            return False
        claimed = False
        try:
            with self._lock:
                changed = self._conn.execute("UPDATE taskflow_steps SET status = 'running', started_at = ?, finished_at = NULL WHERE id = ? AND status = 'waiting'",
                                             (time.time(), step["id"])).rowcount
                if not changed:
                    return False
                self._conn.execute("UPDATE taskflows SET status = 'running', updated_at = ? WHERE id = ? AND status = 'waiting'",
                                   (time.time(), flow["id"]))
                self._conn.commit()
            claimed = True
            self._approved_lane_owners[flow["id"]] = owner
        finally:
            if not claimed:
                self._release_effect_lane(owner)
        task = asyncio.current_task()
        if task is not None:
            self._approved_tasks[flow["id"]] = task
        return True

    def approved_dispatch_allowed(self, pending: dict) -> bool:
        flow, step = self._approval_step(pending)
        return bool(flow and flow["status"] == "running" and step["status"] == "running"
                    and not self._stop_event.is_set()
                    and self._approved_lane_owners.get(flow["id"]) == (flow["id"], step["id"])
                    and not (self._supervisor is not None and self._supervisor.paused))

    async def finish_approved_dispatch(self, pending: dict, result: Optional[dict] = None,
                                       *, rejected: bool = False, uncertain: bool = False):
        flow_id = (pending.get("taskflow") or {}).get("flow_id", "")
        flow, step = self._approval_step(pending)
        expected_owner = (flow_id, step["id"]) if flow and step else None
        try:
            return await self._finish_approved_dispatch(pending, result, rejected=rejected,
                                                        uncertain=uncertain)
        finally:
            owner = self._approved_lane_owners.get(flow_id)
            if owner is not None and owner == expected_owner:
                assert owner is not None
                self._approved_lane_owners.pop(flow_id, None)
                self._release_effect_lane(owner)
            self._wake_event.set()

    async def _finish_approved_dispatch(self, pending: dict, result: Optional[dict] = None,
                                       *, rejected: bool = False, uncertain: bool = False):
        """Persist the actual dispatcher response, never dispatch again."""
        flow, step = self._approval_step(pending)
        binding = pending.get("taskflow") or {}
        if not flow or step["status"] not in {"waiting", "running"}:
            return False
        self._approved_tasks.pop(binding.get("flow_id", ""), None)
        if uncertain or flow["status"] == "cancelled":
            self._mark_unknown(flow, step, "Approval dispatch interrupted; external effect outcome is unknown")
            return True
        result = result or {}
        success = not rejected and result.get("success") is True
        error = None if success else ("Approval rejected" if rejected else str(result.get("error") or result.get("note") or "Dispatcher refused the workflow action"))
        if step["step_type"] == "llm.chat" and success:
            prior = step.get("result") or {}
            action = prior.get("model_action")
            call_id = binding.get("model_call_id")
            if not isinstance(action, dict) or action.get("terms", {}).get("call_id") != call_id:
                self._mark_unknown(flow, step, "Approved model action checkpoint is unavailable")
                return True
            actions = dict(prior.get("model_actions") or {})
            record = {**action, "result": _model_receipt_result(pending["tool_name"], result, self._skill_registry)}
            if len(json.dumps(record, allow_nan=False).encode()) > 65536:
                self._mark_unknown(flow, step, "Approved model action result exceeds checkpoint budget")
                return True
            actions[call_id] = record
            continuation = {"status": "pending", "reason": "model_continuation", "model_actions": actions}
            with self._lock:
                changed = self._conn.execute("UPDATE taskflow_steps SET status='pending',result_json=?,error=NULL,finished_at=NULL WHERE id=? AND status='running'", (json.dumps(continuation, allow_nan=False), step["id"])).rowcount
                if changed:
                    self._conn.execute("UPDATE taskflows SET status='queued',error=NULL,wait_until=NULL,updated_at=? WHERE id=? AND status!='cancelled'", (time.time(), flow["id"]))
                    self._conn.commit()
            return bool(changed)
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
            self._wake_event.clear()
            while len(set(self._flow_tasks) | set(self._approved_tasks)) < self._max_active_flows:
                flow_id = self._next_ready_flow_id()
                if not flow_id:
                    break
                task = asyncio.create_task(self._run_retained_flow(flow_id))
                self._flow_tasks[flow_id] = task
                # Let the persisted flow/step claim and resource lane settle
                # before admitting another candidate from the queue.
                await asyncio.sleep(0)
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                continue

    async def _run_retained_flow(self, flow_id: str):
        try:
            await self._run_flow(flow_id, single_step=True)
        except Exception as exc:
            logger.error("TaskFlow runner interrupted (%s, %s)", flow_id, type(exc).__name__)
        finally:
            self._flow_tasks.pop(flow_id, None)
            self._wake_event.set()

    def _next_ready_flow_id(self) -> Optional[str]:
        now = time.time()
        with self._lock:
            conn = self._conn
            rows = conn.execute(
                """
                SELECT id
                FROM taskflows
                WHERE status = ?
                   OR (status = ? AND wait_until IS NOT NULL AND wait_until <= ?)
                ORDER BY updated_at ASC, id ASC
                LIMIT 200 OFFSET ?
                """,
                (TaskFlowStatus.QUEUED.value, TaskFlowStatus.WAITING.value, now,
                 self._ready_scan_offset),
            ).fetchall()
        for row in rows:
            flow_id = row["id"]
            if flow_id in self._flow_tasks or flow_id in self._approved_tasks:
                continue
            flow = self.get_flow(flow_id)
            if flow is None:
                continue
            index = int(flow["current_step"])
            if (self._effect_owner is not None and index < len(flow["steps"])
                    and not self._independent_step(flow["steps"][index])):
                continue
            self._ready_scan_offset = 0
            return flow_id
        # Bound each queue scan, but keep a blocked first page from starving
        # independent reads further down the persisted queue. Polling yields
        # between pages; no extra action or task is admitted by this cursor.
        if len(rows) == 200:
            self._ready_scan_offset += len(rows)
            self._wake_event.set()
        else:
            self._ready_scan_offset = 0
        return None

    async def _run_flow(self, flow_id: str, *, single_step: bool = False):
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
            changed = conn.execute(
                """
                UPDATE taskflows
                SET status = ?, started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE id = ? AND (status = 'queued' OR
                    (status = 'waiting' AND wait_until IS NOT NULL AND wait_until <= ?))
                """,
                (TaskFlowStatus.RUNNING.value, now, now, flow_id, now),
            ).rowcount
            conn.commit()
        if not changed:
            return

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
            lane_owner = None
            if not self._independent_step(step):
                lane_owner = (flow_id, step_id)
                while not self._claim_effect_lane(lane_owner):
                    await self._lane_changed.wait()
                latest = self.get_flow(flow_id)
                if self._stop_event.is_set() or latest is None or latest["status"] == "cancelled":
                    self._release_effect_lane(lane_owner)
                    return
            try:
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
            except BaseException:
                if lane_owner is not None:
                    self._release_effect_lane(lane_owner)
                raise

            step_task = asyncio.create_task(self._execute_step(flow, step))
            self._step_tasks[flow_id] = step_task
            try:
                outcome = await step_task
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
                if self._step_tasks.get(flow_id) is step_task:
                    self._step_tasks.pop(flow_id, None)
                if lane_owner is not None:
                    self._release_effect_lane(lane_owner)
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

            if status == "outcome_unknown":
                self._mark_unknown(flow, step, "Model task outcome requires reconciliation")
                return
            if status not in {"completed", "failed"}:
                outcome = {"status": "failed", "error": "Workflow returned an unsupported processing outcome", "dispatch_started": False}
                status = "failed"
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
            if single_step and current_step < len(steps):
                with self._lock:
                    self._conn.execute("UPDATE taskflows SET status = 'queued' WHERE id = ? AND status = 'running'",
                                       (flow_id,))
                    self._conn.commit()
                return

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
            try:
                surface = self.execution_surface_for_flow(flow["id"])
            except RuntimeError:
                return {"status": "failed", "error": "Workflow execution surface is unavailable", "dispatch_started": False}
            if runner is None:
                # Explain an explicit deny for legacy headless callers, but
                # never substitute direct skill execution for full authority.
                from security.safety_resolver import resolve_policy, LEVEL_DENY
                policy = resolve_policy(tool_name, args, surface=surface, registry=self._skill_registry)
                error = (f"denied by safety policy: {policy.deny_reason}" if policy.level == LEVEL_DENY
                         else "Full policy dispatcher unavailable; no skill executed")
                return {"status": "failed", "error": error, "dispatch_started": False}
            if self._supervisor is not None and self._supervisor.paused:
                return {"status": "failed", "error": "Agent supervisor is paused", "dispatch_started": False}
            session_id = flow.get("session_id") or f"taskflow-{flow['id']}"
            call_id = f"taskflow:{flow['id']}:{step['id']}"
            with bind_context(session_id=session_id, surface=surface, call_id=call_id):
                refusal = runner.enforce_plan_mode(tool_name, session_id)
                if refusal is None:
                    refusal = runner.enforce_safety(tool_name, args, session_id=session_id, surface=surface)
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
                    review = {"status": "waiting", "reason": "approval_required", "approval": approval, "dispatch_surface": surface}
                    with self._lock:
                        self._conn.execute("UPDATE taskflow_steps SET status = 'waiting', result_json = ?, finished_at = NULL WHERE id = ?", (json.dumps(review), step["id"]))
                        self._conn.execute("UPDATE taskflows SET status = 'waiting', wait_until = NULL, updated_at = ? WHERE id = ? AND status != 'cancelled'", (time.time(), flow["id"]))
                        self._conn.commit()
                    # Persist identity/action binding before publishing the
                    # existing review, including an immediate user response.
                    await runner._notify_user_of_pending_approval(session_id, tool_name, refusal)
                    return {"status": "deferred"}
                result = await runner.execute_tool_call_for_llm(
                    session_id, {"id": call_id, "name": tool_name, "args": args}, [], surface=surface
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
            scope = None
            try:
                origin = flow.get("context", {}).get("task_origin", {})
                command_context = {"surface": self.execution_surface_for_flow(flow["id"])}
                if isinstance(origin, dict) and origin.get("source") in {"tracked_chat_turn", "legacy_agent_context"}:
                    from security.dangerous_tools import known_surfaces
                    with self._lock:
                        stored = self._conn.execute(
                            "SELECT handoff_key, terms_digest, origin_session_id, origin_surface, context_json FROM taskflows WHERE id = ?",
                            (flow["id"],),
                        ).fetchone()
                    if (stored is None or not stored["origin_surface"]
                            or origin.get("contract_version") != 1
                            or origin.get("session_id") != stored["origin_session_id"]
                            or origin.get("surface") != stored["origin_surface"]
                            or origin != json.loads(stored["context_json"]).get("task_origin")
                            or origin.get("surface") not in known_surfaces()):
                        return {"status": "failed", "error": "Agent handoff origin is not durably bound", "dispatch_started": False}
                    if origin.get("source") == "tracked_chat_turn" and (
                            not stored["handoff_key"] or not stored["terms_digest"]
                            or origin.get("handoff_key") != stored["handoff_key"]
                            or origin.get("terms_digest") != stored["terms_digest"]):
                        return {"status": "failed", "error": "Tracked handoff identity is not durably bound", "dispatch_started": False}
                    command_context = {"surface": origin["surface"]}
                # L4 — capture the reply text so downstream steps can consume
                # it via {{ previous_output }} / {{ step_N }}.
                scope = TaskFlowModelStep(self, flow, step, session_id)
                if isinstance(flow.get("context", {}).get("goal"), str):
                    from observability.log_redaction import redact
                    earlier = []
                    for previous in flow["steps"][:step["step_index"]]:
                        if previous["status"] != "completed":
                            return {"status": "failed", "error": "Prior task step is not durably completed", "dispatch_started": False}
                        result = previous.get("result") or {}
                        output = self._step_output_text(result)
                        earlier.append({"step_index": previous["step_index"], "role": "assistant",
                                        "evidence_kind": "persisted_processing_result", "action_outcome": "not_asserted",
                                        "output": redact(output) if isinstance(output, str) else output})
                    checkpoint = {"goal": {"role": "user", "evidence_kind": "accepted_task_goal", "text": flow["context"]["goal"]},
                                  "completed_steps": earlier}
                    prefix = json.dumps(checkpoint, ensure_ascii=False)
                    if len(prefix.encode()) > 65536:
                        return {"status": "failed", "error": "Persisted task context exceeds its continuation budget", "dispatch_started": False}
                    prompt = "Use this persisted task context; assistant processing output is not proof an external action succeeded.\n" + prefix + "\nCurrent requested step:\n" + prompt
                prior = step.get("result") or {}
                if prior.get("reason") == "model_continuation":
                    from skills.result_budget import serialize_tool_result
                    from observability.log_redaction import redact
                    receipts = [{"call_id": key, "tool_name": record["terms"]["tool_name"],
                                 "result": redact(serialize_tool_result(record["terms"]["tool_name"], record["result"], registry=self._skill_registry))}
                                for key, record in scope.actions.items()]
                    if len(json.dumps(receipts, ensure_ascii=False).encode()) > 65536:
                        return {"status": "failed", "error": "Model continuation exceeds its context budget", "model_actions": scope.actions}
                    prompt = ("Continue the unfinished background task from these recorded action receipts. "
                              "The actions already executed; do not dispatch them again. Their success is only what their envelopes report. "
                              "Original task context: " + prompt + "\nRecorded actions: " + json.dumps(receipts, ensure_ascii=False))
                token = _model_step.set(scope)
                try:
                    if command_context is None:
                        reply = await self._orchestrator.handle_command(session_id, prompt)
                    else:
                        reply = await self._orchestrator.handle_command(session_id, prompt, context=command_context)
                finally:
                    _model_step.reset(token)
                if scope.review_issued:
                    return {"status": "deferred"}
                if scope.unknown or scope.calls:
                    return {"status": "outcome_unknown"}
                reply_text = reply if isinstance(reply, str) else scope.text
                if scope.error or not isinstance(reply_text, str) or not reply_text.strip():
                    return {"status": "failed", "error": "Model task did not produce a completed processing result", "model_actions": scope.actions, "dispatch_started": bool(scope.actions)}
                return {"status": "completed", "prompt": prompt, "output": reply_text, "reply": reply_text, "model_actions": scope.actions}
            except Exception as exc:
                if scope is not None and scope.review_issued:
                    return {"status": "deferred"}
                if scope is not None and scope.calls:
                    return {"status": "outcome_unknown"}
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
