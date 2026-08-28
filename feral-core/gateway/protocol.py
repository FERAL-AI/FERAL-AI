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
import json
import logging
import time
from typing import Callable, Awaitable, Optional, Any
from uuid import uuid4

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

    @registry.method("chat.send")
    async def chat_send(session_id: str, params: dict, session: GatewaySession):
        text = params.get("text", "")
        context = params.get("context", {})
        if not text:
            raise GatewayError("INVALID_PARAMS", "text is required")

        if state.memory:
            state.memory.working_push(session_id, {"role": "user", "text": text})

        await session.emit("chat.thinking", {"status": "processing"})

        if state.orchestrator:
            await state.orchestrator.handle_command_stream(
                session_id=session_id, text=text, context=context,
            )
        return {"status": "delivered"}

    @registry.method("chat.abort")
    async def chat_abort(session_id: str, params: dict, session: GatewaySession):
        return {"status": "aborted"}

    @registry.method("session.reset")
    async def session_reset(session_id: str, params: dict, session: GatewaySession):
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
        mode = params.get("mode", "realtime")
        if state.voice_router:
            state.voice_router.set_session_voice_mode(session_id, mode)
            if mode == "disabled":
                await state.voice_router.stop_session_voice(session_id)
        return {"mode": mode, "status": "ok"}

    @registry.method("voice.audio")
    async def voice_audio(session_id: str, params: dict, session: GatewaySession):
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
        # Fallback: fire-and-forget if mesh not available
        ws = state.daemons[node_id]
        req_id = str(uuid4())[:8]
        await ws.send_json({
            "type": "command", "request_id": req_id,
            "command": command, "args": cmd_params,
        })
        return {"dispatched": True, "request_id": req_id}

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
