"""Tests for ``feral-core/perception/glasses_buffer.py``.

Pins the Lane 08-ACK'd public API (GlassesBuffer.ingest / latest /
recent / latest_data_urls / device_ids_with_frames + GlassesFrame
dataclass shape) and the documented behaviors:

- Per-device ring with a configurable cap (default 30)
- 30 s default freshness gate, configurable per call
- ``latest(device_id=None)`` picks the freshest across devices
- Defensive against malformed payloads (no exceptions on the write
  path)
- ``to_data_url()`` produces an OpenAI/Anthropic-compatible
  ``data:image/...;base64,...`` URL

The buffer is the contract between Lane 11 (write path) and Lane 08
(read path via ``perception/context_attach.py``). Lane 08's
``test_vision_context_attach.py`` re-asserts the read API from the
other side.
"""

from __future__ import annotations

import time

import pytest

from perception.glasses_buffer import (
    GlassesBuffer,
    GlassesFrame,
    KNOWN_SOURCES,
)


# ─────────────────────────────────────────────
# GlassesFrame
# ─────────────────────────────────────────────


def test_glasses_frame_is_frozen():
    f = GlassesFrame(device_id="d", timestamp=time.time(), data_b64="AAAA")
    with pytest.raises(Exception):
        # frozen=True: mutation raises FrozenInstanceError.
        f.timestamp = 0.0  # type: ignore[misc]


def test_age_seconds_uses_timestamp_when_present():
    now = 1_000_000.0
    f = GlassesFrame(device_id="d", timestamp=now - 5.0, data_b64="A")
    assert f.age_seconds(now=now) == pytest.approx(5.0, abs=0.001)


def test_age_seconds_falls_back_to_ingested_at_for_missing_timestamp():
    f = GlassesFrame(
        device_id="d", timestamp=0.0, data_b64="A",
        ingested_at=1_000_000.0 - 2.5,
    )
    assert f.age_seconds(now=1_000_000.0) == pytest.approx(2.5, abs=0.001)


def test_age_seconds_clamps_negative_to_zero():
    """Future-stamped frames (clock drift) report age 0 rather than negative."""
    now = 1_000_000.0
    f = GlassesFrame(device_id="d", timestamp=now + 5.0, data_b64="A")
    assert f.age_seconds(now=now) == 0.0


def test_to_data_url_includes_mime_and_b64():
    f = GlassesFrame(device_id="d", timestamp=0, data_b64="AAAA", encoding="jpeg")
    assert f.to_data_url() == "data:image/jpeg;base64,AAAA"


def test_to_data_url_falls_back_to_jpeg_for_unknown_encoding():
    f = GlassesFrame(device_id="d", timestamp=0, data_b64="AAAA", encoding="bmp")
    assert f.to_data_url() == "data:image/jpeg;base64,AAAA"


# ─────────────────────────────────────────────
# Ingest
# ─────────────────────────────────────────────


def test_ingest_returns_a_frame_and_buckets_by_device():
    buf = GlassesBuffer()
    f = buf.ingest(
        {"device_id": "w610-1", "data_b64": "AAAA", "source": "w610"},
        node_id="iphone-1",
    )
    assert isinstance(f, GlassesFrame)
    assert f.device_id == "w610-1"
    assert f.node_id == "iphone-1"
    assert "w610-1" in buf.device_ids_with_frames()


def test_ingest_returns_none_on_missing_device_id():
    buf = GlassesBuffer()
    assert buf.ingest({"data_b64": "AAAA"}, node_id="n") is None
    assert buf.device_ids_with_frames() == []


def test_ingest_returns_none_on_missing_data_b64():
    buf = GlassesBuffer()
    assert buf.ingest({"device_id": "d"}, node_id="n") is None


def test_ingest_never_raises_on_garbage_payload():
    buf = GlassesBuffer()
    # No exception; just None.
    assert buf.ingest("not a dict", node_id="n") is None  # type: ignore[arg-type]
    assert buf.ingest({"device_id": "d", "data_b64": "A", "width": "not-an-int"}, node_id="n") is not None


def test_ingest_coerces_string_sequence_to_int():
    """Older daemons send ``sequence`` as a string; the buffer must coerce."""
    buf = GlassesBuffer()
    f = buf.ingest(
        {"device_id": "d", "data_b64": "A", "sequence": "42"}, node_id="n"
    )
    assert f is not None and f.sequence == 42


def test_ingest_ring_evicts_oldest_when_full():
    buf = GlassesBuffer(max_frames_per_device=3)
    for i in range(5):
        buf.ingest(
            {"device_id": "d", "data_b64": f"frame-{i}", "timestamp": float(i)},
            node_id="n",
        )
    # Only the last 3 should survive.
    recent = buf.recent(device_id="d", max_count=10, max_age_s=float("inf"))
    assert [f.data_b64 for f in recent] == ["frame-4", "frame-3", "frame-2"]


