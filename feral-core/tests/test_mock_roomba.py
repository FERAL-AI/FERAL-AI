"""Tests for the brain-side mock Roomba (HARDWARE/mock_roomba.py).

Closes THESIS_SCENARIOS S5 step 4-5 on demo machines without a real
Home Assistant + Roomba — same structured shape Lane 10's
``HomeAssistantIntegration.vacuum_start`` returns so the orchestrator
tool path is backend-agnostic.

Tests assert:

- ``MockRoomba.start`` returns ``{success: True, data: {started: True,
  ...}}`` and flips ``is_running``.
- Latency stays under the documented < 500 ms SLA (the actual budget
  is much tighter in practice — < 5 ms locally — but the test uses
  the spec value so a regression on memory back-pressure surfaces
  immediately).
- ``start`` with a mismatched ``entity_id`` returns a truthful
  ``wrong_entity`` failure rather than impersonating the call.
- ``stop`` parities with HA's ``vacuum.stop`` shape.
- Memory wiring (when present) records a ``category=actuator`` episode.
- ``register_with_mesh`` writes a virtual ``vacuum.mock_roomba`` entry
  into ``HardwareMesh._announced_devices`` so the Lane 12 Devices page
  shows it.
- ``is_enabled`` honors the operator override.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest

from hardware.mesh import HardwareMesh
from hardware.mock_roomba import (
    DEFAULT_ENTITY_ID,
    MockRoomba,
    is_enabled,
    register_with_mesh,
)
from hardware.protocol import DeviceRegistry


# ─────────────────────────────────────────────
# MockRoomba.start / stop
# ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_returns_lane_10_shape_and_flips_state():
    mock = MockRoomba()
    out = await mock.start()
    assert out["success"] is True
    assert out["data"]["started"] is True
    assert out["data"]["entity_id"] == DEFAULT_ENTITY_ID
    assert out["data"]["service"] == "vacuum.start"
    assert "duration_ms" in out["data"]
    assert mock.is_running is True
    assert mock.started_at is not None


@pytest.mark.asyncio
async def test_start_meets_500ms_sla():
    """The Lane 11 SLA is < 500 ms; the actual cost is <5 ms locally."""
    mock = MockRoomba()
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await mock.start()
    elapsed = loop.time() - t0
    assert elapsed < 0.5, f"start() took {elapsed*1000:.2f} ms (SLA: 500ms)"


@pytest.mark.asyncio
async def test_start_with_wrong_entity_id_is_truthful():
    """Truthfulness gate: don't impersonate a real Roomba entity."""
    mock = MockRoomba()
    out = await mock.start(entity_id="vacuum.living_room")
    assert out["success"] is False
    assert out["reason"] == "wrong_entity"
    assert mock.is_running is False


@pytest.mark.asyncio
async def test_stop_parity_with_ha():
    mock = MockRoomba()
    await mock.start()
    out = await mock.stop()
    assert out["success"] is True
    assert out["data"]["stopped"] is True
    assert out["data"]["service"] == "vacuum.stop"
    assert mock.is_running is False
    assert mock.stopped_at is not None


@pytest.mark.asyncio
async def test_start_records_episode_via_memory():
    memory = AsyncMock()
    memory.episode_save = AsyncMock()
    mock = MockRoomba(memory=memory)
    await mock.start()
    memory.episode_save.assert_awaited_once()
    args, kwargs = memory.episode_save.await_args
    # Must use the real MemoryStore.episode_save signature (regression: it
    # used episode_save(summary, metadata=...) which raised TypeError and
    # silently dropped the episode so it could never be recalled).
    assert args == ()
    assert "metadata" not in kwargs
    assert kwargs["session_id"] == "mock-roomba"
    assert kwargs["event_type"] == "actuator"
    # The summary names the action, the device, and that it is simulated.
    # Recall and the timeline render ``summary``, not ``detail``, so a
    # summary that read like a real vacuum cycle was the one place a demo
    # event was indistinguishable from a real one. See
    # tests/test_simulated_device_is_labelled.py.
    assert "started" in kwargs["summary"]
    assert "mock_roomba" in kwargs["summary"]
    assert "simulated" in kwargs["summary"].lower()
    # Structured context is embedded in ``detail`` for semantic recall.
    detail = json.loads(kwargs["detail"])
    assert detail["category"] == "actuator"
    assert detail["actuator"] == "vacuum"
    assert detail["action"] == "started"
    assert detail["entity_id"] == DEFAULT_ENTITY_ID
    assert detail["source"] == "mock_roomba"


@pytest.mark.asyncio
async def test_start_tolerates_memory_failure():
    """Memory write failure must not break the SLA — log + continue."""
    memory = AsyncMock()
    memory.episode_save = AsyncMock(side_effect=RuntimeError("disk full"))
    mock = MockRoomba(memory=memory)
    out = await mock.start()
    assert out["success"] is True  # not blocked by memory write


def test_status_reports_running_flag():
    mock = MockRoomba()
    s = mock.status()
    assert s["entity_id"] == DEFAULT_ENTITY_ID
    assert s["is_running"] is False
    assert s["enabled"] is True


# ─────────────────────────────────────────────
# Mesh registration
# ─────────────────────────────────────────────


def _mesh() -> HardwareMesh:
    return HardwareMesh(device_registry=DeviceRegistry(), daemons={})


def test_register_with_mesh_writes_virtual_device():
    mesh = _mesh()
    mock = MockRoomba()
    register_with_mesh(mesh, mock)
    devices = mesh.list_announced_devices()
    assert len(devices) == 1
    rec = devices[0]
    assert rec["device_id"] == DEFAULT_ENTITY_ID
    assert rec["scanner_node_id"] == "brain"
    assert rec["metadata"]["mock"] is True
    assert "vacuum" in rec["metadata"]["tags"]


def test_register_with_mesh_tolerates_missing_mesh():
    """Early boot path: mesh may not exist; register must not raise."""
    mock = MockRoomba()
    register_with_mesh(None, mock)  # type: ignore[arg-type]


# ─────────────────────────────────────────────
# Feature flag
# ─────────────────────────────────────────────


@pytest.mark.parametrize("value,expected", [
    ("0", False), ("false", False), ("no", False), ("off", False), ("", False),
    ("1", True), ("true", True), ("yes", True), ("on", True),
])
def test_is_enabled_honors_env(monkeypatch, value, expected):
    monkeypatch.setenv("FERAL_MOCK_ROOMBA", value)
    assert is_enabled() is expected


def test_is_enabled_defaults_on(monkeypatch):
    """Default-on so a fresh demo machine works without configuration."""
    monkeypatch.delenv("FERAL_MOCK_ROOMBA", raising=False)
    assert is_enabled() is True
