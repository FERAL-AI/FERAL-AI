"""HUP v1.3.0 new envelope tests — ``glasses_frame`` (§5.4.3) and
``device_announce`` (§5.4.4).

Both envelopes are introduced by Lane 11 to close THESIS_SCENARIOS S3
(multi-device peripheral memory) and S5 (vision + voice + actuator).
Tests assert:

- Pydantic models accept the canonical payloads documented in HUP_SPEC.md.
- The brain's ``_handle_glasses_frame`` writes into
  ``state.glasses_buffer`` and rejects oversize frames.
- The brain's ``_handle_device_announce`` routes through
  ``state.hardware_mesh.ingest_device_announce`` and is robust to
  missing ``scanner_node_id``.
- ``parse_message`` registers both types so the daemon WS dispatcher
  never falls through to "Unknown message type".

These tests stay independent of the WS test fixture in
``test_hup_protocol.py`` so they can run as a focused subset under
R-PROD-003 (≤ 200 tests / pytest invocation).
"""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.protocol import (
    DeviceAnnouncePayload,
    GlassesFramePayload,
    HUP_VERSION,
    MESSAGE_TYPES,
    parse_message,
)


def _b64(n: int) -> str:
    return base64.b64encode(b"\x00" * n).decode("ascii")


# ─────────────────────────────────────────────
# GlassesFramePayload
# ─────────────────────────────────────────────


def test_glasses_frame_minimum_required_fields():
    p = GlassesFramePayload(device_id="w610-D344", data_b64=_b64(1024))
    assert p.device_id == "w610-D344"
    assert p.encoding == "jpeg"
    assert p.source == "glasses"
    assert p.width is None and p.height is None and p.sequence is None
    # ``timestamp`` is populated via ``default_factory`` (unix epoch).
    assert p.timestamp > 0


def test_glasses_frame_accepts_all_documented_sources():
    # Source enum is intentionally open — the spec lists the canonical
    # values, the wire accepts any string. Verifies that all
    # documented values round-trip without coercion.
    for src in [
        "glasses", "phone_camera", "screen_loop",
        "w610", "camera_fallback", "jw_w300", "browser_camera",
    ]:
        p = GlassesFramePayload(device_id="d", data_b64=_b64(16), source=src)
        assert p.source == src


def test_glasses_frame_rejects_bad_encoding():
    with pytest.raises(Exception):
        GlassesFramePayload(
            device_id="d", data_b64=_b64(16), encoding="bmp"
        )


def test_glasses_frame_registered_in_message_types():
    assert MESSAGE_TYPES["glasses_frame"] is GlassesFramePayload


def test_parse_message_decodes_glasses_frame():
    raw = {
        "hup_version": HUP_VERSION,
        "type": "glasses_frame",
        "ts": 1716393600.0,
        "payload": {
            "device_id": "feral-iphone-abc",
            "data_b64": _b64(128),
            "timestamp": 1716393600.1,
            "encoding": "jpeg",
            "source": "camera_fallback",
            "width": 1280,
            "height": 720,
            "sequence": 7,
        },
    }
    msg, payload = parse_message(raw)
    assert msg.type == "glasses_frame"
    assert isinstance(payload, GlassesFramePayload)
    assert payload.device_id == "feral-iphone-abc"
    assert payload.source == "camera_fallback"


# ─────────────────────────────────────────────
# _handle_glasses_frame (brain WS handler)
# ─────────────────────────────────────────────


def test_handle_glasses_frame_writes_to_buffer(monkeypatch):
    """Happy path: frame within cap → buffer.ingest called once."""
    from api import server as srv

    buf = MagicMock()
    fake_state = MagicMock()
    fake_state.glasses_buffer = buf
    fake_state.orchestrator = None
    monkeypatch.setattr(srv, "state", fake_state)

    srv._handle_glasses_frame(
        "feral-iphone-1",
        {"device_id": "w610-D344", "data_b64": _b64(2048)},
    )
    buf.ingest.assert_called_once()
    args, kwargs = buf.ingest.call_args
    assert kwargs["node_id"] == "feral-iphone-1"
    assert args[0]["device_id"] == "w610-D344"


def test_handle_glasses_frame_rejects_oversize(monkeypatch, caplog):
    """Decoded-size cap is the same 512 KiB as video_frame."""
    from api import server as srv

    buf = MagicMock()
    fake_state = MagicMock()
    fake_state.glasses_buffer = buf
    monkeypatch.setattr(srv, "state", fake_state)

    too_big = "A" * (srv.VIDEO_FRAME_MAX_BYTES + 16)
    with caplog.at_level("WARNING"):
        srv._handle_glasses_frame(
            "n1", {"device_id": "d", "data_b64": too_big}
        )
    buf.ingest.assert_not_called()
    assert any("Rejecting oversized glasses_frame" in r.message for r in caplog.records)


