"""Workstream C — CuteBot robot state in the perception feedback loop.

Pins:
  * ``update_sensors(session_id, {"robot": {...}})`` populates PerceptionFrame
  * ``to_system_context()`` emits a compact robot line when online + fresh
  * stale / offline robot state is omitted from LLM context
  * ``gave_up`` surfaces explicit repositioning hint
  * HUP ``device_event`` types ``robot_telemetry`` / ``robot_event`` route
    into the same ``robot`` sensor contract (mirrors api/server.py dispatch)
"""

from __future__ import annotations

import time

import pytest

import api.server as srv
from perception.fusion import (
    PerceptionEngine,
    PerceptionFrame,
    _ROBOT_CONTEXT_FRESH_S,
)


# ---------------------------------------------------------------------------
# Helpers — mirror the robot branch in api/server.py device_event dispatch
# ---------------------------------------------------------------------------


def _dispatch_robot_device_event(monkeypatch, node_id: str, payload: dict) -> dict:
    """Execute the same normalization the WS handler's robot branch performs."""
    captured: dict = {}

    class _Perc:
        def update_sensors(self, sid, sensors):  # noqa: ARG002
            captured.update(sensors)

    monkeypatch.setattr(srv.state, "perception", _Perc(), raising=False)
    monkeypatch.setattr(
        srv.state,
        "get_sessions_for_daemon",
        lambda n: ["s1"] if n == node_id else [],
        raising=False,
    )

    de_payload = srv._unwrap_hup_frame(payload)
    robot_sensors = {
        "mode": str(de_payload.get("mode") or ""),
        "state": str(de_payload.get("state") or ""),
        "sonar_cm": float(de_payload.get("sonar_cm") or 0.0),
        "online": True,
        "battery": bool(de_payload.get("battery", False)),
    }
    for sid in srv.state.get_sessions_for_daemon(node_id):
        srv.state.perception.update_sensors(sid, {"robot": robot_sensors})
    return captured


def _robot_sensors(**overrides) -> dict:
    base = {
        "mode": "line_follow",
        "state": "ok",
        "sonar_cm": 14.0,
        "online": True,
        "battery": True,
    }
    base.update(overrides)
    return {"robot": base}


# ---------------------------------------------------------------------------
# update_sensors → PerceptionFrame
# ---------------------------------------------------------------------------


def test_update_sensors_robot_populates_frame():
    perc = PerceptionEngine()
    sid = "session-robot"

    perc.update_sensors(sid, _robot_sensors())

    frame = perc.get_frame(sid)
    assert frame.robot_online is True
    assert frame.robot_mode == "line_follow"
    assert frame.robot_state == "ok"
    assert frame.robot_sonar_cm == 14.0
    assert frame.robot_battery is True
    assert frame.robot_ts > 0


def test_update_sensors_robot_does_not_disturb_hr():
    """Robot branch must be additive — wearable priority logic unchanged."""
    perc = PerceptionEngine()
    sid = "session-mixed"
    now = time.time()

    perc.update_sensors(sid, {
        "ppg_heart_rate": 72,
        "heart_rate_source": "veepoo_wristband",
        "heart_rate_sample_ts": now,
    })
    perc.update_sensors(sid, _robot_sensors(mode="explore", sonar_cm=22.0))

    frame = perc.get_frame(sid)
    assert frame.heart_rate == 72
    assert frame.heart_rate_source == "veepoo_wristband"
    assert frame.robot_mode == "explore"
    assert frame.robot_sonar_cm == 22.0


# ---------------------------------------------------------------------------
# to_system_context — freshness + phrasing
# ---------------------------------------------------------------------------


def test_to_system_context_includes_fresh_online_robot():
    frame = PerceptionFrame(
        robot_online=True,
        robot_mode="line_follow",
        robot_state="ok",
        robot_sonar_cm=14.0,
        robot_battery=True,
        robot_ts=time.time(),
    )
    ctx = frame.to_system_context()
    assert "Robot (CuteBot): mode=line_follow state=ok sonar=14cm battery=ok" in ctx