def test_ingest_creates_independent_buckets_per_device():
    buf = GlassesBuffer(max_frames_per_device=2)
    buf.ingest({"device_id": "a", "data_b64": "1"}, node_id="n")
    buf.ingest({"device_id": "b", "data_b64": "2"}, node_id="n")
    buf.ingest({"device_id": "a", "data_b64": "3"}, node_id="n")
    assert sorted(buf.device_ids_with_frames()) == ["a", "b"]
    assert buf.latest(device_id="a", max_age_s=float("inf")).data_b64 == "3"
    assert buf.latest(device_id="b", max_age_s=float("inf")).data_b64 == "2"


# ─────────────────────────────────────────────
# Read API
# ─────────────────────────────────────────────


def test_latest_with_device_id_returns_freshest():
    buf = GlassesBuffer()
    for i in range(3):
        buf.ingest(
            {"device_id": "d", "data_b64": f"f{i}", "timestamp": time.time()},
            node_id="n",
        )
    assert buf.latest(device_id="d").data_b64 == "f2"


def test_latest_without_device_id_picks_across_devices():
    buf = GlassesBuffer()
    now = time.time()
    buf.ingest({"device_id": "a", "data_b64": "older", "timestamp": now - 2}, node_id="n")
    buf.ingest({"device_id": "b", "data_b64": "newer", "timestamp": now - 1}, node_id="n")
    assert buf.latest().data_b64 == "newer"


def test_latest_respects_max_age_gate():
    buf = GlassesBuffer(max_age_s=5.0)
    buf.ingest(
        {"device_id": "d", "data_b64": "stale", "timestamp": time.time() - 60.0},
        node_id="n",
    )
    # Default gate (5s) → filtered out.
    assert buf.latest(device_id="d") is None
    # Overridden gate (infinite) → returns the frame.
    assert buf.latest(device_id="d", max_age_s=float("inf")) is not None


def test_recent_returns_freshest_first():
    buf = GlassesBuffer()
    now = time.time()
    for i in range(5):
        buf.ingest(
            {"device_id": "d", "data_b64": f"f{i}", "timestamp": now + i},
            node_id="n",
        )
    out = buf.recent(device_id="d", max_count=3)
    assert [f.data_b64 for f in out] == ["f4", "f3", "f2"]


def test_recent_max_count_zero_returns_empty():
    buf = GlassesBuffer()
    buf.ingest({"device_id": "d", "data_b64": "A"}, node_id="n")
    assert buf.recent(device_id="d", max_count=0) == []


def test_recent_filters_stale_frames():
    buf = GlassesBuffer(max_age_s=10.0)
    now = time.time()
    buf.ingest(
        {"device_id": "d", "data_b64": "stale", "timestamp": now - 100.0},
        node_id="n",
    )
    buf.ingest({"device_id": "d", "data_b64": "fresh", "timestamp": now}, node_id="n")
    out = buf.recent(device_id="d", max_count=5)
    assert [f.data_b64 for f in out] == ["fresh"]


def test_recent_across_devices_interleaves_by_timestamp():
    buf = GlassesBuffer()
    now = time.time()
    buf.ingest({"device_id": "a", "data_b64": "a1", "timestamp": now - 3}, node_id="n")
    buf.ingest({"device_id": "b", "data_b64": "b1", "timestamp": now - 2}, node_id="n")
    buf.ingest({"device_id": "a", "data_b64": "a2", "timestamp": now - 1}, node_id="n")
    out = buf.recent(max_count=5)
    assert [f.data_b64 for f in out] == ["a2", "b1", "a1"]


def test_latest_data_urls_returns_data_url_strings():
    buf = GlassesBuffer()
    buf.ingest({"device_id": "d", "data_b64": "AAAA", "encoding": "jpeg"}, node_id="n")
    urls = buf.latest_data_urls()
    assert urls == ["data:image/jpeg;base64,AAAA"]


def test_known_sources_documented():
    """Lane 11's KNOWN_SOURCES is the contract for cost-budget tiering.

    Pinning the set prevents accidental removal — Lane 08's
    context-attach reads this catalog to pick a cheaper vision tier
    for ``camera_fallback`` than for ``w610``.
    """
    assert "w610" in KNOWN_SOURCES
    assert "camera_fallback" in KNOWN_SOURCES
    assert "jw_w300" in KNOWN_SOURCES
    assert "browser_camera" in KNOWN_SOURCES


# ─────────────────────────────────────────────
# Boot wiring
# ─────────────────────────────────────────────


def test_brain_state_has_glasses_buffer():
    """BrainState constructor wires glasses_buffer alongside vision_buffer."""
    from api.state import BrainState

    s = BrainState()
    assert isinstance(s.glasses_buffer, GlassesBuffer)
    # Default capacity + age gate match the documented constants.
    assert s.glasses_buffer._max_frames == GlassesBuffer.DEFAULT_MAX_FRAMES
    assert s.glasses_buffer._max_age_s == GlassesBuffer.DEFAULT_MAX_AGE_S
