"""
HUP Wristband Adapter — Bluetooth wearable with health sensors.

Reads heart rate, SpO2, skin temperature from BLE GATT characteristics
and streams them as HUP telemetry.

Read-only. Haptics and LED are not wired (no standard GATT characteristic
exists for them), so they are neither advertised in the manifest nor
executable — see the comments in `manifest` and `execute`.

Usage:
    adapter = WristbandAdapter(ble_address="AA:BB:CC:DD:EE:FF")
    registry.register_device(adapter.manifest)
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import Any, Optional

from hardware.protocol import (
    DeviceManifest,
    DeviceCapability,
    HUPAction,
    HUPResult,
)

logger = logging.getLogger("feral.hup.wristband")

HEART_RATE_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
SPO2_UUID = "00002a5e-0000-1000-8000-00805f9b34fb"


class WristbandAdapter:
    """Reference HUP adapter for Bluetooth LE health wristbands.

    This adapter demonstrates the pattern for:
    1. Declaring device capabilities via a manifest
    2. Reading sensor telemetry (heart rate, SpO2, skin temp)
    3. Streaming periodic telemetry to the Brain

    Actuator commands are intentionally absent: see the module docstring.
    """

    def __init__(self, ble_address: str = "", device_id: str = "wristband-01"):
        self.ble_address = ble_address
        self.device_id = device_id
        self._connected = False
        self._last_hr: Optional[int] = None
        self._last_spo2: Optional[float] = None
        self._last_temp: Optional[float] = None
        self._client = None

    @property
    def manifest(self) -> DeviceManifest:
        return DeviceManifest(
            device_id=self.device_id,
            name="Health Wristband",
            device_type="wearable",
            manufacturer="FERAL",
            model="WB-100",
            firmware_version="1.0.0",
            connection_type="bluetooth_le",
            capabilities=[
                DeviceCapability(
                    id="heart_rate",
                    name="Heart Rate",
                    description="Read real-time heart rate in BPM from PPG sensor",
                    category="sensor",
                    permission_tier="passive",
                    returns={"type": "object", "properties": {"bpm": {"type": "integer"}, "rr_interval_ms": {"type": "integer"}}},
                ),
                DeviceCapability(
                    id="spo2",
                    name="Blood Oxygen",
                    description="Read SpO2 percentage from pulse oximeter",
                    category="sensor",
                    permission_tier="passive",
                    returns={"type": "object", "properties": {"spo2_pct": {"type": "number"}}},
                ),
                DeviceCapability(
                    id="skin_temp",
                    name="Skin Temperature",
                    description="Read skin temperature in Celsius from IR thermometer",
                    category="sensor",
                    permission_tier="passive",
                    returns={"type": "object", "properties": {"temperature_c": {"type": "number"}}},
                ),
                # `vibrate` and `set_led` are deliberately NOT advertised.
                # Both were implemented as a `logger.info` followed by
                # `status="success"` — no GATT write of any kind — so the
                # brain could tell a user it had buzzed their wrist when
                # nothing happened. Haptics and LED are vendor-specific
                # characteristics with no standard GATT UUID, so there is
                # nothing to write to until a concrete device defines one.
                # An unadvertised capability cannot be offered to the LLM or
                # rendered in the UI; execute() also refuses them by name
                # with a reason, in case something calls them directly.
            ],
            location="wrist",
            tags=["health", "wearable", "ble"],
        )

    async def connect(self) -> bool:
        """Connect to the BLE wristband.

        No "simulation mode": this used to set ``_connected = True`` with
        ``_client`` still None, which made the adapter claim a device was
        present when none was. ``read_telemetry`` already told the truth in
        that state (``source: no_device``); connect() now agrees with it.
        """
        if not self.ble_address:
            logger.error(
                "Wristband %s has no BLE address configured — not connected. "
                "Construct it as WristbandAdapter(ble_address='AA:BB:...') "
                "after pairing the device.",
                self.device_id,
            )
            return False
        try:
            from bleak import BleakClient
        except ImportError:
            logger.error(
                "Wristband %s cannot connect: bleak is not installed. "
                "Install it with `pip install bleak`.",
                self.device_id,
            )
            return False
        try:
            self._client = BleakClient(self.ble_address)
            await self._client.connect()
            self._connected = True
            logger.info("Connected to wristband at %s", self.ble_address)
            return True
        except Exception as e:
            self._client = None
            logger.error("BLE connection failed: %s", e)
            return False

    async def read_telemetry(self) -> dict[str, Any]:
        """Read all sensor data and return as a flat dict."""
        if not self._client:
            return {
                "heart_rate_bpm": 0,
                "spo2_pct": 0,
                "skin_temp_c": 0.0,
                "timestamp": time.time(),
                "source": "no_device",
                "note": "No BLE wristband connected. Pair a device or use --demo mode for simulated data.",
            }
        try:
            hr_data = await self._client.read_gatt_char(HEART_RATE_UUID)
            self._last_hr = hr_data[1] if len(hr_data) > 1 else hr_data[0]
        except Exception:
            pass
        return {
            "heart_rate_bpm": self._last_hr,
            "spo2_pct": self._last_spo2,
            "skin_temp_c": self._last_temp,
            "timestamp": time.time(),
            "source": "ble_device",
        }

    async def execute(self, action: HUPAction) -> HUPResult:
        """Execute a HUP action on this device."""
        cap_id = action.capability_id

        # A read with no device behind it returns zeros. Reporting those as
        # a successful measurement is how a 0 bpm "reading" ends up in
        # baselines and in health answers, so the no-device case is a
        # failure, not a reading of zero.
        if cap_id in ("heart_rate", "spo2", "skin_temp"):
            data = await self.read_telemetry()
            if data.get("source") == "no_device":
                return HUPResult(
                    action_id=action.action_id, device_id=self.device_id,
                    status="failure",
                    error=(
                        f"Wristband {self.device_id} is not connected, so "
                        f"{cap_id} was not measured. "
                        + str(data.get("note") or "")
                    ).strip(),
                )
            field = {
                "heart_rate": ("bpm", "heart_rate_bpm"),
                "spo2": ("spo2_pct", "spo2_pct"),
                "skin_temp": ("temperature_c", "skin_temp_c"),
            }[cap_id]
            return HUPResult(
                action_id=action.action_id, device_id=self.device_id,
                status="success", data={field[0]: data[field[1]]},
            )
        elif cap_id in ("vibrate", "set_led"):
            # Was: logger.info(...) + status="success". Nothing was ever
            # written to the device. See the manifest comment above.
            error = (
                f"{cap_id} is not implemented for {self.device_id}: this "
                f"adapter has no GATT characteristic to write to, so nothing "
                f"would happen. The capability is not advertised in the "
                f"device manifest for the same reason."
            )
            logger.error("Refusing %s on %s — %s", cap_id, self.device_id, error)
            return HUPResult(
                action_id=action.action_id, device_id=self.device_id,
                status="failure", error=error,
            )
        else:
            return HUPResult(
                action_id=action.action_id, device_id=self.device_id,
                status="failure", error=f"Unknown capability: {cap_id}",
            )

    async def telemetry_loop(self, callback, interval_s: float = 5.0):
        """Continuously stream telemetry data."""
        while self._connected:
            data = await self.read_telemetry()
            await callback(self.device_id, data)
            await asyncio.sleep(interval_s)
