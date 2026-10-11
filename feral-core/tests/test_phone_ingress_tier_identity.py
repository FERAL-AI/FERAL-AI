"""Sensor frames require node identity before capability-tier lookup."""
import pytest

from api import server


@pytest.mark.parametrize("node_id", [None, ""])
@pytest.mark.parametrize("kind", sorted(server._FRAME_TIER_BY_TYPE))
def test_unregistered_sensor_frame_is_refused_without_lookup(monkeypatch, node_id, kind):
    def unexpected_lookup(*args):
        pytest.fail("Unregistered sensor frame reached capability lookup")
    monkeypatch.setattr(server, "frame_tier_enabled", unexpected_lookup)
    assert server._frame_tier_refused(node_id, kind, {}) == server._FRAME_TIER_BY_TYPE[kind]


@pytest.mark.parametrize("event", sorted(server._FRAME_TIER_BY_EVENT_TYPE))
def test_unregistered_sensor_device_event_is_refused(event):
    assert server._frame_tier_refused(None, "device_event", {
        "payload": {"event_type": event}
    }) == server._FRAME_TIER_BY_EVENT_TYPE[event]


def test_registration_is_not_a_sensor_frame():
    assert server._frame_tier_refused(None, "node_hello", {}) == ""


@pytest.mark.parametrize("enabled", [False, True])
def test_registered_frame_preserves_operator_tier(monkeypatch, enabled):
    calls = []
    def lookup(node_id, tier):
        calls.append((node_id, tier))
        return enabled
    monkeypatch.setattr(server, "frame_tier_enabled", lookup)
    expected = "" if enabled else server._FRAME_TIER_BY_TYPE["camera_frame"]
    assert server._frame_tier_refused("registered-node", "camera_frame", {}) == expected
    assert calls == [("registered-node", server._FRAME_TIER_BY_TYPE["camera_frame"])]
