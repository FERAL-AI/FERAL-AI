"""The brain must be told which hardware is actually connected.

Measured on the live install (``~/.feral/memory.db``) at audit time:

    node_id                    capability         status  provenance
    feral-iphone-6053b3cdc4ed  jw_health_glasses  ready   ble
    feral-iphone-2a210fa1      veepoo_wristband   ready   ble
    ... 7 rows across 6 iPhone nodes, all provenance=ble

``memory/node_subdevices.py`` opens with a docstring naming its
consumers: "the web dashboard, native iOS UI, future MCP clients, the
orchestrator's prompt context". The first three read it. The fourth
never did. The only hardware line the prompt carried was

    Connected devices: ['feral-iphone-6053b3cdc4ed']

so a user asking "are my glasses connected" got an opaque node id, and
the model had no way to answer from context. The BLE peripherals behind
the phone -- which is the entire "whether software or hardware" half of
the product promise -- were invisible to the brain while being fully
rendered on the dashboard.

These tests drive the real ``IdentityLoader`` and the real
``NodeSubdeviceStore`` against a temp SQLite file. No mocks of either.
"""

from __future__ import annotations

import time

import pytest

from agents.identity_loader import IdentityLoader
from memory.node_subdevices import NodeSubdeviceStore


class _Frame:
    """Minimal PerceptionFrame stand-in; only what build_system_prompt reads."""

    connected_nodes: list = []

    def to_system_context(self) -> str:
        return "Connected nodes: feral-iphone-6053b3cdc4ed"


@pytest.fixture()
def store(tmp_path):
    return NodeSubdeviceStore(db_path=str(tmp_path / "memory.db"))


def _seed_live_glasses(store: NodeSubdeviceStore) -> None:
    """The exact shape the live install holds, with a fresh heartbeat."""
    store.upsert(
        node_id="feral-iphone-6053b3cdc4ed",
        capability="jw_health_glasses",
        status="ready",
        attrs={"battery_pct": 69, "device_name": "W300"},
        provenance="ble",
    )


async def _build(loader: IdentityLoader, **kwargs) -> str:
    frame = _Frame()
    frame.connected_nodes = ["feral-iphone-6053b3cdc4ed"]
    return await loader.build_system_prompt(
        frame, [], session_id="s1", identity_text="", full_catalog=[], **kwargs
    )


@pytest.mark.asyncio
async def test_live_subdevice_named_in_prompt(store):
    """The capability and its device name must appear in the prompt."""
    _seed_live_glasses(store)
    loader = IdentityLoader(memory=None)
    loader.subdevice_store = store

    prompt = await _build(loader)

    assert "jw_health_glasses" in prompt
    assert "W300" in prompt


@pytest.mark.asyncio
async def test_prompt_has_a_connected_hardware_section(store):
    _seed_live_glasses(store)
    loader = IdentityLoader(memory=None)
    loader.subdevice_store = store

    prompt = await _build(loader)

    assert "## Connected Hardware" in prompt


@pytest.mark.asyncio
async def test_stale_subdevice_is_labelled_not_hidden(store):
    """A BLE row past its 30s window is still shown, marked disconnected.

    Hiding it would make "my glasses dropped" indistinguishable from
    "you never had glasses", which is the same lie in the other
    direction.
    """
    store.upsert(
        node_id="feral-iphone-2a210fa1",
        capability="veepoo_wristband",
        status="ready",
        attrs={"device_name": "VITRO"},
        provenance="ble",
        observed_at=time.time() - 3600,
    )
    loader = IdentityLoader(memory=None)
    loader.subdevice_store = store

    prompt = await _build(loader)

    assert "veepoo_wristband" in prompt
    assert "not reporting" in prompt
    # And it must NOT be presented as live.
    hardware = prompt.split("## Connected Hardware", 1)[1].split("\n##", 1)[0]
    assert "connected, reporting now" not in hardware


@pytest.mark.asyncio
async def test_live_and_stale_are_distinguishable(store):
    _seed_live_glasses(store)
    store.upsert(
        node_id="feral-iphone-2a210fa1",
        capability="veepoo_wristband",
        status="ready",
        attrs={"device_name": "VITRO"},
        provenance="ble",
        observed_at=time.time() - 3600,
    )
    loader = IdentityLoader(memory=None)
    loader.subdevice_store = store

    prompt = await _build(loader)
    hardware = prompt.split("## Connected Hardware", 1)[1].split("\n##", 1)[0]

    glasses_line = [ln for ln in hardware.splitlines() if "jw_health_glasses" in ln][0]
    band_line = [ln for ln in hardware.splitlines() if "veepoo_wristband" in ln][0]
    assert "connected, reporting now" in glasses_line
    assert "not reporting" in band_line


@pytest.mark.asyncio
async def test_no_store_wired_produces_no_section(store):
    """A brain with no subdevice store must not grow an empty block."""
    loader = IdentityLoader(memory=None)

    prompt = await _build(loader)

    assert "## Connected Hardware" not in prompt


@pytest.mark.asyncio
async def test_empty_store_says_so_rather_than_going_silent(store):
    """Zero rows is a real answer and must be stated.

    Silence here reads to the model as "no information", and the model
    then guesses. "No peripherals have reported" is the ground truth
    and stops the guess.
    """
    loader = IdentityLoader(memory=None)
    loader.subdevice_store = store

    prompt = await _build(loader)

    assert "## Connected Hardware" in prompt
    assert "No peripherals" in prompt


@pytest.mark.asyncio
async def test_store_failure_is_visible_not_swallowed(store, caplog):
    """A store that raises must degrade loudly.

    A silent except here would recreate the exact defect being fixed:
    the prompt would look normal while carrying no hardware truth.
    """

    class _Broken:
        def list_all(self):
            raise sqlite_error()

    def sqlite_error():
        import sqlite3

        return sqlite3.OperationalError("database is locked")

    loader = IdentityLoader(memory=None)
    loader.subdevice_store = _Broken()

    with caplog.at_level("WARNING", logger="feral.orchestrator.identity"):
        prompt = await _build(loader)

    assert any("subdevice" in r.message.lower() for r in caplog.records)
    # And the prompt must say the hardware view is unavailable rather
    # than implying nothing is attached.
    assert "hardware status unavailable" in prompt.lower()


@pytest.mark.asyncio
async def test_orchestrator_exposes_a_wiring_setter():
    """api/state.py must have a supported way to wire the store.

    Reaching into ``orchestrator.identity_loader.subdevice_store`` from
    BrainState would be a second, undocumented wiring path; every other
    subsystem the prompt reads (calendar, somatic engine) has a setter.
    """
    from agents.orchestrator import Orchestrator

    assert hasattr(Orchestrator, "set_subdevice_store")


@pytest.mark.asyncio
async def test_setter_reaches_the_identity_loader(store, monkeypatch):
    from agents.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.identity_loader = IdentityLoader(memory=None)
    Orchestrator.set_subdevice_store(orch, store)

    assert orch.identity_loader.subdevice_store is store
