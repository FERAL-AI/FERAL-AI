"""
HUP Robot Arm Adapter — Control serial/MQTT-connected robotic arms via HUP.

Demonstrates the "dangerous" permission tier: the agent must request
explicit user confirmation before executing physical movements.

Usage:
    adapter = RobotArmAdapter(serial_port="/dev/ttyUSB0")
    registry.register_device(adapter.manifest)
"""

from __future__ import annotations
import asyncio
import logging

from hardware.protocol import (
    DeviceManifest,
    DeviceCapability,
    HUPAction,
    HUPResult,
)

logger = logging.getLogger("feral.hup.robot_arm")


class RobotArmAdapter:
    """Reference HUP adapter for serial/MQTT-connected robot arms.

    Demonstrates:
    1. Dangerous-tier capabilities requiring confirmation
    2. Serial/MQTT communication patterns
    3. Multi-DOF (degree of freedom) joint control
    4. Emergency stop capability
    5. Coordinate-based movement commands
    """

    def __init__(
        self,
        serial_port: str = "",
        mqtt_topic: str = "",
        device_id: str = "robot-arm-01",
        dof: int = 6,
    ):
        self.serial_port = serial_port
        self.mqtt_topic = mqtt_topic
        self.device_id = device_id
        self.dof = dof
        self._position = [0.0] * dof  # Joint angles in degrees
        self._gripper_open = True
        self._estop = False
        self._serial = None

    @property
    def manifest(self) -> DeviceManifest:
        return DeviceManifest(
            device_id=self.device_id,
            name="Robot Arm",
            device_type="robot",
            manufacturer="FERAL",
            model="RA-6DOF",
            firmware_version="1.0.0",
            connection_type="serial",
            capabilities=[
                DeviceCapability(
                    id="move_joints",
                    name="Move Joints",
                    description="Move robot arm joints to target angles (degrees)",
                    category="actuator",
                    permission_tier="dangerous",
                    parameters=[
                        {"name": "joints", "type": "array", "description": "Target angles for each joint [j1, j2, ..., j6]"},
                        {"name": "speed_pct", "type": "integer", "description": "Movement speed 1-100", "default": 50},
                    ],
                    requires_confirmation=True,
                    reversible=True,
                    safety_notes="Physical movement — ensure workspace is clear of obstacles and humans.",
                ),
                DeviceCapability(
                    id="move_cartesian",
                    name="Move to Position",
                    description="Move end-effector to XYZ position in mm",
                    category="actuator",
                    permission_tier="dangerous",
                    parameters=[
                        {"name": "x", "type": "number", "description": "X position in mm"},
                        {"name": "y", "type": "number", "description": "Y position in mm"},
                        {"name": "z", "type": "number", "description": "Z position in mm"},
                        {"name": "speed_pct", "type": "integer", "description": "Movement speed 1-100", "default": 30},
                    ],
                    requires_confirmation=True,
                    reversible=True,
                    safety_notes="Physical movement — ensure workspace is clear.",
                ),
                DeviceCapability(
                    id="gripper",
                    name="Gripper Control",
                    description="Open or close the end-effector gripper",
                    category="actuator",
                    permission_tier="active",
                    parameters=[{"name": "state", "type": "string", "description": "open or close"}],
                    reversible=True,
                ),
                DeviceCapability(
                    id="home",
                    name="Home Position",
                    description="Move all joints to the home/zero position",
                    category="actuator",
                    permission_tier="active",
                    requires_confirmation=True,
                    reversible=True,
                ),
                DeviceCapability(
                    id="estop",
                    name="Emergency Stop",
                    description="Immediately halt all movement",
                    category="actuator",
                    permission_tier="passive",
                    reversible=False,
                    safety_notes="Cannot be undone remotely — requires physical reset.",
                ),
                DeviceCapability(
                    id="read_position",
                    name="Read Joint Positions",
                    description="Read current joint angles and end-effector position",
                    category="sensor",
                    permission_tier="passive",
                    returns={"type": "object", "properties": {"joints": {"type": "array"}, "gripper_open": {"type": "boolean"}}},
                ),
            ],
            location="workspace",
            tags=["robot", "arm", "actuator", "manufacturing"],
        )

    async def connect(self) -> bool:
        # There is no "simulation mode". connect() used to return True with
        # self._serial still None whenever the port was unset or pyserial was
        # missing, and every subsequent execute() then reported success for
        # movements that never happened — the LLM had a tool named "move the
        # robot" that always said it worked. An adapter with no transport is
        # not connected, and says so.
        if not self.serial_port:
            logger.error(
                "Robot arm %s has no serial port configured — not connected. "
                "Construct it as RobotArmAdapter(serial_port='/dev/ttyUSB0') "
                "so actuation has somewhere to go.",
                self.device_id,
            )
            return False
        try:
            import serial as pyserial
        except ImportError:
            logger.error(
                "Robot arm %s cannot connect: pyserial is not installed. "
                "Install it with `pip install pyserial`.",
                self.device_id,
            )
            return False
        try:
            self._serial = pyserial.Serial(self.serial_port, 115200, timeout=1)
            logger.info("Connected to robot arm on %s", self.serial_port)
            return True
        except Exception as e:
            logger.error("Serial connection failed: %s", e)
            return False

    #: Capability ids this adapter implements. Kept next to the transport
    #: guard so an unknown id still gets "Unknown capability" rather than
    #: "not connected".
    _CAPABILITY_IDS = frozenset({
        "move_joints", "move_cartesian", "gripper",
        "home", "estop", "read_position",
    })

    def _no_transport_error(self) -> str:
        """Operator-facing reason why nothing can be actuated right now."""
        if not self.serial_port:
            return (
                f"Robot arm {self.device_id} is not connected: no serial port "
                f"configured. Nothing was actuated. Configure a serial port "
                f"(e.g. /dev/ttyUSB0) and reconnect."
            )
        return (
            f"Robot arm {self.device_id} is not connected on "
            f"{self.serial_port}. Nothing was actuated. Call connect() and "
            f"check the cable, permissions, and that pyserial is installed."
        )

    async def execute(self, action: HUPAction) -> HUPResult:
        cap_id = action.capability_id
        params = action.parameters or {}

        # Refuse every KNOWN capability while there is no serial transport.
        # This covers reads as well as writes: `read_position` would
        # otherwise report self._position, which is the adapter's own
        # bookkeeping, not where the arm actually is. Unknown ids fall
        # through to the "Unknown capability" answer below, which is the
        # more specific and more useful error.
        if self._serial is None and cap_id in self._CAPABILITY_IDS:
            logger.error(
                "Refusing %s on %s — %s", cap_id, self.device_id,
                self._no_transport_error(),
            )
            return HUPResult(
                action_id=action.action_id, device_id=self.device_id,
                status="failure", error=self._no_transport_error(),
            )

        if self._estop and cap_id != "estop":
            return HUPResult(
                action_id=action.action_id, device_id=self.device_id,
                status="failure", error="Emergency stop is active — physical reset required",
            )

        if cap_id == "move_joints":
            joints = params.get("joints", [0.0] * self.dof)
            speed = int(params.get("speed_pct", 50))
            await self._send_gcode(f"G0 {' '.join(f'J{i}{a}' for i, a in enumerate(joints))} F{speed}")
            self._position = [float(j) for j in joints[:self.dof]]
            return HUPResult(
                action_id=action.action_id, device_id=self.device_id,
                status="success", data={"joints": self._position, "speed_pct": speed},
            )

        elif cap_id == "move_cartesian":
            x, y, z = float(params.get("x", 0)), float(params.get("y", 0)), float(params.get("z", 0))
            speed = int(params.get("speed_pct", 30))
            await self._send_gcode(f"G1 X{x} Y{y} Z{z} F{speed}")
            return HUPResult(
                action_id=action.action_id, device_id=self.device_id,
                status="success", data={"position": {"x": x, "y": y, "z": z}},
            )

        elif cap_id == "gripper":
            state = params.get("state", "open").lower()
            self._gripper_open = state == "open"
            cmd = "M3 S0" if self._gripper_open else "M3 S255"
            await self._send_gcode(cmd)
            return HUPResult(
                action_id=action.action_id, device_id=self.device_id,
                status="success", data={"gripper_open": self._gripper_open},
            )

        elif cap_id == "home":
            await self._send_gcode("G28")
            self._position = [0.0] * self.dof
            return HUPResult(
                action_id=action.action_id, device_id=self.device_id,
                status="success", data={"joints": self._position, "homed": True},
            )

        elif cap_id == "estop":
            self._estop = True
            await self._send_gcode("M112")
            logger.critical("EMERGENCY STOP activated on %s", self.device_id)
            return HUPResult(
                action_id=action.action_id, device_id=self.device_id,
                status="success",
                data={"estop": True, "message": "Emergency stop activated. Physical reset required."},
            )

        elif cap_id == "read_position":
            return HUPResult(
                action_id=action.action_id, device_id=self.device_id,
                status="success",
                data={"joints": self._position, "gripper_open": self._gripper_open, "estop": self._estop},
            )

        return HUPResult(action_id=action.action_id, device_id=self.device_id, status="failure", error=f"Unknown capability: {cap_id}")

    async def _send_gcode(self, command: str):
        """Send a G-code command via serial.

        Raises RuntimeError when there is no serial transport. The old
        ``else: logger.debug("Simulated G-code")`` branch is what let
        ``execute()`` return success for movements that never left the
        process. execute() already refuses when ``_serial`` is None; this
        raise is the backstop so no future caller can reintroduce a silent
        no-op path.
        """
        if self._serial is None:
            raise RuntimeError(self._no_transport_error())
        self._serial.write(f"{command}\n".encode())
        await asyncio.sleep(0.01)
        response = self._serial.readline().decode().strip()
        logger.debug("G-code response: %s", response)
