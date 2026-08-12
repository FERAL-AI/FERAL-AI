"""
FERAL MCP Server — Expose Hardware to Any AI Agent
======================================================
This makes FERAL an MCP server. That means:
- Claude Desktop can control your robot
- Cursor can read your glasses' heart rate
- Any MCP client gets access to FERAL's hardware capabilities

FERAL speaks MCP both ways: it can host MCP servers and consume remote ones.
FERAL IS an MCP server — any AI can use your hardware.

This is the bridge between the AI agent world and the physical world.

MCP Spec: JSON-RPC 2.0 over stdio/SSE/HTTP
Implements: tools, resources, prompts
"""

from __future__ import annotations
import json
import logging
import sys
from typing import Any, Optional, TYPE_CHECKING

from skills.call_context import bind_context

if TYPE_CHECKING:
    from hardware.protocol import DeviceCapability, DeviceRegistry
    from memory.store import MemoryStore
    from perception.fusion import PerceptionEngine

logger = logging.getLogger("feral.mcp.server")


class FeralMCPServer:
    """
    FERAL as an MCP server.
    Exposes hardware devices, memory, and perception as MCP tools + resources.
    """

    def __init__(
        self,
        device_registry: Optional["DeviceRegistry"] = None,
        memory: Optional["MemoryStore"] = None,
        perception: Optional["PerceptionEngine"] = None,
        skill_registry: Optional[Any] = None,
        skill_executor: Optional[Any] = None,
    ):
        self._devices = device_registry
        self._memory = memory
        self._perception = perception
        # PR 11: optional FERAL skill projection. When both ``skill_registry``
        # and ``skill_executor`` are wired, ``handle_tools_list`` exports a
        # *safe subset* of FERAL skills as ``feral_skill_<skill>__<endpoint>``
        # MCP tools — gated by the ``mcp`` surface deny list AND the
        # manifest-aware safety resolver (only AUTO endpoints are exposed
        # by default; CONFIRM/DENY require explicit operator opt-in).
        # The projection is OFF by default and must be explicitly turned
        # on via ``enable_skill_projection`` so booting the brain doesn't
        # silently expose skills to whichever MCP client connects first.
        self._skill_registry = skill_registry
        self._skill_executor = skill_executor
        self._skill_projection_enabled = False
        self._server_info = {
            "name": "feral",
            "version": "0.7.0",
        }

    # ─────────────────────────────────────────
    # MCP Protocol: initialize
    # ─────────────────────────────────────────

    def handle_initialize(self, params: dict) -> dict:
        capabilities = {
            "tools": {"listChanged": True},
            "resources": {"subscribe": False, "listChanged": True},
            "prompts": {"listChanged": False},
        }
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": capabilities,
            "serverInfo": self._server_info,
        }

    # ─────────────────────────────────────────
    # MCP Protocol: tools/list
    # ─────────────────────────────────────────

    def handle_tools_list(self) -> dict:
        tools = []

        # Core tools
        tools.append({
            "name": "feral_list_devices",
            "description": "List all connected hardware devices in the FERAL ecosystem",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        })

        tools.append({
            "name": "feral_device_status",
            "description": "Get the status and capabilities of a specific device",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "The device ID to query"},
                },
                "required": ["device_id"],
            },
        })

        tools.append({
            "name": "feral_read_sensor",
            "description": "Read a sensor value from a connected device (heart rate, SpO2, temperature, UV, steps, etc.)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "Device to read from"},
                    "sensor": {"type": "string", "description": "Sensor type: heart_rate, spo2, temperature, uv, steps"},
                },
                "required": ["device_id", "sensor"],
            },
        })

        tools.append({
            "name": "feral_execute_action",
            "description": "Execute a hardware action on a connected device (move robot, display notification, capture photo, etc.)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "Target device"},
                    "capability_id": {"type": "string", "description": "Capability to execute"},
                    "action_type": {
                        "type": "string",
                        "enum": ["read", "write", "execute", "stream_start", "stream_stop", "configure", "calibrate", "reset", "status"],
                    },
                    "parameters": {"type": "object", "description": "Action parameters"},
                },
                "required": ["device_id", "capability_id"],
            },
        })

        tools.append({
            "name": "feral_memory_query",
            "description": "Query FERAL's memory — notes, episodes, knowledge graph. Ask about the user's preferences, history, or learned facts.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for in memory"},
                    "memory_tier": {
                        "type": "string",
                        "enum": ["notes", "episodes", "knowledge", "all"],
                        "description": "Which memory tier to search",
                    },
                },
                "required": ["query"],
            },
        })

        tools.append({
            "name": "feral_perception_snapshot",
            "description": "Get the current fused perception state — what FERAL sees, hears, and senses right now",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        })

        tools.append({
            "name": "feral_find_devices_by_capability",
            "description": "Find all devices that have a specific capability category (sensor, actuator, display, audio, etc.)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["sensor", "actuator", "display", "audio", "network", "compute"],
                    },
                },
                "required": ["category"],
            },
        })

        # Dynamically add device-specific tools
        if self._devices:
            for device in self._devices.list_devices():
                for cap in device.capabilities:
                    tools.append({
                        "name": f"feral_{device.device_id}_{cap.id}",
                        "description": f"[{device.name}] {cap.description}",
                        "inputSchema": self._capability_to_schema(cap),
                    })

        # PR 11: project the safe subset of FERAL skills.
        for projected in self._projected_skill_tools():
            tools.append(projected)

        return {"tools": tools}

    # ─────────────────────────────────────────
    # PR 11: FERAL skill projection
    # ─────────────────────────────────────────

    _SKILL_TOOL_PREFIX = "feral_skill_"

    def configure_skill_projection(
        self,
        *,
        skill_registry: Optional[Any] = None,
        skill_executor: Optional[Any] = None,
        enabled: bool = True,
    ) -> dict:
        """Wire (or rewire) the skill projection at runtime.

        Returns a snapshot ``{enabled, ready, projected_count}`` so the
        operator can verify the wiring took effect — no silent
        no-ops."""
        if skill_registry is not None:
            self._skill_registry = skill_registry
        if skill_executor is not None:
            self._skill_executor = skill_executor
        self._skill_projection_enabled = bool(enabled)
        return self.projection_status()

    def projection_status(self) -> dict:
        """Reflect the current projection state honestly."""
        ready = bool(
            self._skill_projection_enabled
            and self._skill_registry is not None
            and self._skill_executor is not None
        )
        projected_count = len(self._projected_skill_tools()) if ready else 0
        return {
            "enabled": self._skill_projection_enabled,
            "ready": ready,
            "projected_count": projected_count,
            "registry_wired": self._skill_registry is not None,
            "executor_wired": self._skill_executor is not None,
        }

    def _projected_skill_tools(self) -> list[dict]:
        """Project FERAL skill endpoints as MCP tools when policy permits.

        Eligibility rules:
        * ``skill_registry`` AND ``skill_executor`` must be wired.
        * The tool must pass the ``mcp`` surface deny list
          (``security.dangerous_tools.is_tool_allowed(name, 'mcp')``).
        * The manifest-aware resolver must verdict ``AUTO``. Skills with
          declared ``confirm`` / ``deny`` tiers (or that hit the danger
          map) are intentionally excluded: an external MCP client has
          no operator-present approval channel, so CONFIRM cannot be
          satisfied across that boundary.

        Returns a list of MCP tool definitions; never raises.
        """
        if not self._skill_projection_enabled:
            return []
        if not self._skill_registry or not self._skill_executor:
            return []

        try:
            from security.dangerous_tools import is_tool_allowed
            from security.safety_resolver import LEVEL_AUTO, resolve_policy
        except Exception:
            logger.exception("MCP projection skipped: safety module import failed")
            return []

        out: list[dict] = []
        skills = getattr(self._skill_registry, "skills", {}) or {}
        for skill_id, skill in skills.items():
            for endpoint in getattr(skill, "endpoints", []) or []:
                feral_tool_id = f"{skill_id}__{endpoint.id}"
                if not is_tool_allowed(feral_tool_id, "mcp"):
                    continue
                try:
                    decision = resolve_policy(
                        feral_tool_id, args={}, surface="mcp",
                        registry=self._skill_registry,
                    )
                except Exception:
                    continue
                if decision.level != LEVEL_AUTO:
                    continue
                out.append({
                    "name": f"{self._SKILL_TOOL_PREFIX}{feral_tool_id}",
                    "description": getattr(endpoint, "description", "") or f"FERAL skill {feral_tool_id}",
                    "inputSchema": self._endpoint_to_schema(endpoint),
                })
        return out

    @staticmethod
    def _endpoint_to_schema(endpoint) -> dict:
        """Best-effort conversion of a SkillEndpoint's params to JSON Schema."""
        props: dict[str, dict] = {}
        required: list[str] = []
        type_map = {
            "string": "string", "number": "number", "integer": "integer",
            "boolean": "boolean", "array": "array", "object": "object",
        }
        for p in getattr(endpoint, "params", []) or []:
            prop_type = type_map.get(getattr(p, "type", "string"), "string")
            entry: dict = {"type": prop_type, "description": getattr(p, "description", "") or ""}
            enum_vals = getattr(p, "enum", None)
            if enum_vals:
                entry["enum"] = list(enum_vals)
            props[p.name] = entry
            if getattr(p, "required", False):
                required.append(p.name)
        return {"type": "object", "properties": props, "required": required}

    # ─────────────────────────────────────────
    # MCP Protocol: tools/call
    # ─────────────────────────────────────────

    async def handle_tools_call(self, name: str, arguments: dict) -> dict:
        try:
            if name == "feral_list_devices":
                return self._call_list_devices()
            elif name == "feral_device_status":
                return self._call_device_status(arguments)
            elif name == "feral_read_sensor":
                return await self._call_read_sensor(arguments)
            elif name == "feral_execute_action":
                return await self._call_execute_action(arguments)
            elif name == "feral_memory_query":
                return await self._call_memory_query(arguments)
            elif name == "feral_perception_snapshot":
                return self._call_perception_snapshot()
            elif name == "feral_find_devices_by_capability":
                return self._call_find_by_capability(arguments)
            elif name.startswith(self._SKILL_TOOL_PREFIX):
                # PR 11: dispatch a projected FERAL skill. Re-check policy
                # at call time so a manifest update that bumps a tool to
                # CONFIRM/DENY takes effect immediately for in-flight MCP
                # sessions that already cached tools/list output.
                return await self._call_projected_skill(name, arguments)
            elif name.startswith("feral_"):
                return await self._call_dynamic_capability(name, arguments)
            else:
                return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
        except Exception as e:
            logger.error(f"MCP tool call error: {e}")
            return {"content": [{"type": "text", "text": f"Error: {str(e)}"}], "isError": True}

    async def _call_projected_skill(self, mcp_name: str, arguments: dict) -> dict:
        """Execute a projected FERAL skill on behalf of an MCP client.

        Re-runs the surface deny check + safety resolver at call time
        so a runtime policy update reaches in-flight MCP sessions even
        if they cached the older ``tools/list`` response. Anything not
        AUTO is refused with an ``isError`` payload — the MCP boundary
        has no operator-present approval channel."""
        if not self._skill_projection_enabled:
            return {"content": [{"type": "text", "text": "FERAL skill projection is disabled on this MCP server. Enable it from Settings → MCP or via FERAL_MCP_PROJECT_SKILLS=1."}], "isError": True}
        if not self._skill_registry or not self._skill_executor:
            return {"content": [{"type": "text", "text": "FERAL skill projection is enabled but the registry/executor isn't wired yet — brain still booting."}], "isError": True}

        feral_tool_id = mcp_name[len(self._SKILL_TOOL_PREFIX):]
        try:
            from security.dangerous_tools import is_tool_allowed
            from security.safety_resolver import LEVEL_AUTO, resolve_policy
        except Exception:
            return {"content": [{"type": "text", "text": "Safety policy module unavailable; refusing projected skill call."}], "isError": True}

        if not is_tool_allowed(feral_tool_id, "mcp"):
            return {"content": [{"type": "text", "text": f"Tool {feral_tool_id} is denied on the MCP surface."}], "isError": True}

        decision = resolve_policy(
            feral_tool_id, args=arguments or {}, surface="mcp",
            registry=self._skill_registry,
        )
        if decision.level != LEVEL_AUTO:
            return {"content": [{"type": "text", "text": (
                f"Tool {feral_tool_id} requires {decision.level} on MCP; "
                "external clients cannot satisfy that gate. Use the chat surface."
            )}], "isError": True}

        skill_id, _, endpoint_id = feral_tool_id.partition("__")
        skill = self._skill_registry.skills.get(skill_id) if skill_id else None
        endpoint = None
        if skill is not None:
            endpoint = next((e for e in skill.endpoints if e.id == endpoint_id), None)
        if skill is None or endpoint is None:
            return {"content": [{"type": "text", "text": f"Unknown FERAL skill endpoint: {feral_tool_id}"}], "isError": True}

        try:
            # Bind a context so the call is attributable. This surface is
            # FERAL exposed to external MCP clients, so an unattributed
            # tool call here is the one that most needs a row in
            # execution_log; before this the executor saw session "".
            with bind_context(
                session_id="mcp-external",
                surface="mcp",
                tool_name=feral_tool_id,
            ):
                result = await self._skill_executor.execute(
                    feral_tool_id, dict(arguments or {}), skill, endpoint,
                )
        except Exception as exc:
            logger.exception("MCP projected skill execution failed")
            return {"content": [{"type": "text", "text": f"FERAL skill {feral_tool_id} failed: {exc}"}], "isError": True}

        if isinstance(result, dict) and result.get("success") is False:
            return {"content": [{"type": "text", "text": json.dumps(result, default=str)}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}

    # ─────────────────────────────────────────
    # MCP Protocol: resources/list
    # ─────────────────────────────────────────

    def handle_resources_list(self) -> dict:
        resources = [
            {
                "uri": "feral://devices",
                "name": "Connected Devices",
                "description": "All hardware devices connected to FERAL",
                "mimeType": "application/json",
            },
            {
                "uri": "feral://perception",
                "name": "Perception State",
                "description": "Current fused perception — vision, audio, biometrics, location",
                "mimeType": "application/json",
            },
            {
                "uri": "feral://memory/stats",
                "name": "Memory Statistics",
                "description": "Memory tier sizes and usage statistics",
                "mimeType": "application/json",
            },
        ]

        if self._devices:
            for device in self._devices.list_devices():
                resources.append({
                    "uri": f"feral://device/{device.device_id}",
                    "name": f"Device: {device.name}",
                    "description": f"{device.device_type} with {len(device.capabilities)} capabilities",
                    "mimeType": "application/json",
                })

        return {"resources": resources}

    # ─────────────────────────────────────────
    # MCP Protocol: resources/read
    # ─────────────────────────────────────────

    async def handle_resources_read(self, uri: str) -> dict:
        if uri == "feral://devices":
            devices = self._devices.list_devices() if self._devices else []
            content = json.dumps([d.model_dump() for d in devices], indent=2)
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": content}]}

        elif uri == "feral://perception":
            # Return a summary of all perception frames
            content = json.dumps({"status": "active", "note": "Use feral_perception_snapshot tool for full data"})
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": content}]}

        elif uri == "feral://memory/stats":
            stats = (await self._memory.stats()) if self._memory else {}
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(stats, indent=2)}]}

        elif uri.startswith("feral://device/"):
            device_id = uri.replace("feral://device/", "")
            device = self._devices.get_device(device_id) if self._devices else None
            if device:
                return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(device.model_dump(), indent=2)}]}
            return {"contents": [{"uri": uri, "mimeType": "text/plain", "text": f"Device not found: {device_id}"}]}

        return {"contents": [{"uri": uri, "mimeType": "text/plain", "text": f"Unknown resource: {uri}"}]}

    # ─────────────────────────────────────────
    # MCP Protocol: prompts/list
    # ─────────────────────────────────────────

    def handle_prompts_list(self) -> dict:
        return {
            "prompts": [
                {
                    "name": "feral_hardware_context",
                    "description": "Get full hardware context for reasoning about the physical environment",
                    "arguments": [],
                },
                {
                    "name": "feral_health_summary",
                    "description": "Get a summary of the user's recent health metrics from wearable sensors",
                    "arguments": [
                        {"name": "period", "description": "Time period: last_hour, today, this_week", "required": False},
                    ],
                },
            ],
        }

    # ─────────────────────────────────────────
    # JSON-RPC Handler
    # ─────────────────────────────────────────

    async def handle_jsonrpc(self, request: dict) -> dict:
        """Handle a single JSON-RPC 2.0 request."""
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")

        result = None
        error = None

        try:
            if method == "initialize":
                result = self.handle_initialize(params)
            elif method == "initialized":
                result = {}
            elif method == "tools/list":
                result = self.handle_tools_list()
            elif method == "tools/call":
                result = await self.handle_tools_call(params.get("name", ""), params.get("arguments", {}))
            elif method == "resources/list":
                result = self.handle_resources_list()
            elif method == "resources/read":
                result = await self.handle_resources_read(params.get("uri", ""))
            elif method == "prompts/list":
                result = self.handle_prompts_list()
            elif method == "ping":
                result = {}
            else:
                error = {"code": -32601, "message": f"Method not found: {method}"}
        except Exception as e:
            error = {"code": -32603, "message": str(e)}

        response: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
        if error:
            response["error"] = error
        else:
            response["result"] = result
        return response

    # ─────────────────────────────────────────
    # Stdio Transport (for MCP clients like Claude Desktop)
    # ─────────────────────────────────────────

    async def run_stdio(self):
        """Run as a stdio MCP server. Claude Desktop / Cursor connects here."""
        logger.info("FERAL MCP Server starting (stdio transport)")

        import asyncio

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            try:
                line = await reader.readline()
                if not line:
                    break
                request = json.loads(line.decode().strip())
                response = await self.handle_jsonrpc(request)
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
            except json.JSONDecodeError:
                continue
            except Exception as e:
                logger.error(f"MCP stdio error: {e}")

    # ─────────────────────────────────────────
    # HTTP Transport (for SSE / Streamable HTTP)
    # ─────────────────────────────────────────

    def get_http_routes(self):
        """Return FastAPI routes for MCP HTTP transport.

        The route handlers intentionally use built-in annotations (``dict``)
        for the request body rather than ``fastapi.Request``. The module
        uses ``from __future__ import annotations``, which turns every
        annotation into a string. FastAPI / pydantic resolve those strings
        against the *module* globals at route-registration time. ``Request``
        was previously imported inside this function, so it lived only in
        the local scope and the resolver raised
        ``PydanticUndefinedAnnotation: name 'Request' is not defined``.
        Using ``body: dict`` avoids the forward-reference dance and matches
        the JSON-RPC contract used by ``api/routes/mcp.py``.
        """
        from fastapi import APIRouter
        from fastapi.responses import JSONResponse

        router = APIRouter(prefix="/mcp", tags=["MCP"])

        @router.post("/")
        async def mcp_endpoint(body: dict):
            response = await self.handle_jsonrpc(body)
            return JSONResponse(content=response)

        @router.get("/health")
        async def mcp_health():
            return {"status": "ok", "server": self._server_info}

        return router

    # ─────────────────────────────────────────
    # Tool call implementations
    # ─────────────────────────────────────────

    def _call_list_devices(self) -> dict:
        if not self._devices:
            return {"content": [{"type": "text", "text": "No device registry available."}]}
        devices = self._devices.list_devices()
        text = json.dumps([
            {
                "device_id": d.device_id,
                "name": d.name,
                "type": d.device_type,
                "capabilities": [c.id for c in d.capabilities],
                "sensors": d.sensors,
                "location": d.location,
            }
            for d in devices
        ], indent=2)
        return {"content": [{"type": "text", "text": text}]}

    def _call_device_status(self, args: dict) -> dict:
        device_id = args.get("device_id", "")
        if not self._devices:
            return {"content": [{"type": "text", "text": "No device registry."}]}
        device = self._devices.get_device(device_id)
        if not device:
            return {"content": [{"type": "text", "text": f"Device not found: {device_id}"}]}
        return {"content": [{"type": "text", "text": json.dumps(device.model_dump(), indent=2)}]}

    async def _call_read_sensor(self, args: dict) -> dict:
        from hardware.protocol import HUPAction, HUPActionType
        device_id = args.get("device_id", "")
        sensor = args.get("sensor", "")
        if not self._devices:
            return {"content": [{"type": "text", "text": "No device registry."}]}

        action = HUPAction(
            device_id=device_id,
            capability_id=f"read_{sensor}",
            action_type=HUPActionType.READ,
        )
        result = await self._devices.execute_action(action)
        return {"content": [{"type": "text", "text": json.dumps(result.model_dump(), indent=2)}]}

    async def _call_execute_action(self, args: dict) -> dict:
        from hardware.protocol import HUPAction, HUPActionType
        action = HUPAction(
            device_id=args.get("device_id", ""),
            capability_id=args.get("capability_id", ""),
            action_type=HUPActionType(args.get("action_type", "execute")),
            parameters=args.get("parameters", {}),
        )
        if not self._devices:
            return {"content": [{"type": "text", "text": "No device registry."}]}
        result = await self._devices.execute_action(action)
        return {"content": [{"type": "text", "text": json.dumps(result.model_dump(), indent=2)}]}

    async def _call_memory_query(self, args: dict) -> dict:
        query = args.get("query", "")
        tier = args.get("memory_tier", "all")
        if not self._memory:
            return {"content": [{"type": "text", "text": "Memory not available."}]}
        results = []
        if tier in ("notes", "all"):
            notes = (await self._memory.search_notes(query)) if hasattr(self._memory, "search_notes") else []
            results.extend([{"tier": "notes", "content": n} for n in notes[:10]])
        if tier in ("knowledge", "all"):
            triples = (await self._memory.knowledge_search(query)) if hasattr(self._memory, "knowledge_search") else []
            results.extend([{"tier": "knowledge", "content": t} for t in triples[:10]])
        return {"content": [{"type": "text", "text": json.dumps(results, indent=2, default=str)}]}

    def _call_perception_snapshot(self) -> dict:
        if not self._perception:
            return {"content": [{"type": "text", "text": "Perception engine not available."}]}
        frames = {}
        for sid, frame in self._perception._frames.items():
            frames[sid] = {
                "heart_rate": frame.heart_rate_bpm,
                "spo2": frame.spo2_pct,
                "temperature": frame.temperature_c,
                "scene": frame.scene_description,
                "gesture": frame.gesture,
                "location": frame.gps,
                "connected_nodes": frame.connected_nodes,
            }
        return {"content": [{"type": "text", "text": json.dumps(frames, indent=2, default=str)}]}

    def _call_find_by_capability(self, args: dict) -> dict:
        category = args.get("category", "")
        if not self._devices:
            return {"content": [{"type": "text", "text": "No device registry."}]}
        devices = self._devices.find_by_capability(category)
        return {"content": [{"type": "text", "text": json.dumps([d.device_id for d in devices], indent=2)}]}

    async def _call_dynamic_capability(self, name: str, arguments: dict) -> dict:
        from hardware.protocol import HUPAction, HUPActionType
        parts = name.replace("feral_", "").split("_", 1)
        if len(parts) < 2 or not self._devices:
            return {"content": [{"type": "text", "text": f"Cannot parse dynamic tool: {name}"}], "isError": True}
        device_id, capability_id = parts[0], parts[1]

        # Try to find device with partial match
        device = self._devices.get_device(device_id)
        if not device:
            for d in self._devices.list_devices():
                if device_id in d.device_id:
                    device = d
                    break

        if not device:
            return {"content": [{"type": "text", "text": f"Device not found: {device_id}"}], "isError": True}

        action = HUPAction(
            device_id=device.device_id,
            capability_id=capability_id,
            action_type=HUPActionType.EXECUTE,
            parameters=arguments,
        )
        result = await self._devices.execute_action(action)
        return {"content": [{"type": "text", "text": json.dumps(result.model_dump(), indent=2)}]}

    @staticmethod
    def _capability_to_schema(cap: "DeviceCapability") -> dict:
        properties = {}
        required = []
        for param in cap.parameters:
            prop: dict[str, Any] = {"type": param.get("type", "string")}
            if "description" in param:
                prop["description"] = param["description"]
            properties[param["name"]] = prop
            if param.get("required", False):
                required.append(param["name"])
        return {"type": "object", "properties": properties, "required": required}
