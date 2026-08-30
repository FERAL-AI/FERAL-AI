#!/usr/bin/env python3
"""
FERAL Node SDK — Robot Actuator Template
========================================
Connect any physical robot (ROS, serial, I2C, CAN) to the FERAL Brain.

READ THIS BEFORE YOU COPY THE FILE
----------------------------------
This template is deliberately incomplete: out of the box it refuses
every actuator command. That is the point. FERAL's contract with the
user is that the brain never claims a device did something the device
cannot confirm, and a node is the last place that promise can be broken.

Two brain-side files set the standard this template follows:

* ``feral-core/hardware/capability_skill.py`` — after every actuator
  call the brain re-reads telemetry and reports ``verified``
  true/false/None. On mismatch it returns success=False with "Do NOT
  claim it worked — report the device's actual state."
* ``feral-core/hardware/adapters/robot_arm.py`` — has no "simulation
  mode". An adapter with no serial transport is not connected and says
  so, because it previously returned success for movements that never
  happened, giving the LLM a tool named "move the robot" that always
  said it worked.

So this node:

1. Refuses to actuate when no :class:`RobotTransport` is wired in, and
   does not even advertise ``robot_move`` / ``robot_grip`` in that state.
   You cannot honestly offer a capability you have no way to perform.
2. Re-reads the robot's state after every command and compares it with
   what was asked for. Telemetry wins over the command's return value.
3. Publishes only telemetry it actually read. There are no placeholder
   sensor values in this file; a fake battery percentage is a lie with a
   decimal point in it.

To use it, implement :class:`RobotTransport` against your hardware (a
serial sketch is included below), then:

    python3 robot_template.py --brain ws://localhost:9090 --serial-port /dev/ttyUSB0
"""

import argparse
import asyncio
import json
import logging
import os
import socket
import time
import uuid
from typing import Any, Optional

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
logger = logging.getLogger("robot_node")


# ─────────────────────────────────────────────
# The hardware seam
# ─────────────────────────────────────────────

class RobotTransport:
    """Whatever actually talks to your robot. Implement all four methods.

    ``read_state`` is not optional and not decorative: it is how the node
    tells the brain what happened instead of what was requested. Return
    ``None`` (or raise) when the robot cannot be read — the node reports
    that as "unknown", never as success.
    """

    async def connect(self) -> bool:
        """Open the link. Return False if the robot is not reachable."""
        raise NotImplementedError

    async def move(self, direction: str, speed: int) -> None:
        """Command motion. Raise on any driver-level rejection."""
        raise NotImplementedError

    async def grip(self, action: str) -> None:
        """Open or close the gripper. Raise on rejection."""
        raise NotImplementedError

    async def read_state(self) -> Optional[dict]:
        """Read the robot back. Keys should include the verify fields
        used in :data:`VERIFY_CONTRACTS` (``motion``, ``gripper``) plus
        whatever sensors you publish as telemetry."""
        raise NotImplementedError


class SerialRobotTransport(RobotTransport):
    """Reference implementation over a serial link. Adapt the framing.

    Mirrors ``hardware/adapters/robot_arm.py``: no port or no pyserial
    means not connected, and nothing downstream pretends otherwise.
    """

    def __init__(self, port: str, baud: int = 115200):
        self.port = port
        self.baud = baud
        self._serial = None

    async def connect(self) -> bool:
        if not self.port:
            logger.error("No serial port configured — robot node will refuse to actuate.")
            return False
        try:
            import serial as pyserial
        except ImportError:
            logger.error("pyserial is not installed (`pip install pyserial`) — not connected.")
            return False
        try:
            self._serial = pyserial.Serial(self.port, self.baud, timeout=1)
            logger.info("Connected to robot on %s", self.port)
            return True
        except Exception as exc:
            logger.error("Could not open %s: %s", self.port, exc)
            return False

    async def _command(self, line: str) -> str:
        if self._serial is None:
            raise RuntimeError("serial link is not open")
        self._serial.write((line + "\n").encode())
        return self._serial.readline().decode(errors="replace").strip()

    async def move(self, direction: str, speed: int) -> None:
        reply = await self._command(f"MOVE {direction} {speed}")
        # Adapt to your firmware. Anything that is not an explicit
        # acknowledgement is an error, not a shrug.
        if not reply.startswith("OK"):
            raise RuntimeError(f"robot rejected MOVE: {reply!r}")

    async def grip(self, action: str) -> None:
        reply = await self._command(f"GRIP {action}")
        if not reply.startswith("OK"):
            raise RuntimeError(f"robot rejected GRIP: {reply!r}")

    async def read_state(self) -> Optional[dict]:
        try:
            reply = await self._command("STATE")
            return json.loads(reply)  # firmware returns a JSON object
        except Exception as exc:
            logger.warning("State read-back failed: %s", exc)
            return None


