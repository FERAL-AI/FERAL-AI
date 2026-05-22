"""Tests for ``HardwareMesh.ingest_device_announce`` (HUP v1.3.0 §5.4.4).

Closes THESIS_SCENARIOS S3 (hardware peripheral memory). Asserts:

- A first announce creates an in-memory record AND a KG entity with
  ``category=device``.
- A repeat announce updates ``last_seen`` / ``rssi_dbm`` in place
  rather than duplicating the record (mention-count bump goes through
  the KG's ``add_entity`` → ``_bump_mention`` path, exercised here by
  asserting ``add_entity`` is called with the same name twice).
- Missing ``device_id`` is dropped without raising.
- KG metadata includes a ``tags`` list with device_kind + "peripheral"
  + scanner_node_id — Lane 05's ``find_entities_by_tag(category=
  'device')`` query consumes this.
- ``list_announced_devices`` exposes the discoveries for Lane 12's
  Devices page.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from hardware.mesh import HardwareMesh
from hardware.protocol import DeviceRegistry


def _build_mesh(kg: object | None = None) -> HardwareMesh:
    return HardwareMesh(
        device_registry=DeviceRegistry(),
        daemons={},
        knowledge_graph=kg,
    )


@pytest.mark.asyncio
async def test_ingest_creates_record_and_writes_kg_entity():
    kg = AsyncMock()
    mesh = _build_mesh(kg=kg)

    record = await mesh.ingest_device_announce({
        "scanner_node_id": "feral-iphone-1",
        "device_id": "AA:BB:CC:DD:EE:FF",
        "device_kind": "bluetooth_le",
        "name": "AirPods Pro",
        "manufacturer": "Apple",
        "rssi_dbm": -54,
        "advertised_services": ["180F"],
        "last_seen": 1716393631.5,
    })

    assert record["device_id"] == "AA:BB:CC:DD:EE:FF"
    assert record["name"] == "AirPods Pro"
    assert record["rssi_dbm"] == -54

    kg.add_entity.assert_awaited_once()
    kw = kg.add_entity.await_args.kwargs
    assert kw["name"] == "AirPods Pro"
    assert kw["entity_type"] == "device"
    meta = kw["metadata"]
    assert meta["category"] == "device"
    assert meta["device_kind"] == "bluetooth_le"
    assert meta["scanner_node_id"] == "feral-iphone-1"
    assert "bluetooth_le" in meta["tags"]
    assert "peripheral" in meta["tags"]
    assert "feral-iphone-1" in meta["tags"]


@pytest.mark.asyncio
async def test_repeat_announce_updates_in_place():
    kg = AsyncMock()
    mesh = _build_mesh(kg=kg)

    await mesh.ingest_device_announce({
        "device_id": "AA:BB:CC:DD:EE:FF",
        "rssi_dbm": -54,
        "last_seen": 100.0,
    })
    await mesh.ingest_device_announce({
        "device_id": "AA:BB:CC:DD:EE:FF",
        "rssi_dbm": -42,
        "last_seen": 200.0,
        "metadata": {"tx_power": 4},
    })

    devices = mesh.list_announced_devices()
    assert len(devices) == 1
    assert devices[0]["rssi_dbm"] == -42
    assert devices[0]["last_seen"] == 200.0
    assert devices[0]["metadata"]["tx_power"] == 4

    # KG add_entity called twice — the KG itself dedupes by name + type
    # (covered by knowledge_graph.add_entity's _find_entity_by_name
    # branch). We assert the mesh delegates correctly.
    assert kg.add_entity.await_count == 2


@pytest.mark.asyncio
async def test_ingest_drops_missing_device_id():
    kg = AsyncMock()
    mesh = _build_mesh(kg=kg)

    out = await mesh.ingest_device_announce({"name": "ghost"})
    assert out == {}
    assert mesh.list_announced_devices() == []
    kg.add_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_tolerates_missing_kg():
    """Early-boot path: KG isn't wired yet → ingest still records the
    discovery in-memory so the REST surface is non-empty."""
    mesh = _build_mesh(kg=None)
    record = await mesh.ingest_device_announce({
        "device_id": "x",
        "name": "Some Thing",
    })
    assert record["device_id"] == "x"
    assert mesh.find_announced_device("x")["name"] == "Some Thing"


@pytest.mark.asyncio
async def test_set_knowledge_graph_late_binds():
    """BrainState may bind the KG after the mesh is constructed."""
    mesh = _build_mesh(kg=None)
    kg = AsyncMock()
    mesh.set_knowledge_graph(kg)
    await mesh.ingest_device_announce({"device_id": "x"})
    kg.add_entity.assert_awaited_once()


def test_find_announced_device_returns_none_when_missing():
    mesh = _build_mesh(kg=None)
    assert mesh.find_announced_device("not-a-device") is None
