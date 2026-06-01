"""Pins that biometric device_events forward source + sample_ts into the
sensors dict, so perception/fusion can light the Context "fresh" dot
(dashboard.py gates HR/SpO2 freshness on heart_rate_sample_ts /
spo2_sample_ts). Before this, _handle_biometric_device_event dropped those
fields and wearable vitals always rendered as stale.
"""
from __future__ import annotations

import time


def _drive(monkeypatch, event_type: str, payload: dict) -> dict:
    from api import server as srv

    captured: dict = {}

    class _Perc:
        def update_sensors(self, sid, sensors):
            captured.update(sensors)

    monkeypatch.setattr(srv.state, "perception", _Perc(), raising=False)
    monkeypatch.setattr(srv.state, "get_sessions_for_daemon", lambda n: ["s1"], raising=False)
    monkeypatch.setattr(srv.state, "somatic_engine", None, raising=False)
    monkeypatch.setattr(srv.state, "baseline_engine", None, raising=False)
    srv._handle_biometric_device_event("node-1", event_type, payload)
    return captured


def test_heart_rate_forwards_source_and_sample_ts(monkeypatch):
    captured = _drive(monkeypatch, "heart_rate", {
        "bpm": 72,
        "heart_rate_source": "veepoo_wristband",
        "heart_rate_sample_ts": 1717260000.0,
    })
    assert captured.get("ppg_heart_rate") == 72
    assert captured.get("heart_rate_source") == "veepoo_wristband"
    assert captured.get("heart_rate_sample_ts") == 1717260000.0


def test_spo2_forwards_source_and_sample_ts(monkeypatch):
    captured = _drive(monkeypatch, "spo2", {
        "current": 97,
        "spo2_source": "jw_health_glasses",
        "spo2_sample_ts": 1717260000.0,
    })
    assert captured.get("spo2_pct") == 97
    assert captured.get("spo2_source") == "jw_health_glasses"
    assert captured.get("spo2_sample_ts") == 1717260000.0


def test_heart_rate_stamps_arrival_time_when_no_ts(monkeypatch):
    before = time.time()
    captured = _drive(monkeypatch, "heart_rate", {"bpm": 80, "source": "veepoo_wristband"})
    ts = captured.get("heart_rate_sample_ts")
    assert ts is not None and ts >= before
    # `source` is accepted as the fallback for the typed *_source key.
    assert captured.get("heart_rate_source") == "veepoo_wristband"
