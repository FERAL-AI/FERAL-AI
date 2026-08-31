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
shape.

**The mock is opt-in.** ``FERAL_MOCK_ROOMBA`` defaults to ``0``: set it
to ``1`` on the demo machine that needs S5. It used to default on, which
meant a fresh install advertised ``vacuum.mock_roomba`` in
``/api/hardware/mesh`` and on the Devices page next to the operator's
real hardware. FERAL's whole claim about devices is that it re-reads
telemetry and refuses to say an actuator worked when the device
disagrees (``hardware/capability_skill.py``), and that an adapter with
no transport reports itself not connected rather than faking acks
(``hardware/adapters/robot_arm.py``). A device in the list that nothing
in the house corresponds to spends the credibility those two paths earn.
Being labelled ``simulated`` makes the mock honest once you call it;
being off by default is what keeps it from being asserted at all.

The Lane 11 acceptance criterion is:

* HUP-node-shaped registration in ``hardware/mesh.py`` ✅
* ``vacuum_start`` returns ``{started: True}`` in < 500 ms ✅
* Live verify trace: orchestrator → memory writes → mesh entry ✅
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional


logger = logging.getLogger("feral.hardware.mock_roomba")


DEFAULT_ENTITY_ID = "vacuum.mock_roomba"

# Stamped into every response this class produces. The mock exists so the
# demo has a vacuum; it must never be mistaken for one.
SIMULATED_NOTE = (
    "Simulated device. No physical vacuum was commanded. This entity "
    "exists only because FERAL_MOCK_ROOMBA=1 was set; unset it to remove "
    "it, or connect a real vacuum through Home Assistant."
)

# Values that turn the mock ON. Everything else — including the unset
# default and any typo — leaves it off, because the failure mode of
# guessing wrong is a device that does not exist showing up in the
# operator's device list.
_TRUTHY = {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    """Honor ``FERAL_MOCK_ROOMBA``. Opt-in: defaults to OFF.

    Default-off is the honest default. A fresh install has no vacuum, so
    the brain must not claim one; the demo machine that wants
    THESIS_SCENARIOS S5 sets ``FERAL_MOCK_ROOMBA=1`` deliberately.
    """
    return os.getenv("FERAL_MOCK_ROOMBA", "0").strip().lower() in _TRUTHY


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
                # Shape parity with HomeAssistantIntegration.vacuum_start is
                # the point of this class, and it is also the hazard: without
                # these two fields the envelope for "a vacuum in this house
                # started cleaning" is byte-identical to the envelope for
                # "nothing happened, there is no vacuum". The mock is opt-in
                # now (FERAL_MOCK_ROOMBA defaults to "0"), but once enabled it
                # is reachable from POST /api/hardware/mock_roomba/start, and
                # whoever set the flag is not necessarily whoever reads this.
                "simulated": True,
                "note": SIMULATED_NOTE,
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
                "simulated": True,
                "note": SIMULATED_NOTE,
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
            "simulated": True,
            "note": SIMULATED_NOTE,
        }

    async def _record_event(self, kind: str, ts: float, entity: str) -> None:
        memory = self._memory
        if memory is None:
            return
        try:
            episode_save = getattr(memory, "episode_save", None)
            if episode_save is None:
                return
            # MemoryStore.episode_save(session_id, event_type, summary,
            # detail="", ...). The structured context is JSON-encoded into
            # ``detail`` so it is embedded for semantic recall — passing it
            # as a ``metadata`` kwarg (the old bug) raised TypeError and
            # silently dropped every actuator episode.
            await episode_save(
                session_id="mock-roomba",
                event_type="actuator",
                # "simulated" in the summary, not only in the JSON detail:
                # recall and the timeline render the summary, so a demo
                # event was indistinguishable from a real one in the one
                # place a user actually reads.
                summary=f"simulated vacuum {kind} for {entity} (mock_roomba)",
                detail=json.dumps(
                    {
                        "category": "actuator",
                        "actuator": "vacuum",
                        "entity_id": entity,
                        "action": kind,
                        "source": "mock_roomba",
                        "ts": ts,
                    },
                    default=str,
                ),
                location="home",
                importance=0.5,
            )
        except Exception as exc:
            # Warning, not debug: a dropped actuator episode is device
            # history that recall will never surface again, and debug is
            # off in every normal deployment.
            logger.warning("mock_roomba episode_save failed: %s", exc)


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
        logger.warning("mock_roomba register_with_mesh failed: %s", exc)


__all__ = ["MockRoomba", "register_with_mesh", "is_enabled", "DEFAULT_ENTITY_ID", "SIMULATED_NOTE"]
