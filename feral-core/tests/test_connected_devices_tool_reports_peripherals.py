"""`self_introspection.connected_devices` is the tool an LLM calls when
the user asks "what is connected". It read `state.device_registry` only.

`device_registry` holds HUP nodes -- the iPhone. It does not hold what
is paired behind the iPhone. The BLE peripherals (7 rows on the audited
install: W300 glasses across five nodes, a VITRO wristband) live in
`state.node_subdevices`, which `/api/devices/connected` merges into
every row via `_subdevices_for()` and this skill did not.

So the dashboard and the tool answered the same question differently,
and the tool -- the one the model actually reads -- was the one missing
the hardware. `attrs` also carries the only human-readable name the
device has ("W300"), so without it the answer cannot name the thing the
user is wearing.
"""

from __future__ import annotations

import time

import pytest

from skills.impl.self_introspection import SelfIntrospectionSkill


class _Store:
    def __init__(self, rows):
        self._rows = rows

    def list_for_node(self, node_id):
        return [r for r in self._rows if r["node_id"] == node_id]

    def list_all(self):
        return list(self._rows)


class _Registry:
    def list_devices(self):
        return [{
            "device_id": "feral-iphone-6053b3cdc4ed",
            "device_type": "phone",
            "name": "iPhone",
            "capabilities": ["camera", "mic"],
            "last_seen": time.time(),
        }]


class _State:
    def __init__(self, store):
        self.device_registry = _Registry()
        self.node_subdevices = store


def _row(**kw):
    base = {
        "node_id": "feral-iphone-6053b3cdc4ed",
        "capability": "jw_health_glasses",
        "status": "ready",
        "attrs": {"device_name": "W300", "battery_pct": 69},
        "provenance": "ble",
        "first_seen": time.time() - 100,
        "last_seen": time.time(),
        "live": True,
        "liveness_window_s": 30.0,
    }
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_peripherals_appear_under_their_node(monkeypatch):
    st = _State(_Store([_row()]))
    monkeypatch.setattr(SelfIntrospectionSkill, "_state", staticmethod(lambda: st))

    result = await SelfIntrospectionSkill().execute("connected_devices", {}, {})

    assert result["success"] is True
    device = result["data"]["devices"][0]
    assert "subdevices" in device, "node row carries no sub-device tree"
    assert device["subdevices"][0]["capability"] == "jw_health_glasses"


@pytest.mark.asyncio
async def test_peripheral_name_and_liveness_are_reported(monkeypatch):
    """Without the name the model cannot say WHICH device; without
    `live` it cannot distinguish paired from actually reporting."""
    st = _State(_Store([_row()]))
    monkeypatch.setattr(SelfIntrospectionSkill, "_state", staticmethod(lambda: st))

    result = await SelfIntrospectionSkill().execute("connected_devices", {}, {})
    sub = result["data"]["devices"][0]["subdevices"][0]

    assert sub["name"] == "W300"
    assert sub["live"] is True


@pytest.mark.asyncio
async def test_stale_peripheral_is_listed_and_marked_not_live(monkeypatch):
    st = _State(_Store([_row(live=False, last_seen=time.time() - 3600)]))
    monkeypatch.setattr(SelfIntrospectionSkill, "_state", staticmethod(lambda: st))

    result = await SelfIntrospectionSkill().execute("connected_devices", {}, {})
    subs = result["data"]["devices"][0]["subdevices"]

    assert len(subs) == 1, "a stale peripheral must not be dropped"
    assert subs[0]["live"] is False


@pytest.mark.asyncio
async def test_no_subdevice_store_still_answers(monkeypatch):
    """Boot ordering must not turn this into an error."""
    st = _State(None)
    monkeypatch.setattr(SelfIntrospectionSkill, "_state", staticmethod(lambda: st))

    result = await SelfIntrospectionSkill().execute("connected_devices", {}, {})

    assert result["success"] is True
    assert result["data"]["devices"][0]["subdevices"] == []


@pytest.mark.asyncio
async def test_registry_failure_is_logged_not_silently_empty(monkeypatch, caplog):
    """The pre-fix code was `except Exception: return []`.

    An empty device list is a valid answer meaning "nothing attached",
    so returning it on a crash makes a broken registry indistinguishable
    from a bare machine, and the model confidently says "you have no
    devices connected".
    """

    class _Boom:
        def list_devices(self):
            raise RuntimeError("registry exploded")

    st = _State(_Store([]))
    st.device_registry = _Boom()
    monkeypatch.setattr(SelfIntrospectionSkill, "_state", staticmethod(lambda: st))

    with caplog.at_level("WARNING"):
        result = await SelfIntrospectionSkill().execute("connected_devices", {}, {})

    assert any("registry exploded" in r.getMessage() or "device" in r.getMessage().lower()
               for r in caplog.records), "failure was swallowed with no log"
    assert result["data"].get("error"), "caller cannot tell empty from broken"
