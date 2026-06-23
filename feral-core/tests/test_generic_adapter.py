"""Tests for the generic self-describing transport adapter + HUP wire format.

These prove the *zero per-device code* path end-to-end: a companion device
that only exposes ``capabilities()`` / ``execute()`` / ``status()`` becomes a
fully controllable HUP device — manifest, LLM tools, and dispatch — with no
device-specific FERAL code.
"""

from __future__ import annotations

import pytest

from hardware.adapters.generic import GenericSelfDescribingAdapter
from hardware.capability_skill import device_manifest_to_skill_manifest
from hardware.protocol import (
    HUPAction,
    HUPActionType,
    device_capability_from_action,
    device_manifest_from_capabilities,
)


class _FakeDevice:
    def __init__(self, *, online: bool = True):
        self._online = online
        self.calls: list[tuple[str, dict]] = []

    def capabilities(self):
        return {
            "device_type": "wristband",
            "transport": {"kind": "ble"},
            "sensors": ["heart_rate"],
            "actions": [
                {"name": "read_hr", "category": "sensor",
                 "permission_tier": "passive", "description": "Heart rate."},
                {"name": "buzz", "category": "actuator",
                 "permission_tier": "active", "description": "Haptic buzz.",
                 "params": [{"name": "ms", "type": "integer", "required": True}]},
            ],
        }

    def execute(self, command, **params):
        self.calls.append((command, params))
        if command not in {"read_hr", "buzz"}:
            return {"ok": False, "error": f"unknown command: {command}"}
        return {"ok": True, "command": command, **params}

    def status(self):
        return {"online": self._online, "heart_rate": 71}


class TestWireFormatConverters:
    def test_action_descriptor_maps_to_capability(self):
        cap = device_capability_from_action(
            {"name": "buzz", "category": "actuator", "permission_tier": "active",
             "params": [{"name": "ms", "type": "integer"}],
             "verify": {"via": "read_hr", "field": "x", "expect": [1]}}
        )
        assert cap is not None
        assert cap.id == "buzz"
        assert cap.category == "actuator"
        assert cap.parameters[0]["name"] == "ms"
        assert cap.verify["via"] == "read_hr"

    def test_action_without_id_is_skipped(self):
        assert device_capability_from_action({"category": "actuator"}) is None

    def test_params_and_parameters_both_accepted(self):
        wire = device_capability_from_action({"name": "a", "params": [{"name": "p"}]})
        model = device_capability_from_action({"id": "a", "parameters": [{"name": "p"}]})
        assert wire.parameters == model.parameters == [{"name": "p"}]

    def test_manifest_from_capabilities_is_generic(self):
        caps = _FakeDevice().capabilities()
        m = device_manifest_from_capabilities(
            "band-0", caps, name="Band", type_aliases={"wristband": "wearable"}
        )
        assert m.device_type == "wearable"
        assert m.connection_type == "ble"
        assert {c.id for c in m.capabilities} == {"read_hr", "buzz"}


class TestGenericAdapter:
    def test_manifest_self_described(self):
        a = GenericSelfDescribingAdapter("band-0", device=_FakeDevice(),
                                         identity={"name": "Veepoo Band"})
        m = a.manifest
        assert m.name == "Veepoo Band"
        assert {c.id for c in m.capabilities} == {"read_hr", "buzz"}
        # And it flows into LLM tools with no per-device code.
        skill = device_manifest_to_skill_manifest(m)
        assert {e.id for e in skill.endpoints} == {"read_hr", "buzz"}

    @pytest.mark.asyncio
    async def test_execute_passthrough(self):
        dev = _FakeDevice()
        a = GenericSelfDescribingAdapter("band-0", device=dev)
        res = await a.execute(HUPAction(
            device_id="band-0", capability_id="buzz",
            action_type=HUPActionType.EXECUTE, parameters={"ms": 200},
        ))
        assert res.status == "success"
        assert dev.calls[-1] == ("buzz", {"ms": 200})

    @pytest.mark.asyncio
    async def test_execute_reports_device_failure_honestly(self):
        a = GenericSelfDescribingAdapter("band-0", device=_FakeDevice())
        res = await a.execute(HUPAction(
            device_id="band-0", capability_id="teleport",
            action_type=HUPActionType.EXECUTE,
        ))
        assert res.status == "failure"
        assert "unknown command" in res.error

    @pytest.mark.asyncio
    async def test_not_connected_fails(self):
        a = GenericSelfDescribingAdapter("band-0")
        res = await a.execute(HUPAction(
            device_id="band-0", capability_id="buzz",
            action_type=HUPActionType.EXECUTE,
        ))
        assert res.status == "failure"
        assert "not connected" in res.error

    @pytest.mark.asyncio
    async def test_connect_via_factory(self):
        a = GenericSelfDescribingAdapter("band-0", device_factory=_FakeDevice)
        assert await a.connect() is True
        state = await a.get_state()
        assert state["online"] is True

    @pytest.mark.asyncio
    async def test_get_state_offline_without_device(self):
        a = GenericSelfDescribingAdapter("band-0")
        assert (await a.get_state())["online"] is False
