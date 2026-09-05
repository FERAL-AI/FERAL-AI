"""
HUPDevice — Base class for FERAL Hardware Unification Protocol device adapters.

Create device adapters that connect physical hardware (wristbands, robots,
smart home devices) to the FERAL Brain via the HUP mesh.

Usage::

    class WristbandAdapter(HUPDevice):
        device_type = "wearable"
        capabilities = ["heart_rate", "spo2", "skin_temp"]

        async def read_telemetry(self) -> dict:
            return {"heart_rate": 72, "spo2": 98}

        async def execute_action(self, action: str, params: dict) -> dict:
            if action == "vibrate":
                ...
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger("feral.sdk.device")


class HUPDevice:
    """Base class for Hardware Unification Protocol device adapters.

    Subclass this to create a device adapter. The adapter connects to
    the Brain's hardware mesh and receives/sends HUP messages.
    """

    device_type: str = "generic"
    device_name: str = ""
    capabilities: list[str] = []
    telemetry_interval_s: float = 5.0

    def __init__(self):
        if not self.device_name:
            self.device_name = type(self).__name__
        self._ws = None
        self._running = False

    async def connect(self, brain_url: str = "ws://localhost:9090/v1/daemon"):
        """Connect to the Brain's hardware mesh."""
        import websockets

        self._ws = await websockets.connect(brain_url)
        await self._ws.send(json.dumps({
            "type": "node_register",
            "payload": {
                "node_id": self.device_name.lower().replace(" ", "-"),
                "node_type": self.device_type,
                "capabilities": self.capabilities,
                "manifest": self.get_manifest(),
            },
        }))
        logger.info("HUP device '%s' connected to Brain", self.device_name)
        self._running = True

    async def run(self, brain_url: str = "ws://localhost:9090/v1/daemon"):
        """Connect and run the telemetry + command loop."""
        await self.connect(brain_url)
        telemetry_task = asyncio.create_task(self._telemetry_loop())
        command_task = asyncio.create_task(self._command_loop())
        try:
            await asyncio.gather(telemetry_task, command_task)
        finally:
            self._running = False
            if self._ws:
                await self._ws.close()

    async def _telemetry_loop(self):
        while self._running and self._ws:
            try:
                data = await self.read_telemetry()
                if data and self._ws:
                    await self._ws.send(json.dumps({
                        "type": "telemetry",
                        "payload": {"device": self.device_name, "data": data},
                    }))
            except Exception as e:
                logger.warning("Telemetry error: %s", e)
            await asyncio.sleep(self.telemetry_interval_s)

    async def _command_loop(self):
        while self._running and self._ws:
            try:
                raw = await self._ws.recv()
                msg = json.loads(raw)
                if msg.get("type") == "node.invoke":
                    action = msg.get("payload", {}).get("action", "")
                    params = msg.get("payload", {}).get("params", {})
                    result = await self.execute_action(action, params)
                    if self._ws:
                        await self._ws.send(json.dumps({
                            "type": "node.invoke_result",
                            "payload": {"action": action, "result": result},
                        }))
            except Exception as e:
                if self._running:
                    logger.warning("Command loop error: %s", e)
                break

    def get_manifest(self) -> dict:
        """Return the HUP device manifest."""
        return {
            "device_type": self.device_type,
            "name": self.device_name,
            "capabilities": self.capabilities,
            "telemetry_interval_s": self.telemetry_interval_s,
        }

    async def read_telemetry(self) -> dict:
        """Override to return current sensor/telemetry data."""
        return {}

    async def execute_action(self, action: str, params: dict) -> dict:
        """Override to handle incoming commands from the Brain."""
        return {"error": f"Action '{action}' not implemented"}
