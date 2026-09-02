"""`FeralNode` — the public class every Python hardware daemon is built on.

Provides a clean async surface (`on_action`, `emit_event`, `run`) over the
HUP v1 wire protocol defined in `HUP_SPEC.md`: single persistent WebSocket,
auto-reconnect with jittered exponential backoff, automatic heartbeat loop,
outbound schema validation, optional mDNS discovery, and first-time pairing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, Awaitable, Callable, Iterable, Optional, Union

import websockets
from websockets.client import WebSocketClientProtocol

from . import pairing as _pairing
from .capability import Capability
from .discovery import discover_brain as _discover_brain
from .schemas import (
    HUP_VERSION,
    DeviceEventPayload,
    HUPActionResponsePayload,
    MESSAGE_TYPES,
    NodeByePayload,
    NodeHeartbeatPayload,
    NodeRegisterPayload,
    build_frame,
)

logger = logging.getLogger("feral_node_sdk")

ActionHandler = Callable[[dict[str, Any]], Awaitable[Any]]
CapLike = Union[Capability, str]


class FeralNode:
    """High-level HUP v1 node client.

    Typical lifecycle:

        node = FeralNode(node_id=..., capabilities=[...], ...)

        @node.on_action("buzz")
        async def buzz(params): ...

        node.run(main_coro())

    The `run()` call blocks until `main_coro()` completes or Ctrl-C.
    """

    def __init__(
        self,
        *,
        node_id: str,
        name: str = "",
        firmware_version: str = "0.0.0",
        brain_url: Optional[str] = None,
        api_key: Optional[str] = None,
        capabilities: Iterable[CapLike] = (),
        node_type: str = "sensor",
        manufacturer: str = "",
        model: str = "",
        platform: str = "",
        sensors: Optional[Iterable[str]] = None,
        actuators: Optional[Iterable[str]] = None,
        location: str = "",
        tags: Optional[Iterable[str]] = None,
        heartbeat_ms: int = 10_000,
        log_level: int = logging.INFO,
    ) -> None:
        logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        self.node_id = node_id
        self.name = name or node_id
        self.firmware_version = firmware_version
        self.brain_url = brain_url
        self._api_key_override = api_key
        self.node_type = node_type
        self.manufacturer = manufacturer
        self.model = model
        self.platform = platform
        self.location = location
        self.tags: list[str] = list(tags or [])

        caps: list[str] = [c.value if isinstance(c, Capability) else str(c) for c in capabilities]
        self.capabilities: list[str] = caps
        self.sensors: list[str] = list(sensors) if sensors is not None else [
            c for c in caps if c in {
                "heart_rate","spo2","temperature","uv","accelerometer","gyroscope",
                "ambient_light","steps","battery","gps","microphone","camera",
            }
        ]
        self.actuators: list[str] = list(actuators) if actuators is not None else [
            c for c in caps if c in {
                "display","speaker","haptic","buzzer","led","motor","relay","valve",
            }
        ]

        self._heartbeat_ms = heartbeat_ms
        self._action_handlers: dict[str, ActionHandler] = {}
        self._ws: Optional[WebSocketClientProtocol] = None
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        # ``None`` until the brain's node_ack lands, which is NOT the
        # same as the empty set. An empty set is a brain saying "you may
        # do nothing"; None is "nobody has told me yet". The dispatch
        # gate below has to be able to tell those apart, which is exactly
        # what the old ``granted or self.capabilities`` fallback could
        # not do.
        self._granted: Optional[set[str]] = None
        self._session_token: Optional[str] = None
        self._connected = asyncio.Event()

    # ───────────────────────── public API ─────────────────────────

    def on_action(self, name: str) -> Callable[[ActionHandler], ActionHandler]:
        """Decorator: register an async handler for an `hup_action_request.name`."""

        def _wrap(fn: ActionHandler) -> ActionHandler:
            self._action_handlers[name] = fn
            return fn

        return _wrap

    async def emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Send a validated `device_event` frame to the brain."""
        payload = DeviceEventPayload(
            node_id=self.node_id,
            event_type=event_type,
            data=data,
            ts=time.time(),
        )
        await self._send("device_event", payload)

    async def emit_audio_frame(
        self,
        data_b64: str,
        *,
        codec: str = "opus",
        sample_rate: int = 24000,
        channels: int = 1,
        sequence: int = 0,
        frame_ms: int = 20,
    ) -> None:
        """Send a HUP v1.1 ``audio_frame`` to the brain.

        The SDK validates the payload against ``AudioFramePayload`` before
        sending, so an oversize or malformed frame raises locally instead
        of being rejected by the brain with error 4020.
        """
        from .schemas import AudioFramePayload, DeviceEventPayload as _DE

        af = AudioFramePayload(
            codec=codec,
            sample_rate=sample_rate,
            channels=channels,
            frame_ms=frame_ms,
            sequence=sequence,
            data_b64=data_b64,
        )
        envelope = _DE(
            node_id=self.node_id,
            event_type="audio_frame",
            data=af.model_dump(),
            ts=time.time(),
        )
        await self._send("device_event", envelope)

    async def emit_video_frame(
        self,
        data_b64: str,
        *,
        codec: str = "jpeg",
        width: int,
        height: int,
        sequence: int = 0,
        keyframe: bool = True,
    ) -> None:
        """Send a HUP v1.1 ``video_frame`` to the brain."""
        from .schemas import VideoFramePayload, DeviceEventPayload as _DE

        vf = VideoFramePayload(
            codec=codec,
            width=width,
            height=height,
            sequence=sequence,
            keyframe=keyframe,
            data_b64=data_b64,
        )
        envelope = _DE(
            node_id=self.node_id,
            event_type="video_frame",
            data=vf.model_dump(),
            ts=time.time(),
        )
        await self._send("device_event", envelope)

    async def emit_glasses_frame(
        self,
        data_b64: str,
        *,
        device_id: Optional[str] = None,
        encoding: str = "jpeg",
        width: Optional[int] = None,
        height: Optional[int] = None,
        source: str = "glasses",
        sequence: Optional[int] = None,
    ) -> None:
        """Send a HUP v1.3.0 ``glasses_frame`` to the brain (HUP_SPEC §5.4.3).

        The brain stores accepted frames into the per-device glasses
        circular buffer at ``feral-core/perception/glasses_buffer.py``
        which the orchestrator's vision-context-attach reads.

        ``device_id`` defaults to the node's own id when omitted — the
        common case for nodes that are themselves the glasses (e.g. a
        W300 daemon). A bridging node (e.g. the iPhone forwarding W610
        frames) should set ``device_id`` to the glasses hardware id.
        """
        from .schemas import GlassesFramePayload

        payload = GlassesFramePayload(
            device_id=device_id or self.node_id,
            encoding=encoding,
            data_b64=data_b64,
            width=width,
            height=height,
            source=source,
            sequence=sequence,
        )
        await self._send("glasses_frame", payload)

    async def send_device_announce(
        self,
        device_id: str,
        *,
        device_kind: str = "bluetooth_le",
        name: str = "",
        manufacturer: str = "",
        rssi_dbm: Optional[int] = None,
        advertised_services: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Send a HUP v1.3.0 ``device_announce`` (HUP_SPEC §5.4.4).

        Inform the brain of a peripheral observed by this node so the
        hardware mesh can record it as a memory entity. Brain dedupes
        by ``device_id`` and updates ``last_seen`` in place.
        """
        from .schemas import DeviceAnnouncePayload

        payload = DeviceAnnouncePayload(
            scanner_node_id=self.node_id,
            device_id=device_id,
            device_kind=device_kind,
            name=name,
            manufacturer=manufacturer,
            rssi_dbm=rssi_dbm,
            advertised_services=list(advertised_services or []),
            last_seen=time.time(),
            metadata=dict(metadata or {}),
        )
        await self._send("device_announce", payload)

    @staticmethod
    async def discover_brain(timeout_s: float = 3.0) -> Optional[str]:
        """Resolve a FERAL brain on the LAN via mDNS. See `discovery.py`."""
        return await _discover_brain(timeout_s=timeout_s)

    @classmethod
    async def pair(
        cls,
        node_id: str,
        brain_url: str,
        *,
        code: Optional[str] = None,
        name: str = "",
        timeout_s: float = 300.0,
        verify_tls: bool = True,
    ) -> str:
        """Run the 6-digit pairing flow. Returns the persisted API key."""
        return await _pairing.pair(
            node_id=node_id,
            brain_url=brain_url,
            name=name,
            code=code,
            timeout_s=timeout_s,
            verify_tls=verify_tls,
        )

    async def run_async(self, main_coro: Optional[Awaitable[Any]] = None) -> None:
        """Async entry-point. Use from inside an existing event loop.

        Daemons that orchestrate their own asyncio.run(...) — for example
        the theora_glasses_daemon and wristband_daemon packages — await
        this directly instead of calling the sync ``run()`` wrapper
        (which would nest ``asyncio.run`` inside an already-running
        event loop and raise ``RuntimeError``).
        """
        await self._run_async(main_coro)

    def run(self, main_coro: Optional[Awaitable[Any]] = None) -> None:
        """Sync CLI entry-point. Blocks until the node shuts down.

        Calls ``asyncio.run(run_async(main_coro))`` internally, so this
        MUST NOT be called from inside an already-running event loop.
        From async code, call ``await node.run_async(...)`` instead.
        """
        try:
            asyncio.run(self._run_async(main_coro))
        except KeyboardInterrupt:
            logger.info("Daemon stopped by user.")

    async def close(self, reason: str = "shutdown") -> None:
        """Send `node_bye` and tear down the current connection."""
        self._stop.set()
        try:
            await self._send("node_bye", NodeByePayload(reason=reason))
        except Exception:
            pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ─────────────────────── internal machinery ───────────────────

    def _resolve_api_key(self) -> Optional[str]:
        return self._api_key_override or _pairing.load_key(self.node_id)

    async def _run_async(self, main_coro: Optional[Awaitable[Any]]) -> None:
        main_task: Optional[asyncio.Task[Any]] = None
        try:
            ws_task = asyncio.create_task(self._ws_supervisor(), name="feral-ws")
            if main_coro is not None:
                main_task = asyncio.create_task(self._run_main(main_coro), name="feral-main")
                done, _ = await asyncio.wait({ws_task, main_task}, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    t.result()
            else:
                await ws_task
        finally:
            self._stop.set()
            if main_task and not main_task.done():
                main_task.cancel()
            await self.close()

    async def _run_main(self, coro: Awaitable[Any]) -> None:
        await self._connected.wait()
        await coro

    async def _ws_supervisor(self) -> None:
        backoff = 0.1
        while not self._stop.is_set():
            url = self.brain_url or await _discover_brain(timeout_s=3.0)
            if not url:
                logger.warning("No brain_url set and mDNS found none. Retrying in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            key = self._resolve_api_key()
            headers = [("Authorization", f"Bearer {key}")] if key else []

            try:
                logger.info("Connecting to %s", url)
                async with websockets.connect(
                    url,
                    additional_headers=headers,
                    max_size=2 * 1024 * 1024,
                    ping_interval=None,
                ) as ws:
                    self._ws = ws
                    backoff = 0.1
                    await self._handshake()
                    self._connected.set()
                    await asyncio.gather(
                        self._read_loop(),
                        self._heartbeat_loop(),
                    )
            except Exception as exc:
                logger.warning("WS connection error: %s", exc)
            finally:
                self._ws = None
                self._connected.clear()

            if self._stop.is_set():
                break

            sleep_for = backoff * (0.5 + random.random())
            sleep_for = min(sleep_for, 30.0)
            logger.info("Reconnecting in %.2fs", sleep_for)
            await asyncio.sleep(sleep_for)
            backoff = min(backoff * 2, 30.0)

    async def _handshake(self) -> None:
        payload = NodeRegisterPayload(
            node_id=self.node_id,
            node_type=self.node_type,  # type: ignore[arg-type]
            name=self.name,
            manufacturer=self.manufacturer,
            model=self.model,
            firmware_version=self.firmware_version,
            platform=self.platform,
            capabilities=self.capabilities,
            sensors=self.sensors,
            actuators=self.actuators,
            location=self.location,
            tags=self.tags,
        )
        await self._send("node_register", payload)

    async def _read_loop(self) -> None:
        assert self._ws is not None
        async for raw in self._ws:
            try:
                frame = json.loads(raw)
            except Exception:
                logger.warning("Dropped non-JSON frame")
                continue
            msg_type = frame.get("type", "")
            payload = frame.get("payload") or {}
            if msg_type == "node_ack":
                self._session_token = payload.get("session_token")
                # ``if "granted_capabilities" in payload``, not
                # ``payload.get(...) or self.capabilities``. The old
                # expression read an empty grant list -- a brain saying
                # "you may do nothing" -- as falsy and substituted
                # everything this node declared, so the one answer the
                # operator most needs to be able to give was the one that
                # could not be transmitted. A brain that omits the field
                # entirely is a pre-HUP-1.4 brain with no grant store, and
                # only that case falls back to the declaration.
                if "granted_capabilities" in payload:
                    self._granted = {
                        str(c) for c in (payload.get("granted_capabilities") or [])
                    }
                else:
                    self._granted = set(self.capabilities)  # pre-1.4 brain
                hb = int(payload.get("heartbeat_ms") or self._heartbeat_ms)
                self._heartbeat_ms = max(1000, hb)
                logger.info("node_ack: granted=%s denied=%s heartbeat=%dms",
                            sorted(self._granted),
                            sorted(payload.get("denied_capabilities") or []),
                            self._heartbeat_ms)
            elif msg_type == "hup_action_request":
                asyncio.create_task(self._dispatch_action(payload))
            elif msg_type in ("text_response", "error", "node_bye"):
                logger.debug("frame %s: %s", msg_type, payload)
            else:
                logger.debug("ignoring unknown frame type: %s", msg_type)

    async def _dispatch_action(self, payload: dict[str, Any]) -> None:
        action_id = str(payload.get("action_id", ""))
        name = str(payload.get("name", ""))
        params = dict(payload.get("params") or {})

        # HUP_SPEC.md section 6, nodes: "MUST refuse any
        # hup_action_request whose name is not in their registered
        # capabilities, replying with success=false,
        # error='capability_denied'". ``self._granted`` was assigned from
        # node_ack and logged and then never read by anything, so the
        # grant the brain sent had no effect on this side at all. A
        # handler registered for a capability the operator has since
        # denied ran exactly as if it had not been.
        if self._granted is not None and name not in self._granted:
            logger.warning(
                "refusing action %s: not in granted capabilities %s",
                name, sorted(self._granted),
            )
            await self._send_action_response(
                action_id, success=False, error=f"capability_denied: {name}"
            )
            return

        handler = self._action_handlers.get(name)
        if handler is None:
            await self._send_action_response(
                action_id, success=False, error=f"capability_denied: {name}"
            )
            return

        started = time.time()
        try:
            result = await handler(params)
        except Exception as exc:
            logger.exception("action %s failed", name)
            await self._send_action_response(
                action_id, success=False, error=str(exc),
                duration_ms=int((time.time() - started) * 1000),
            )
            return

        if not isinstance(result, dict):
            result = {"ok": True, "value": result}
        await self._send_action_response(
            action_id, success=True, result=result,
            duration_ms=int((time.time() - started) * 1000),
        )

    async def _send_action_response(
        self,
        action_id: str,
        *,
        success: bool,
        result: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
        duration_ms: int = 0,
    ) -> None:
        payload = HUPActionResponsePayload(
            action_id=action_id,
            success=success,
            result=result or {},
            error=error,
            duration_ms=duration_ms,
        )
        await self._send("hup_action_response", payload)

    async def _heartbeat_loop(self) -> None:
        while self._ws is not None and not self._stop.is_set():
            try:
                await asyncio.sleep(self._heartbeat_ms / 1000.0)
                await self._send(
                    "node_heartbeat",
                    NodeHeartbeatPayload(ts=time.time()),
                )
            except Exception as exc:
                logger.debug("heartbeat stopped: %s", exc)
                return

    async def _send(self, type_: str, payload: Any) -> None:
        if self._ws is None:
            logger.debug("dropping %s frame — not connected", type_)
            return
        model_cls = MESSAGE_TYPES.get(type_)
        if model_cls is None:
            raise ValueError(f"unknown HUP type: {type_!r}")
        if not isinstance(payload, model_cls):
            payload = model_cls(**(payload or {}))
        frame = {
            "hup_version": HUP_VERSION,
            "type": type_,
            "ts": time.time(),
            "payload": payload.model_dump(exclude_none=False),
        }
        async with self._send_lock:
            await self._ws.send(json.dumps(frame))


__all__ = ["FeralNode"]
