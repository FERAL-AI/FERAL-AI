"""FERAL MQTT Bridge — connects to any MQTT broker for IoT device integration.

Subscribes to configurable topics, maps messages to HUP device events,
publishes commands back. Supports Home Assistant MQTT auto-discovery.

Hardened: TLS support, persistent client, username/password auth.
"""
import asyncio
import json
import logging
import os
import ssl
import time
from typing import Optional, Callable

logger = logging.getLogger("feral.integrations.mqtt")


def _build_ssl_context() -> ssl.SSLContext:
    """Build an SSL context from env vars, falling back to system CA bundle."""
    ca_cert = os.getenv("FERAL_MQTT_CA_CERT", "")
    client_cert = os.getenv("FERAL_MQTT_CLIENT_CERT", "")
    client_key = os.getenv("FERAL_MQTT_CLIENT_KEY", "")

    if ca_cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(ca_cert)
    else:
        ctx = ssl.create_default_context()

    if client_cert and client_key:
        ctx.load_cert_chain(certfile=client_cert, keyfile=client_key)

    return ctx


class MQTTBridge:
    """Bidirectional MQTT bridge for IoT devices."""

    def __init__(self, broker_url: str = "", topics: list[str] = None,
                 on_device_event: Optional[Callable] = None):
        self._broker_url = broker_url or os.getenv("FERAL_MQTT_BROKER", "")
        self._topics = topics or self._default_topics()
        self._on_device_event = on_device_event
        self._client = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._devices: dict[str, dict] = {}
        self._message_count = 0
        self._publish_lock = asyncio.Lock()
        self._tls_enabled = os.getenv("FERAL_MQTT_TLS", "").lower() in ("true", "1", "yes")
        self._username = os.getenv("FERAL_MQTT_USERNAME", "") or None
        self._password = os.getenv("FERAL_MQTT_PASSWORD", "") or None

    @staticmethod
    def _default_topics() -> list[str]:
        custom = os.getenv("FERAL_MQTT_TOPICS", "")
        if custom:
            return [t.strip() for t in custom.split(",")]
        return [
            "homeassistant/+/+/config",
            "home/+/+",
            "sensor/+",
            "zigbee2mqtt/+",
            "tasmota/+/+",
        ]

    def _client_kwargs(self) -> dict:
        """Build kwargs dict for aiomqtt.Client()."""
        kwargs: dict = {"hostname": self._broker_url}
        if self._tls_enabled:
            kwargs["tls_context"] = _build_ssl_context()
        if self._username:
            kwargs["username"] = self._username
        if self._password:
            kwargs["password"] = self._password
        return kwargs

    @property
    def configured(self) -> bool:
        return bool(self._broker_url)

    async def start(self) -> bool:
        if not self.configured:
            logger.info("MQTT bridge: no broker configured (set FERAL_MQTT_BROKER)")
            return False

        try:
            import aiomqtt  # noqa: F401
        except ImportError:
            logger.warning("aiomqtt not installed — MQTT bridge disabled. pip install aiomqtt")
            return False

        self._running = True
        # Keep the handle: api/state.py registers it so shutdown can
        # cancel the loop.
        self._task = asyncio.create_task(self._subscribe_loop())
        logger.info("MQTT bridge started: %s (topics: %s, tls: %s)",
                     self._broker_url, self._topics, self._tls_enabled)
        return True

    async def stop(self):
        self._running = False
        self._client = None

    async def publish(self, topic: str, payload: dict) -> bool:
        if not self._running or self._client is None:
            logger.debug("MQTT publish skipped: bridge not running or no persistent client")
            return False
        try:
            async with self._publish_lock:
                await self._client.publish(topic, json.dumps(payload))
            return True
        except Exception as e:
            logger.error("MQTT publish failed: %s", e)
            return False

    async def _subscribe_loop(self):
        import aiomqtt
        while self._running:
            try:
                kwargs = self._client_kwargs()
                async with aiomqtt.Client(**kwargs) as client:
                    self._client = client
                    for topic in self._topics:
                        await client.subscribe(topic)
                        logger.debug("MQTT subscribed: %s", topic)

                    async for message in client.messages:
                        if not self._running:
                            break
                        await self._handle_message(str(message.topic), message.payload)
            except Exception as e:
                self._client = None
                if self._running:
                    logger.warning("MQTT connection lost: %s — reconnecting in 10s", e)
                    await asyncio.sleep(10)

    async def _handle_message(self, topic: str, payload: bytes):
        self._message_count += 1
        try:
            data = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {"raw": payload.decode(errors="replace")}

        if topic.startswith("homeassistant/"):
            await self._handle_ha_discovery(topic, data)
            return

        device_id = self._topic_to_device_id(topic)

        if self._on_device_event:
            try:
                await self._on_device_event({
                    "source": "mqtt",
                    "topic": topic,
                    "device_id": device_id,
                    "data": data,
                    "timestamp": time.time(),
                })
            except Exception as e:
                logger.warning("MQTT event handler error: %s", e)

    async def _handle_ha_discovery(self, topic: str, config: dict):
        """Process Home Assistant MQTT auto-discovery messages."""
        parts = topic.split("/")
        if len(parts) < 4:
            return

        component_type = parts[1]
        device_id = parts[2]

        device_info = {
            "device_id": f"mqtt_{device_id}",
            "name": config.get("name", device_id),
            "type": component_type,
            "manufacturer": config.get("device", {}).get("manufacturer", "Unknown"),
            "model": config.get("device", {}).get("model", ""),
            "state_topic": config.get("state_topic", ""),
            "command_topic": config.get("command_topic", ""),
            "unique_id": config.get("unique_id", device_id),
        }

        self._devices[device_id] = device_info

        if device_info["state_topic"] and self._client:
            try:
                await self._client.subscribe(device_info["state_topic"])
            except Exception:
                pass

        logger.info("MQTT discovered device: %s (%s) — %s", device_info["name"], component_type, device_id)

    @staticmethod
    def _topic_to_device_id(topic: str) -> str:
        parts = topic.replace("/", "_")
        return f"mqtt_{parts}"

    def list_devices(self) -> list[dict]:
        return list(self._devices.values())

    def stats(self) -> dict:
        return {
            "configured": self.configured,
            "running": self._running,
            "broker": self._broker_url,
            "topics": self._topics,
            "devices_discovered": len(self._devices),
            "messages_received": self._message_count,
            "tls_enabled": self._tls_enabled,
        }
