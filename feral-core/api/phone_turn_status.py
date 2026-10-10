"""Read-only paired phone receipt reconciliation. Never admits or replays a turn."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from agents.chat_turns import ChatTurnManager, get_chat_turn_manager
from api.phone_chat_intake import PhoneChatIntake
from memory.store import MemoryStore
from security.approval_ingress import PairedDevicePrincipal
from security.device_pairing import DevicePairingStore

PHONE_TURN_CONTRACT = {"contract_version": 1, "durable_receipts": True,
    "reconnect_reconciliation": True, "readonly_status": True}


@dataclass(frozen=True)
class PhoneTurnReader:
    intake: PhoneChatIntake = field(repr=False)
    principal: PairedDevicePrincipal = field(repr=False)
    manager: ChatTurnManager = field(repr=False)
    memory: MemoryStore = field(repr=False)
    pairing_store: DevicePairingStore = field(repr=False)
    orchestrator: object = field(repr=False)
    generation: object = field(repr=False)

    @classmethod
    async def ready(cls, intake, principal):
        """Return capability only after opening actual durable receipt storage."""
        state = intake.state
        if (type(intake) is not PhoneChatIntake or type(principal) is not PairedDevicePrincipal
                or type(state.memory) is not MemoryStore
                or type(state.device_pairing_store) is not DevicePairingStore
                or not isinstance(state.memory.db_path, str) or state.memory.db_path in {"", ":memory:"}
                or getattr(intake.ws, "_feral_node_type", None) not in {"phone", "ios", "android"}):
            return None
        principal.require_current()
        manager = get_chat_turn_manager(state)
        if type(manager) is not ChatTurnManager:
            return None
        reader = cls(intake, principal, manager, state.memory, state.device_pairing_store,
            state.orchestrator, getattr(state, "_native_agent_turn_generation", None))
        reader.guard(require_store=False)
        try:
            await manager.start()
        except Exception:
            reader.guard(require_store=False)
            return None
        reader.guard()
        return reader

    def guard(self, *, require_store=True):
        state = self.intake.state
        self.intake.guard()
        self.principal.require_current()
        if (state.chat_turns is not self.manager or self.manager.state is not state
                or state.memory is not self.memory or state.device_pairing_store is not self.pairing_store
                or state.orchestrator is not self.orchestrator
                or getattr(state, "_native_agent_turn_generation", None) != self.generation
                or require_store and self.manager._store is not self.memory):
            raise asyncio.CancelledError("Phone receipt runtime changed")

    async def status(self, payload):
        self.guard()
        response = {"contract_version": 1, "request_id": payload.request_id,
            "session_id": payload.session_id, "status": "unavailable"}
        try:
            receipt = await self.manager.status(session_id=payload.session_id,
                request_id=payload.request_id, source_principal=self.principal)
        except Exception:
            self.guard()
            return response
        self.guard()
        response["status"] = "found" if receipt is not None else "not_found"
        if receipt is not None:
            response["receipt"] = receipt
        return response
