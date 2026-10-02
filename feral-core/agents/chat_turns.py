"""Correlated chat lifecycle receipts on the existing memory and agent runner.

This owns no tool execution authority. Accepted means committed receipt;
completed means processing finished, never that an external action succeeded.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import os
from typing import TYPE_CHECKING, Callable
from uuid import UUID, uuid4

from security.agent_turn_lease import spawn_agent_turn

if TYPE_CHECKING:
    from models.protocol import FeralMessage


class ChatTurnError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def exact_uuid(value) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ChatTurnError("chat_turn_invalid_request")
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise ChatTurnError("chat_turn_invalid_request") from None
    return value


@dataclass
class TurnAudit:
    session_id: str
    turn_id: str
    request_id: str = ""
    began: bool = False
    cancel_requested: bool = False
    closed: bool = False
    final_text: str = ""
    error: bool = False
    budget_exceeded: bool = False
    refused: bool = False
    approval_request_ids: list[str] = field(default_factory=list)


_audit: ContextVar[TurnAudit | None] = ContextVar("feral_tracked_chat_turn", default=None)


def turn_audit(session_id: str) -> TurnAudit | None:
    audit = _audit.get()
    return audit if audit is not None and audit.session_id == session_id and not audit.closed else None


def correlate_progress(session_id: str, message: FeralMessage) -> FeralMessage:
    """Attach trusted turn identity without rewriting skill/permission identifiers."""
    audit = turn_audit(session_id)
    if audit is None or not audit.request_id:
        return message
    return message.model_copy(update={"payload": {
        **message.payload,
        "chat_turn": {"contract_version": 1, "request_id": audit.request_id, "turn_id": audit.turn_id},
    }})


def observe_tool_result(session_id: str, result: dict):
    audit = turn_audit(session_id)
    if audit is None or not isinstance(result, dict):
        return
    if result.get("status") == "pending_approval":
        request_id = result.get("request_id")
        if isinstance(request_id, str) and request_id not in audit.approval_request_ids:
            audit.approval_request_ids.append(request_id)
    elif result.get("status") == "PermissionOutcome::Deny" or result.get("error_code") == "plan_mode_blocked":
        audit.refused = True


@dataclass
class LiveTurn:
    owner: object
    audit: TurnAudit
    request_id: str
    task: asyncio.Task | None = None
    subscribers: dict = field(default_factory=dict)
    terminal_notified: set[int] = field(default_factory=set)


class ChatTurnManager:
    """Track exact connection/session/turn tasks, using existing SQLite receipts."""
    def __init__(self, state):
        self.state = state
        self._store = None
        self._start_lock = asyncio.Lock()
        self._submit_lock = asyncio.Lock()
        self._live: dict[tuple[str, str], LiveTurn] = {}
        self._settlements: set[asyncio.Task] = set()

    async def start(self):
        async with self._start_lock:
            store = getattr(self.state, "memory", None)
            if store is None or not callable(getattr(store, "chat_turn_claim", None)):
                raise ChatTurnError("chat_turn_receipt_unavailable")
            if self._store is not store:
                if self._live:
                    raise ChatTurnError("chat_turn_receipt_unavailable")
                await store.chat_turn_recover()
                self._store = store

    async def submit(self, *, owner, session_id: str, request_id: str, terms: dict,
                     run: Callable, emit: Callable) -> dict:
        request_id = exact_uuid(request_id)
        if (not isinstance(session_id, str) or not session_id or len(session_id) > 1024
                or session_id.strip() != session_id or any(ord(c) < 32 or ord(c) == 127 for c in session_id)):
            raise ChatTurnError("chat_turn_invalid_request")
        try:
            canonical = json.dumps(terms, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError):
            raise ChatTurnError("chat_turn_invalid_request") from None
        encoded = canonical.encode()
        if len(encoded) > 8 * 1024 * 1024:
            raise ChatTurnError("chat_turn_input_too_large")
        digest = hashlib.sha256(encoded).hexdigest()
        try:
            receipt_limit = int(os.environ.get("FERAL_CHAT_TURN_RECEIPT_LIMIT", "100000"))
            if not 1 <= receipt_limit <= 1000000:
                raise ValueError()
        except ValueError:
            raise ChatTurnError("chat_turn_receipt_unavailable") from None
        await self.start()
        async with self._submit_lock:
            turn_id = str(uuid4())
            accepted = dict(contract_version=1, request_id=request_id, turn_id=turn_id,
                            session_id=session_id, status="accepted", durable=True, replayed=False)
            result = await self._store.chat_turn_claim(session_id=session_id, request_id=request_id,
                                                       turn_id=turn_id, input_digest=digest, receipt=accepted, receipt_limit=receipt_limit)
            if result.get("conflict"):
                raise ChatTurnError("chat_turn_request_conflict")
            if result.get("quota"):
                raise ChatTurnError("chat_turn_quota")
            receipt = dict(result["receipt"])
            if not result["created"]:
                receipt["replayed"] = True
                ack = {k: receipt[k] for k in ("contract_version", "request_id", "turn_id", "session_id", "durable")}
                ack.update(status="accepted", replayed=True)
                await emit("chat_turn_accepted", ack)
                active = self._live.get((session_id, receipt["turn_id"]))
                if active is not None:
                    if len(active.subscribers) >= 8 and id(owner) not in active.subscribers:
                        raise ChatTurnError("chat_turn_quota")
                    active.subscribers[id(owner)] = (owner, emit)
                # Read again after publishing acceptance, because completion
                # can race a duplicate reconnect during its first await.
                latest = await self._store.chat_turn_get(session_id=session_id, turn_id=receipt["turn_id"])
                if latest and "processing_outcome" in latest and (active is None or active.audit.closed):
                    if active is None or id(owner) not in active.terminal_notified:
                        if active is not None:
                            active.terminal_notified.add(id(owner))
                        await emit("chat_turn_terminal", {**latest, "replayed": True})
                return ack
            audit = TurnAudit(session_id, turn_id, request_id=request_id)
            live = LiveTurn(owner, audit, request_id)
            live.subscribers[id(owner)] = (owner, emit)
            self._live[(session_id, turn_id)] = live
            try:
                await emit("chat_turn_accepted", accepted)
            except BaseException:
                self._live.pop((session_id, turn_id), None)
                terminal = self._terminal(accepted, "cancelled", audit)
                await self._store.chat_turn_update(session_id=session_id, turn_id=turn_id, status="terminal", receipt=terminal)
                raise
            # Preserve the existing reviewed native agent generation lease.
            live.task = spawn_agent_turn(self.state, self._run(live, accepted, run, emit))

            def settled(task):
                if not task.cancelled():
                    task.exception()
                if not audit.closed:
                    completion = asyncio.create_task(self._finish(live, accepted, emit, "cancelled"))
                    self._settlements.add(completion)
                    completion.add_done_callback(self._settlements.discard)

            live.task.add_done_callback(settled)
            return accepted

    @staticmethod
    def _terminal(accepted: dict, outcome: str, audit: TurnAudit) -> dict:
        receipt = {k: v for k, v in accepted.items() if k != "status"}
        receipt.update(processing_outcome=outcome,
                       final_text=audit.final_text if outcome in {"completed", "awaiting_approval", "refused"} else "",
                       action_outcome="unknown" if outcome == "outcome_unknown" or (outcome == "cancelled" and audit.began) else "not_asserted",
                       approval_request_ids=audit.approval_request_ids[:128])
        return receipt

    async def _run(self, live: LiveTurn, accepted: dict, run, emit):
        audit = live.audit
        token = _audit.set(audit)
        outcome = "unavailable"
        try:
            running = {**accepted, "status": "running"}
            updated = await self._store.chat_turn_update(session_id=audit.session_id, turn_id=audit.turn_id,
                                                         status="running", receipt=running)
            if not updated:
                return
            value = await run()
            if audit.cancel_requested:
                outcome = "cancelled"
            elif audit.error:
                outcome = "failed"
            elif audit.budget_exceeded:
                outcome = "budget_exceeded"
            elif audit.approval_request_ids:
                outcome = "awaiting_approval"
            elif audit.refused:
                outcome = "refused"
            else:
                outcome = "completed" if isinstance(value, str) and value or audit.final_text else "unavailable"
            if isinstance(value, str) and value:
                audit.final_text = value
        except asyncio.CancelledError:
            outcome = "cancelled"
        except Exception:
            outcome = "outcome_unknown" if audit.began else "failed"
        finally:
            _audit.reset(token)
            await self._finish(live, accepted, emit, outcome)

    async def _finish(self, live, accepted, emit, outcome):
        audit = live.audit
        if audit.closed:
            return
        audit.closed = True
        terminal = self._terminal(accepted, outcome, audit)
        try:
            committed = await self._store.chat_turn_update(session_id=audit.session_id, turn_id=audit.turn_id,
                                                           status="terminal", receipt=terminal)
            if committed:
                for _owner, subscriber in list(live.subscribers.values()):
                    if id(_owner) in live.terminal_notified:
                        continue
                    live.terminal_notified.add(id(_owner))
                    try:
                        await subscriber("chat_turn_terminal", terminal)
                    except Exception:
                        pass
        except Exception:
            # Never certify a terminal receipt that did not commit.
            try:
                await emit("error", {"code": "chat_turn_receipt_unavailable", "request_id": accepted["request_id"],
                                     "message": "The turn receipt could not be confirmed. Inspect its status before retrying.", "recoverable": True})
            except Exception:
                pass
        finally:
            self._live.pop((audit.session_id, audit.turn_id), None)

    async def abort(self, *, owner, session_id: str, turn_id: str, request_id: str) -> dict:
        turn_id, request_id = exact_uuid(turn_id), exact_uuid(request_id)
        live = self._live.get((session_id, turn_id))
        # All three identities were bound after durable acceptance. Avoid an
        # await here so cancellation can fence an as-yet-unscheduled runner.
        if live is None or live.owner is not owner or live.request_id != request_id or live.task is None or live.task.done():
            return {"status": "not_active", "cancel_requested": False, "turn_id": turn_id, "request_id": request_id}
        if not live.audit.cancel_requested:
            live.audit.cancel_requested = True
            live.task.cancel()
        return {"status": "cancel_requested", "cancel_requested": True, "turn_id": turn_id, "request_id": request_id}

    async def status(self, *, session_id: str, turn_id: str = "", request_id: str = ""):
        if not (turn_id or request_id):
            raise ChatTurnError("chat_turn_invalid_request")
        if turn_id:
            turn_id = exact_uuid(turn_id)
        if request_id:
            request_id = exact_uuid(request_id)
        await self.start()
        return await self._store.chat_turn_get(session_id=session_id, turn_id=turn_id, request_id=request_id)

    async def detach(self, owner):
        for live in self._live.values():
            live.subscribers.pop(id(owner), None)
        tasks = [live.task for live in self._live.values() if live.owner is owner and live.task is not None and not live.task.done()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=2)
            for task in pending:
                for live in self._live.values():
                    if live.task is task:
                        live.audit.cancel_requested = True
                task.cancel()


def get_chat_turn_manager(state) -> ChatTurnManager:
    manager = getattr(state, "chat_turns", None)
    if not isinstance(manager, ChatTurnManager):
        manager = state.chat_turns = ChatTurnManager(state)
    return manager