def test_to_system_context_omits_stale_robot():
    frame = PerceptionFrame(
        robot_online=True,
        robot_mode="line_follow",
        robot_state="ok",
        robot_sonar_cm=14.0,
        robot_battery=True,
        robot_ts=time.time() - _ROBOT_CONTEXT_FRESH_S - 1.0,
    )
    ctx = frame.to_system_context()
    assert "Robot (CuteBot)" not in ctx


def test_to_system_context_omits_offline_robot():
    frame = PerceptionFrame(
        robot_online=False,
        robot_mode="idle",
        robot_state="ok",
        robot_sonar_cm=14.0,
        robot_battery=True,
        robot_ts=time.time(),
    )
    ctx = frame.to_system_context()
    assert "Robot (CuteBot)" not in ctx


def test_to_system_context_gave_up_repositioning_hint():
    frame = PerceptionFrame(
        robot_online=True,
        robot_mode="line_follow",
        robot_state="gave_up",
        robot_sonar_cm=8.0,
        robot_battery=False,
        robot_ts=time.time(),
    )
    ctx = frame.to_system_context()
    assert "state=gave_up (needs repositioning on track)" in ctx
    assert "battery=low" in ctx


def test_perception_engine_end_to_end_context_line():
    perc = PerceptionEngine()
    sid = "session-e2e"
    perc.update_sensors(sid, _robot_sensors(state="ok", sonar_cm=14.0))

    ctx = perc.get_frame(sid).to_system_context()
    assert "Robot (CuteBot): mode=line_follow state=ok sonar=14cm battery=ok" in ctx


# ---------------------------------------------------------------------------
# HUP device_event → perception (robot_telemetry / robot_event)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event_type", ["robot_telemetry", "robot_event"])
def test_robot_device_event_reaches_perception(monkeypatch, event_type: str):
    payload = {
        "event_type": event_type,
        "device_id": "cutebot-usb-0",
        "mode": "explore",
        "state": "obstacle",
        "sonar_cm": 6.5,
        "battery": True,
    }
    captured = _dispatch_robot_device_event(monkeypatch, "cutebot-bridge-0", payload)

    robot = captured.get("robot", {})
    assert robot["mode"] == "explore"
    assert robot["state"] == "obstacle"
    assert robot["sonar_cm"] == 6.5
    assert robot["online"] is True
    assert robot["battery"] is True


def test_robot_telemetry_through_perception_engine(monkeypatch):
    """Full path: dispatch normalization → update_sensors → context line."""
    payload = {
        "event_type": "robot_telemetry",
        "device_id": "cutebot-usb-0",
        "mode": "line_follow",
        "state": "ok",
        "sonar_cm": 14.0,
        "battery": True,
    }

    perc = PerceptionEngine()
    monkeypatch.setattr(srv.state, "perception", perc, raising=False)
    monkeypatch.setattr(
        srv.state,
        "get_sessions_for_daemon",
        lambda n: ["s1"],
        raising=False,
    )

    de_payload = srv._unwrap_hup_frame(payload)
    robot_sensors = {
        "mode": str(de_payload.get("mode") or ""),
        "state": str(de_payload.get("state") or ""),
        "sonar_cm": float(de_payload.get("sonar_cm") or 0.0),
        "online": True,
        "battery": bool(de_payload.get("battery", False)),
    }
    for sid in srv.state.get_sessions_for_daemon("cutebot-bridge-0"):
        srv.state.perception.update_sensors(sid, {"robot": robot_sensors})

    ctx = perc.get_frame("s1").to_system_context()
    assert "Robot (CuteBot): mode=line_follow state=ok sonar=14cm battery=ok" in ctx


def test_robot_device_event_nested_data_shape(monkeypatch):
    """SDK nested ``data`` envelope must unwrap like other device_events."""
    payload = {
        "event_type": "robot_telemetry",
        "data": {
            "mode": "idle",
            "state": "ok",
            "sonar_cm": 20.0,
            "battery": False,
        },
    }
    captured = _dispatch_robot_device_event(monkeypatch, "node-robot", payload)
    robot = captured["robot"]
    assert robot["mode"] == "idle"
    assert robot["battery"] is False
