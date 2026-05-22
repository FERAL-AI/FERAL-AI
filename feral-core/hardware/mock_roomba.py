"""Mock Roomba HUP node — closes THESIS_SCENARIOS S5 on demo machines.

The Roomba scenario (S5 in ``ASOS/AUDIT-r14/phase2/THESIS_SCENARIOS.md``)
goes: glasses stream → voice command "this room is dirty, send the
Roomba" → vision context attached to LLM → LLM calls
``home_assistant__vacuum_start(entity_id="vacuum.living_room")`` →
Home Assistant dispatches → Roomba starts.

On a demo machine without HA wired up (no Home Assistant token in the
vault, no Roomba on the LAN) the Lane 10 ``HomeAssistantIntegration``
correctly returns a structured ``{success: False, reason: …}`` error —
which is honest, but kills the demo.

This module ships a brain-side mock that:

1. Registers a virtual ``vacuum.mock_roomba`` entity in the
   ``HardwareMesh._announced_devices`` table so ``/api/hardware/mesh``
   and the Lane 12 Devices page show it.
2. Exposes :meth:`MockRoomba.start` returning
   ``{success: True, data: {started: True, entity_id,
   service: "vacuum.start"}}`` — exactly the structured shape Lane
   10's ``HomeAssistantIntegration.vacuum_start`` returns — in
   < 500 ms (typically < 5 ms; the budget is for slow CI hardware).
3. Records the event in episodic memory with ``category=actuator`` so
   the orchestrator's memory-of-actions log answers "did the brain
   start the Roomba today?" the same way it answers integration calls.

No real HUP WebSocket loop — the mock lives in the brain process. The
abstraction it presents matches Lane 10's contract so the orchestrator
tool-dispatch path (Lane 08) can ask either backend and get the same
shape. The boot wiring is the user-visible feature flag: when
``MOCK_ROOMBA_ENABLED`` defaults to True (demo mode), the brain
auto-registers; operators can set ``FERAL_MOCK_ROOMBA=0`` to disable
when wiring up real HA.

The Lane 11 acceptance criterion is:

* HUP-node-shaped registration in ``hardware/mesh.py`` ✅
* ``vacuum_start`` returns ``{started: True}`` in < 500 ms ✅
* Live verify trace: orchestrator → memory writes → mesh entry ✅
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional


logger = logging.getLogger("feral.hardware.mock_roomba")


DEFAULT_ENTITY_ID = "vacuum.mock_roomba"


def is_enabled() -> bool:
    """Honor ``FERAL_MOCK_ROOMBA`` (default on for demo reliability)."""
    raw = os.getenv("FERAL_MOCK_ROOMBA", "1").strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


class MockRoomba:
    """A vacuum entity that exists only inside the brain process.

    The class is intentionally tiny — most of its complexity lives in
    the boot wiring (registering with the hardware mesh + memory) and
    the response-shape parity with Lane 10's
    ``HomeAssistantIntegration.vacuum_start``.

    State: a single ``is_running`` flag + the last ``started_at`` /
    ``stopped_at`` timestamps. The flag is reset on every fresh brain
    boot (the mock is not persisted across restarts — a real HA-backed
    Roomba would be queried from HA on cold start instead).
    """

    def __init__(self, entity_id: str = DEFAULT_ENTITY_ID, *, memory=None):
        self.entity_id = entity_id
        self._memory = memory
        self.is_running: bool = False
        self.started_at: Optional[float] = None
        self.stopped_at: Optional[float] = None
        # SLA the live verify trace asserts. Bumping this means
        # updating Lane 11's PR body evidence table too.
        self.start_budget_seconds: float = 0.5

    def bind_memory(self, memory) -> None:
        """Late-bind the memory store so ``start`` can record an episode."""
        self._memory = memory

    async def start(self, entity_id: str = "", **kwargs) -> dict:
        """Begin a vacuum cycle. Returns the Lane 10 ``vacuum_start`` shape.

        Honors ``entity_id`` for parity with the HA call. When the
        caller passes a different ``entity_id`` (e.g. the orchestrator
        tries to start a real entity but HA isn't reachable), we
        respond with a structured ``{success: False, reason:
        "wrong_entity"}`` rather than impersonating the real device —
        truthfulness gate.
        """
        target = (entity_id or self.entity_id).strip()
        if target != self.entity_id:
            return {
                "success": False,
                "error": (
                    f"mock_roomba only handles {self.entity_id!r}, "
                    f"got entity_id={target!r}"
                ),
                "reason": "wrong_entity",
            }
        t0 = time.time()
        self.is_running = True
        self.started_at = t0
        self.stopped_at = None
        await self._record_event("started", t0, target)
        elapsed = time.time() - t0
        if elapsed > self.start_budget_seconds:
            # The mock itself should never breach budget; this guard
            # is for the memory-write back-pressure case (sync wal
            # offload misconfigured). Logs so the live-verify trace
            # surfaces the slow path.
            logger.warning(
                "mock_roomba.start exceeded SLA: %.3fs (budget=%.3fs)",
                elapsed, self.start_budget_seconds,
            )
        return {
            "success": True,
            "data": {
                "started": True,
                "entity_id": target,
                "service": "vacuum.start",
                "duration_ms": int(elapsed * 1000),
            },
        }

    async def stop(self, entity_id: str = "", **kwargs) -> dict:
        """Stop the mock vacuum cycle. Parity with HA vacuum.stop."""
        target = (entity_id or self.entity_id).strip()
        if target != self.entity_id:
            return {
                "success": False,
                "error": f"mock_roomba only handles {self.entity_id!r}",
                "reason": "wrong_entity",
            }
        self.is_running = False
        self.stopped_at = time.time()
        await self._record_event("stopped", self.stopped_at, target)
        return {
            "success": True,
            "data": {
                "stopped": True,
                "entity_id": target,
                "service": "vacuum.stop",
            },
        }

    def status(self) -> dict:
        """REST snapshot for ``/api/hardware/mock_roomba``."""
        return {
            "entity_id": self.entity_id,
            "is_running": self.is_running,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "enabled": True,
        }

    async def _record_event(self, kind: str, ts: float, entity: str) -> None:
        memory = self._memory
        if memory is None:
            return
        try:
            episode_save = getattr(memory, "episode_save", None)
            if episode_save is None:
                return
            await episode_save(
                f"mock_roomba {kind} for {entity}",
                metadata={
                    "category": "actuator",
                    "actuator": "vacuum",
                    "entity_id": entity,
                    "action": kind,
                    "source": "mock_roomba",
                    "ts": ts,
                },
            )
        except Exception as exc:
            logger.debug("mock_roomba episode_save failed: %s", exc)


def register_with_mesh(mesh, mock: MockRoomba) -> None:
    """Register the mock as a discovered vacuum in ``HardwareMesh``.

    This makes the mock visible to ``/api/hardware/mesh`` + Lane 12's
    Devices page without a separate REST surface. The mesh's
    ``_announced_devices`` table is the canonical "things the brain
    knows about" registry, so writing here keeps the demo UI honest
    (the operator sees a real entry, not a hand-rolled mock card).
    """
    if mesh is None:
        return
    try:
        mesh._announced_devices[mock.entity_id] = {
            "device_id": mock.entity_id,
            "scanner_node_id": "brain",
            "device_kind": "mdns",  # closest match for a network-discovered actuator
            "name": "Mock Roomba (demo)",
            "manufacturer": "FERAL",
            "rssi_dbm": None,
            "advertised_services": ["vacuum.start", "vacuum.stop"],
            "first_seen": time.time(),
            "last_seen": time.time(),
            "metadata": {
                "category": "device",
                "kind": "vacuum",
                "mock": True,
                "tags": ["vacuum", "mock", "actuator"],
            },
        }
    except Exception as exc:
        logger.debug("mock_roomba register_with_mesh failed: %s", exc)


__all__ = ["MockRoomba", "register_with_mesh", "is_enabled", "DEFAULT_ENTITY_ID"]
