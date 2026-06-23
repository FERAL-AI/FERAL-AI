"""Bridged-peripheral HUP adapter — devices reached *through* a node.

The glasses, the wristband, and any other BLE peripheral don't talk to the
brain directly; a phone (or other node) acts as a Bluetooth bridge and relays
HUP actions over its WebSocket. ``peripheral_bridge_register`` announces those
sub-devices with a self-description; this adapter is what lets the brain
actually *drive* them: every action is forwarded to the bridge node with the
target ``device_id`` attached, so the node can route it to the right peripheral.

It is a thin transport — no per-device code. The peripheral's own manifest
(capabilities + verify contracts) drives tools, safety, and the honesty loop
through the same generic ``capability_skill`` path as a USB device.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from hardware.protocol import DeviceManifest, HUPAction, HUPResult

logger = logging.getLogger("feral.hup.bridge")


class BridgedPeripheralAdapter:
    """Routes HUP actions for one peripheral through its bridge node."""

    def __init__(
        self,
        device_id: str,
        *,
        node_id: str,
        mesh: Any,
        manifest: Optional[DeviceManifest] = None,
        timeout_s: float = 10.0,
    ):
        self.device_id = device_id
        self._node_id = node_id
        self._mesh = mesh
        self._manifest = manifest
        self._timeout_s = timeout_s

    @property
    def manifest(self) -> Optional[DeviceManifest]:
        return self._manifest

    def set_manifest(self, manifest: DeviceManifest) -> None:
        self._manifest = manifest

    async def execute(self, action: HUPAction) -> HUPResult:
        if self._mesh is None:
            return HUPResult(
                action_id=action.action_id,
                device_id=self.device_id,
                status="failure",
                error="No bridge available for this peripheral.",
            )
        params = dict(action.parameters or {})
        # Tell the bridge node which sub-device this action targets.
        params.setdefault("device_id", self.device_id)
        try:
            result = await self._mesh.invoke(
                self._node_id,
                action.capability_id,
                params,
                timeout=self._timeout_s,
            )
        except Exception as exc:
            return HUPResult(
                action_id=action.action_id,
                device_id=self.device_id,
                status="failure",
                error=str(exc),
            )
        if isinstance(result, HUPResult):
            return result
        result = result if isinstance(result, dict) else {"success": True, "data": result}
        ok = bool(result.get("success", False))
        return HUPResult(
            action_id=action.action_id,
            device_id=self.device_id,
            status="success" if ok else "failure",
            data=result.get("data", result) if isinstance(result.get("data"), dict) else result,
            error="" if ok else str(result.get("error", "bridge command failed")),
        )

    async def get_status(self) -> dict[str, Any]:
        return {"online": self._node_id in getattr(self._mesh, "_daemons", {})}


__all__ = ["BridgedPeripheralAdapter"]
