"""AUDIT-FIXES F-02: the brain's wire models must reject hostile payloads.

Before this file, ``models/protocol.py`` declared every field bare:
``device_id: str``, ``width: Optional[int]``, ``sequence: Optional[int]``,
``data_b64: str``. The audit reproduced, against the brain's own model:

    device_id=''  width=-5  height=1000000000  sequence=-42
    decoded payload bytes=900000   (the documented cap is 512 KiB)
    DeviceAnnouncePayload rssi_dbm=-9999

All accepted. Meanwhile the Python node SDK
(``feral-nodes/python-node-sdk/src/feral_node_sdk/schemas.py``) bounds every
one of those fields and its docstring claims to "mirror the brain's
``GlassesFramePayload``". The claim was false in the direction that matters:
every constraint sat in the component an attacker controls.

The rejection plumbing already existed. ``parse_message`` validates against
``PAYLOAD_MODELS`` and ``api/server.py`` turns a ``ValidationError`` into a
HUP section 8 error frame (code 1003) while keeping the socket alive. These
tests pin what that plumbing now fires on.

Two things are asserted here that are easy to lose again:

1. Hostile values are rejected with a typed ``ValidationError``, not clamped.
2. For the two models the SDK constrains, the SDK and the brain agree field
   for field, so "mirrors the brain" is enforced rather than asserted in
   a docstring.
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from models.protocol import (
    MESSAGE_TYPES,
    AttachmentRef,
    AudioChunkPayload,
    DeviceAnnouncePayload,
    DeviceRegisterPayload,
    ExecuteCommandPayload,
    FeralMessage,
    GlassesFramePayload,
    LocationUpdatePayload,
    NodeRegisterPayload,
    VIDEO_FRAME_MAX_BYTES,
    parse_message,
)


def _b64(n: int) -> str:
    """A base64 string whose DECODED length is exactly ``n`` bytes."""
    return base64.b64encode(b"\x00" * n).decode("ascii")


def _ok_glasses_frame(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "device_id": "w610-D344",
        "data_b64": _b64(1024),
        "encoding": "jpeg",
        "width": 640,
        "height": 480,
        "source": "w610",
        "sequence": 7,
    }
    payload.update(overrides)
    return payload


# ─────────────────────────────────────────────
# 1. The exact values the audit reproduced
# ─────────────────────────────────────────────

def test_glasses_frame_accepts_a_realistic_frame():
    """The bounds must not reject anything real hardware sends."""
    frame = GlassesFramePayload(**_ok_glasses_frame())
    assert frame.device_id == "w610-D344"
    assert frame.sequence == 7


@pytest.mark.parametrize(
    "field,value",
    [
        ("device_id", ""),                 # audit: accepted
        ("device_id", "x" * 129),          # SDK caps at 128
        ("width", -5),                     # audit: accepted
        ("width", 0),
        ("height", 1000000000),            # audit: accepted
        ("sequence", -42),                 # audit: accepted
    ],
)
def test_glasses_frame_rejects_hostile_scalars(field, value):
    with pytest.raises(ValidationError):
        GlassesFramePayload(**_ok_glasses_frame(**{field: value}))


def test_glasses_frame_rejects_oversized_decoded_payload():
    """900000 decoded bytes against a 512 KiB cap, the audit's number.

    The cap is measured on DECODED bytes, not on base64 characters. See
    the F-03 note in AUDIT-FIXES.md: ``api/server.py`` still measures
    characters, so the two disagree until F-03 lands.
    """
    with pytest.raises(ValidationError) as exc:
        GlassesFramePayload(**_ok_glasses_frame(data_b64=_b64(900000)))
    assert "900000" in str(exc.value)


def test_glasses_frame_accepts_a_frame_at_exactly_the_cap():
    """The boundary itself is legal; only strictly-over is rejected."""
    frame = GlassesFramePayload(
        **_ok_glasses_frame(data_b64=_b64(VIDEO_FRAME_MAX_BYTES))
    )
    assert frame.device_id == "w610-D344"


def test_glasses_frame_cap_is_measured_on_decoded_bytes_not_characters():
    """A blob whose base64 text exceeds the cap but decodes under it passes.

    This is the exact discrepancy F-03 describes at ``api/server.py:3672``.
    400 KiB of JPEG is 400 KiB decoded (legal) but ~533 KiB of base64
    characters (which the server's character-counting check rejects).
    """
    decoded = 400 * 1024
    payload = _ok_glasses_frame(data_b64=_b64(decoded))
    assert len(payload["data_b64"]) > VIDEO_FRAME_MAX_BYTES
    frame = GlassesFramePayload(**payload)
    assert len(base64.b64decode(frame.data_b64)) == decoded


def test_device_announce_rejects_absurd_rssi():
    with pytest.raises(ValidationError):
        DeviceAnnouncePayload(device_id="aa:bb:cc", rssi_dbm=-9999)


@pytest.mark.parametrize("value", ["", "x" * 129])
def test_device_announce_rejects_bad_device_id(value):
    with pytest.raises(ValidationError):
        DeviceAnnouncePayload(device_id=value)


def test_device_announce_accepts_a_real_advertisement():
    ann = DeviceAnnouncePayload(
        scanner_node_id="iphone-1",
        device_id="AA:BB:CC:DD:EE:FF",
        device_kind="bluetooth_le",
        rssi_dbm=-67,
    )
    assert ann.rssi_dbm == -67


# ─────────────────────────────────────────────
# 2. The rejection reaches the HUP error path
# ─────────────────────────────────────────────

def test_parse_message_raises_on_hostile_glasses_frame():
    """``parse_message`` is what ``api/server.py`` wraps in the 1003 handler.

    If this raised nothing the socket would accept the frame; if it raised
    something untyped the handler could not name the offending field.
    """
    raw = {
        "type": "glasses_frame",
        "payload": _ok_glasses_frame(device_id="", sequence=-42),
    }
    with pytest.raises(ValidationError):
        parse_message(raw)


def test_parse_message_still_accepts_a_good_glasses_frame():
    msg, payload = parse_message(
        {"type": "glasses_frame", "payload": _ok_glasses_frame()}
    )
    assert msg.type == "glasses_frame"
    assert isinstance(payload, GlassesFramePayload)


# ─────────────────────────────────────────────
# 3. The SDK's "mirrors the brain" claim, enforced
# ─────────────────────────────────────────────

_SDK_SCHEMAS = (
    Path(__file__).resolve().parents[2]
    / "feral-nodes"
    / "python-node-sdk"
    / "src"
    / "feral_node_sdk"
    / "schemas.py"
)


def _load_sdk_schemas():
    if not _SDK_SCHEMAS.is_file():
        pytest.skip(f"node SDK not present at {_SDK_SCHEMAS}")
    name = "_f02_sdk_schemas"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SDK_SCHEMAS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _numeric_bounds(model: type[BaseModel], field: str) -> dict[str, Any]:
    """Extract the comparable constraint metadata for one field.

    Pydantic v2 stores ``ge`` / ``le`` / ``min_length`` / ``max_length`` as
    annotated metadata objects rather than plain attributes, so compare by
    ``(type name, value)`` pairs.
    """
    info = model.model_fields[field]
    out: dict[str, Any] = {}
    for meta in info.metadata:
        for attr in ("ge", "le", "gt", "lt", "min_length", "max_length"):
            if hasattr(meta, attr):
                out[attr] = getattr(meta, attr)
    return out


@pytest.mark.parametrize(
    "model_name,fields",
    [
        (
            "GlassesFramePayload",
            ["device_id", "width", "height", "sequence"],
        ),
        ("DeviceAnnouncePayload", ["device_id", "rssi_dbm"]),
    ],
)
def test_sdk_constraints_match_the_brain(model_name, fields):
    """The SDK docstring says it mirrors the brain. Make that testable.

    Until F-02 the mirror was inverted: the SDK bounded these fields and
    the brain did not, so every constraint lived in the component an
    attacker controls.
    """
    sdk = _load_sdk_schemas()
    sdk_model = getattr(sdk, model_name)
    brain_model = {
        "GlassesFramePayload": GlassesFramePayload,
        "DeviceAnnouncePayload": DeviceAnnouncePayload,
    }[model_name]
    for field in fields:
        assert _numeric_bounds(brain_model, field) == _numeric_bounds(
            sdk_model, field
        ), f"{model_name}.{field} constraints diverge between brain and SDK"


def test_sdk_and_brain_agree_on_the_decoded_frame_cap():
    sdk = _load_sdk_schemas()
    assert sdk.VIDEO_FRAME_MAX_BYTES == VIDEO_FRAME_MAX_BYTES


@pytest.mark.parametrize(
    "payload",
    [
        {"device_id": "", "data_b64": ""},
        {"device_id": "w610", "data_b64": _b64(900000)},
        {"device_id": "w610", "data_b64": "", "sequence": -1},
    ],
)
def test_sdk_and_brain_reject_the_same_hostile_frames(payload):
    """Same input, same verdict, both directions of the wire."""
    sdk = _load_sdk_schemas()
    with pytest.raises(ValidationError):
        sdk.GlassesFramePayload(**payload)
    with pytest.raises(ValidationError):
        GlassesFramePayload(**payload)


# ─────────────────────────────────────────────
# 4. The gap was universal, not glasses-specific
# ─────────────────────────────────────────────

def test_envelope_rejects_a_negative_timestamp():
    with pytest.raises(ValidationError):
        FeralMessage(type="text_command", timestamp_ms=-1)


def test_envelope_rejects_an_oversized_session_id():
    with pytest.raises(ValidationError):
        FeralMessage(type="text_command", session_id="s" * 4096)


def test_audio_chunk_rejects_negative_counters():
    with pytest.raises(ValidationError):
        AudioChunkPayload(chunk_index=-1)
    with pytest.raises(ValidationError):
        AudioChunkPayload(sample_rate=0)


def test_node_register_rejects_empty_node_id():
    with pytest.raises(ValidationError):
        NodeRegisterPayload(node_id="", node_type="sensor")


def test_device_register_rejects_empty_device_id():
    with pytest.raises(ValidationError):
        DeviceRegisterPayload(device_id="", device_type="glasses")


def test_attachment_ref_rejects_negative_size():
    with pytest.raises(ValidationError):
        AttachmentRef(upload_id="u1", size_bytes=-1)


def test_execute_command_rejects_negative_timeout():
    with pytest.raises(ValidationError):
        ExecuteCommandPayload(executor="shell", action="ls", timeout_ms=-1)


@pytest.mark.parametrize(
    "field,value",
    [("lat", 91.0), ("lat", -91.0), ("lon", 181.0), ("lon", -181.0)],
)
def test_location_update_rejects_impossible_coordinates(field, value):
    kwargs = {"node_id": "phone-1", "lat": 0.0, "lon": 0.0}
    kwargs[field] = value
    with pytest.raises(ValidationError):
        LocationUpdatePayload(**kwargs)


def test_location_update_accepts_core_location_invalid_sentinels():
    """CoreLocation reports -1 for unknown accuracy / course / speed.

    Bounding these to ``ge=0`` would reject every iPhone fix taken
    indoors. They are deliberately left unconstrained; this test exists so
    nobody "completes" the sweep by adding the bound.
    """
    upd = LocationUpdatePayload(
        node_id="phone-1",
        lat=37.33,
        lon=-122.03,
        accuracy_m=-1.0,
        heading_deg=-1.0,
        speed_mps=-1.0,
    )
    assert upd.speed_mps == -1.0


def test_glasses_status_accepts_the_unknown_battery_sentinel():
    """``battery_level = -1`` is this file's own default for "unknown".

    A ``ge=0`` bound here would reject the brain's own declared default,
    which is why battery is left unconstrained everywhere except
    ``NodeHeartbeatPayload``, where the SDK already bounds it.
    """
    from models.protocol import GlassesStatusPayload

    assert GlassesStatusPayload(node_id="phone-1").battery_level == -1
    assert GlassesStatusPayload(node_id="phone-1", battery_level=-1).battery_level == -1


def test_biometric_payload_is_deliberately_unbounded():
    """No semantic ceilings. Real sensors report surprising values.

    A 220 bpm heart rate during exercise and a 43 C skin temperature in a
    sauna are both real; a wrong ceiling turns a working device into a
    rejected one. Recorded as a test so the omission is a decision, not
    an oversight.
    """
    from models.protocol import BiometricPayload

    bio = BiometricPayload(
        heart_rate_bpm=220, spo2_pct=100, temperature_c=43.0, uv_index=12
    )
    assert bio.heart_rate_bpm == 220


# ─────────────────────────────────────────────
# 5. Coverage ratchet
# ─────────────────────────────────────────────

#: Wire models that carry no field constraint at all, each for a stated
#: reason. Anything NOT in this set must constrain at least one field.
#: Shrinking this set is the follow-up work; growing it silently is the
#: regression this test exists to catch.
DELIBERATELY_UNCONSTRAINED: set[str] = set()


def test_every_registered_payload_model_constrains_something():
    unconstrained = []
    for msg_type, model in sorted(MESSAGE_TYPES.items()):
        if model.__name__ in DELIBERATELY_UNCONSTRAINED:
            continue
        has_bound = any(
            info.metadata for info in model.model_fields.values()
        )
        if not has_bound:
            unconstrained.append(f"{msg_type} -> {model.__name__}")
    assert not unconstrained, (
        "wire models with zero field constraints (F-02): "
        + ", ".join(unconstrained)
    )
