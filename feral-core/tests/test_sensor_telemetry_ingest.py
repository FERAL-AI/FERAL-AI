"""Regression coverage for the HealthKit / sensor-telemetry alias.

Background
----------

Pre-v2026.5.43 the iOS bridge shipped two overloads of
``FeralBrainClient.sendSensorData``: the enum overload emitted the
canonical key ``"sensor"`` while the string overload (used by
``HealthKitManager`` for heart-rate / spo2 / steps / sleep and by
``FeralLocationManager`` for location) emitted ``"sensor_type"``. The
brain's ``SensorTelemetryPayload`` only declared ``sensor: str``, so
the legacy frames failed ``parse_message`` with a ValidationError and
the operator's HealthKit stream never reached the orchestrator
biometric fusion path (S2 thesis).

The fix has two layers:

1. ``SensorTelemetryPayload.sensor`` declares a Pydantic v2
   ``validation_alias=AliasChoices("sensor", "sensor_type")`` so
   ``parse_message`` accepts either wire shape and normalises to the
   canonical attribute name.

2. The raw-dict ingest branch in ``api/server.py`` (which does not go
   through ``parse_message`` — it reads the envelope dict directly so
   it can log the raw payload) reads
   ``payload_dict.get("sensor") or payload_dict.get("sensor_type", "")``
   for defence in depth.

Both layers exist to unblock legacy iOS deployments until the App
Store update flips the string overload to the canonical key. See
``AUDIT-r14/round3/findings/lane8-daemon-shell-and-healthkit.md``
§B for the migration plan and S2 dependency.
"""
from __future__ import annotations

import pytest

from models.protocol import (
    SensorTelemetryPayload,
    parse_message,
)


# ─────────────────────────────────────────────
# Layer 1: SensorTelemetryPayload + parse_message
# ─────────────────────────────────────────────


def test_payload_accepts_canonical_sensor_key() -> None:
    payload = SensorTelemetryPayload(
        node_id="iphone-test",
        sensor="heart_rate",
        data={"bpm": 72},
        timestamp="2026-05-27T16:00:00Z",
    )
    assert payload.sensor == "heart_rate"
    assert payload.data == {"bpm": 72}


def test_payload_accepts_legacy_sensor_type_alias() -> None:
    """Direct dict construction with the legacy key — the alias must
    coerce ``sensor_type`` → ``sensor`` without complaint."""
    payload = SensorTelemetryPayload.model_validate({
        "node_id": "iphone-old",
        "sensor_type": "heart_rate",
        "data": {"bpm": 72},
        "timestamp": "2026-05-27T16:00:00Z",
    })
    assert payload.sensor == "heart_rate"


