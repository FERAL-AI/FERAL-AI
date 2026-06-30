"""Generic self-describing HUP adapter — one adapter per *transport*.

The whole point of the Hardware Use Protocol is that the brain should learn a
device from the device, not from hand-written per-device code. The CuteBot
adapter proved the manifest half of that (``QtBot.capabilities()['actions']``
→ ``DeviceManifest`` → generic LLM tools + safety + honesty loop). This module
proves the *execute* half: a single adapter that dispatches any capability by
passing it straight through to the device's own ``execute(command, **params)``.

Any companion library that exposes the small duck-typed surface below plugs in
with **zero** FERAL-side code — no ``if cap_id == ...`` chain, no per-device
adapter:

    capabilities() -> dict      # the HUP self-description envelope
    execute(command, **params) -> dict   # returns {"ok": bool, ...}
    status() -> dict            # optional telemetry snapshot
    poll_events(seconds) -> list[dict]    # optional event stream
    close() -> None             # optional teardown

Device-specific *safety* (e.g. clamp a wheel speed, gate motion on battery)
still belongs in a thin transport adapter that overrides
:meth:`_preprocess` / :meth:`_harden_params`; everything else is generic. The
``CuteBotAdapter`` is exactly that — a ``GenericSelfDescribingAdapter`` with a
battery gate, a drive clamp, and a telemetry/episode loop layered on top.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from hardware.protocol import (
    DeviceManifest,
    HUPAction,
    HUPResult,
    device_manifest_from_capabilities,
)

logger = logging.getLogger("feral.hup.generic")


class GenericSelfDescribingAdapter:
    """Passthrough HUP adapter backed by a self-describing companion device."""

    def __init__(
        self,
        device_id: str,
        *,
        device: Any = None,
        device_factory: Optional[Callable[[], Any]] = None,
        identity: Optional[dict[str, Any]] = None,
        type_aliases: Optional[dict[str, str]] = None,
        memory: Any = None,
        telemetry_key: Optional[str] = None,
    ):
        self.device_id = device_id
        self._device = device
        self._device_factory = device_factory
        self._identity = dict(identity or {})
        self._type_aliases = dict(type_aliases or {})
        self._memory = memory
        self._telemetry_key = telemetry_key
        self._connected = device is not None
        self._telemetry_running = False
        # Serializes all access to the (typically non-thread-safe) device.
        from hardware.async_io_lock import ThreadAsyncIOLock

        self._io_lock = ThreadAsyncIOLock()

    # ── manifest (the device tells us what it can do) ────────────────────

    @property
    def manifest(self) -> DeviceManifest:
        device = self._device
        if device is not None and hasattr(device, "capabilities"):
            try:
                caps = device.capabilities()
                return device_manifest_from_capabilities(
                    self.device_id,
                    caps,
                    type_aliases=self._type_aliases,
                    **self._identity,
                )
            except Exception as exc:
                logger.debug(
                    "%s self-description failed (%s); using minimal manifest",
                    self.device_id,
                    exc,
                )
        return DeviceManifest(
            device_id=self.device_id,
            device_type=str(self._identity.get("device_type", "device")),
            name=str(self._identity.get("name", self.device_id)),
            manufacturer=str(self._identity.get("manufacturer", "")),
            model=str(self._identity.get("model", "")),
            connection_type=str(self._identity.get("connection_type", "websocket")),
            location=str(self._identity.get("location", "")),
        )

    # ── connection lifecycle ─────────────────────────────────────────────

    async def connect(self) -> bool:
        async with self._io_lock:
            if self._device is not None:
                self._connected = True
                return True
            if self._device_factory is None:
                return False
            try:
                self._device = await asyncio.to_thread(self._device_factory)
                self._connected = True
                logger.info("Connected generic adapter %s", self.device_id)
                return True
            except Exception as exc:
                logger.warning("Generic connect failed for %s: %s", self.device_id, exc)
                self._device = None
                self._connected = False
                return False

    async def disconnect(self) -> None:
        async with self._io_lock:
            device = self._device
            self._device = None
            self._connected = False
            close = getattr(device, "close", None)
            if close is None:
                return
            try:
                await asyncio.to_thread(close)
            except Exception as exc:
                logger.debug("%s disconnect error: %s", self.device_id, exc)

    # ── execute (the passthrough core) ───────────────────────────────────

    async def execute(self, action: HUPAction) -> HUPResult:
        device = self._device
        if device is None:
            return HUPResult(
                action_id=action.action_id,
                device_id=self.device_id,
                status="failure",
                error=f"{self.device_id} not connected",
            )
        cap_id = action.capability_id
        params = await self._preprocess(action, dict(action.parameters or {}))
        if isinstance(params, HUPResult):  # a precheck refused the action
            return params
        params = self._harden_params(cap_id, params)
        try:
            async with self._io_lock:
                result = await asyncio.to_thread(device.execute, cap_id, **params)
        except Exception as exc:
            return HUPResult(
                action_id=action.action_id,
                device_id=self.device_id,
                status="failure",
                error=str(exc),
            )
        return self._normalize_result(action, result)

    def _normalize_result(self, action: HUPAction, result: Any) -> HUPResult:
        if isinstance(result, HUPResult):
            return result
        if isinstance(result, dict):
            ok = bool(result.get("ok", True))
            return HUPResult(
                action_id=action.action_id,
                device_id=self.device_id,
                status="success" if ok else "failure",
                data=result,
                error="" if ok else str(result.get("error", "command failed")),
            )
        return HUPResult(
            action_id=action.action_id,
            device_id=self.device_id,
            status="success",
            data={"result": result},
        )

    async def _preprocess(
        self, action: HUPAction, params: dict[str, Any]
    ) -> Any:
        """Hook for device-specific prechecks (battery gate, etc.).

        Return the (possibly mutated) params dict to proceed, or a HUPResult
        to refuse the action. Base implementation is a no-op passthrough.
        """
        return params

    def _harden_params(self, cap_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Hook for device-specific param clamping. Base is a no-op."""
        return params

    # ── state / telemetry ────────────────────────────────────────────────

    async def get_state(self) -> dict[str, Any]:
        device = self._device
        if device is None or not hasattr(device, "status"):
            return {"online": False}
        try:
            async with self._io_lock:
                raw = await asyncio.to_thread(device.status)
            return raw if isinstance(raw, dict) else {"online": False}
        except Exception as exc:
            logger.debug("%s get_state failed: %s", self.device_id, exc)
            return {"online": False}

    async def get_status(self) -> dict[str, Any]:
        return await self.get_state()

    async def poll_events(self, seconds: float = 0.5) -> list[dict[str, Any]]:
        device = self._device
        poll = getattr(device, "poll_events", None)
        if poll is None:
            await asyncio.sleep(seconds)
            return []
        try:
            async with self._io_lock:
                return await asyncio.to_thread(poll, seconds)
        except Exception as exc:
            logger.debug("%s poll_events failed: %s", self.device_id, exc)
            return []

    async def start_telemetry_loop(
        self,
        perception: Any,
        get_session_ids: Callable[[], list[str]],
        *,
        memory: Any = None,
    ) -> None:
        """Push ``{telemetry_key: state}`` to perception ~1 Hz.

        Generic and best-effort: a device with no ``status()`` simply produces
        no telemetry. The key defaults to the device's type so multiple device
        classes don't collide in the perception sensor map.
        """
        self._telemetry_running = True
        key = self._telemetry_key or self.manifest.device_type or self.device_id
        reconnect_interval = 5.0
        while self._telemetry_running:
            try:
                if not self._connected or self._device is None:
                    if not await self.connect():
                        await asyncio.sleep(reconnect_interval)
                        continue
                state = await self.get_state()
                for sid in get_session_ids():
                    perception.update_sensors(sid, {key: state})
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("%s telemetry loop error: %s", self.device_id, exc)
                await asyncio.sleep(reconnect_interval)

    def stop_telemetry_loop(self) -> None:
        self._telemetry_running = False


__all__ = ["GenericSelfDescribingAdapter"]
