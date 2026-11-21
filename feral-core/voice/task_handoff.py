"""Paired voice background admission; receipts grant no execution authority.

Only background_task__start uses this bridge. Direct tools retain the existing
response fence. A stopped speech subscriber never cancels an accepted TaskFlow.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from agents.chat_turns import (
    exact_uuid,
    get_chat_turn_manager,
    observe_tool_result,
    turn_audit,
)
from agents.taskflow import TaskFlowReceiptError
from security.approval_ingress import (
    PairedDevicePrincipal,
    begin_node_approval_ingress,
    end_node_approval_ingress,
)
from skills.call_context import bind_context

logger = logging.getLogger("feral.voice.task_handoff")

MAX_VOICE_TASKS = 16
TERMINAL = frozenset({"completed", "failed", "cancelled", "outcome_unknown"})
VOICE_TASK_GUIDANCE = (
    "For an explicitly requested computer/browser task that takes several steps, "
    "use background_task__start once with the exact authorized goal and terms. "
    "Acceptance means durable processing was queued, never that an action succeeded. "
    "Continue the conversation while it works. Reports arrive as separate task receipts. "
    "Do not repeat a start after an uncertain response; recover its task receipt. "
    "Explicit cancellation names the task; interrupting speech does not cancel accepted work."
)


class VoiceTaskError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _identity(runtime, session_id, task_id, principal, request_id):
    """One bounded read-only query; private provenance never joins a payload."""
    if type(principal) is not PairedDevicePrincipal:
        raise VoiceTaskError("task_not_found")
    principal.require_current()
    conn = sqlite3.connect(
        Path(runtime._db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1
    )
    try:
        row = conn.execute(
            "SELECT context_json, origin_principal_json FROM taskflows "
            "WHERE id=? AND origin_session_id=? AND handoff_key IS NOT NULL",
            (task_id, session_id),
        ).fetchone()
    except sqlite3.Error:
        raise VoiceTaskError("task_receipt_unavailable") from None
    finally:
        conn.close()
    principal.require_current()
    try:
        if row is None or json.loads(row[1]) != principal.storage_binding():
            raise ValueError()
        origin = json.loads(row[0])["task_origin"]
        if origin["request_id"] != request_id:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise VoiceTaskError("task_not_found") from None


def _flow_for_handoff(runtime, handoff_key, session_id, principal):
    principal.require_current()
    conn = sqlite3.connect(
        Path(runtime._db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1
    )
    try:
        row = conn.execute(
            "SELECT id FROM taskflows WHERE handoff_key=? AND origin_session_id=? "
            "AND origin_principal_json=?",
            (
                handoff_key,
                session_id,
                json.dumps(
                    principal.storage_binding(), sort_keys=True, separators=(",", ":")
                ),
            ),
        ).fetchone()
    except sqlite3.Error:
        raise VoiceTaskError("task_receipt_unavailable") from None
    finally:
        conn.close()
    principal.require_current()
    return row[0] if row is not None else None


def read_task_result(
    runtime,
    *,
    session_id,
    task_id,
    request_id,
    principal,
    guard,
    replayed=False,
    recovery_request_id=None,
):
    """Exact-device terminal recovery, never resume/retry/create work."""
    request_id = exact_uuid(request_id)
    if not isinstance(task_id, str) or not 1 <= len(task_id) <= 128:
        raise VoiceTaskError("task_not_found")

    def owned():
        guard()
        principal.require_current()

    owned()
    _identity(runtime, session_id, task_id, principal, request_id)
    receipt = runtime.read_origin_receipt(session_id, task_id, guard=owned)
    owned()
    _identity(runtime, session_id, task_id, principal, request_id)
    if not receipt:
        raise VoiceTaskError("task_not_found")
    outcome = receipt["processing_outcome"]
    if outcome not in TERMINAL:
        raise VoiceTaskError("task_result_pending")
    report = (
        receipt.get("result_text")
        or {
            "completed": "Task processing completed; no external action outcome was asserted.",
            "failed": "Task processing failed. Inspect its receipt before retrying.",
            "cancelled": "Task processing was cancelled; prior effects are not asserted.",
            "outcome_unknown": "Task outcome is unknown. Inspect prior effects before retrying.",
        }[outcome]
    )
    payload = dict(
        contract_version=1,
        task_id=task_id,
        request_id=request_id,
        delivery_id=str(
            uuid5(
                NAMESPACE_URL,
                "feral/task-result/" + task_id + "/" + receipt["result_digest"],
            )
        ),
        session_id=session_id,
        processing_outcome=outcome,
        action_outcome="unknown" if outcome == "outcome_unknown" else "not_asserted",
        report=report,
        report_truncated=receipt["result_truncated"],
        durable=True,
        replayed=replayed,
    )
    if recovery_request_id is not None:
        payload["recovery_request_id"] = exact_uuid(recovery_request_id)
    from models.protocol import TaskResultPayload

    return TaskResultPayload.model_validate(payload).model_dump(exclude_none=True)


def read_task_receipts(
    runtime,
    *,
    session_id,
    principal,
    guard,
    recovery_request_id,
    limit=16,
    older_than_task_id=None,
):
    """Bounded identity-only discovery after an acceptance notice was lost."""
    from models.protocol import (
        TaskAcceptedPayload,
        TaskReceiptsGetPayload,
        TaskReceiptsPayload,
    )

    request = TaskReceiptsGetPayload.model_validate(
        dict(
            contract_version=1,
            session_id=session_id,
            limit=limit,
            older_than_task_id=older_than_task_id,
        )
    )
    recovery_request_id = exact_uuid(recovery_request_id)
    if type(principal) is not PairedDevicePrincipal:
        raise VoiceTaskError("task_device_authority_unavailable")
    source_json = json.dumps(
        principal.storage_binding(), sort_keys=True, separators=(",", ":")
    )
    deadline = time.monotonic() + 2.0

    def current():
        guard()
        principal.require_current()
        if time.monotonic() > deadline:
            raise VoiceTaskError("task_receipt_unavailable")

    while True:
        current()
        conn = sqlite3.connect(
            Path(runtime._db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            params = [session_id, source_json]
            query = "SELECT * FROM taskflows WHERE origin_session_id=? AND origin_principal_json=? AND handoff_key IS NOT NULL"
            if request.older_than_task_id is not None:
                cursor = conn.execute(
                    query + " AND id=?", [*params, request.older_than_task_id]
                ).fetchone()
                if cursor is None:
                    raise VoiceTaskError("task_cursor_unavailable")
                query += " AND (created_at < ? OR (created_at = ? AND id < ?))"
                params.extend(
                    [cursor["created_at"], cursor["created_at"], cursor["id"]]
                )
            rows = conn.execute(
                query + " ORDER BY created_at DESC, id DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
            receipts = []
            for row in rows[:limit]:
                current()
                steps = conn.execute(
                    "SELECT * FROM taskflow_steps WHERE flow_id=? ORDER BY step_index",
                    (row["id"],),
                ).fetchall()
                try:
                    runtime._origin_receipt_projection(row, steps)
                    origin = json.loads(row["context_json"])["task_origin"]
                    accepted = TaskAcceptedPayload.model_validate(
                        dict(
                            contract_version=1,
                            session_id=session_id,
                            request_id=origin["request_id"],
                            task_id=row["id"],
                            durable=True,
                            replayed=True,
                            action_outcome="not_asserted",
                        )
                    )
                except (ValueError, TypeError, KeyError):
                    # Corrupt provenance cannot join a discovery receipt.
                    continue
                receipts.append(accepted.model_dump())
            current()
            payload = dict(
                contract_version=1,
                session_id=session_id,
                recovery_request_id=recovery_request_id,
                receipts=receipts,
                next_cursor=(
                    receipts[-1]["task_id"] if receipts else rows[limit - 1]["id"]
                )
                if len(rows) > limit
                else None,
            )
            return TaskReceiptsPayload.model_validate(payload).model_dump(
                exclude_none=True
            )
        except sqlite3.OperationalError as exc:
            if (getattr(exc, "sqlite_errorcode", 0) & 255) not in (
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            ):
                raise VoiceTaskError("task_receipt_unavailable") from None
        finally:
            conn.close()
        current()
        time.sleep(0.025)


class VoiceTaskHandoff:
    """Bounded exact-attempt subscriptions to independently owned TaskFlow work."""

    def __init__(self, brain, binding, principal, emit, *, current_state):
        if type(principal) is not PairedDevicePrincipal:
            raise VoiceTaskError("task_device_authority_unavailable")
        self.brain, self.binding, self.principal, self.emit = (
            brain,
            binding,
            principal,
            emit,
        )
        self.runtime, self.memory, self.orchestrator = (
            brain.taskflows,
            brain.memory,
            brain.orchestrator,
        )
        self.current_state = current_state
        self.generation = getattr(brain, "_native_agent_turn_generation", 0)
        self.subscriptions = {}
        self.admissions = {}
        self.pending_creations = {}

    def runtime_current(self):
        return (
            self.current_state() is self.brain
            and self.brain.taskflows is self.runtime
            and self.brain.memory is self.memory
            and self.brain.orchestrator is self.orchestrator
            and getattr(self.brain, "_native_agent_turn_generation", 0)
            == self.generation
            and getattr(self.brain, "_native_bootstrap_required", False) is not True
        )

    def guard(self, *, speech=False):
        self.principal.require_current()
        if not self.runtime_current() or speech and not self.binding.current():
            raise asyncio.CancelledError("Retired voice task subscriber")

    async def _publish(self, kind, payload):
        self.guard()
        from models.protocol import TaskAcceptedPayload, TaskResultPayload

        contract = TaskAcceptedPayload if kind == "task_accepted" else TaskResultPayload
        payload = contract.model_validate(payload).model_dump(exclude_none=True)
        await asyncio.wait_for(self.emit(kind, payload), 2.0)
        self.guard()

    async def _check_identity(self, task_id, request_id):
        self.guard()
        await asyncio.wait_for(
            asyncio.to_thread(
                _identity,
                self.runtime,
                self.binding.session_id,
                task_id,
                self.principal,
                request_id,
            ),
            3.0,
        )
        self.guard()

    def _subscribe(self, task_id, request_id, *, replayed=False):
        if task_id in self.subscriptions:
            return
        if len(self.subscriptions) >= MAX_VOICE_TASKS:
            raise VoiceTaskError("task_subscription_quota")

        async def watch():
            try:
                await self._publish(
                    "task_accepted",
                    dict(
                        contract_version=1,
                        task_id=task_id,
                        request_id=request_id,
                        session_id=self.binding.session_id,
                        durable=True,
                        replayed=replayed,
                        action_outcome="not_asserted",
                    ),
                )
                while True:
                    self.guard()
                    try:
                        payload = await asyncio.to_thread(
                            read_task_result,
                            self.runtime,
                            session_id=self.binding.session_id,
                            task_id=task_id,
                            request_id=request_id,
                            principal=self.principal,
                            guard=lambda: self.guard(),
                        )
                    except VoiceTaskError as exc:
                        if exc.code not in {
                            "task_result_pending",
                            "task_receipt_unavailable",
                        }:
                            return
                    except TaskFlowReceiptError:
                        # Read-only contention is not a task outcome. The
                        # bounded subscriber retries without executing work.
                        pass
                    else:
                        await self._publish("task_result", payload)
                        return
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                # Subscriber retirement leaves the durable task intact.
                return
            except Exception as exc:
                logger.warning(
                    "Voice task subscription failed (code=task_subscription_unavailable type=%s)",
                    type(exc).__name__,
                )
                return

        task = asyncio.create_task(watch())
        self.subscriptions[task_id] = task

        def settled(done):
            if self.subscriptions.get(task_id) is done:
                self.subscriptions.pop(task_id, None)

        task.add_done_callback(settled)

    def _subscribe_creation(self, request_id, turn_id, call_id):
        if request_id in self.pending_creations:
            return
        handoff_key = hashlib.sha256(
            json.dumps(
                [self.binding.session_id, request_id, turn_id, call_id],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

        async def watch_creation():
            try:
                while True:
                    self.guard()
                    try:
                        task_id = await asyncio.wait_for(
                            asyncio.to_thread(
                                _flow_for_handoff,
                                self.runtime,
                                handoff_key,
                                self.binding.session_id,
                                self.principal,
                            ),
                            3.0,
                        )
                    except (VoiceTaskError, TimeoutError):
                        task_id = None
                    self.guard()
                    if task_id:
                        await self._check_identity(task_id, request_id)
                        self._subscribe(task_id, request_id)
                        return
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(
                    "Voice task creation subscription unavailable (code=task_creation_subscription_unavailable type=%s)",
                    type(exc).__name__,
                )

        task = asyncio.create_task(watch_creation())
        self.pending_creations[request_id] = task

        def settled(done):
            if self.pending_creations.get(request_id) is done:
                self.pending_creations.pop(request_id, None)

        task.add_done_callback(settled)

    async def start(self, call_id, args, executor, skill, endpoint):
        self.guard(speech=True)
        if (
            not isinstance(call_id, str)
            or not 1 <= len(call_id) <= 1024
            or not isinstance(args, dict)
        ):
            raise VoiceTaskError("task_invalid_request")
        request_id = str(
            uuid5(
                NAMESPACE_URL,
                json.dumps(
                    [
                        "feral/voice-task/v1",
                        self.principal.device_id,
                        self.binding.session_id,
                        self.binding.attempt_id,
                        call_id,
                    ],
                    separators=(",", ":"),
                ),
            )
        )
        if (
            request_id not in self.admissions
            and len(self.admissions)
            + len(self.subscriptions)
            + len(self.pending_creations)
            >= MAX_VOICE_TASKS
        ):
            raise VoiceTaskError("task_subscription_quota")
        # Freeze the admitted terms before any await. Downstream execution
        # and durable dedup compare the same bounded canonical input.
        try:
            encoded = json.dumps(args, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode()) > 65536:
                raise ValueError()
            args = json.loads(encoded)
        except (ValueError, TypeError):
            raise VoiceTaskError("task_invalid_request") from None
        manager = get_chat_turn_manager(self.brain)
        result_ready = self.admissions.get(request_id)
        created_future = result_ready is None
        if created_future:
            result_ready = asyncio.get_running_loop().create_future()
            self.admissions[request_id] = result_ready

        async def emit_turn(kind, receipt):
            # Internal CTM creation receipt is not a spoken conversation turn.
            self.guard()

        async def run():
            try:
                self.guard()
                with bind_context(
                    session_id=self.binding.session_id,
                    surface="websocket",
                    tool_name="background_task__start",
                    call_id=call_id,
                ):
                    result = await executor.execute(
                        "background_task__start", args, skill, endpoint
                    )
                    observe_tool_result(self.binding.session_id, result)
                self.guard()
                data = result.get("data") if isinstance(result, dict) else None
                audit = turn_audit(self.binding.session_id)
                if audit is not None and audit.approval_request_ids:
                    # Local review keeps its exact captured tracked origin.
                    # Observe the eventual SQLite commit; never repeat start.
                    self._subscribe_creation(request_id, audit.turn_id, call_id)
                if (
                    isinstance(data, dict)
                    and isinstance(data.get("flow_id"), str)
                    and result.get("success") is True
                ):
                    task_id = data["flow_id"]
                    await self._check_identity(task_id, request_id)
                    try:
                        self._subscribe(task_id, request_id)
                    except Exception as exc:
                        logger.warning(
                            "Voice task subscription refused (code=task_subscription_unavailable type=%s)",
                            type(exc).__name__,
                        )
                        await self._publish(
                            "task_accepted",
                            dict(
                                contract_version=1,
                                task_id=task_id,
                                request_id=request_id,
                                session_id=self.binding.session_id,
                                durable=True,
                                replayed=False,
                                action_outcome="not_asserted",
                            ),
                        )
                        result = {
                            **result,
                            "success": False,
                            "outcome_unknown": True,
                            "error_code": "task_result_subscription_unavailable",
                            "data": {
                                **data,
                                "voice_request_id": request_id,
                                "handoff": {
                                    **data.get("handoff", {}),
                                    "result_subscription": "unavailable",
                                },
                            },
                        }
                    else:
                        result = {
                            **result,
                            "data": {
                                **data,
                                "voice_request_id": request_id,
                                "handoff": {
                                    **data.get("handoff", {}),
                                    "result_subscription": "attached",
                                },
                            },
                        }
                if not result_ready.done():
                    result_ready.set_result(result)
                return "Background task admission processed; see the task receipt for its outcome."
            except BaseException:
                if not result_ready.done():
                    result_ready.set_result(
                        {
                            "success": False,
                            "error_code": "task_admission_outcome_unknown",
                            "outcome_unknown": True,
                            "request_id": request_id,
                        }
                    )
                raise
            finally:
                self.admissions.pop(request_id, None)

        token = begin_node_approval_ingress(self.principal)
        try:
            receipt = await manager.submit(
                owner=self,
                session_id=self.binding.session_id,
                request_id=request_id,
                terms={
                    "entry_kind": "voice_background_task",
                    "tool_name": "background_task__start",
                    "args": args,
                },
                run=run,
                emit=emit_turn,
                source_principal=self.principal,
            )
        except BaseException:
            if created_future and self.admissions.get(request_id) is result_ready:
                self.admissions.pop(request_id, None)
            raise
        finally:
            end_node_approval_ingress(token)
        if receipt.get("replayed") is True and not result_ready.done():
            # Never invoke run again. Durable discovery can recover a previous
            # terminal creation; no goal or external tool is replayed here.
            handoff_key = hashlib.sha256(
                json.dumps(
                    [self.binding.session_id, request_id, receipt["turn_id"], call_id],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            self.guard()
            task_id = await asyncio.wait_for(
                asyncio.to_thread(
                    _flow_for_handoff,
                    self.runtime,
                    handoff_key,
                    self.binding.session_id,
                    self.principal,
                ),
                3.0,
            )
            self.guard()
            if task_id:
                await self._check_identity(task_id, request_id)
                self._subscribe(task_id, request_id, replayed=True)
                result_ready.set_result(
                    {
                        "success": True,
                        "data": {
                            "flow_id": task_id,
                            "voice_request_id": request_id,
                            "handoff": {
                                "accepted": True,
                                "durable": True,
                                "replayed": True,
                                "action_outcome": "not_asserted",
                            },
                        },
                    }
                )
            else:
                self.guard()
                latest = await manager._store.chat_turn_get(
                    session_id=self.binding.session_id,
                    turn_id=receipt["turn_id"],
                    source_principal=self.principal.storage_binding(),
                )
                self.guard()
                outcome = (
                    latest.get("processing_outcome")
                    if isinstance(latest, dict)
                    else None
                )
                if outcome is not None:
                    if outcome == "awaiting_approval":
                        self._subscribe_creation(
                            request_id, receipt["turn_id"], call_id
                        )
                    result_ready.set_result(
                        {
                            "success": False,
                            "status": "admission_" + outcome,
                            "error_code": "task_admission_" + outcome,
                            "request_id": request_id,
                            "replayed": True,
                            "outcome_unknown": outcome
                            in {"outcome_unknown", "cancelled"},
                        }
                    )
                    if self.admissions.get(request_id) is result_ready:
                        self.admissions.pop(request_id, None)
        try:
            active = manager._live.get((self.binding.session_id, receipt["turn_id"]))
            if active is not None and active.task is not None:
                # A pending review must be settled before the voice tool reply
                # can invite its resolution. Barge-in cancels only this waiter.
                await asyncio.wait_for(asyncio.shield(active.task), 2.5)
            return await asyncio.wait_for(asyncio.shield(result_ready), 2.5)
        except TimeoutError:
            if self.admissions.get(request_id) is result_ready:
                self.admissions.pop(request_id, None)
            return {
                "success": False,
                "status": "admission_pending",
                "error_code": "task_admission_pending",
                "outcome_unknown": True,
                "request_id": request_id,
                "action_outcome": "not_asserted",
                "message": "Admission is processing; do not repeat the goal.",
            }