def test_payload_rejects_when_both_keys_missing() -> None:
    """Removing the validation alias would also remove this safety
    net, so pin the failure mode explicitly."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SensorTelemetryPayload.model_validate({
            "node_id": "iphone-bad",
            "data": {"bpm": 72},
        })


def test_parse_message_accepts_canonical_sensor_key() -> None:
    raw = {
        "type": "sensor_telemetry",
        "payload": {
            "node_id": "iphone-test",
            "sensor": "heart_rate",
            "data": {"bpm": 72},
            "timestamp": "2026-05-27T16:00:00Z",
        },
    }
    msg, payload = parse_message(raw)
    assert msg.type == "sensor_telemetry"
    assert isinstance(payload, SensorTelemetryPayload)
    assert payload.sensor == "heart_rate"
    assert payload.data == {"bpm": 72}


def test_parse_message_accepts_legacy_sensor_type_alias() -> None:
    """The exact wire shape a pre-v2026.5.43 iOS HealthKit caller sends:
    ``sendSensorData(type: "heart_rate", data: {...})`` serialises as
    ``"sensor_type": "heart_rate"`` in the JSON envelope."""
    raw = {
        "type": "sensor_telemetry",
        "payload": {
            "node_id": "iphone-old",
            "sensor_type": "heart_rate",
            "data": {"bpm": 72},
            "timestamp": "2026-05-27T16:00:00Z",
        },
    }
    msg, payload = parse_message(raw)
    assert isinstance(payload, SensorTelemetryPayload)
    assert payload.sensor == "heart_rate"


@pytest.mark.parametrize(
    "sensor_name",
    ["heart_rate", "spo2", "steps", "sleep", "location", "temperature"],
)
def test_parse_message_legacy_alias_works_for_every_healthkit_sensor(
    sensor_name: str,
) -> None:
    """Pin coverage across the full set of sensor names the legacy
    string overload is known to emit (HealthKitManager + FeralLocationManager)."""
    raw = {
        "type": "sensor_telemetry",
        "payload": {
            "node_id": "iphone-old",
            "sensor_type": sensor_name,
            "data": {"value": 1},
        },
    }
    _msg, payload = parse_message(raw)
    assert isinstance(payload, SensorTelemetryPayload)
    assert payload.sensor == sensor_name


# ─────────────────────────────────────────────
# Layer 2: raw-dict ingest path in api/server.py
# ─────────────────────────────────────────────


def _resolve_sensor_name(payload_dict: dict) -> str:
    """Mirror of the defensive lookup in ``api/server.py`` for the
    ``sensor_telemetry`` branch. Kept in lock-step with that handler
    so any drift surfaces here first."""
    return payload_dict.get("sensor") or payload_dict.get("sensor_type", "")


def test_raw_dict_handler_prefers_canonical_key() -> None:
    payload_dict = {
        "node_id": "iphone-test",
        "sensor": "heart_rate",
        "data": {"bpm": 72},
    }
    assert _resolve_sensor_name(payload_dict) == "heart_rate"


def test_raw_dict_handler_falls_back_to_legacy_key() -> None:
    payload_dict = {
        "node_id": "iphone-old",
        "sensor_type": "heart_rate",
        "data": {"bpm": 72},
    }
    assert _resolve_sensor_name(payload_dict) == "heart_rate"


def test_raw_dict_handler_canonical_wins_when_both_present() -> None:
    """If a transitional client somehow ships both keys (e.g. an
    enum-overload + manual dict merge bug), the canonical key must
    win so the server interprets the same value Pydantic would."""
    payload_dict = {
        "node_id": "iphone-mixed",
        "sensor": "heart_rate",
        "sensor_type": "spo2",  # legacy — should be ignored
        "data": {"bpm": 72},
    }
    assert _resolve_sensor_name(payload_dict) == "heart_rate"


def test_raw_dict_handler_returns_empty_string_when_neither_present() -> None:
    """Existing behaviour — preserve so downstream ``sensors_map = {"":
    ...}`` short-circuits in the orchestrator path stay valid."""
    payload_dict = {"node_id": "iphone-bad", "data": {"bpm": 72}}
    assert _resolve_sensor_name(payload_dict) == ""


def test_server_handler_uses_same_defensive_lookup() -> None:
    """Read ``api/server.py`` and confirm the ``sensor_telemetry``
    branch literally contains the defensive ``.get("sensor") or
    .get("sensor_type", "")`` expression. If a future refactor splits
    this into a helper, update both this assertion and the docstring
    above."""
    from pathlib import Path

    server_src = (
        Path(__file__).resolve().parents[1] / "api" / "server.py"
    ).read_text()
    needle = (
        'payload_dict.get("sensor") or payload_dict.get("sensor_type", "")'
    )
    assert needle in server_src, (
        "api/server.py sensor_telemetry branch no longer reads both "
        "'sensor' and 'sensor_type' from the raw payload dict. The "
        "alias on SensorTelemetryPayload covers parse_message-based "
        "ingest, but the raw-dict path in the daemon WebSocket handler "
        "needs the same tolerance until the iOS App Store update "
        "ships."
    )
