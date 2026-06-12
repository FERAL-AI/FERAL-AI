"""
FERAL Hardware Use Protocol (HUP)
====================================
Like "computer use" made screens controllable by AI agents,
HUP makes ANY hardware controllable through a universal abstraction.

Other agent frameworks offer browser-use or sandboxed computer-use.
FERAL has EVERYTHING use.

Every device — glasses, robot arm, drone, 3D printer, CNC, HVAC,
light switch, garage door, medical device, industrial sensor —
speaks this protocol. The agent doesn't need to know the hardware;
it speaks HUP.

Architecture:
  Agent → HUP Action → Sandbox Policy Check → Permission Tier →
  → Device Adapter → Physical Hardware → Result

Device Capabilities are declared, not coded. A YAML manifest
tells the Brain what a device can do, and the agent figures out how.
"""

from __future__ import annotations
import logging
import time
from enum import Enum
from typing import Optional, Any
from pydantic import BaseModel, Field
from uuid import uuid4

logger = logging.getLogger("feral.hup")


# ─────────────────────────────────────────────
# Device Capability Schema
# ─────────────────────────────────────────────

class DeviceCapability(BaseModel):
    """A single thing a device can do."""
    id: str
    name: str
    description: str
    category: str  # "sensor", "actuator", "display", "audio", "network", "compute"
    permission_tier: str = "passive"  # passive, active, privileged, dangerous
    parameters: list[dict] = Field(default_factory=list)
    returns: Optional[dict] = None
    rate_limit_per_minute: Optional[int] = None
    requires_confirmation: bool = False
    reversible: bool = True
    safety_notes: str = ""


class DeviceManifest(BaseModel):
    """
    Declarative description of a hardware device.
    Devices self-describe — the agent reads this manifest to understand
    what the device can do without any device-specific code.
    """
    device_id: str
    device_type: str  # "glasses", "robot", "drone", "printer3d", "light", "sensor_hub", "camera", "speaker"
    name: str
    manufacturer: str = ""
    model: str = ""
    firmware_version: str = ""
    connection_type: str = "websocket"  # "websocket", "ble", "mqtt", "serial", "http", "zigbee", "zwave"
    capabilities: list[DeviceCapability] = Field(default_factory=list)
    sensors: list[str] = Field(default_factory=list)
    actuators: list[str] = Field(default_factory=list)
    battery_powered: bool = False
    location: str = ""  # "living_room", "garage", "wrist", "head"
    tags: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────
# HUP Actions — The Universal Command Language
# ─────────────────────────────────────────────

