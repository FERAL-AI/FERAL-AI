"""
Real Philips Hue + Home Assistant smart home adapter. No simulation.

Bridges the HUP protocol to the Philips Hue local bridge API.
The agent says "turn off the lights" and HUP routes it here.

Usage:
    adapter = SmartHomeAdapter(bridge_ip="192.168.1.42", api_key="...")
    registry.register_device(adapter.manifest)
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
from typing import Any, Optional

import httpx

from hardware.protocol import (
    DeviceManifest,
    DeviceCapability,
    HUPAction,
    HUPResult,
)

logger = logging.getLogger("feral.hardware.hue")


class PhilipsHueAdapter:
    """Controls Philips Hue lights via the local bridge API."""

    def __init__(self):
        self._bridge_ip = os.getenv("HUE_BRIDGE_IP", "")
        self._api_key = os.getenv("HUE_API_KEY", "")
        self._client = httpx.AsyncClient(timeout=10)

    @property
    def configured(self) -> bool:
        return bool(self._bridge_ip and self._api_key)

    @property
    def _base_url(self) -> str:
        return f"http://{self._bridge_ip}/api/{self._api_key}"

    async def discover_bridge(self) -> Optional[dict]:
        """Discover Hue bridge via meethue.com, mDNS fallback, or manual env."""
        try:
            r = await self._client.get("https://discovery.meethue.com/", timeout=5)
            bridges = r.json()
            if bridges:
                self._bridge_ip = bridges[0].get("internalipaddress", "")
                logger.info("Hue bridge found via meethue.com: %s", self._bridge_ip)
                return {"success": True, "ip": self._bridge_ip, "method": "meethue"}
        except Exception as e:
            logger.warning("meethue.com discovery failed: %s — trying mDNS", e)

        try:
            from zeroconf import Zeroconf, ServiceBrowser
            import socket
            zc = Zeroconf()
            found_ip = None

            class Listener:
                def add_service(self, zc_inst, stype, name):
                    nonlocal found_ip
                    info = zc_inst.get_service_info(stype, name)
                    if info and info.addresses:
                        found_ip = socket.inet_ntoa(info.addresses[0])

                def remove_service(self, *a):
                    pass

                def update_service(self, *a):
                    pass

            ServiceBrowser(zc, "_hue._tcp.local.", Listener())
            await asyncio.sleep(3)
            zc.close()
            if found_ip:
                self._bridge_ip = found_ip
                logger.info("Hue bridge found via mDNS: %s", found_ip)
                return {"success": True, "ip": found_ip, "method": "mdns"}
        except ImportError:
            logger.debug("zeroconf not installed — skipping mDNS discovery")
        except Exception as e:
            logger.warning("mDNS discovery failed: %s", e)

        return {
            "success": False,
            "reason": "bridge_not_found",
            "hint": "Set HUE_BRIDGE_IP manually",
        }

    async def register(self, device_type: str = "feral-brain") -> Optional[str]:
        """Register with the bridge (user must press the button first)."""
        if not self._bridge_ip:
            return None
        try:
            r = await self._client.post(
                f"http://{self._bridge_ip}/api",
                json={"devicetype": device_type},
            )
            data = r.json()
            if data and "success" in data[0]:
                self._api_key = data[0]["success"]["username"]
                return self._api_key
            error = data[0].get("error", {}).get("description", "Unknown error")
            logger.warning(f"Hue registration failed: {error}")
        except Exception as e:
            logger.warning(f"Hue registration error: {e}")
        return None

    async def get_lights(self) -> dict:
        if not self.configured:
            return {"success": False, "error": "Hue not configured. Set HUE_BRIDGE_IP and HUE_API_KEY."}
        try:
            r = await self._client.get(f"{self._base_url}/lights")
            return {"success": True, "data": r.json()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def set_light_state(self, light_id: str, state: dict) -> dict:
        if not self.configured:
            return {"success": False, "error": "Hue not configured"}
        try:
            r = await self._client.put(f"{self._base_url}/lights/{light_id}/state", json=state)
            return {"success": True, "data": r.json()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def set_scene(self, scene_name: str) -> dict:
        if not self.configured:
            return {"success": False, "error": "Hue not configured"}
        try:
            r = await self._client.get(f"{self._base_url}/scenes")
            scenes = r.json()
            scene_id = None
            for sid, s in scenes.items():
                if s.get("name", "").lower() == scene_name.lower():
                    scene_id = sid
                    break
            if not scene_id:
                return {"success": False, "error": f"Scene '{scene_name}' not found"}
            r = await self._client.put(f"{self._base_url}/groups/0/action", json={"scene": scene_id})
            return {"success": True, "data": r.json()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def toggle_light(self, light_id: str) -> dict:
        if not self.configured:
            return {"success": False, "error": "Hue not configured"}
        try:
            r = await self._client.get(f"{self._base_url}/lights/{light_id}")
            current = r.json().get("state", {}).get("on", False)
            return await self.set_light_state(light_id, {"on": not current})
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def close(self):
        await self._client.aclose()


# Shared Hue adapter instance (lazily configured from env)
_hue = PhilipsHueAdapter()


class SmartHomeAdapter:
    """HUP adapter for smart home devices — delegates to PhilipsHueAdapter for real Hue control."""

    def __init__(
        self,
        bridge_ip: str = "",
        api_key: str = "",
        device_id: str = "smart-home-01",
    ):
        self.device_id = device_id
        self._hue = _hue
        if bridge_ip:
            self._hue._bridge_ip = bridge_ip
        if api_key:
            self._hue._api_key = api_key
        self._target_light = os.getenv("HUE_DEFAULT_LIGHT", "1")

    @property
    def manifest(self) -> DeviceManifest:
        return DeviceManifest(
            device_id=self.device_id,
            name="Smart Home Hub",
            device_type="smart_home",
            manufacturer="FERAL",
            model="SH-Hub",
            firmware_version="2.0.0",
            connection_type="wifi",
            capabilities=[
                DeviceCapability(
                    id="lights_toggle",
                    name="Toggle Lights",
                    description="Turn lights on or off",
                    category="actuator",
                    permission_tier="active",
                    parameters=[{"name": "state", "type": "string", "description": "on or off"}],
                    reversible=True,
                ),
                DeviceCapability(
                    id="lights_brightness",
                    name="Set Brightness",
                    description="Adjust light brightness (0-100)",
                    category="actuator",
                    permission_tier="active",
                    parameters=[{"name": "brightness", "type": "integer", "description": "0-100"}],
                    reversible=True,
                ),
                DeviceCapability(
                    id="lights_color",
                    name="Set Light Color",
                    description="Change light color (hex)",
                    category="actuator",
                    permission_tier="active",
                    parameters=[{"name": "color", "type": "string", "description": "Hex color e.g. #FF6600"}],
                    reversible=True,
                ),
                # `thermostat_set` is deliberately NOT advertised. It used to
                # return status="success" with a `"note": "requires Home
                # Assistant integration"` buried in the payload — so the
                # brain reported the setpoint as applied while no thermostat
                # was ever contacted. This is a Hue adapter; there is no
                # Home Assistant client in the tree to route it to. Reading
                # temperature still works (Hue motion sensors expose it), so
                # `thermostat_read` stays.
                DeviceCapability(
                    id="thermostat_read",
                    name="Read Temperature",
                    description="Read current room temperature from Hue motion sensor",
                    category="sensor",
                    permission_tier="passive",
                    returns={"type": "object", "properties": {"current_c": {"type": "number"}, "setpoint_c": {"type": "number"}}},
                ),
                DeviceCapability(
                    id="scene_activate",
                    name="Activate Scene",
                    description="Activate a Hue lighting scene by name",
                    category="actuator",
                    permission_tier="active",
                    parameters=[{"name": "scene", "type": "string", "description": "Scene name"}],
                ),
            ],
            location="home",
            tags=["smart-home", "lights", "hue"],
        )

    async def execute(self, action: HUPAction) -> HUPResult:
        cap_id = action.capability_id
        params = action.parameters or {}
        light_id = params.get("light_id", self._target_light)

        if cap_id == "lights_toggle":
            on = params.get("state", "on").lower() == "on"
            result = await self._hue.set_light_state(light_id, {"on": on})
            if result.get("success"):
                return HUPResult(action_id=action.action_id, device_id=self.device_id, status="success", data={"lights_on": on, **result})
            return HUPResult(action_id=action.action_id, device_id=self.device_id, status="failure", error=result.get("error", "Hue command failed"))

        elif cap_id == "lights_brightness":
            bri = int(params.get("brightness", 80))
            bri_hue = max(0, min(254, int(bri * 2.54)))
            result = await self._hue.set_light_state(light_id, {"bri": bri_hue, "on": True})
            if result.get("success"):
                return HUPResult(action_id=action.action_id, device_id=self.device_id, status="success", data={"brightness": bri, **result})
            return HUPResult(action_id=action.action_id, device_id=self.device_id, status="failure", error=result.get("error", ""))

        elif cap_id == "lights_color":
            color = params.get("color", "#FFFFFF")
            hue_state = self._hex_to_hue_state(color)
            result = await self._hue.set_light_state(light_id, hue_state)
            if result.get("success"):
                return HUPResult(action_id=action.action_id, device_id=self.device_id, status="success", data={"color": color, **result})
            return HUPResult(action_id=action.action_id, device_id=self.device_id, status="failure", error=result.get("error", ""))

        elif cap_id == "thermostat_set":
            # Was: status="success" with the setpoint echoed back and a note
            # admitting it needed an integration that does not exist. See the
            # manifest comment — this capability is not advertised either.
            error = (
                "Thermostat control is not implemented: this adapter speaks "
                "the Philips Hue API only, and no thermostat was contacted. "
                "The setpoint was NOT applied. Wire a Home Assistant (or "
                "vendor) client before offering this capability."
            )
            logger.error("Refusing thermostat_set on %s — %s", self.device_id, error)
            return HUPResult(
                action_id=action.action_id, device_id=self.device_id,
                status="failure", error=error,
            )

        elif cap_id == "thermostat_read":
            result = await self._read_hue_temperature_sensor()
            # The helper reports its failures as {"error": ...}; returning
            # that as status="success" made "Hue not configured" look like a
            # temperature reading.
            if "error" in result:
                return HUPResult(
                    action_id=action.action_id, device_id=self.device_id,
                    status="failure", error=str(result["error"]),
                )
            return HUPResult(action_id=action.action_id, device_id=self.device_id, status="success", data=result)

        elif cap_id == "scene_activate":
            scene = params.get("scene", "relax")
            result = await self._hue.set_scene(scene)
            if result.get("success"):
                return HUPResult(action_id=action.action_id, device_id=self.device_id, status="success", data={"scene": scene, **result})
            return HUPResult(action_id=action.action_id, device_id=self.device_id, status="failure", error=result.get("error", ""))

        return HUPResult(action_id=action.action_id, device_id=self.device_id, status="failure", error=f"Unknown capability: {cap_id}")

    async def _read_hue_temperature_sensor(self) -> dict:
        """Read temperature from a Hue motion sensor if available."""
        if not self._hue.configured:
            return {"error": "Hue not configured"}
        try:
            r = await self._hue._client.get(f"{self._hue._base_url}/sensors")
            sensors = r.json()
            for sid, sensor in sensors.items():
                if sensor.get("type") == "ZLLTemperature":
                    raw_temp = sensor.get("state", {}).get("temperature", 0)
                    return {"current_c": round(raw_temp / 100.0, 1), "source": "hue_sensor", "sensor_id": sid}
            return {"error": "No temperature sensor found on Hue bridge"}
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _hex_to_hue_state(hex_color: str) -> dict:
        """Convert a hex color to Hue xy color space (CIE 1931)."""
        hex_color = hex_color.lstrip("#")
        if len(hex_color) != 6:
            return {"on": True}
        r = int(hex_color[0:2], 16) / 255.0
        g = int(hex_color[2:4], 16) / 255.0
        b = int(hex_color[4:6], 16) / 255.0

        r = ((r + 0.055) / 1.055) ** 2.4 if r > 0.04045 else r / 12.92
        g = ((g + 0.055) / 1.055) ** 2.4 if g > 0.04045 else g / 12.92
        b = ((b + 0.055) / 1.055) ** 2.4 if b > 0.04045 else b / 12.92

        x = r * 0.4124 + g * 0.3576 + b * 0.1805
        y = r * 0.2126 + g * 0.7152 + b * 0.0722
        z = r * 0.0193 + g * 0.1192 + b * 0.9505

        total = x + y + z
        if total == 0:
            return {"on": True}
        cx = round(x / total, 4)
        cy = round(y / total, 4)
        return {"on": True, "xy": [cx, cy]}
