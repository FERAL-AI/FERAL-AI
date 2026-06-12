"""CuteBot skill — routes LLM tool calls to the brain-local DeviceRegistry."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

from skills.base import BaseSkill
from skills.impl import register_skill

logger = logging.getLogger("feral.skills.cutebot")

DEVICE_ID = "cutebot-usb-0"
NOT_CONNECTED_ERROR = "CuteBot is not connected (no USB robot found)"

# endpoint_id -> (capability_id, action_type_name)
_ENDPOINT_MAP: dict[str, Tuple[str, str]] = {
    "follow_line": ("follow_line", "execute"),
    "explore": ("explore", "execute"),
    "drive": ("drive", "execute"),
    "halt": ("halt", "execute"),
    "status": ("read_telemetry", "read"),
}


def _state():
    try:
        from api.state import state
        return state
    except Exception:
        return None


@register_skill
class CuteBotSkill(BaseSkill):
    def __init__(self):
        super().__init__(skill_id="cutebot")
        self._device_registry_override: Optional[Any] = None

    def set_device_registry(self, registry: Any) -> None:
        """Test hook — inject a mock DeviceRegistry without booting the brain."""
        self._device_registry_override = registry

    def _get_device_registry(self) -> Optional[Any]:
        if self._device_registry_override is not None:
            return self._device_registry_override
        state = _state()
        if state is None:
            return None
        return getattr(state, "device_registry", None)

    def _is_device_registered(self, registry: Any) -> bool:
        if registry is None:
            return False
        if hasattr(registry, "get_device"):
            return registry.get_device(DEVICE_ID) is not None
        devices = getattr(registry, "_devices", None)
        if isinstance(devices, dict):
            return DEVICE_ID in devices
        return False

    async def execute(
        self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str]
    ) -> Dict[str, Any]:
        if endpoint_id == "follow_line":
            return await self._run_capability(endpoint_id, args)
        if endpoint_id == "explore":
            return await self._run_capability(endpoint_id, args)
        if endpoint_id == "drive":
            return await self._run_capability(endpoint_id, args)
        if endpoint_id == "halt":
            return await self._run_capability(endpoint_id, args)
        if endpoint_id == "status":
            return await self._run_capability(endpoint_id, args)
        return {
            "success": False,
            "status_code": 400,
            "data": None,
            "error": f"Unknown CuteBot endpoint: {endpoint_id}",
        }

    async def _run_capability(
        self, endpoint_id: str, args: Dict[str, Any]
    ) -> Dict[str, Any]:
        mapping = _ENDPOINT_MAP.get(endpoint_id)
        if mapping is None:
            return {
                "success": False,
                "status_code": 400,
                "data": None,
                "error": f"Unknown CuteBot endpoint: {endpoint_id}",
            }

        capability_id, action_type_name = mapping
        registry = self._get_device_registry()
        if registry is None:
            return {
                "success": False,
                "status_code": 503,
                "data": None,
                "error": "FERAL hardware registry not ready.",
            }

        # Halt is the emergency path — attempt even when the device looks offline.
        if endpoint_id != "halt" and not self._is_device_registered(registry):
            return {
                "success": False,
                "status_code": 503,
                "data": None,
                "error": NOT_CONNECTED_ERROR,
            }

        try:
            from hardware.protocol import HUPAction, HUPActionType, HUPResult

            action_type = HUPActionType(action_type_name)
            action = HUPAction(
                action_id=f"skill_cutebot_{endpoint_id}_{uuid4().hex[:8]}",
                device_id=DEVICE_ID,
                capability_id=capability_id,
                action_type=action_type,
                parameters=dict(args or {}),
            )
            result = await registry.execute_action(action)
            if isinstance(result, dict):
                status = result.get("status", "failure")
                data = result.get("data")
                error = result.get("error") or None
            elif isinstance(result, HUPResult):
                status = result.status
                data = result.data
                error = result.error or None
            else:
                status = "success"
                data = result
                error = None

            success = status == "success"
            return {
                "success": success,
                "status_code": 200 if success else 500,
                "data": data,
                "error": error,
            }
        except Exception as exc:
            logger.error("CuteBot skill error on %s: %s", endpoint_id, exc, exc_info=True)
            return {
                "success": False,
                "status_code": 500,
                "data": None,
                "error": str(exc),
            }