def test_handle_glasses_frame_tolerates_missing_buffer(monkeypatch):
    """If state.glasses_buffer isn't wired yet (early boot), drop cleanly."""
    from api import server as srv

    fake_state = MagicMock()
    fake_state.glasses_buffer = None
    monkeypatch.setattr(srv, "state", fake_state)

    # No exception even with a valid frame.
    srv._handle_glasses_frame("n1", {"device_id": "d", "data_b64": _b64(64)})


# ─────────────────────────────────────────────
# DeviceAnnouncePayload + _handle_device_announce
# ─────────────────────────────────────────────


def test_device_announce_minimum_required_fields():
    p = DeviceAnnouncePayload(device_id="AA:BB:CC:DD:EE:FF")
    assert p.device_id == "AA:BB:CC:DD:EE:FF"
    assert p.device_kind == "unknown"
    assert p.advertised_services == []
    assert p.metadata == {}


def test_device_announce_full_payload():
    p = DeviceAnnouncePayload(
        scanner_node_id="feral-iphone-abc",
        device_id="AA:BB:CC:DD:EE:FF",
        device_kind="bluetooth_le",
        name="AirPods Pro",
        manufacturer="Apple",
        rssi_dbm=-54,
        advertised_services=["180F"],
        first_seen=1716393600.0,
        last_seen=1716393631.5,
        metadata={"tx_power": 4},
    )
    assert p.scanner_node_id == "feral-iphone-abc"
    assert p.rssi_dbm == -54
    assert p.advertised_services == ["180F"]


def test_device_announce_rejects_unknown_kind():
    with pytest.raises(Exception):
        DeviceAnnouncePayload(device_id="x", device_kind="thread_mesh")


def test_parse_message_decodes_device_announce():
    raw = {
        "hup_version": HUP_VERSION,
        "type": "device_announce",
        "ts": 1716393600.0,
        "payload": {
            "device_id": "AA:BB:CC:DD:EE:FF",
            "device_kind": "bluetooth_le",
            "name": "AirPods Pro",
            "manufacturer": "Apple",
            "rssi_dbm": -54,
        },
    }
    msg, payload = parse_message(raw)
    assert msg.type == "device_announce"
    assert isinstance(payload, DeviceAnnouncePayload)
    assert payload.name == "AirPods Pro"


def test_handle_device_announce_routes_through_mesh(monkeypatch):
    from api import server as srv

    ingest = AsyncMock()
    mesh = MagicMock(ingest_device_announce=ingest)
    fake_state = MagicMock(hardware_mesh=mesh)
    monkeypatch.setattr(srv, "state", fake_state)

    asyncio.run(srv._handle_device_announce("feral-iphone-1", {
        "device_id": "AA:BB:CC:DD:EE:FF",
        "device_kind": "bluetooth_le",
        "name": "AirPods Pro",
    }))
    ingest.assert_awaited_once()
    args, _ = ingest.await_args
    payload = args[0]
    # Brain back-fills scanner_node_id from the WS-level node id when
    # the daemon omits it.
    assert payload["scanner_node_id"] == "feral-iphone-1"
    assert payload["device_id"] == "AA:BB:CC:DD:EE:FF"


def test_handle_device_announce_preserves_explicit_scanner_id(monkeypatch):
    from api import server as srv

    ingest = AsyncMock()
    mesh = MagicMock(ingest_device_announce=ingest)
    fake_state = MagicMock(hardware_mesh=mesh)
    monkeypatch.setattr(srv, "state", fake_state)

    asyncio.run(srv._handle_device_announce("ws-level-node", {
        "scanner_node_id": "explicit-scanner",
        "device_id": "x",
    }))
    payload = ingest.await_args.args[0]
    assert payload["scanner_node_id"] == "explicit-scanner"


def test_handle_device_announce_tolerates_missing_mesh(monkeypatch):
    """If hardware_mesh isn't ready, drop the frame cleanly."""
    from api import server as srv

    fake_state = MagicMock(hardware_mesh=None)
    monkeypatch.setattr(srv, "state", fake_state)

    # No exception.
    asyncio.run(srv._handle_device_announce("n1", {"device_id": "d"}))
