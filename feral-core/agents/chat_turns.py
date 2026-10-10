"""Correlated chat lifecycle receipts on the existing memory and agent runner.

This owns no tool execution authority. Accepted means committed receipt;
completed means processing finished, never that an external action succeeded.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import InitVar, dataclass, field
import hashlib
import json
import os
from typing import TYPE_CHECKING, Any, Callable, ClassVar
from uuid import UUID, uuid4

from security.agent_turn_lease import spawn_agent_turn
from agents.runtime_context_checkpoint import RuntimeContextScopeReceipt
from memory.runtime_session_checkpoint import CheckpointFence
from security.approval_ingress import (
    PairedDevicePrincipal, current_node_principal, device_approval_authority_unavailable,
)

if TYPE_CHECKING:
    from models.protocol import FeralMessage


class ChatTurnError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _admitted_source(source: PairedDevicePrincipal | None) -> PairedDevicePrincipal | None:
    """Use server admission only; node membership or a supplied SID is insufficient."""
    admitted = current_node_principal()
    if device_approval_authority_unavailable():
        if admitted is None or (source is not None and source is not admitted):
            raise ChatTurnError("chat_turn_device_authority_unavailable")
        source = admitted
    if source is not None:
        if type(source) is not PairedDevicePrincipal:
            raise ChatTurnError("chat_turn_device_authority_unavailable")
        source.require_current()
    return source


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
    context_receipt: RuntimeContextScopeReceipt | None = None
    context_checkpoint: dict | None = None
    source_principal: PairedDevicePrincipal | None = field(default=None, repr=False)


@dataclass(frozen=True)
class CommittedChatResult:
    """Trusted runner result; the scope itself is the exact commit authority."""
    text: str | None
    scope: RuntimeContextScopeReceipt

    def checkpoint(self, session_id: str) -> dict | None:
        if self.text is not None and type(self.text) is not str:
            raise ChatTurnError("chat_turn_context_invalid")
        if type(self.scope) is not RuntimeContextScopeReceipt:
            raise ChatTurnError("chat_turn_context_invalid")
        fence = self.scope.committed_fence
        if fence is None:
            return None  # An explicitly legacy scope never invents a checkpoint.
        if type(fence) is not CheckpointFence or fence.session_id != session_id:
            raise ChatTurnError("chat_turn_context_invalid")
        # Revalidate even a trusted object whose fields were altered externally.
        CheckpointFence(fence.session_id, fence.generation, fence.revision, fence.attempt_id)
        return {"contract_version": 1, "session_id": fence.session_id,
                "generation": fence.generation, "revision": fence.revision,
                "attempt_id": fence.attempt_id, "durable": True}


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


def _review_digest(args: dict) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class PendingTaskOrigin:
    """Private original-input evidence, never a JSON approval credential."""
    manager: Any
    state: Any
    store: Any
    runner: Any
    runtime: Any
    owner: object
    audit: TurnAudit
    original_task: asyncio.Task
    approval_id: str
    session_id: str
    request_id: str
    turn_id: str
    call_id: str
    surface: str
    args: dict
    args_digest: str
    coordinator: Any
    generation: str | None
    agent_generation: int
    source_principal: PairedDevicePrincipal | None = field(default=None, repr=False)

    def guard(self) -> None:
        state = self.state
        coordinator = getattr(self.runner._orch, "_context_checkpoints", None)
        source = self.source_principal
        if source is not None:
            source.require_current()
        if (state.chat_turns is not self.manager or self.manager._store is not self.store
                or self.manager.state is not state
                or state.memory is not self.store or state.taskflows is not self.runtime
                or getattr(state.orchestrator, "tool_runner", None) is not self.runner
                or (source is None and state.sessions.get(self.session_id) is not self.owner)
                or self.audit.cancel_requested or self.original_task.cancelled()
                or self.audit.source_principal is not source
                or (self.audit.session_id, self.audit.request_id, self.audit.turn_id)
                    != (self.session_id, self.request_id, self.turn_id)
                or self.original_task.cancelling()
                or coordinator is not self.coordinator
                or getattr(state, "_native_agent_turn_generation", 0) != self.agent_generation
                or self.args_digest != _review_digest(self.args)):
            raise ChatTurnError("origin_superseded")
        if coordinator is not None and not coordinator.review_generation_valid(self.session_id, self.generation):
            raise ChatTurnError("origin_superseded")
        if self.runner is None:
            raise ChatTurnError("origin_superseded")
        self.runner._guard_agent_lease()

    def matches(self, pending: dict) -> bool:
        try:
            self.guard()
            return self.static_matches(pending)
        except (Exception, asyncio.CancelledError):
            return False

    def static_matches(self, pending: dict) -> bool:
        """Historical display/revocation evidence, never current execution permission."""
        try:
            return (self.args_digest == _review_digest(self.args)
                    and self.audit.source_principal is self.source_principal
                    and (self.audit.session_id, self.audit.request_id, self.audit.turn_id)
                        == (self.session_id, self.request_id, self.turn_id)
                    and pending.get("request_id") == self.approval_id
                    and pending.get("session_id") == self.session_id
                    and pending.get("tool_name") == "background_task__start"
                    and isinstance(pending.get("args"), dict)
                    and _review_digest(pending["args"]) == self.args_digest)
        except (Exception, asyncio.CancelledError):
            return False


class TaskOriginTransfer:
    """One registered executor backing task may claim the original input."""
    def __init__(self, origin: PendingTaskOrigin, dispatch_task: asyncio.Task):
        self.origin = origin
        self.dispatch_task = dispatch_task
        self.backing_task: asyncio.Task | None = None
        self.active = True
        self.claimed = False

    def guard(self):
        self.origin.guard()
        if (not self.active or self.dispatch_task.done() or self.dispatch_task.cancelling()
                or self.backing_task is None or self.backing_task.done() or self.backing_task.cancelling()):
            raise ChatTurnError("origin_superseded")


class ValidatedTaskApprovalOrigin:
    """Resolver-task-bound proof issued only after the durable receipt read."""
    def __init__(self, manager, origin, seal):
        if seal is not manager._task_approval_seal:
            raise ChatTurnError("origin_unavailable")
        self.manager = manager
        self.origin = origin
        self._seal = seal
        self.owner_task = asyncio.current_task()
        self.used = False

    def consume(self, origin):
        task = asyncio.current_task()
        if (self.used or self.origin is not origin or self.manager is not origin.manager
                or self._seal is not self.manager._task_approval_seal
                or task is None or task is not self.owner_task or task.cancelling()):
            raise ChatTurnError("origin_superseded")
        origin.guard()
        self.used = True


_task_origin_transfer: ContextVar[TaskOriginTransfer | None] = ContextVar("feral_task_origin_transfer", default=None)


@dataclass(frozen=True)
class TrackedTaskOriginGuard:
    """Private creation identity carried beside, never inside, public origin.

    The principal is an init-only value kept outside dataclass fields, so
    dataclass projections cannot turn it into model or wire metadata.
    """
    check: Callable[[], None] = field(repr=False)
    _source_principal: ClassVar[PairedDevicePrincipal | None]
    principal: InitVar[PairedDevicePrincipal | None] = field(default=None, repr=False)

    def __post_init__(self, principal):
        if not callable(self.check) or (principal is not None and type(principal) is not PairedDevicePrincipal):
            raise ChatTurnError("origin_unavailable")
        object.__setattr__(self, "_source_principal", principal)

    @property
    def source_principal(self) -> PairedDevicePrincipal | None:
        return self._source_principal

    def __call__(self):
        if self.source_principal is not None:
            self.source_principal.require_current()
        self.check()


@contextmanager
def bind_task_origin_transfer(transfer: TaskOriginTransfer):
    if transfer.backing_task is not None or not transfer.active:
        raise ChatTurnError("origin_superseded")
    transfer.backing_task = asyncio.current_task()
    token = _task_origin_transfer.set(transfer)
    try:
        transfer.guard()
        yield
    finally:
        transfer.active = False
        _task_origin_transfer.reset(token)


def claim_task_origin_transfer(args: dict):
    transfer = _task_origin_transfer.get()
    if transfer is None:
        return None
    transfer.guard()
    from skills.call_context import current_context
    ctx = current_context()
    origin = transfer.origin
    if (transfer.claimed or transfer.backing_task is not asyncio.current_task()
            or ctx.session_id != origin.session_id or ctx.call_id != origin.call_id
            or ctx.turn_id != origin.turn_id or ctx.surface != origin.surface
            or ctx.tool_name != "background_task__start" or _review_digest(args) != origin.args_digest):
        raise ChatTurnError("origin_superseded")
    transfer.claimed = True
    return ({"contract_version": 1, "source": "tracked_chat_turn", "owner_verified": True,
             "session_id": origin.session_id, "request_id": origin.request_id, "turn_id": origin.turn_id,
             "tool_call_id": origin.call_id, "surface": origin.surface,
             "input_revision": None, "context_commit": "pending"},
            TrackedTaskOriginGuard(transfer.guard, origin.source_principal))


class ChatTurnManager:
    """Track exact connection/session/turn tasks, using existing SQLite receipts."""
    def __init__(self, state):
        self.state = state
        self._store = None
        self._start_lock = asyncio.Lock()
        self._submit_lock = asyncio.Lock()
        self._live: dict[tuple[str, str], LiveTurn] = {}
        self._settlements: set[asyncio.Task] = set()
        self._task_approval_seal = object()

    def capture_task_approval_origin(self, runner, approval_id: str, args: dict, ctx) -> PendingTaskOrigin:
        audit = turn_audit(ctx.session_id)
        live = self._live.get((ctx.session_id, audit.turn_id)) if audit is not None else None
        source = audit.source_principal if audit is not None else None
        if source is not None:
            source.require_current()
        from security.dangerous_tools import known_surfaces
        if (audit is None or live is None or live.audit is not audit or live.request_id != audit.request_id
                or live.task is None or live.task.done() or live.task.cancelling()
                or audit.cancel_requested or (source is None and self.state.sessions.get(ctx.session_id) is not live.owner)
                or self._store is not self.state.memory or ctx.surface not in known_surfaces()
                or ctx.tool_name != "background_task__start" or not ctx.call_id or len(ctx.call_id) > 256
                or any(ord(c) < 32 for c in ctx.call_id)):
            raise ChatTurnError("origin_unavailable")
        exact_uuid(audit.request_id)
        exact_uuid(audit.turn_id)
        coordinator = getattr(runner._orch, "_context_checkpoints", None)
        origin = PendingTaskOrigin(self, self.state, self._store, runner, self.state.taskflows,
            live.owner, audit, live.task, approval_id, ctx.session_id, audit.request_id, audit.turn_id,
            ctx.call_id, ctx.surface, copy.deepcopy(args), _review_digest(args), coordinator,
            coordinator.review_generation(ctx.session_id) if coordinator is not None else None,
            getattr(self.state, "_native_agent_turn_generation", 0), source_principal=source)
        origin.guard()
        return origin

    async def validate_task_approval_origin(self, origin: PendingTaskOrigin):
        origin.guard()
        try:
            receipt = await self._store.chat_turn_get(session_id=origin.session_id,
                request_id=origin.request_id, turn_id=origin.turn_id,
                source_principal=origin.source_principal.storage_binding() if origin.source_principal is not None else None)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ChatTurnError("origin_unavailable") from None
        origin.guard()
        if (not isinstance(receipt, dict) or receipt.get("contract_version") != 1
                or receipt.get("durable") is not True or receipt.get("session_id") != origin.session_id
                or receipt.get("request_id") != origin.request_id or receipt.get("turn_id") != origin.turn_id):
            raise ChatTurnError("origin_unavailable")
        if receipt.get("status") in {"accepted", "running"}:
            raise ChatTurnError("origin_unsettled")
        if (not origin.audit.closed or not origin.original_task.done()
                or receipt.get("processing_outcome") != "awaiting_approval"
                or origin.approval_id not in receipt.get("approval_request_ids", [])
                or origin.approval_id not in origin.audit.approval_request_ids):
            raise ChatTurnError("origin_superseded")
        return ValidatedTaskApprovalOrigin(self, origin, self._task_approval_seal)

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
                     run: Callable, emit: Callable,
                     source_principal: PairedDevicePrincipal | None = None) -> dict:
        source_principal = _admitted_source(source_principal)
        source_binding = source_principal.storage_binding() if source_principal is not None else None
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
            if source_principal is not None:
                source_principal.require_current()
            if sum(live.owner is owner and not live.audit.closed for live in self._live.values()) >= 16:
                # Existing requests may reconnect at capacity. A new request
                # cannot bypass the bounded ingress queue using another SID.
                existing = await self._store.chat_turn_get(session_id=session_id, request_id=request_id,
                                                            source_principal=source_binding)
                if source_principal is not None:
                    source_principal.require_current()
                if existing is None:
                    raise ChatTurnError("chat_turn_quota")
            turn_id = str(uuid4())
            accepted = dict(contract_version=1, request_id=request_id, turn_id=turn_id,
                            session_id=session_id, status="accepted", durable=True, replayed=False)
            result = await self._store.chat_turn_claim(session_id=session_id, request_id=request_id,
                                                       turn_id=turn_id, input_digest=digest, receipt=accepted, receipt_limit=receipt_limit,
                                                       source_principal=source_binding)
            if result.get("conflict"):
                raise ChatTurnError("chat_turn_request_conflict")
            if result.get("quota"):
                raise ChatTurnError("chat_turn_quota")
            receipt = dict(result["receipt"])
            if not result["created"]:
                if source_principal is not None:
                    source_principal.require_current()
                receipt["replayed"] = True
                ack = {k: receipt[k] for k in ("contract_version", "request_id", "turn_id", "session_id", "durable")}
                ack.update(status="accepted", replayed=True)
                await emit("chat_turn_accepted", ack)
                if source_principal is not None:
                    source_principal.require_current()
                active = self._live.get((session_id, receipt["turn_id"]))
                if active is not None:
                    if len(active.subscribers) >= 8 and id(owner) not in active.subscribers:
                        raise ChatTurnError("chat_turn_quota")
                    active.subscribers[id(owner)] = (owner, emit)
                # Read again after publishing acceptance, because completion
                # can race a duplicate reconnect during its first await.
                latest = await self._store.chat_turn_get(session_id=session_id, turn_id=receipt["turn_id"],
                                                          source_principal=source_binding)
                if source_principal is not None:
                    source_principal.require_current()
                if latest and "processing_outcome" in latest and (active is None or active.audit.closed):
                    if active is None or id(owner) not in active.terminal_notified:
                        if active is not None:
                            active.terminal_notified.add(id(owner))
                        await emit("chat_turn_terminal", {**latest, "replayed": True})
                return ack
            audit = TurnAudit(session_id, turn_id, request_id=request_id, source_principal=source_principal)
            live = LiveTurn(owner, audit, request_id)
            live.subscribers[id(owner)] = (owner, emit)
            self._live[(session_id, turn_id)] = live
            try:
                if source_principal is not None:
                    source_principal.require_current()
                await emit("chat_turn_accepted", accepted)
                if source_principal is not None:
                    source_principal.require_current()
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
        if outcome in {"completed", "awaiting_approval", "refused"} and audit.context_checkpoint is not None:
            receipt["context_checkpoint"] = dict(audit.context_checkpoint)
        return receipt

    async def _run(self, live: LiveTurn, accepted: dict, run, emit):
        audit = live.audit
        token = _audit.set(audit)
        outcome = "unavailable"
        try:
            if audit.source_principal is not None:
                audit.source_principal.require_current()
            running = {**accepted, "status": "running"}
            updated = await self._store.chat_turn_update(session_id=audit.session_id, turn_id=audit.turn_id,
                                                         status="running", receipt=running)
            if not updated:
                return
            if audit.source_principal is not None:
                audit.source_principal.require_current()
            value = await run()
            if isinstance(value, CommittedChatResult):
                audit.context_checkpoint = value.checkpoint(audit.session_id)
                audit.context_receipt = value.scope
                value = value.text
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
        if audit.context_receipt is not None:
            try:
                audit.context_receipt.assert_current()
            except (Exception, asyncio.CancelledError):
                outcome = "outcome_unknown" if audit.began else "failed"
                audit.context_checkpoint = None
        terminal = self._terminal(accepted, outcome, audit)
        try:
            committed = await self._store.chat_turn_update(session_id=audit.session_id, turn_id=audit.turn_id,
                                                           status="terminal", receipt=terminal)
            if committed:
                if audit.context_checkpoint is not None and "context_checkpoint" in terminal:
                    try:
                        audit.context_receipt.assert_current()
                    except (Exception, asyncio.CancelledError):
                        # The immutable receipt remains historical evidence. A
                        # superseded attachment cannot receive it as live media
                        # authority. Status reconciliation never replays work.
                        return
                for _owner, subscriber in list(live.subscribers.values()):
                    if id(_owner) in live.terminal_notified:
                        continue
                    live.terminal_notified.add(id(_owner))
                    try:
                        await subscriber("chat_turn_terminal", terminal)
                    except (Exception, asyncio.CancelledError):
                        # A disconnected/revoked subscriber cannot prevent a
                        # current subscriber from reconciling committed results.
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

    async def abort(self, *, owner, session_id: str, turn_id: str, request_id: str,
                    source_principal: PairedDevicePrincipal | None = None) -> dict:
        source_principal = _admitted_source(source_principal)
        turn_id, request_id = exact_uuid(turn_id), exact_uuid(request_id)
        live = self._live.get((session_id, turn_id))
        # All three identities were bound after durable acceptance. Avoid an
        # await here so cancellation can fence an as-yet-unscheduled runner.
        if (live is None or live.owner is not owner or live.request_id != request_id or live.task is None or live.task.done()
                or (source_principal is not None and (live.audit.source_principal is None
                    or live.audit.source_principal.storage_binding() != source_principal.storage_binding()))):
            return {"status": "not_active", "cancel_requested": False, "turn_id": turn_id, "request_id": request_id}
        if not live.audit.cancel_requested:
            live.audit.cancel_requested = True
            live.task.cancel()
        return {"status": "cancel_requested", "cancel_requested": True, "turn_id": turn_id, "request_id": request_id}

    def has_active_session(self, session_id: str) -> bool:
        """Include accepted/queued work until terminal settlement removes it."""
        return any(sid == session_id for sid, _turn in self._live)

    async def status(self, *, session_id: str, turn_id: str = "", request_id: str = "",
                     source_principal: PairedDevicePrincipal | None = None):
        source_principal = _admitted_source(source_principal)
        if not (turn_id or request_id):
            raise ChatTurnError("chat_turn_invalid_request")
        if turn_id:
            turn_id = exact_uuid(turn_id)
        if request_id:
            request_id = exact_uuid(request_id)
        await self.start()
        if source_principal is not None:
            source_principal.require_current()
        receipt = await self._store.chat_turn_get(session_id=session_id, turn_id=turn_id, request_id=request_id,
            source_principal=source_principal.storage_binding() if source_principal is not None else None)
        if source_principal is not None:
            source_principal.require_current()
        return receipt

    async def detach(self, owner) -> bool:
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
            if pending:
                _, pending = await asyncio.wait(pending, timeout=2)
            if pending:
                return False
        # A task cancelled before its first step schedules a terminal settlement
        # in its done callback. Let retained settlements commit before cleanup.
        settlements = {task for task in self._settlements if not task.done()}
        if settlements:
            _, pending = await asyncio.wait(settlements, timeout=2)
            if pending:
                return False
        return True


def get_chat_turn_manager(state) -> ChatTurnManager:
    manager = getattr(state, "chat_turns", None)
    if not isinstance(manager, ChatTurnManager):
        manager = state.chat_turns = ChatTurnManager(state)
    return manager
