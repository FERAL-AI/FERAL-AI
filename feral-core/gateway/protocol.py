"""
FERAL Gateway Protocol — Typed WebSocket RPC
===============================================
Every WS message is one of:
  - req:   { type: "req",   id: "uuid", method: "chat.send", params: {...} }
  - res:   { type: "res",   id: "uuid", ok: true|false, payload: {...} }
  - event: { type: "event", event: "stream.delta", payload: {...}, seq: N }

Method dispatch registry maps method names to async handler functions.
Correlation: every request gets a response with the same id.
Streaming uses events with incrementing seq numbers.
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import Callable, Awaitable, Optional, Any
from uuid import uuid4
from contextlib import asynccontextmanager

from agents.runtime_context_checkpoint import (
    RuntimeContextCoordinator, RuntimeContextError, RuntimeContextReadiness, legacy_context_mutation, runtime_coordinator,
)
from memory.runtime_session_checkpoint import CheckpointFence, CheckpointValidationError

from config.loader import feral_home

logger = logging.getLogger("feral.gateway")


class GatewayError(Exception):
    """Structured gateway error."""
    def __init__(self, code: str, message: str, details: dict = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


ERROR_CODES = {
    "NOT_FOUND": "Resource not found",
    "INVALID_PARAMS": "Invalid parameters",
    "METHOD_NOT_FOUND": "Method not found",
    "INTERNAL": "Internal server error",
    "UNAUTHORIZED": "Unauthorized",
    "RATE_LIMITED": "Rate limit exceeded",
    "TIMEOUT": "Request timed out",
}


def make_request(method: str, params: dict = None, req_id: str = None) -> dict:
    return {
        "type": "req",
        "id": req_id or str(uuid4())[:12],
        "method": method,
        "params": params or {},
    }


def make_response(req_id: str, payload: Any = None, ok: bool = True, error: dict = None) -> dict:
    msg = {"type": "res", "id": req_id, "ok": ok}
    if ok:
        msg["payload"] = payload or {}
    else:
        msg["error"] = error or {"code": "INTERNAL", "message": "Unknown error"}
    return msg


def make_event(event_name: str, payload: dict = None, seq: int = 0) -> dict:
    return {
        "type": "event",
        "event": event_name,
        "payload": payload or {},
        "seq": seq,
    }


MethodHandler = Callable[[str, dict, "GatewaySession"], Awaitable[Any]]


class MethodRegistry:
    """Registry of RPC method handlers."""

    def __init__(self):
        self._handlers: dict[str, MethodHandler] = {}

    def register(self, method: str, handler: MethodHandler):
        self._handlers[method] = handler

    def method(self, method_name: str):
        """Decorator for registering a method handler."""
        def decorator(fn: MethodHandler):
            self._handlers[method_name] = fn
            return fn
        return decorator

    def get(self, method: str) -> Optional[MethodHandler]:
        return self._handlers.get(method)

    @property
    def methods(self) -> list[str]:
        return list(self._handlers.keys())


class GatewaySession:
    """
    Represents a single WebSocket session with the typed protocol.
    Handles dispatch, correlation, and event sequencing.
    """

    def __init__(self, session_id: str, ws, registry: MethodRegistry):
        self.session_id = session_id
        self._ws = ws
        self._registry = registry
        self._seq = 0
        self._created_at = time.time()
        self.metadata: dict = {}

    async def send(self, msg: dict):
        try:
            await self._ws.send_json(msg)
        except Exception as e:
            logger.error(f"Gateway send error [{self.session_id[:8]}]: {e}")

    async def send_response(self, req_id: str, payload: Any = None):
        await self.send(make_response(req_id, payload, ok=True))

    async def send_error(self, req_id: str, code: str, message: str, details: dict = None):
        await self.send(make_response(req_id, ok=False, error={
            "code": code, "message": message, "details": details or {},
        }))

    async def emit(self, event_name: str, payload: dict = None):
        self._seq += 1
        await self.send(make_event(event_name, payload, self._seq))

    async def handle_message(self, raw: dict):
        """Dispatch an incoming message through the protocol."""
        msg_type = raw.get("type")

        if msg_type == "req":
            await self._handle_request(raw)
        elif msg_type == "res":
            pass
        elif msg_type == "event":
            pass
        else:
            # Legacy message — try to handle as old protocol for backward compat
            await self._handle_legacy(raw)

    async def _handle_request(self, raw: dict):
        req_id = raw.get("id", str(uuid4())[:8])
        method = raw.get("method", "")
        params = raw.get("params", {})

        handler = self._registry.get(method)
        if not handler:
            await self.send_error(req_id, "METHOD_NOT_FOUND", f"Method not found: {method}")
            return

        try:
            if method == "chat.send":
                params = {**params, "_gateway_request_id": req_id}
            result = await handler(self.session_id, params, self)
            await self.send_response(req_id, result)
        except GatewayError as e:
            await self.send_error(req_id, e.code, str(e), e.details)
        except Exception as e:
            logger.error(f"Handler error for {method}: {e}", exc_info=True)
            await self.send_error(req_id, "INTERNAL", str(e))

    async def _handle_legacy(self, raw: dict):
        """Handle old-style messages for backward compatibility."""
        msg_type = raw.get("type", "")
        legacy_method_map = {
            "text_command": "chat.send",
            "voice_config": "voice.config",
            "audio_chunk": "voice.audio",
            "ui_event": "ui.action",
            "device_register": "device.register",
            "vision_frame": "vision.frame",
            "vision_query": "vision.query",
            "biometric": "sensor.biometric",
        }
        method = legacy_method_map.get(msg_type)
        if method:
            fake_req = {
                "type": "req",
                "id": str(uuid4())[:8],
                "method": method,
                "params": raw.get("payload", raw),
            }
            await self._handle_request(fake_req)


def register_core_methods(registry: MethodRegistry, state):
    """Register all core gateway RPC methods."""

    def context_error(exc: RuntimeContextError) -> GatewayError:
        return GatewayError(exc.code, "Runtime context could not be confirmed; inspect the exact thread and its earlier actions.",
                            {"retry_safe": False, "effects_may_have_occurred": exc.effects_may_have_occurred,
                             "action_outcome": "unknown" if exc.effects_may_have_occurred else "not_asserted"})

    async def attachment_guard(session_id: str, session: GatewaySession, *, unsupported: str | None = None):
        """Respect the exact authenticated attachment even when its ledger is absent."""
        lookup = session.metadata.get("runtime_context_readiness")
        requested = session.metadata.get("context_checkpoint_requested") is True
        if not callable(lookup):
            if requested:
                raise context_error(RuntimeContextError("context_unavailable"))
            return
        try:
            readiness = await asyncio.wait_for(lookup(), timeout=10)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise context_error(RuntimeContextError("context_unavailable")) from None
        if not isinstance(readiness, RuntimeContextReadiness) or readiness.session_id != session_id:
            raise context_error(RuntimeContextError("context_unavailable"))
        if requested or readiness.managed:
            if not readiness.ready:
                raise context_error(RuntimeContextError(f"context_{readiness.state.value}"))
            if unsupported is not None:
                raise context_error(RuntimeContextError(f"managed_{unsupported}_unsupported"))
        elif readiness.state.value != "legacy":
            raise context_error(RuntimeContextError(f"context_{readiness.state.value}"))

    @asynccontextmanager
    async def legacy_guard(identities: tuple[str, ...], operation: str):
        try:
            async with legacy_context_mutation(state.orchestrator, state.memory, identities, operation):
                yield
        except RuntimeContextError as exc:
            raise GatewayError(exc.code, "This path cannot modify managed runtime context; use a new thread or inspect its status.") from None

    @registry.method("chat.capabilities")
    async def chat_capabilities(session_id: str, params: dict, session: GatewaySession):
        supported = callable(session.metadata.get("tracked_chat_send")) and callable(
            getattr(getattr(state, "memory", None), "chat_turn_claim", None)
        )
        result: dict[str, object] = {"turn_contract_versions": [1] if supported else [], "durable_receipts": supported,
                  "whole_turn_terminal": supported, "session_id": session_id}
        lookup = session.metadata.get("runtime_context_readiness")
        result.update({"context_checkpoint_versions": [], "context_recovery_versions": [], "context_ready": False, "context_state": "unavailable", "context_managed": False})
        if callable(lookup):
            readiness = await lookup()
            if isinstance(readiness, RuntimeContextReadiness) and readiness.session_id == session_id:
                result.update({"context_checkpoint_versions": [1], "context_ready": readiness.ready,
                               "context_state": readiness.state.value, "context_managed": readiness.managed,
                               "managed_unsupported_paths": ["voice", "handoff", "reset", "compact", "snapshot", "branch", "restore", "delete"]})
                if runtime_coordinator(state.orchestrator) is not None:
                    result["context_recovery_versions"] = [1]
                if readiness.ready and readiness.fence is not None:
                    checkpoint = public_checkpoint(readiness)
                    checkpoint.pop("attempt_id")
                    result["context_checkpoint"] = checkpoint
                elif readiness.state.value == "in_progress" and readiness.recovery_fence is not None:
                    result["context_recovery"] = {"contract_version": 1, "session_id": session_id,
                        "generation": readiness.recovery_fence.generation, "revision": readiness.recovery_fence.revision,
                        "attempt_id": readiness.recovery_fence.attempt_id, "state": "in_progress", "durable": True,
                        "requires_unknown_effects_acknowledgement": True}
        return result

    def public_checkpoint(readiness: RuntimeContextReadiness) -> dict:
        fence = readiness.fence
        if not readiness.ready or fence is None:
            raise context_error(RuntimeContextError("context_recovery_unavailable"))
        return {"contract_version": 1, "session_id": readiness.session_id,
                "generation": fence.generation, "revision": fence.revision, "attempt_id": fence.attempt_id,
                "durable": True, "initialized": readiness.initialized, "omissions": dict(readiness.omissions)}

    async def recovery_terms(session_id: str, params: dict, session: GatewaySession, *, acknowledge: bool) -> tuple[RuntimeContextCoordinator, CheckpointFence]:
        keys = {"contract_version", "session_id", "generation", "revision", "attempt_id"}
        if acknowledge:
            keys.add("acknowledge_unknown_effects")
        if not isinstance(params, dict) or set(params) != keys or type(params.get("contract_version")) is not int or params["contract_version"] != 1:
            raise context_error(RuntimeContextError("context_recovery_invalid"))
        if params.get("session_id") != session_id or session.session_id != session_id:
            raise context_error(RuntimeContextError("context_recovery_session_mismatch"))
        if acknowledge and params.get("acknowledge_unknown_effects") is not True:
            raise context_error(RuntimeContextError("context_recovery_invalid"))
        try:
            fence = CheckpointFence(session_id, params["generation"], params["revision"], params["attempt_id"])
        except CheckpointValidationError:
            raise context_error(RuntimeContextError("context_recovery_invalid")) from None
        lookup = session.metadata.get("runtime_context_readiness")
        coordinator = runtime_coordinator(state.orchestrator)
        if coordinator is None or not callable(lookup):
            raise context_error(RuntimeContextError("context_recovery_unavailable"))
        if acknowledge:
            from agents.chat_turns import ChatTurnManager
            manager = getattr(state, "chat_turns", None)
            if coordinator.has_writers(session_id) or (isinstance(manager, ChatTurnManager) and manager.has_active_session(session_id)):
                raise context_error(RuntimeContextError("context_recovery_busy"))
        try:
            readiness = await asyncio.wait_for(lookup(), timeout=10)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise context_error(RuntimeContextError("context_recovery_unavailable")) from None
        if not isinstance(readiness, RuntimeContextReadiness) or readiness.session_id != session_id or not readiness.managed:
            raise context_error(RuntimeContextError("context_recovery_unavailable"))
        if runtime_coordinator(state.orchestrator) is not coordinator or state.memory is not coordinator.store:
            raise context_error(RuntimeContextError("context_recovery_unavailable"))
        return coordinator, fence

    @registry.method("session.context.recover")
    async def context_recover(session_id: str, params: dict, session: GatewaySession):
        from agents.chat_turns import ChatTurnManager
        coordinator, fence = await recovery_terms(session_id, params, session, acknowledge=True)

        def active_turn() -> bool:
            manager = getattr(state, "chat_turns", None)
            return (runtime_coordinator(state.orchestrator) is not coordinator or state.memory is not coordinator.store
                    or (isinstance(manager, ChatTurnManager) and manager.has_active_session(session_id)))

        try:
            ready = await coordinator.recover(session_id, fence, active_turn=active_turn)
        except RuntimeContextError as exc:
            raise context_error(exc) from None
        if runtime_coordinator(state.orchestrator) is not coordinator or state.memory is not coordinator.store:
            raise context_error(RuntimeContextError("context_recovery_unavailable", effects_may_have_occurred=True))
        return {"contract_version": 1, "session_id": session_id, "status": "recovered", "context_ready": True,
                "durable": True, "replayed": False, "action_outcome": "unknown", "context_checkpoint": public_checkpoint(ready)}

    @registry.method("session.context.recoveryStatus")
    async def context_recovery_status(session_id: str, params: dict, session: GatewaySession):
        coordinator, fence = await recovery_terms(session_id, params, session, acknowledge=False)
        try:
            status, readiness = await coordinator.recovery_status(session_id, fence)
        except RuntimeContextError as exc:
            raise context_error(exc) from None
        if runtime_coordinator(state.orchestrator) is not coordinator or state.memory is not coordinator.store:
            raise context_error(RuntimeContextError("context_recovery_unavailable", effects_may_have_occurred=True))
        result: dict[str, object] = {"contract_version": 1, "session_id": session_id, "status": status,
                  "recovered": status == "recovered", "context_ready": status == "recovered" and readiness.ready,
                  "durable": True, "replayed": False, "action_outcome": "unknown"}
        if status == "recovered" and readiness.ready:
            result["context_checkpoint"] = public_checkpoint(readiness)
        return result

    @registry.method("chat.send")
    async def chat_send(session_id: str, params: dict, session: GatewaySession):
        text = params.get("text", "")
        context = params.get("context", {})
        if not text:
            raise GatewayError("INVALID_PARAMS", "text is required")
        await attachment_guard(session_id, session)

        if params.get("turn_contract_version") is not None:
            from agents.chat_turns import ChatTurnError
            if type(params["turn_contract_version"]) is not int or params["turn_contract_version"] != 1:
                raise GatewayError("INVALID_PARAMS", "Unsupported tracked turn contract")
            submit = session.metadata.get("tracked_chat_send")
            if not callable(submit):
                raise GatewayError("UNAVAILABLE", "Tracked chat transport is unavailable")

            async def emit(kind, payload):
                if kind == "chat_turn_terminal":
                    await session.emit("chat.turn_terminal", payload)
                elif kind == "error":
                    await session.emit("chat.turn_error", payload)

            try:
                return await submit(params, params.get("_gateway_request_id"), emit)
            except ChatTurnError as exc:
                raise GatewayError(exc.code, "Tracked chat request was not accepted") from None
            except RuntimeContextError as exc:
                raise GatewayError(exc.code, "Runtime context could not be confirmed; inspect the exact thread and its earlier actions.",
                                   {"retry_safe": False, "effects_may_have_occurred": exc.effects_may_have_occurred,
                                    "action_outcome": "unknown" if exc.effects_may_have_occurred else "not_asserted"}) from None

        async def run():
            if state.memory:
                state.memory.working_push(session_id, {"role": "user", "text": text})
            await session.emit("chat.thinking", {"status": "processing"})
            if state.orchestrator:
                await state.orchestrator.handle_command_stream(session_id=session_id, text=text, context=context)
        coordinator = runtime_coordinator(state.orchestrator)
        if coordinator is None:
            await run()
        else:
            try:
                async with coordinator.write_scope(session_id, command_handoff=True):
                    await run()
            except RuntimeContextError as exc:
                raise GatewayError(exc.code, "Runtime context could not be confirmed; inspect the exact thread and its earlier actions.") from None
        return {"status": "delivered"}

    @registry.method("chat.abort")
    async def chat_abort(session_id: str, params: dict, session: GatewaySession):
        from agents.chat_turns import ChatTurnError, exact_uuid, get_chat_turn_manager
        try:
            return await get_chat_turn_manager(state).abort(owner=session._ws, session_id=session_id,
                                                            turn_id=exact_uuid(params.get("turn_id")), request_id=exact_uuid(params.get("request_id")))
        except ChatTurnError as exc:
            raise GatewayError(exc.code, "Exact tracked turn identity is required") from None

    @registry.method("chat.status")
    async def chat_status(session_id: str, params: dict, session: GatewaySession):
        from agents.chat_turns import ChatTurnError, get_chat_turn_manager
        try:
            receipt = await get_chat_turn_manager(state).status(session_id=session_id,
                                                               turn_id=params.get("turn_id", ""), request_id=params.get("request_id", ""))
            return {"receipt": receipt, "found": receipt is not None}
        except ChatTurnError as exc:
            raise GatewayError(exc.code, "Exact tracked turn identity is required") from None

    @registry.method("session.reset")
    async def session_reset(session_id: str, params: dict, session: GatewaySession):
        await attachment_guard(session_id, session, unsupported="reset")
        async with legacy_guard((session_id,), "reset"):
            return await _session_reset(session_id, params, session)

    async def _session_reset(session_id: str, params: dict, session: GatewaySession):
        # F2 — end-of-session trigger. Compact whatever history we
        # have before throwing it away; that preserves the session as
        # a real episode row instead of letting it vanish.
        if state.orchestrator and state.memory:
            history = state.orchestrator.conversation_history.get(session_id, [])
            if history:
                try:
                    await state.memory.compact_session(
                        session_id, history, llm=state.orchestrator.llm,
                    )
                except Exception:
                    logger.exception("session.reset: end-of-session compaction failed")
        if state.memory:
            state.memory.working_clear(session_id)
        if state.orchestrator:
            state.orchestrator.conversation_history.pop(session_id, None)
            state.orchestrator._turns_since_compaction.pop(session_id, None)
            # F6: the backlog and idle clocks belong to the session
            # that just ended. Left behind they would make a brand-new
            # session with the same id look overdue on its first turn.
            state.orchestrator._pending_since.pop(session_id, None)
            state.orchestrator._session_last_turn_at.pop(session_id, None)
        return {"status": "reset"}

    @registry.method("session.compact")
    async def session_compact(session_id: str, params: dict, session: GatewaySession):
        await attachment_guard(session_id, session, unsupported="compact")
        async with legacy_guard((session_id,), "compact"):
            return await _session_compact(session_id, params, session)

    async def _session_compact(session_id: str, params: dict, session: GatewaySession):
        if state.orchestrator and state.memory:
            history = state.orchestrator.conversation_history.get(session_id, [])
            result = await state.memory.compact_session(
                session_id, history, llm=state.orchestrator.llm,
            )
            if result.get("compacted") and result.get("history"):
                state.orchestrator.conversation_history[session_id] = result["history"]
            return result
        return {"compacted": False}

    @registry.method("session.snapshot")
    async def session_snapshot(session_id: str, params: dict, session: GatewaySession):
        await attachment_guard(session_id, session, unsupported="snapshot")
        async with legacy_guard((session_id,), "snapshot"):
            return await _session_snapshot(session_id, params, session)

    async def _session_snapshot(session_id: str, params: dict, session: GatewaySession):
        if not state.memory:
            raise GatewayError("NOT_FOUND", "Memory store not initialized")
        history = state.orchestrator.conversation_history.get(session_id, []) if state.orchestrator else []
        snapshot = await state.memory.snapshot_session(
            session_id=session_id,
            history=history,
            label=params.get("label", ""),
            branch_name=params.get("branch_name", "main"),
        )
        return snapshot

    @registry.method("session.snapshots")
    async def session_snapshots(session_id: str, params: dict, session: GatewaySession):
        if not state.memory:
            return {"snapshots": []}
        snapshots = await state.memory.list_snapshots(
            session_id=params.get("session_id", session_id),
            branch_name=params.get("branch_name", ""),
            limit=int(params.get("limit", 50)),
        )
        return {"snapshots": snapshots}

    @registry.method("session.branch")
    async def session_branch(session_id: str, params: dict, session: GatewaySession):
        await attachment_guard(session_id, session, unsupported="branch")
        if not state.memory:
            raise GatewayError("NOT_FOUND", "Memory store not initialized")

        source_snapshot_id = params.get("snapshot_id", "")
        if source_snapshot_id:
            source_snapshot = await state.memory.get_snapshot(source_snapshot_id)
            if not source_snapshot:
                raise GatewayError("NOT_FOUND", f"Snapshot not found: {source_snapshot_id}")
        else:
            base = await session_snapshot(session_id, {"label": "auto-branch-source", "branch_name": "main"}, session)
            source_snapshot_id = base["snapshot_id"]
            source_snapshot = await state.memory.get_snapshot(source_snapshot_id)

        branch_name = params.get("branch_name", f"branch-{int(time.time())}")
        branch_session_id = params.get("target_session_id", f"{session_id}:{branch_name}:{str(uuid4())[:6]}")

        async with legacy_guard((source_snapshot["session_id"], branch_session_id), "branch"):
            state.memory.working_replace(branch_session_id, source_snapshot.get("working", []))
            if state.orchestrator:
                state.orchestrator.conversation_history[branch_session_id] = source_snapshot.get("history", [])

            branched_snapshot = await state.memory.snapshot_session(
                session_id=branch_session_id,
                history=source_snapshot.get("history", []),
                label=params.get("label", f"branch from {source_snapshot_id}"),
                branch_name=branch_name,
                source_snapshot_id=source_snapshot_id,
            )
            return {
                "status": "branched",
                "source_snapshot_id": source_snapshot_id,
                "target_session_id": branch_session_id,
                "snapshot": branched_snapshot,
            }

    @registry.method("session.restore")
    async def session_restore(session_id: str, params: dict, session: GatewaySession):
        await attachment_guard(session_id, session, unsupported="restore")
        if not state.memory:
            raise GatewayError("NOT_FOUND", "Memory store not initialized")
        snapshot_id = params.get("snapshot_id", "")
        if not snapshot_id:
            raise GatewayError("INVALID_PARAMS", "snapshot_id is required")
        snapshot = await state.memory.get_snapshot(snapshot_id)
        if not snapshot:
            raise GatewayError("NOT_FOUND", f"Snapshot not found: {snapshot_id}")

        as_new_session = bool(params.get("as_new_session", False))
        target_session_id = params.get("target_session_id")
        if not target_session_id:
            target_session_id = f"{session_id}:restore:{str(uuid4())[:6]}" if as_new_session else session_id

        async with legacy_guard((snapshot["session_id"], target_session_id), "restore"):
            state.memory.working_replace(target_session_id, snapshot.get("working", []))
            if state.orchestrator:
                state.orchestrator.conversation_history[target_session_id] = snapshot.get("history", [])

            restore_snapshot = await state.memory.snapshot_session(
                session_id=target_session_id,
                history=snapshot.get("history", []),
                label=params.get("label", f"restore {snapshot_id}"),
                branch_name=snapshot.get("branch_name", "main"),
                source_snapshot_id=snapshot_id,
            )
            return {
                "status": "restored",
                "target_session_id": target_session_id,
                "restored_from_snapshot_id": snapshot_id,
                "snapshot": restore_snapshot,
            }

    @registry.method("voice.config")
    async def voice_config(session_id: str, params: dict, session: GatewaySession):
        from bridges.client_voice_configuration import configure_client_voice, ClientVoiceConfigurationError

        mode = params.get("mode", "realtime")
        if mode != "disabled":
            await attachment_guard(session_id, session, unsupported="voice")
        try:
            return await configure_client_voice(state, session_id, session._ws, params)
        except ClientVoiceConfigurationError as exc:
            raise GatewayError(exc.code, str(exc)) from None

    @registry.method("voice.audio")
    async def voice_audio(session_id: str, params: dict, session: GatewaySession):
        await attachment_guard(session_id, session, unsupported="voice")
        audio_b64 = params.get("data_b64", "")
        if state.voice_router and audio_b64:
            await state.voice_router.handle_audio_from_client(
                session_id=session_id,
                audio_b64=audio_b64,
                chunk_index=params.get("chunk_index", 0),
                is_final=params.get("is_final", False),
                encoding=params.get("encoding", "pcm16"),
                sample_rate=params.get("sample_rate", 24000),
            )
        return {"received": True}

    @registry.method("memory.search")
    async def memory_search(session_id: str, params: dict, session: GatewaySession):
        query = params.get("query", "")
        limit = params.get("limit", 10)
        if not query or not state.memory:
            return {"results": []}
        results = await state.memory.search_all(query, limit=limit)
        return {"results": results}

    @registry.method("taskflow.create")
    async def taskflow_create(session_id: str, params: dict, session: GatewaySession):
        if not state.taskflows:
            raise GatewayError("NOT_FOUND", "TaskFlow runtime not initialized")
        steps = params.get("steps", [])
        if not isinstance(steps, list) or not steps:
            raise GatewayError("INVALID_PARAMS", "steps (non-empty list) is required")
        flow = state.taskflows.create_flow(
            session_id=params.get("session_id", session_id),
            title=params.get("title", "Background TaskFlow"),
            steps=steps,
            context=params.get("context", {}),
        )
        return flow

    @registry.method("taskflow.list")
    async def taskflow_list(session_id: str, params: dict, session: GatewaySession):
        if not state.taskflows:
            return {"flows": []}
        flows = state.taskflows.list_flows(
            session_id=params.get("session_id", ""),
            status=params.get("status", ""),
            limit=int(params.get("limit", 50)),
        )
        return {"flows": flows}

    @registry.method("taskflow.get")
    async def taskflow_get(session_id: str, params: dict, session: GatewaySession):
        if not state.taskflows:
            raise GatewayError("NOT_FOUND", "TaskFlow runtime not initialized")
        flow_id = params.get("flow_id", "")
        if not flow_id:
            raise GatewayError("INVALID_PARAMS", "flow_id is required")
        flow = state.taskflows.get_flow(flow_id)
        if not flow:
            raise GatewayError("NOT_FOUND", f"TaskFlow not found: {flow_id}")
        return flow

    @registry.method("taskflow.resume")
    async def taskflow_resume(session_id: str, params: dict, session: GatewaySession):
        if not state.taskflows:
            raise GatewayError("NOT_FOUND", "TaskFlow runtime not initialized")
        flow_id = params.get("flow_id", "")
        if not flow_id:
            raise GatewayError("INVALID_PARAMS", "flow_id is required")
        flow = state.taskflows.resume_flow(flow_id)
        if not flow:
            raise GatewayError("NOT_FOUND", f"TaskFlow not found: {flow_id}")
        return flow

    @registry.method("taskflow.cancel")
    async def taskflow_cancel(session_id: str, params: dict, session: GatewaySession):
        if not state.taskflows:
            raise GatewayError("NOT_FOUND", "TaskFlow runtime not initialized")
        flow_id = params.get("flow_id", "")
        if not flow_id:
            raise GatewayError("INVALID_PARAMS", "flow_id is required")
        flow = state.taskflows.cancel_flow(flow_id)
        if not flow:
            raise GatewayError("NOT_FOUND", f"TaskFlow not found: {flow_id}")
        return flow

    @registry.method("identity.get")
    async def identity_get(session_id: str, params: dict, session: GatewaySession):
        identity_path = feral_home() / "identity.yaml"
        if identity_path.exists():
            try:
                import yaml
                with open(identity_path) as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                pass
        return {"name": "FERAL"}

    @registry.method("identity.update")
    async def identity_update(session_id: str, params: dict, session: GatewaySession):
        import yaml

        identity_path = feral_home() / "identity.yaml"
        with open(identity_path, "w") as f:
            yaml.dump(params, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return {"ok": True}

    @registry.method("config.get")
    async def config_get(session_id: str, params: dict, session: GatewaySession):
        return state.config.to_client_safe_dict()

    @registry.method("config.set")
    async def config_set(session_id: str, params: dict, session: GatewaySession):
        section = params.get("section", "")
        key = params.get("key", "")
        value = params.get("value")
        if not section or not key:
            raise GatewayError("INVALID_PARAMS", "section and key required")
        state.config.update_settings(section, key, value)
        return {"ok": True}

    @registry.method("node.invoke")
    async def node_invoke(session_id: str, params: dict, session: GatewaySession):
        node_id = params.get("node_id", "")
        command = params.get("command", "")
        cmd_params = params.get("params", {})
        timeout = params.get("timeout", 10.0)
        if not node_id or not command:
            raise GatewayError("INVALID_PARAMS", "node_id and command required")
        if not state.daemons.get(node_id):
            raise GatewayError("NOT_FOUND", f"Node not found: {node_id}")
        # Use HardwareMesh.invoke which waits for the response
        if state.hardware_mesh:
            result = await state.hardware_mesh.invoke(node_id, command, cmd_params, timeout)
            if not result.get("success", True) and "error" in result:
                raise GatewayError("INTERNAL", result["error"])
            return result
        # Fallback: fire-and-forget if mesh not available.
        #
        # This used to send ``{"type": "command", "request_id", "command",
        # "args"}``. That is not a HUP frame in any version: ``command``
        # is a deprecated alias removed in 2026.7.0 (HUP_SPEC.md section
        # 5.5's alias table), and the shape put the fields at the top
        # level with no ``payload``, no ``hup_version`` and no ``ts``. No
        # current SDK has a branch for it, so the fallback dispatched into
        # silence and reported ``{"dispatched": true}``.
        # ``tests/test_hup_protocol.py`` asserted the alias was gone from
        # mesh.py and tool_runner.py and never looked here.
        from hardware.action_frames import build_action_request

        ws = state.daemons[node_id]
        gate = build_action_request(
            node_id, command, cmd_params, timeout_ms=int(timeout * 1000),
        )
        if not gate.allowed:
            raise GatewayError("FORBIDDEN", gate.denied_reason)
        await ws.send_json(gate.frame)
        return {"dispatched": True, "request_id": gate.action_id}

    @registry.method("hardware.execute")
    async def hardware_execute(session_id: str, params: dict, session: GatewaySession):
        if not state.device_registry:
            raise GatewayError("NOT_FOUND", "No device registry")
        from hardware.protocol import HUPAction, HUPActionType
        action = HUPAction(
            device_id=params.get("device_id", ""),
            capability_id=params.get("capability_id", ""),
            action_type=HUPActionType(params.get("action_type", "execute")),
            parameters=params.get("parameters", {}),
            timeout_ms=params.get("timeout_ms", 5000),
        )
        result = await state.device_registry.execute_action(action)
        return result.model_dump()

    @registry.method("ui.action")
    async def ui_action(session_id: str, params: dict, session: GatewaySession):
        await attachment_guard(session_id, session)
        action_id = params.get("action_id", "")
        event = params.get("event", "tap")
        value = params.get("value")
        app_id = params.get("app_id")
        screen_id = params.get("screen_id")
        if state.orchestrator:
            await state.orchestrator.handle_ui_event(
                session_id,
                action_id,
                event,
                value,
                app_id=app_id,
                screen_id=screen_id,
            )
        return {"handled": True}

    @registry.method("vision.frame")
    async def vision_frame(session_id: str, params: dict, session: GatewaySession):
        frame_payload = params
        virtual_node = f"webclient_{session_id[:8]}"
        state.vision_buffer.push(virtual_node, frame_payload)
        state.perception.update_vision(session_id, state.vision_buffer, virtual_node)
        return {"received": True}

    @registry.method("sensor.biometric")
    async def sensor_biometric(session_id: str, params: dict, session: GatewaySession):
        if state.orchestrator:
            state.orchestrator.update_biometric(session_id, params)
        state.perception.update_sensors(session_id, params)
        if state.somatic_engine:
            state.somatic_engine.update_from_perception_frame(session_id, params)
        return {"received": True}