# ─────────────────────────────────────────────
# Verification contracts
# ─────────────────────────────────────────────
# Same idea as a DeviceCapability's ``verify`` block in
# feral-core/hardware/capability_skill.py: name the telemetry field that
# proves the command landed, and what it must read.

VERIFY_CONTRACTS: dict[str, dict[str, Any]] = {
    "robot_move": {
        "field": "motion",
        "expected": lambda args: {str(args.get("direction", "forward"))},
    },
    "robot_grip": {
        "field": "gripper",
        "expected": lambda args: {str(args.get("action", "close"))},
    },
}

ACTUATOR_CAPABILITIES = ("robot_move", "robot_grip")


class RobotNode:
    def __init__(self, brain_url: str, api_key: str, transport: Optional[RobotTransport] = None):
        self.brain_ws_url = f"{brain_url}/v1/node?api_key={api_key}"
        self.node_id = f"daemon_{socket.gethostname()}-robot-{uuid.uuid4().hex[:4]}"
        self.node_type = "actuator"
        self.transport = transport
        self.ws = None
        self.running = True

    # ── registration ──────────────────────────

    def capabilities(self) -> list[str]:
        """Advertise only what this node can actually do right now.

        With no transport the node is a telemetry stub. Listing
        ``robot_move`` anyway would put a tool in front of the LLM that
        can only ever fail, which is the fake-acknowledgement problem
        moved one step earlier in the pipeline.
        """
        caps = ["telemetry"]
        if self.transport is not None:
            caps.extend(ACTUATOR_CAPABILITIES)
        else:
            logger.warning(
                "No RobotTransport wired in: registering as telemetry-only. "
                "Implement RobotTransport and pass it to RobotNode to actuate."
            )
        return caps

    async def connect(self):
        """Connect to the FERAL Brain."""
        if self.transport is not None and not await self.transport.connect():
            # Hardware unreachable at startup. Drop the transport so the
            # node registers without actuator capabilities rather than
            # offering movements it cannot make. Deliberately requires a
            # restart once the robot is powered up: a node that silently
            # regains actuation mid-session is a node whose advertised
            # capabilities no longer match what the brain was told.
            logger.error(
                "Robot transport failed to connect — actuation disabled for this "
                "run. Fix the link and restart the node."
            )
            self.transport = None

        while self.running:
            try:
                logger.info(f"Connecting to FERAL Brain at {self.brain_ws_url}...")
                async with websockets.connect(self.brain_ws_url) as ws:
                    self.ws = ws
                    logger.info("Connected! Registering Robot Node...")

                    register_msg = {
                        "hop": "daemon",
                        "type": "node_register",
                        "payload": {
                            "node_id": self.node_id,
                            "node_type": self.node_type,
                            "capabilities": self.capabilities(),
                        }
                    }
                    await ws.send(json.dumps(register_msg))

                    listener = asyncio.create_task(self._listen_loop())
                    telemetry = asyncio.create_task(self._telemetry_loop())

                    done, pending = await asyncio.wait(
                        [listener, telemetry],
                        return_when=asyncio.FIRST_COMPLETED
                    )

                    for task in pending:
                        task.cancel()

            except (websockets.ConnectionClosed, ConnectionRefusedError) as e:
                logger.warning(f"Connection lost. Retrying... ({e})")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                await asyncio.sleep(5)

    async def _listen_loop(self):
        """Listen for physical commands from the feral LLM hook."""
        try:
            async for message in self.ws:
                data = json.loads(message)
                if data.get("type") == "execute":
                    await self._handle_command(data.get("payload", {}), data.get("msg_id"))
        except websockets.ConnectionClosed:
            logger.info("WebSocket connection closed.")

    # ── command handling ──────────────────────

    async def _handle_command(self, payload: dict, msg_id: str):
        resp = await self.build_result(payload, msg_id)
        await self.ws.send(json.dumps(resp))
        logger.info(
            "Result sent to Brain for request %s: status=%s verified=%s",
            msg_id, resp["payload"]["status"], resp["payload"]["verified"],
        )

    @staticmethod
    def _envelope(msg_id: str, *, status: str, stdout: str = "", error: str = "",
                  verified: Optional[bool] = None, **extra) -> dict:
        return {
            "hop": "daemon",
            "type": "execute_result",
            "payload": {
                "request_id": msg_id,
                "status": status,
                "stdout": stdout,
                "error": error,
                # `verified` mirrors capability_skill.py: True = telemetry
                # agrees, False = telemetry disagrees, None = could not be
                # read. None is an honest answer; it is not a success.
                "verified": verified,
                **extra,
            },
        }

    async def build_result(self, payload: dict, msg_id: str) -> dict:
        """Execute one command and report what the robot actually did.

        Kept separate from the socket so it can be tested without one,
        and so the honesty rules live in one readable place.
        """
        executor = payload.get("executor", "")
        args = payload.get("args", {}) or {}
        logger.info(f"EXECUTING ROBOT CMD: {executor} | Args: {args}")

        contract = VERIFY_CONTRACTS.get(executor)
        if contract is None:
            msg = f"Unknown command: {executor}"
            logger.error(msg)
            return self._envelope(msg_id, status="error", error=msg)

        # 1. Fail closed. No hardware means no acknowledgement.
        if self.transport is None:
            msg = (
                f"Refusing {executor}: this node has no RobotTransport, so "
                f"nothing was commanded. Implement RobotTransport and pass "
                f"it to RobotNode(transport=...) before wiring this node to "
                f"a brain that will act on its answers."
            )
            logger.error(msg)
            return self._envelope(msg_id, status="error", error=msg)

        # 2. Actuate. A driver-level rejection is an error, full stop.
        try:
            if executor == "robot_move":
                direction = str(args.get("direction", "forward"))
                speed = int(args.get("speed", 30))
                await self.transport.move(direction, speed)
            else:
                action = str(args.get("action", "close"))
                await self.transport.grip(action)
        except Exception as exc:
            msg = f"{executor} failed on the robot: {exc}"
            logger.error(msg)
            return self._envelope(msg_id, status="error", error=msg)

        # 3. Read the robot back. This, not the call above, is the evidence.
        telemetry = await self._safe_read_state()
        field = contract["field"]
        expected = contract["expected"](args)
        observed = telemetry.get(field) if isinstance(telemetry, dict) else None

        if telemetry is None:
            return self._envelope(
                msg_id,
                status="success",
                stdout=(
                    f"{executor} was accepted by the robot, but its state "
                    f"could not be read back, so the outcome is unknown. "
                    f"Report the uncertainty; do not assert the robot moved."
                ),
                verified=None,
                observed=None,
                expected=sorted(expected),
            )

        if observed not in expected:
            # Wording matches feral-core/hardware/capability_skill.py so the
            # brain and its nodes say the same thing about the same event.
            msg = (
                f"{executor} was acked but telemetry shows {field}={observed!r} "
                f"(expected one of {sorted(map(str, expected))}). Do NOT claim "
                f"it worked — report the device's actual state."
            )
            logger.warning(msg)
            return self._envelope(
                msg_id, status="error", error=msg,
                verified=False, observed=observed, expected=sorted(expected),
            )

        return self._envelope(
            msg_id,
            status="success",
            stdout=f"{executor} confirmed by telemetry: {field}={observed!r}",
            verified=True,
            observed=observed,
            expected=sorted(expected),
        )

    async def _safe_read_state(self) -> Optional[dict]:
        if self.transport is None:
            return None
        try:
            state = await self.transport.read_state()
        except Exception as exc:
            logger.warning("read_state failed: %s", exc)
            return None
        return state if isinstance(state, dict) else None

    # ── telemetry ─────────────────────────────

    async def build_telemetry(self) -> Optional[dict]:
        """Build one telemetry frame, or None when there is nothing to report.

        Returning None is the honest branch: a node with no hardware to
        read has no sensor readings, and inventing plausible ones is how
        a dashboard ends up showing the battery level of a robot that is
        not plugged in.
        """
        sensors = await self._safe_read_state()
        if not sensors:
            return None
        return {
            "hop": "daemon",
            "type": "telemetry",
            "payload": {
                "node_id": self.node_id,
                "sensors": sensors,
                "timestamp": time.time(),
            },
        }

    async def _telemetry_loop(self):
        """Publish real readings only."""
        warned = False
        try:
            while self.running:
                frame = await self.build_telemetry()
                if frame is None:
                    if not warned:
                        logger.warning(
                            "No telemetry to publish: the transport is absent or "
                            "unreadable. Publishing nothing rather than placeholders."
                        )
                        warned = True
                elif self.ws:
                    warned = False
                    await self.ws.send(json.dumps(frame))
                await asyncio.sleep(5.0)
        except websockets.ConnectionClosed:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FERAL Robot Node SDK")
    parser.add_argument("--brain", default="ws://localhost:9090", help="WebSocket URL of FERAL Brain")
    parser.add_argument("--api-key", default=os.environ.get("NODE_API_KEY", ""), help="Authentication key for Brain connection (or set NODE_API_KEY env var)")
    parser.add_argument("--serial-port", default=os.environ.get("ROBOT_SERIAL_PORT", ""),
                        help="Serial device for the bundled SerialRobotTransport, e.g. /dev/ttyUSB0. "
                             "Without a transport the node registers telemetry-only and refuses to actuate.")
    args = parser.parse_args()

    transport = SerialRobotTransport(args.serial_port) if args.serial_port else None
    if transport is None:
        logger.warning(
            "Starting with no robot transport. Every actuator command will be "
            "refused. Pass --serial-port, or swap in your own RobotTransport."
        )

    node = RobotNode(args.brain, args.api_key, transport=transport)
    try:
        asyncio.run(node.connect())
    except KeyboardInterrupt:
        logger.info("Node shutting down")
        node.running = False
