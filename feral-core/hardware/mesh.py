"""
FERAL Hardware Mesh — Auto-Registration & Node Invoke
========================================================
Bridges the gap between daemon WebSocket connections and the HUP device registry.

- Auto-registers daemons as HUP devices when they connect
- node.invoke pattern: send command to daemon, wait for response with timeout
- Phone as primary node (camera, GPS, health)
- Wristband/glasses as HUP devices
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional
from uuid import uuid4

from hardware.protocol import (
    DeviceRegistry,
    DeviceManifest,
    DeviceCapability,
    HUPAction,
    HUPResult,
    device_manifest_from_capabilities,
)
from hardware.command_contract import (
    CommandEnvelope,
    CommandState,
    CommandLedger,
    NodeHealth,
)

logger = logging.getLogger("feral.hardware.mesh")

# Continuous-motion command names the host-side dead-man watchdog guards.
# On a command timeout to a motion-capable node we send a best-effort halt
# so a robot mid-motion doesn't keep going after the brain stops waiting.
MOTION_COMMANDS = frozenset(
    {"follow_line", "explore", "drive", "resume", "go_to", "patrol"}
)
HALT_COMMAND = "halt"

NODE_COMMANDS = {
    "camera.snap": {
        "description": "Capture a photo",
        "category": "sensor",
        "params": [{"name": "resolution", "type": "string", "default": "1080p"}],
    },
    "camera.clip": {
        "description": "Record a short video clip",
        "category": "sensor",
        "params": [{"name": "duration_s", "type": "integer", "default": 5}],
    },
    "location.get": {
        "description": "Get current GPS location",
        "category": "sensor",
        "params": [],
    },
    "sensor.read": {
        "description": "Read a named sensor value",
        "category": "sensor",
        "params": [{"name": "sensor_name", "type": "string", "required": True}],
    },
    "screen.record": {
        "description": "Start/stop screen recording",
        "category": "sensor",
        "params": [{"name": "action", "type": "string", "default": "start"}],
    },
    "system.run": {
        "description": "Execute a shell command on the node",
        "category": "compute",
        "params": [{"name": "command", "type": "string", "required": True}],
    },
    "notification.send": {
        "description": "Push a notification to the device",
        "category": "display",
        "params": [
            {"name": "title", "type": "string", "required": True},
            {"name": "body", "type": "string", "required": True},
        ],
    },
    "health.read": {
        "description": "Read health sensor data (heart rate, SpO2, etc.)",
        "category": "sensor",
        "params": [{"name": "metric", "type": "string", "default": "all"}],
    },
    "audio.play": {
        "description": "Play audio on the device",
        "category": "audio",
        "params": [{"name": "url", "type": "string", "required": True}],
    },
    "audio.tts": {
        "description": "Speak text on the device",
        "category": "audio",
        "params": [{"name": "text", "type": "string", "required": True}],
    },
}

PHONE_MANIFEST_TEMPLATE = DeviceManifest(
    device_id="",
    device_type="phone",
    name="Phone Bridge",
    manufacturer="FERAL",
    connection_type="websocket",
    capabilities=[
        DeviceCapability(
            id="camera_snap", name="Camera", description="Capture photos",
            category="sensor", permission_tier="active",
        ),
        DeviceCapability(
            id="gps_location", name="GPS", description="Get location",
            category="sensor", permission_tier="passive",
        ),
        DeviceCapability(
            id="health_sensors", name="Health Sensors",
            description="Heart rate, SpO2, temperature via HealthKit or wristband",
            category="sensor", permission_tier="passive",
        ),
        DeviceCapability(
            id="notification", name="Push Notification",
            description="Send notification to phone",
            category="display", permission_tier="active",
        ),
        DeviceCapability(
            id="haptic", name="Haptic Feedback",
            description="Vibrate the device",
            category="actuator", permission_tier="active",
        ),
    ],
    sensors=["camera", "gps", "accelerometer", "gyroscope", "heart_rate", "spo2"],
    actuators=["display", "speaker", "haptic"],
    battery_powered=True,
    location="pocket",
)


class HardwareMesh:
    """
    Manages the mesh of connected hardware nodes.
    Auto-registers daemons as HUP devices and routes commands.
    """

    def __init__(
        self,
        device_registry: DeviceRegistry,
        daemons: dict,
        ledger: Optional[CommandLedger] = None,
        node_health: Optional[NodeHealth] = None,
        *,
        knowledge_graph=None,
        emergency_stop_enabled: bool = True,
    ):
        self._registry = device_registry
        self._daemons = daemons
        self._pending_invokes: dict[str, asyncio.Future] = {}
        self._node_metadata: dict[str, dict] = {}
        self.ledger: CommandLedger = ledger or CommandLedger()
        self.node_health: NodeHealth = node_health or NodeHealth()
        # Host-side dead-man gate (mirrors SandboxPolicy
        # hardware.movement.emergency_stop_enabled). Gates the best-effort
        # halt issued to a motion-capable node when its command times out.
        self._emergency_stop_enabled = bool(emergency_stop_enabled)
        # HUP v1.3.0 §5.4.4 — discovered peripherals. Maps
        # ``device_id`` → discovery record (last_seen, scanner_node_id,
        # rssi, metadata). Lane 12 reads from /api/hardware/mesh; the
        # KG entity write happens in ``ingest_device_announce``.
        self._announced_devices: dict[str, dict] = {}
        self._kg = knowledge_graph

    def set_knowledge_graph(self, kg) -> None:
        """Late-bind the knowledge graph after BrainState wires memory.

        ``BrainState.init`` constructs HardwareMesh in ``_boot_subsystems``
        before MemoryStore.knowledge_graph is reachable from every call
        site; this lets the boot wiring set the reference explicitly
        once the KG is available without re-creating the mesh.
        """
        self._kg = kg

    async def on_node_connected(self, node_id: str, registration_payload: dict):
        """Auto-register a daemon as a HUP device when it connects."""
        node_type = registration_payload.get("node_type", "desktop")
        platform = registration_payload.get("platform", "unknown")
        capabilities = registration_payload.get("capabilities", [])

        # Prefer a node-SUPPLIED self-description (rich actions[] manifest)
        # over the coarse capability-name stubs. A node that knows its own
        # control surface (glasses, wristband) sends `device_manifest`; we
        # build a real DeviceManifest from it via the shared HUP converter,
        # so its tools/safety/verify come from the device itself.
        supplied = registration_payload.get("device_manifest")
        manifest = self._manifest_from_supplied(node_id, node_type, platform, supplied)

        if manifest is None:
            if node_type in ("phone", "ios", "android"):
                manifest = PHONE_MANIFEST_TEMPLATE.model_copy(update={
                    "device_id": node_id,
                    "name": f"Phone ({platform})",
                })
            else:
                manifest = DeviceManifest(
                    device_id=node_id,
                    device_type=node_type,
                    name=f"{node_type.title()} Node ({platform})",
                    manufacturer="FERAL",
                    connection_type="websocket",
                    capabilities=[
                        DeviceCapability(
                            id=cap, name=cap.replace("_", " ").title(),
                            description=f"Device capability: {cap}",
                            category="compute", permission_tier="active",
                        )
                        for cap in capabilities
                    ],
                )

        adapter = WebSocketNodeAdapter(
            node_id, self._daemons, self._pending_invokes,
            emergency_stop_enabled=self._emergency_stop_enabled,
        )
        self._registry.register_device(manifest, adapter)
        self._node_metadata[node_id] = {
            "registered_at": time.time(),
            "node_type": node_type,
            "platform": platform,
        }
        self.node_health.record_connect(node_id)
        # Universal HUP ingress: expose the node's self-described capabilities
        # to the LLM via the SAME generic path as a USB device — no per-node
        # skill code. No-op when the node only announced capability-name stubs
        # (the generic registrar skips empty/stub manifests).
        self._register_generic_skill(manifest, adapter, node_id)
        logger.info(f"Node auto-registered as HUP device: {node_id} ({node_type}/{platform})")

    @staticmethod
    def _manifest_from_supplied(
        node_id: str, node_type: str, platform: str, supplied
    ) -> Optional[DeviceManifest]:
        """Build a DeviceManifest from a node-supplied self-description.

        Accepts either the HUP ``actions[]`` envelope (preferred — routed
        through the shared generic converter) or a full DeviceManifest dict.
        Returns ``None`` to fall back to the coarse stub path."""
        if not isinstance(supplied, dict):
            return None
        try:
            if supplied.get("actions"):
                return device_manifest_from_capabilities(
                    node_id,
                    supplied,
                    name=supplied.get("name") or f"{node_type.title()} ({platform})",
                    manufacturer=supplied.get("manufacturer", ""),
                    model=supplied.get("model", ""),
                    device_type=supplied.get("device_type") or node_type,
                    location=supplied.get("location", ""),
                )
            # Full DeviceManifest dict (capabilities already in model shape).
            data = dict(supplied)
            data.setdefault("device_id", node_id)
            data.setdefault("device_type", node_type)
            data.setdefault("name", f"{node_type.title()} ({platform})")
            data.setdefault("connection_type", "websocket")
            if isinstance(data.get("capabilities"), list) and (
                not data["capabilities"]
                or isinstance(data["capabilities"][0], dict)
            ):
                return DeviceManifest(**data)
        except Exception as exc:
            logger.debug("node %s supplied manifest unusable: %s", node_id, exc)
        return None

    @staticmethod
    def _register_generic_skill(manifest, adapter, device_id: str) -> None:
        """Best-effort generic HUP skill registration for a mesh ingress."""
        try:
            from api.state import state

            register = getattr(state, "register_generic_hardware_skill_for", None)
            if callable(register):
                register(manifest, adapter, device_id=device_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("generic skill registration skipped for %s: %s", device_id, exc)

    def on_node_disconnected(self, node_id: str):
        """Unregister a daemon when it disconnects."""
        self._registry.unregister_device(node_id)
        self._node_metadata.pop(node_id, None)
        self.node_health.record_disconnect(node_id)
        logger.info(f"Node unregistered from HUP: {node_id}")

    async def invoke(
        self,
        node_id: str,
        command: str,
        params: dict = None,
        timeout: float = 10.0,
        correlation_id: str = "",
        idempotency_key: Optional[str] = None,
        priority: str = "interactive",
    ) -> dict:
        """
        Send a command to a daemon node and wait for the response.

        Full command lifecycle:
          1. Build a CommandEnvelope with a full UUID
          2. Check idempotency (skip if duplicate)
          3. Submit to the ledger (SUBMITTED)
          4. Send over WebSocket
          5. On response → SUCCEEDED / FAILED
          6. On timeout  → TIMED_OUT
        """
        ws = self._daemons.get(node_id)
        if not ws:
            return {"success": False, "error": f"Node not connected: {node_id}"}

        envelope = CommandEnvelope(
            node_id=node_id,
            action=command,
            params=params or {},
            correlation_id=correlation_id or str(uuid4()),
            idempotency_key=idempotency_key,
            priority=priority,
            deadline=time.time() + timeout,
        )

        if idempotency_key:
            existing = self.ledger.check_idempotency(idempotency_key)
            if existing is not None:
                logger.info(f"Idempotent hit for key={idempotency_key}, returning cached record")
                return existing.result or {"success": True, "idempotent": True, "command_id": existing.envelope.command_id}

        self.ledger.submit(envelope)
        self.node_health.increment_commands(node_id)

        request_id = envelope.command_id
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_invokes[request_id] = future

        msg = {
            "type": "hup_action_request",
            "payload": {
                "action_id": request_id,
                "name": command,
                "params": params or {},
                "timeout_ms": int(timeout * 1000),
            },
        }

        try:
            await ws.send_json(msg)

            try:
                from api.state import state
                if state.orchestrator:
                    for sid in list(state.sessions.keys()):
                        await state.orchestrator._emit_brain_event(sid, "device_route", {
                            "from_node": "brain", "to_node": node_id, "payload_kind": command,
                        })
            except Exception:
                pass

            result = await asyncio.wait_for(future, timeout=timeout)

            success = result.get("success", False)
            new_state = CommandState.SUCCEEDED if success else CommandState.FAILED
            self.ledger.update_state(
                request_id, new_state,
                message=result.get("error", ""),
                result=result,
            )
            if not success:
                self.node_health.increment_errors(node_id)
            return result

        except asyncio.TimeoutError:
            self._pending_invokes.pop(request_id, None)
            self.ledger.update_state(
                request_id, CommandState.TIMED_OUT,
                message=f"Timeout after {timeout}s waiting for {command}",
            )
            await self._failsafe_halt_on_timeout(node_id, command)
            return {"success": False, "error": f"Timeout waiting for {command} on {node_id}", "command_id": request_id}

        except Exception as e:
            self._pending_invokes.pop(request_id, None)
            self.ledger.update_state(
                request_id, CommandState.FAILED, message=str(e),
            )
            self.node_health.increment_errors(node_id)
            return {"success": False, "error": str(e), "command_id": request_id}

    async def _failsafe_halt_on_timeout(self, node_id: str, command: str) -> None:
        """Host-side dead-man: on a motion-command timeout, send a best-effort
        halt to the node so a robot mid-motion stops even though the brain gave
        up waiting. Fire-and-forget (we do not await an ack — the node is
        already unresponsive). Never raises; logs honestly on failure and does
        not pretend the halt landed when the link is gone.
        """
        if not self._emergency_stop_enabled:
            return
        if command == HALT_COMMAND or command not in MOTION_COMMANDS:
            return
        ws = self._daemons.get(node_id)
        if ws is None:
            logger.warning(
                "Motion command %r to %s timed out but the node is gone; "
                "cannot issue fail-safe halt", command, node_id,
            )
            return
        try:
            await ws.send_json({
                "type": "hup_action_request",
                "payload": {
                    "action_id": str(uuid4()),
                    "name": HALT_COMMAND,
                    "params": {},
                    "timeout_ms": 2000,
                },
            })
            logger.warning(
                "Fail-safe halt sent to %s after motion command %r timed out",
                node_id, command,
            )
        except Exception as exc:
            logger.warning(
                "Fail-safe halt to %s failed after %r timeout: %s",
                node_id, command, exc,
            )

    def resolve_invoke(self, request_id: str, result: dict):
        """Called when a daemon sends back an execute_result.

        If the result carries an ``ack`` flag we transition the ledger
        record to ACKED; otherwise we resolve the pending future which
        will trigger the SUCCEEDED/FAILED transition in ``invoke()``.
        """
        if result.get("ack"):
            self.ledger.ack(request_id)
            return

        future = self._pending_invokes.pop(request_id, None)
        if future and not future.done():
            future.set_result(result)

    @property
    def connected_nodes(self) -> list[dict]:
        return [
            {"node_id": nid, **meta}
            for nid, meta in self._node_metadata.items()
            if nid in self._daemons
        ]

    # ─────────────────────────────────────────────
    # HUP v1.3.0 — peripheral discovery (§5.4.4)
    # ─────────────────────────────────────────────

    async def ingest_device_announce(self, payload: dict) -> dict:
        """Ingest a HUP v1.3.0 ``device_announce`` payload.

        Closes THESIS_SCENARIOS S3 (hardware peripheral memory). Stores
        the discovery in-memory under ``self._announced_devices`` for
        the Lane 12 Devices page REST surface, and upserts a
        knowledge-graph entity (``category=device``) so the orchestrator
        can answer chat queries via the standard memory tool path.

        Repeat announcements for the same ``device_id`` update
        ``last_seen`` / ``rssi_dbm`` in place and bump the KG entity's
        mention count (via ``add_entity`` → ``_bump_mention``) rather
        than duplicating rows.

        Returns the merged in-memory record (useful for tests and the
        Lane 11 ``test_device_announce_*`` pytest).
        """
        device_id = str(payload.get("device_id") or "").strip()
        if not device_id:
            logger.debug("device_announce dropped: missing device_id")
            return {}

        now = time.time()
        scanner_node_id = str(payload.get("scanner_node_id") or "").strip()
        device_kind = str(payload.get("device_kind") or "unknown")
        name = str(payload.get("name") or "")
        manufacturer = str(payload.get("manufacturer") or "")
        rssi_dbm = payload.get("rssi_dbm")
        if rssi_dbm is not None:
            try:
                rssi_dbm = int(rssi_dbm)
            except (TypeError, ValueError):
                rssi_dbm = None
        advertised_services = list(payload.get("advertised_services") or [])
        first_seen = payload.get("first_seen")
        last_seen = payload.get("last_seen", now) or now
        metadata = dict(payload.get("metadata") or {})

        existing = self._announced_devices.get(device_id)
        if existing:
            existing["last_seen"] = float(last_seen)
            if rssi_dbm is not None:
                existing["rssi_dbm"] = rssi_dbm
            if name:
                existing["name"] = name
            if manufacturer:
                existing["manufacturer"] = manufacturer
            if scanner_node_id:
                existing["scanner_node_id"] = scanner_node_id
            if advertised_services:
                existing["advertised_services"] = advertised_services
            if metadata:
                existing["metadata"].update(metadata)
            record = existing
        else:
            record = {
                "device_id": device_id,
                "scanner_node_id": scanner_node_id,
                "device_kind": device_kind,
                "name": name,
                "manufacturer": manufacturer,
                "rssi_dbm": rssi_dbm,
                "advertised_services": advertised_services,
                "first_seen": float(first_seen) if first_seen is not None else now,
                "last_seen": float(last_seen),
                "metadata": metadata,
            }
            self._announced_devices[device_id] = record

        await self._write_kg_entity_for_announce(record)
        logger.debug(
            "device_announce ingested: device=%s scanner=%s kind=%s rssi=%s",
            device_id, scanner_node_id, device_kind, rssi_dbm,
        )
        return record

    async def _write_kg_entity_for_announce(self, record: dict) -> None:
        """Upsert a KG entity for an announced peripheral.

        Skips silently when the KG is unavailable (e.g. during early
        boot or in unit tests that don't wire memory). The KG itself
        dedupes by name + entity_type so repeat calls bump the entity
        mention count instead of inserting a duplicate row.
        """
        kg = self._kg
        if kg is None:
            return
        add_entity = getattr(kg, "add_entity", None)
        if not callable(add_entity):
            return

        device_id = record["device_id"]
        name = record.get("name") or device_id
        kind = record.get("device_kind") or "unknown"
        metadata = {
            "category": "device",
            "device_id": device_id,
            "device_kind": kind,
            "scanner_node_id": record.get("scanner_node_id") or "",
            "manufacturer": record.get("manufacturer") or "",
            "rssi_dbm": record.get("rssi_dbm"),
            "first_seen": record.get("first_seen"),
            "last_seen": record.get("last_seen"),
            "advertised_services": record.get("advertised_services") or [],
            "tags": [kind, "peripheral", record.get("scanner_node_id") or ""],
        }
        # Strip empty tag entries; the KG search expects a clean list.
        metadata["tags"] = [t for t in metadata["tags"] if t]

        try:
            await add_entity(
                name=name,
                entity_type="device",
                metadata=metadata,
            )
        except Exception as exc:
            logger.debug("kg.add_entity raised for device %s: %s", device_id, exc)

    def list_announced_devices(self) -> list[dict]:
        """Return the in-memory peripheral discoveries for REST consumers.

        Lane 12's Devices page reads from ``/api/hardware/mesh`` which
        composes this with ``connected_nodes`` so the UI can show
        "connected daemons" alongside "peripherals their scanners
        observed" without two separate fetches.
        """
        return [dict(rec) for rec in self._announced_devices.values()]

    def find_announced_device(self, device_id: str) -> Optional[dict]:
        """Lookup a single discovered peripheral by id."""
        rec = self._announced_devices.get(device_id)
        return dict(rec) if rec is not None else None


class WebSocketNodeAdapter:
    """
    HUP DeviceAdapter that routes actions to a WebSocket daemon.
    Bridges the HUP action model to the node.invoke pattern.
    """

    def __init__(
        self,
        node_id: str,
        daemons: dict,
        pending: dict,
        *,
        emergency_stop_enabled: bool = True,
    ):
        self._node_id = node_id
        self._daemons = daemons
        self._pending = pending
        # Host-side dead-man gate for the timeout fail-safe halt.
        self._emergency_stop_enabled = bool(emergency_stop_enabled)

    async def execute(self, action: HUPAction) -> HUPResult:
        """Execute a HUP action by sending it to the daemon."""
        ws = self._daemons.get(self._node_id)
        if not ws:
            return HUPResult(
                action_id=action.action_id, device_id=action.device_id,
                status="failure", error=f"Node disconnected: {self._node_id}",
            )

        request_id = str(uuid4())[:8]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future

        command = action.capability_id

        msg = {
            "type": "hup_action_request",
            "payload": {
                "action_id": request_id,
                "name": command,
                "params": action.parameters,
                "timeout_ms": action.timeout_ms,
            },
        }

        try:
            await ws.send_json(msg)
            timeout = action.timeout_ms / 1000.0
            result = await asyncio.wait_for(future, timeout=timeout)

            return HUPResult(
                action_id=action.action_id,
                device_id=action.device_id,
                status="success" if result.get("success") else "failure",
                data=result.get("data", {}),
                error=result.get("error", ""),
            )
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            await self._failsafe_halt_on_timeout(command)
            return HUPResult(
                action_id=action.action_id, device_id=action.device_id,
                status="timeout", error=f"Timeout ({action.timeout_ms}ms)",
            )
        except Exception as e:
            self._pending.pop(request_id, None)
            return HUPResult(
                action_id=action.action_id, device_id=action.device_id,
                status="failure", error=str(e),
            )

    async def _failsafe_halt_on_timeout(self, command: str) -> None:
        """Host-side dead-man: on a motion-action timeout, send a best-effort
        halt to the daemon so a robot mid-motion stops even though the brain
        gave up waiting. Never raises; logs honestly and no-ops when the gate
        is off, the command isn't motion, or the node is gone.
        """
        if not self._emergency_stop_enabled:
            return
        if command == HALT_COMMAND or command not in MOTION_COMMANDS:
            return
        ws = self._daemons.get(self._node_id)
        if ws is None:
            logger.warning(
                "Motion action %r to %s timed out but the node is gone; "
                "cannot issue fail-safe halt", command, self._node_id,
            )
            return
        try:
            await ws.send_json({
                "type": "hup_action_request",
                "payload": {
                    "action_id": str(uuid4()),
                    "name": HALT_COMMAND,
                    "params": {},
                    "timeout_ms": 2000,
                },
            })
            logger.warning(
                "Fail-safe halt sent to %s after motion action %r timed out",
                self._node_id, command,
            )
        except Exception as exc:
            logger.warning(
                "Fail-safe halt to %s failed after %r timeout: %s",
                self._node_id, command, exc,
            )
