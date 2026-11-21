"""
FERAL Cross-Device Session Handoff
=====================================
Tracks connected devices and transfers working-memory context between
sessions so the user can say "continue on desktop" and pick up where
they left off.

Supports four node types: phone, desktop, wristband, glasses.
Each gets a device-optimised response format via ``format_for_device``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional

logger = logging.getLogger("feral.agents.session_handoff")

NODE_TYPES = ("phone", "desktop", "wristband", "glasses", "channel", "browser_node")
DEFAULT_HISTORY_DEPTH = 20


@dataclass
class ConnectedDevice:
    session_id: str
    node_type: str
    node_id: str
    connected_at: float = field(default_factory=time.time)


class SessionHandoffManager:
    """Manages cross-device session handoff and device-aware formatting."""

    def __init__(
        self,
        *,
        sessions: dict | None = None,
        daemons: dict | None = None,
        memory=None,
        orchestrator=None,
        send_to_session: Callable[[str, Any], Awaitable[None]] | None = None,
    ):
        self._sessions = sessions or {}
        self._daemons = daemons or {}
        self._memory = memory
        self._orchestrator = orchestrator
        self._send_to_session = send_to_session

        self._device_registry: dict[str, ConnectedDevice] = {}
        # Target node_type (normalized) -> (source_session_id, history_depth)
        self._pending: dict[str, tuple[str, int]] = {}

    def _normalize_node_type(self, node_type: str) -> str:
        return node_type if node_type in NODE_TYPES else "desktop"

    def register_device(
        self,
        session_id: str,
        node_type: str,
        node_id: str = "",
    ):
        """Track a newly connected session with its device type."""
        effective_node_id = node_id or session_id
        self._device_registry[session_id] = ConnectedDevice(
            session_id=session_id,
            node_type=node_type if node_type in NODE_TYPES else "desktop",
            node_id=effective_node_id,
        )
        logger.info(
            "Device registered for handoff: session=%s type=%s node=%s",
            session_id[:8], node_type, effective_node_id,
        )

    def unregister_device(self, session_id: str):
        dev = self._device_registry.pop(session_id, None)
        if dev:
            logger.info("Device unregistered: session=%s type=%s", session_id[:8], dev.node_type)

    def get_active_devices(self) -> list[dict]:
        """Return connected device types with session IDs."""
        return [
            {
                "session_id": dev.session_id,
                "node_type": dev.node_type,
                "node_id": dev.node_id,
                "connected_at": dev.connected_at,
            }
            for dev in self._device_registry.values()
        ]

    def _find_session_for_node_type(self, node_type: str) -> Optional[str]:
        """Find a live session ID that matches the requested node type."""
        want = self._normalize_node_type(node_type)
        for dev in self._device_registry.values():
            if dev.node_type == want:
                if dev.session_id in self._sessions:
                    return dev.session_id
        return None

    async def _apply_transfer(
        self,
        from_session_id: str,
        to_session_id: str,
        to_node_type: str,
        history_depth: int,
    ) -> dict:
        """Copy working memory and notify both ends."""
        from agents.runtime_context_checkpoint import RuntimeContextError, legacy_context_mutation
        try:
            async with legacy_context_mutation(self._orchestrator, self._memory,
                                               (from_session_id, to_session_id), "handoff"):
                return await self._apply_transfer_legacy(from_session_id, to_session_id, to_node_type, history_depth)
        except RuntimeContextError as exc:
            return {"success": False, "pending": False, "error": "Managed thread handoff is unavailable", "code": exc.code}

    async def _apply_transfer_legacy(self, from_session_id: str, to_session_id: str,
                                     to_node_type: str, history_depth: int) -> dict:
        if to_session_id == from_session_id:
            return {"success": False, "error": "Source and target are the same device"}

        messages_transferred = 0
        if self._memory:
            history = self._memory.working_get(from_session_id, limit=history_depth)
            if history:
                self._memory.working_replace(to_session_id, list(history))
                messages_transferred = len(history)

        if self._send_to_session:
            await self._notify_old_device(from_session_id, to_node_type)
            await self._notify_new_device(to_session_id, from_session_id, messages_transferred)

        logger.info(
            "Handoff complete: %s -> %s (%s), %d messages",
            from_session_id[:8], to_session_id[:8], to_node_type, messages_transferred,
        )

        return {
            "success": True,
            "pending": False,
            "from_session_id": from_session_id,
            "to_session_id": to_session_id,
            "to_node_type": to_node_type,
            "messages_transferred": messages_transferred,
        }

    async def on_session_registered(
        self,
        session_id: str,
        node_type: str,
        node_id: str = "",
    ) -> Optional[dict]:
        """
        Register this WebSocket session for handoff tracking and, if a handoff
        was queued for this device type, apply it to this session.
        """
        self.register_device(session_id, node_type, node_id=node_id)
        want = self._normalize_node_type(node_type)
        pending = self._pending.pop(want, None)
        if not pending:
            return None
        from_session_id, history_depth = pending
        if from_session_id == session_id:
            self._pending[want] = pending
            return None
        if from_session_id not in self._sessions:
            logger.info("Pending handoff dropped: source session no longer connected")
            return {"success": False, "error": "Source session disconnected before handoff"}
        return await self._apply_transfer(
            from_session_id, session_id, want, history_depth,
        )

    async def handoff(
        self,
        from_session_id: str,
        to_node_type: str,
        history_depth: int = DEFAULT_HISTORY_DEPTH,
    ) -> dict:
        """
        Transfer working-memory context from *from_session_id* to a session
        that matches *to_node_type*.

        Returns a status dict with ``success``, ``to_session_id``, and
        ``messages_transferred``.
        """
        want = self._normalize_node_type(to_node_type)
        from agents.runtime_context_checkpoint import RuntimeContextError, legacy_context_mutation
        try:
            async with legacy_context_mutation(self._orchestrator, self._memory, (from_session_id,), "handoff"):
                pass
        except RuntimeContextError as exc:
            return {"success": False, "pending": False, "error": "Managed thread handoff is unavailable", "code": exc.code}
        to_session_id = self._find_session_for_node_type(want)
        if not to_session_id:
            self._pending[want] = (from_session_id, history_depth)
            logger.info(
                "Handoff queued until a %s session connects (from %s)",
                want, from_session_id[:8],
            )
            return {
                "success": True,
                "pending": True,
                "from_session_id": from_session_id,
                "to_node_type": want,
                "messages_transferred": 0,
                "available_devices": [d["node_type"] for d in self.get_active_devices()],
            }

        return await self._apply_transfer(
            from_session_id, to_session_id, want, history_depth,
        )

    async def _notify_old_device(self, session_id: str, target_type: str):
        """Tell the old device that its session has been handed off."""
        if not self._send_to_session:
            return
        from models.protocol import FeralMessage, TextResponsePayload

        msg = FeralMessage(
            session_id=session_id,
            hop="brain",
            type="text_response",
            payload=TextResponsePayload(
                text=f"Session handed off to your {target_type}. You can close this window or keep it open — I'll keep it in sync."
            ).model_dump(),
        )
        try:
            await self._send_to_session(session_id, msg)
        except Exception as exc:
            logger.warning("Failed to notify old device: %s", exc)

    async def _notify_new_device(self, session_id: str, from_session_id: str, msg_count: int):
        """Welcome the new device with a context-aware message."""
        if not self._send_to_session:
            return
        from models.protocol import FeralMessage, TextResponsePayload

        text = (
            f"Picked up where you left off — {msg_count} messages synced from your other device. "
            "How can I help?"
        )
        msg = FeralMessage(
            session_id=session_id,
            hop="brain",
            type="text_response",
            payload=TextResponsePayload(text=text).model_dump(),
        )
        try:
            await self._send_to_session(session_id, msg)
        except Exception as exc:
            logger.warning("Failed to notify new device: %s", exc)


# ── Device-Aware Response Formatting ─────────────────────────────

# Wristband haptic patterns: (duration_ms, intensity 0-1)
_HAPTIC_ACK = [{"duration_ms": 100, "intensity": 0.6}]
_HAPTIC_ALERT = [
    {"duration_ms": 150, "intensity": 1.0},
    {"duration_ms": 80, "intensity": 0.0},
    {"duration_ms": 150, "intensity": 1.0},
]

PHONE_MAX_CHARS = 200


def format_for_device(
    response_text: str,
    sdui_payload: dict | None,
    node_type: str,
) -> dict:
    """
    Re-shape an assistant response for the target device.

    Returns a dict ready to be sent as a message payload:
    - **phone**: short text (≤200 chars), no SDUI, voice-optimised flag
    - **desktop**: full text + optional SDUI cards
    - **wristband**: haptic vibration pattern only
    - **glasses**: audio transcript + minimal display text
    """
    if node_type == "phone":
        short = _truncate(response_text, PHONE_MAX_CHARS)
        return {
            "text": short,
            "voice_optimized": True,
            "sdui": None,
        }

    if node_type == "desktop":
        return {
            "text": response_text,
            "voice_optimized": False,
            "sdui": sdui_payload,
        }

    if node_type == "wristband":
        has_alert_words = any(
            w in response_text.lower()
            for w in ("alert", "warning", "urgent", "danger", "emergency")
        )
        pattern = _HAPTIC_ALERT if has_alert_words else _HAPTIC_ACK
        return {
            "haptic": pattern,
            "text": None,
            "sdui": None,
        }

    if node_type == "glasses":
        display_text = _truncate(response_text, 80)
        return {
            "audio_transcript": response_text,
            "display_text": display_text,
            "voice_optimized": True,
            "sdui": None,
        }

    return {
        "text": response_text,
        "voice_optimized": False,
        "sdui": sdui_payload,
    }


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rsplit(" ", 1)[0] + "…"