class HUPActionType(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    STREAM_START = "stream_start"
    STREAM_STOP = "stream_stop"
    CONFIGURE = "configure"
    CALIBRATE = "calibrate"
    RESET = "reset"
    STATUS = "status"
    DISCOVER = "discover"


class HUPAction(BaseModel):
    """A universal hardware action. The agent constructs these; the adapter executes them."""
    action_id: str = Field(default_factory=lambda: str(uuid4()))
    device_id: str
    capability_id: str
    action_type: HUPActionType
    parameters: dict = Field(default_factory=dict)
    timeout_ms: int = 5000
    priority: int = 0  # 0=normal, 1=high, 2=critical
    requires_confirmation: bool = False
    safety_context: str = ""
    timestamp: float = Field(default_factory=time.time)


class HUPResult(BaseModel):
    """Result of a hardware action."""
    action_id: str
    device_id: str
    status: str  # "success", "failure", "denied", "timeout", "pending_confirmation"
    data: dict = Field(default_factory=dict)
    error: str = ""
    duration_ms: int = 0
    timestamp: float = Field(default_factory=time.time)


# ─────────────────────────────────────────────
# Device Registry — Manages all connected hardware
# ─────────────────────────────────────────────

class DeviceRegistry:
    """
    Central registry of all hardware devices in the FERAL ecosystem.
    Devices register via manifest; the agent queries the registry to
    understand what it can control.
    """

    def __init__(self):
        self._devices: dict[str, DeviceManifest] = {}
        self._adapters: dict[str, "DeviceAdapter"] = {}
        self._action_log: list[HUPResult] = []

    def register_device(self, manifest: DeviceManifest, adapter: Optional["DeviceAdapter"] = None):
        self._devices[manifest.device_id] = manifest
        if adapter:
            self._adapters[manifest.device_id] = adapter
        logger.info(
            f"Device registered: {manifest.name} ({manifest.device_id}) "
            f"— {len(manifest.capabilities)} capabilities, "
            f"{len(manifest.sensors)} sensors, {len(manifest.actuators)} actuators"
        )

    def unregister_device(self, device_id: str):
        self._devices.pop(device_id, None)
        self._adapters.pop(device_id, None)

    def get_device(self, device_id: str) -> Optional[DeviceManifest]:
        return self._devices.get(device_id)

    def get_adapter(self, device_id: str) -> Optional["DeviceAdapter"]:
        return self._adapters.get(device_id)

    def list_devices(self) -> list[DeviceManifest]:
        return list(self._devices.values())

    def find_by_capability(self, capability_category: str) -> list[DeviceManifest]:
        """Find all devices that have a capability in the given category."""
        results = []
        for device in self._devices.values():
            for cap in device.capabilities:
                if cap.category == capability_category:
                    results.append(device)
                    break
        return results

    def find_by_type(self, device_type: str) -> list[DeviceManifest]:
        return [d for d in self._devices.values() if d.device_type == device_type]

    def find_by_location(self, location: str) -> list[DeviceManifest]:
        return [d for d in self._devices.values() if d.location == location]

    async def execute_action(self, action: HUPAction) -> HUPResult:
        """Execute a hardware action through the appropriate adapter."""
        device = self._devices.get(action.device_id)
        if not device:
            return HUPResult(
                action_id=action.action_id, device_id=action.device_id,
                status="failure", error=f"Device not found: {action.device_id}"
            )

        cap = next((c for c in device.capabilities if c.id == action.capability_id), None)
        if not cap:
            return HUPResult(
                action_id=action.action_id, device_id=action.device_id,
                status="failure", error=f"Capability not found: {action.capability_id}"
            )

        if cap.requires_confirmation or action.requires_confirmation:
            return HUPResult(
                action_id=action.action_id, device_id=action.device_id,
                status="pending_confirmation",
                data={"capability": cap.name, "safety_notes": cap.safety_notes},
            )

        adapter = self._adapters.get(action.device_id)
        if not adapter:
            return HUPResult(
                action_id=action.action_id, device_id=action.device_id,
                status="failure", error="No adapter registered for this device"
            )

        start = time.time()
        duration_ms = 0
        try:
            result_data = await adapter.execute(action)
            duration_ms = int((time.time() - start) * 1000)
            if isinstance(result_data, HUPResult):
                result = result_data
                if not result.action_id:
                    result.action_id = action.action_id
                if not result.device_id:
                    result.device_id = action.device_id
                if not result.duration_ms:
                    result.duration_ms = duration_ms
            elif isinstance(result_data, dict):
                result = HUPResult(
                    action_id=action.action_id, device_id=action.device_id,
                    status="success", data=result_data,
                    duration_ms=duration_ms,
                )
            else:
                result = HUPResult(
                    action_id=action.action_id, device_id=action.device_id,
                    status="failure",
                    error=f"Unexpected adapter return type: {type(result_data).__name__}",
                    duration_ms=duration_ms,
                )
        except Exception as e:
            result = HUPResult(
                action_id=action.action_id, device_id=action.device_id,
                status="failure", error=str(e),
                duration_ms=int((time.time() - start) * 1000),
            )

        self._action_log.append(result)
        if len(self._action_log) > 5000:
            self._action_log = self._action_log[-2500:]
        return result

    def to_llm_context(self) -> str:
        """Generate a context string for the LLM describing all available hardware."""
        if not self._devices:
            return "No hardware devices connected."

        lines = ["Connected hardware devices:"]
        for device in self._devices.values():
            lines.append(f"\n[{device.name}] ({device.device_type}, {device.connection_type})")
            if device.location:
                lines.append(f"  Location: {device.location}")
            for cap in device.capabilities:
                params_str = ", ".join(p.get("name", "?") for p in cap.parameters)
                lines.append(f"  - {cap.name}: {cap.description} ({cap.category}) params=[{params_str}]")
        return "\n".join(lines)

    @property
    def stats(self) -> dict:
        return {
            "device_count": len(self._devices),
            "total_capabilities": sum(len(d.capabilities) for d in self._devices.values()),
            "total_sensors": sum(len(d.sensors) for d in self._devices.values()),
            "total_actuators": sum(len(d.actuators) for d in self._devices.values()),
            "actions_executed": len(self._action_log),
        }


# ─────────────────────────────────────────────
# Device Adapter — Base class for hardware integrations
# ─────────────────────────────────────────────

class DeviceAdapter:
    """
    Base class for device-specific adapters.
    Implement this to connect any hardware to FERAL.

    The adapter translates HUP actions into device-specific commands
    and returns results in a standard format.
    """

    async def execute(self, action: HUPAction) -> dict:
        """Execute a HUP action on the physical hardware."""
        raise NotImplementedError

    async def get_status(self) -> dict:
        """Get current device status."""
        return {"status": "unknown"}

    async def disconnect(self):
        """Clean up hardware connection."""
        pass


class WebSocketDeviceAdapter(DeviceAdapter):
    """Adapter for devices connected via WebSocket (most FERAL nodes)."""

    def __init__(self, ws, node_id: str):
        self._ws = ws
        self._node_id = node_id

    async def execute(self, action: HUPAction) -> dict:
        msg = {
            "hop": "brain",
            "type": "hup_action_request",
            "payload": {
                "action_id": action.action_id,
                "name": action.capability_id,
                "params": action.parameters,
                "timeout_ms": action.timeout_ms,
            }
        }
        await self._ws.send_json(msg)
        return {"sent": True, "node_id": self._node_id}

    async def get_status(self) -> dict:
        return {"connected": True, "node_id": self._node_id}


# ─────────────────────────────────────────────
# Pre-built Device Manifests
# ─────────────────────────────────────────────

FERAL_GLASSES_MANIFEST = DeviceManifest(
    device_id="feral-glasses",
    device_type="glasses",
    name="FERAL Smart Glasses",
    manufacturer="FERAL",
    model="W300",
    connection_type="ble",
    sensors=["heart_rate", "spo2", "temperature", "uv", "accelerometer", "gyroscope", "ambient_light"],
    actuators=["display", "speaker", "haptic"],
    battery_powered=True,
    location="head",
    tags=["wearable", "health", "ar"],
    capabilities=[
        DeviceCapability(
            id="read_heart_rate", name="Read Heart Rate",
            description="Get current heart rate in BPM from PPG sensor",
            category="sensor", permission_tier="passive",
            returns={"bpm": "int", "is_wearing": "bool"},
        ),
        DeviceCapability(
            id="read_spo2", name="Read Blood Oxygen",
            description="Get SpO2 percentage from pulse oximeter",
            category="sensor", permission_tier="passive",
            returns={"current": "int", "high": "int", "low": "int"},
        ),
        DeviceCapability(
            id="read_temperature", name="Read Temperature",
            description="Get skin temperature from IR sensor",
            category="sensor", permission_tier="passive",
            returns={"celsius": "float", "fahrenheit": "float"},
        ),
        DeviceCapability(
            id="read_uv", name="Read UV Index",
            description="Get UV exposure level (0-15)",
            category="sensor", permission_tier="passive",
            returns={"level": "int"},
        ),
        DeviceCapability(
            id="read_steps", name="Read Steps",
            description="Get step count, distance, and calories",
            category="sensor", permission_tier="passive",
            returns={"steps": "int", "distance_m": "float", "calories_kcal": "float"},
        ),
        DeviceCapability(
            id="capture_photo", name="Capture Photo",
            description="Take a photo from the glasses camera",
            category="sensor", permission_tier="active",
            requires_confirmation=False,
            returns={"image_b64": "str", "resolution": "str"},
        ),
        DeviceCapability(
            id="display_notification", name="Show Notification",
            description="Display a text notification on the glasses HUD",
            category="display", permission_tier="active",
            parameters=[
                {"name": "text", "type": "string", "required": True},
                {"name": "duration_ms", "type": "number", "default": "3000"},
            ],
        ),
        DeviceCapability(
            id="play_audio", name="Play Audio",
            description="Play audio through glasses speaker",
            category="audio", permission_tier="active",
            parameters=[
                {"name": "audio_b64", "type": "string", "required": True},
                {"name": "encoding", "type": "string", "default": "mp3"},
            ],
        ),
    ],
)
