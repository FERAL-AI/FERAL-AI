"""Location: opt-in, in RAM only, and honest about its age.

The gps device_event already reached PerceptionFrame.location, but it
kept no timestamp, so nothing could tell a fresh fix from an hour-old
one and "where am I" answered with equal confidence either way. The
phone sends a fix on connect and when it changes, and location is off by
default, so absence is the normal resting state and never an error.
"""
from __future__ import annotations

import time

import pytest

import api.server as srv
from perception.fusion import PerceptionEngine, PerceptionFrame

NODE = "feral-iphone-loc"


@pytest.fixture()
def captured(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setitem(srv.state._daemon_session_bindings, NODE, {"loc-session"})
    monkeypatch.setattr(srv.state.perception, "update_sensors",
                        lambda session_id, sensors: seen.append(dict(sensors)))
    return seen


def _send(payload):
    srv._handle_biometric_device_event(NODE, "gps", dict(payload))


def test_a_fix_keeps_its_own_timestamp_and_accuracy(captured):
    fixed_at = time.time() - 120
    _send({"lat": 25.0772, "lon": 55.1385, "accuracy_m": 12, "ts": fixed_at})
    gps = {k: v for d in captured for k, v in d.items()}["gps"]
    assert (gps["lat"], gps["lon"]) == (25.0772, 55.1385)
    assert gps["accuracy_m"] == 12
    assert gps["ts"] == pytest.approx(fixed_at, abs=1)


def test_alternate_field_names_are_accepted(captured):
    _send({"latitude": 25.2, "longitude": 55.3, "accuracy": 30})
    gps = {k: v for d in captured for k, v in d.items()}["gps"]
    assert (gps["lat"], gps["lon"]) == (25.2, 55.3)
    assert gps["accuracy_m"] == 30


def test_a_fix_with_no_timestamp_still_gets_one(captured):
    _send({"lat": 25.2, "lon": 55.3})
    gps = {k: v for d in captured for k, v in d.items()}["gps"]
    assert gps["ts"] > 0


class TestContextLine:
    def _ctx(self, **loc):
        frame = PerceptionFrame()
        frame.location = loc
        return frame.to_system_context()

    def test_reports_accuracy_and_age(self):
        ctx = self._ctx(lat=25.0772, lon=55.1385, accuracy_m=12, ts=time.time() - 180)
        assert "lat=25.0772" in ctx and "±12m" in ctx
        assert "fixed 3 min ago" in ctx
        assert "stale" not in ctx

    def test_an_old_fix_is_marked_stale(self):
        ctx = self._ctx(lat=25.0772, lon=55.1385, ts=time.time() - 7200)
        assert "stale" in ctx
        assert "2 h ago" in ctx

    def test_no_location_says_nothing_at_all(self):
        """Absence is the normal state: opt-in, off by default."""
        ctx = PerceptionFrame().to_system_context()
        assert "Location" not in ctx


class TestRetention:
    def test_last_known_location_is_the_newest_fix(self):
        engine = PerceptionEngine()
        now = time.time()
        engine.get_frame("a").location = {"lat": 1.0, "lon": 1.0, "ts": now - 600}
        engine.get_frame("b").location = {"lat": 2.0, "lon": 2.0, "ts": now}
        assert engine.last_known_location()["lat"] == 2.0

    def test_no_fix_anywhere_is_none_not_an_error(self):
        engine = PerceptionEngine()
        engine.get_frame("a")
        assert engine.last_known_location() is None

    def test_clearing_forgets_it(self):
        engine = PerceptionEngine()
        engine.get_frame("a").location = {"lat": 1.0, "lon": 1.0, "ts": time.time()}
        assert engine.clear_location("a") == 1
        assert engine.last_known_location() is None

    def test_clearing_one_session_leaves_the_others(self):
        engine = PerceptionEngine()
        now = time.time()
        engine.get_frame("a").location = {"lat": 1.0, "lon": 1.0, "ts": now}
        engine.get_frame("b").location = {"lat": 2.0, "lon": 2.0, "ts": now}
        engine.clear_location("a")
        assert engine.get_frame("a").location is None
        assert engine.get_frame("b").location is not None
