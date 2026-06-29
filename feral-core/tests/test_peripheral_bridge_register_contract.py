"""Contract tests for PeripheralBridgeRegisterPayload normalization.

The companion iOS app (and older installs) emit raw transport/kind
spellings — ``protocol="ble"`` and ``kind="wristband"`` — which used to
fail Pydantic validation in ``parse_message`` and leave the app stuck in
a "reconnecting…" loop because the brain rejected the whole registration
envelope. The model now accepts those legacy aliases and maps them to the
canonical literals while leaving canonical values untouched.
"""

import pytest

from models.protocol import (
    PeripheralBridgeDevicePayload,
    PeripheralBridgeRegisterPayload,
    parse_message,
)


def test_legacy_ble_protocol_normalizes_to_native_bridge():
    device = PeripheralBridgeDevicePayload(
        device_id="band_01",
        kind="band",
        protocol="ble",
    )
    assert device.protocol == "native_bridge"


def test_legacy_wristband_kind_normalizes_to_band():
    device = PeripheralBridgeDevicePayload(
        device_id="band_01",
        kind="wristband",
        protocol="ble",
    )
    assert device.kind == "band"
    assert device.protocol == "native_bridge"


@pytest.mark.parametrize(
    "raw_protocol,expected",
    [
        ("ble", "native_bridge"),
        ("BLE", "native_bridge"),
        (" ble ", "native_bridge"),
        ("bluetooth", "native_bridge"),
        ("web_bluetooth", "web_bluetooth"),
        ("native_bridge", "native_bridge"),
        ("none", "none"),
    ],
)
def test_protocol_aliases(raw_protocol, expected):
    device = PeripheralBridgeDevicePayload(
        device_id="d1", kind="band", protocol=raw_protocol
    )
    assert device.protocol == expected


@pytest.mark.parametrize(
    "raw_kind,expected",
    [
        ("wristband", "band"),
        ("Wristband", "band"),
        ("glasses", "glasses"),
        ("watch", "watch"),
        ("band", "band"),
    ],
)
def test_kind_aliases(raw_kind, expected):
    device = PeripheralBridgeDevicePayload(
        device_id="d1", kind=raw_kind, protocol="ble"
    )
    assert device.kind == expected


def test_full_register_payload_from_legacy_app_validates():
    """Mirrors the exact shape from the device-log ValidationError."""
    raw = {
        "type": "peripheral_bridge_register",
        "payload": {
            "bridge_id": "phone-bridge-1",
            "platform": "ios",
            "devices": [
                {"device_id": "glasses_01", "kind": "glasses", "protocol": "ble"},
                {"device_id": "watch_01", "kind": "watch", "protocol": "ble"},
                {"device_id": "band_01", "kind": "wristband", "protocol": "ble"},
            ],
            "expires_at": "2026-04-30T12:00:00Z",
        },
    }
    msg, payload = parse_message(raw)
    assert isinstance(payload, PeripheralBridgeRegisterPayload)
    assert [d.protocol for d in payload.devices] == [
        "native_bridge",
        "native_bridge",
        "native_bridge",
    ]
    assert [d.kind for d in payload.devices] == ["glasses", "watch", "band"]


def test_unknown_protocol_still_rejected():
    """Normalization must not turn the field into a free-for-all."""
    with pytest.raises(Exception):
        PeripheralBridgeDevicePayload(
            device_id="d1", kind="band", protocol="zigbee"
        )
