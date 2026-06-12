"""Rule-based hardware orchestrator — intent → HUP command sequences."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional
from uuid import uuid4

from hardware.command_contract import CommandEnvelope, CommandLedger, CommandState
from hardware.protocol import DeviceRegistry, HUPAction, HUPActionType, HUPResult

logger = logging.getLogger("feral.hardware.orchestrator")

_INTENT_RULES: tuple[tuple[tuple[str, ...], str, dict[str, Any]], ...] = (
    (("follow line", "follow the line", "patrol", "patrol the track"), "follow_line", {}),
    (("explore", "explore the table", "roam"), "explore", {}),
    (("stop", "halt", "stop everything", "emergency stop"), "halt", {}),
    (("back up", "backup", "reverse", "back up a bit"), "drive", {"left": -30, "right": -30}),
    (("drive", "move forward", "go forward"), "drive", {"left": 30, "right": 30}),
)

_POLLABLE_CAPABILITIES = frozenset({"follow_line", "explore", "drive"})


class HardwareOrchestrator:
    """Translate high-level intents into registry actions with polled stop conditions."""

    def __init__(
        self,
        registry: DeviceRegistry,
        ledger: CommandLedger,
        perception: Any = None,
    ):
        self._registry = registry
        self._ledger = ledger
        self._perception = perception

    def _resolve_intent(self, intent: str, params: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        text = (intent or "").strip().lower()
        if not text:
            return None, {}
        for phrases, capability_id, defaults in _INTENT_RULES:
            if any(phrase in text for phrase in phrases):
                merged = {**defaults, **params}
                return capability_id, merged
        return None, {}

    @staticmethod
    def _compare(op: str, left: Any, right: Any) -> bool:
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        return False

    def _event_matches(self, event: dict[str, Any], condition: dict[str, Any]) -> bool:
        name = condition.get("name", "")
        if event.get("event") != name:
            return False
        param = condition.get("param")
        if not param:
            return True
        op = condition.get("op", "==")
        value = condition.get("value")
        return self._compare(op, event.get(param), value)

    def _sensor_matches(self, state: dict[str, Any], condition: dict[str, Any]) -> bool:
        param = condition.get("param")
        if not param:
            return False
        op = condition.get("op", "==")
        value = condition.get("value")
        return self._compare(op, state.get(param), value)

    def _stop_triggered(
        self,
        *,
        state: dict[str, Any],
        events: list[dict[str, Any]],
        stop_conditions: list[dict[str, Any]],
        started_at: float,
    ) -> dict[str, Any] | None:
        now = time.time()
        for condition in stop_conditions:
            ctype = condition.get("type")
            if ctype == "timeout":
                limit = float(condition.get("seconds", 0))
                if limit > 0 and (now - started_at) >= limit:
                    return {"reason": "timeout", "condition": condition}
            elif ctype == "sensor":
                if self._sensor_matches(state, condition):
                    return {"reason": "sensor", "condition": condition, "state": state}
            elif ctype == "event":
                for event in events:
                    if self._event_matches(event, condition):
                        return {"reason": "event", "condition": condition, "event": event}
                if condition.get("name") == "state_changed" and condition.get("param") == "state":
                    op = condition.get("op", "==")
                    value = condition.get("value")
                    if self._compare(op, state.get("state"), value):
                        return {"reason": "state", "condition": condition, "state": state}
        return None

    async def execute_intent(
        self,
        session_id: str,
        device_id: str,
        intent: str,
        *,
        params: dict | None = None,
        stop_conditions: list[dict] | None = None,
        timeout_s: float = 30.0,
    ) -> dict:
        params = dict(params or {})
        stop_conditions = list(stop_conditions or [])
        if not any(c.get("type") == "timeout" for c in stop_conditions):
            stop_conditions.append({"type": "timeout", "seconds": timeout_s})

        capability_id, step_params = self._resolve_intent(intent, params)
        if capability_id is None:
            return {
                "ok": False,
                "session_id": session_id,
                "device_id": device_id,
                "intent": intent,
                "error": f"Unknown intent: {intent}",
            }

        adapter = self._registry.get_adapter(device_id)
        envelope = CommandEnvelope(
            command_id=str(uuid4()),
            correlation_id=session_id,
            node_id=device_id,
            action=capability_id,
            params=step_params,
            deadline=time.time() + timeout_s,
            priority="safety" if capability_id == "halt" else "interactive",
        )
        record = self._ledger.submit(envelope)
        self._ledger.update_state(record.envelope.command_id, CommandState.RUNNING, message="executing")

        action = HUPAction(
            action_id=envelope.command_id,
            device_id=device_id,
            capability_id=capability_id,
            action_type=HUPActionType.EXECUTE,
            parameters=step_params,
            timeout_ms=int(timeout_s * 1000),
            priority=2 if capability_id == "halt" else 0,
        )
        result = await self._registry.execute_action(action)

        stop_reason: dict[str, Any] | None = None
        polled_events: list[dict[str, Any]] = []
        final_state: dict[str, Any] = {}

        if (
            result.status == "success"
            and capability_id in _POLLABLE_CAPABILITIES
            and adapter is not None
        ):
            started_at = time.time()
            while True:
                if hasattr(adapter, "poll_events"):
                    polled_events = await adapter.poll_events(0.5)
                if hasattr(adapter, "get_state"):
                    final_state = await adapter.get_state()
                elif hasattr(adapter, "get_status"):
                    final_state = await adapter.get_status()

                if self._perception is not None and hasattr(adapter, "_telemetry_payload"):
                    payload = adapter._telemetry_payload(final_state)
                    self._perception.update_sensors(session_id, {"robot": payload})

                stop_reason = self._stop_triggered(
                    state=final_state,
                    events=polled_events,
                    stop_conditions=stop_conditions,
                    started_at=started_at,
                )
                if stop_reason is not None:
                    break

                await asyncio.sleep(0.05)
        elif adapter is not None and hasattr(adapter, "get_state"):
            final_state = await adapter.get_state()

        terminal = CommandState.SUCCEEDED if result.status == "success" else CommandState.FAILED
        if stop_reason and stop_reason.get("reason") == "timeout" and result.status == "success":
            terminal = CommandState.TIMED_OUT
        self._ledger.update_state(
            record.envelope.command_id,
            terminal,
            message=stop_reason.get("reason", "") if stop_reason else result.status,
            result={
                "capability_id": capability_id,
                "intent": intent,
                "stop_reason": stop_reason,
                "state": final_state,
                "hup_result": result.model_dump(),
            },
        )

        return {
            "ok": result.status == "success",
            "session_id": session_id,
            "device_id": device_id,
            "intent": intent,
            "capability_id": capability_id,
            "command_id": record.envelope.command_id,
            "result": result.model_dump() if isinstance(result, HUPResult) else result,
            "stop_reason": stop_reason,
            "state": final_state,
        }
