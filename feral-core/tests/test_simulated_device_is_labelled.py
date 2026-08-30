"""A simulated device must never answer like a real one.

``hardware/mock_roomba.py`` is opt-in (``is_enabled()`` returns True
only when ``FERAL_MOCK_ROOMBA`` is explicitly 1/true/yes/on). When the
operator does opt in it is registered into ``HardwareMesh`` at boot by
``api/state.py`` and is reachable over HTTP at ``POST
/api/hardware/mock_roomba/start`` — so it still has to say what it is.

Its envelope was deliberately byte-identical to
``HomeAssistantIntegration.vacuum_start`` "so the orchestrator's tool
dispatch path can use either backend interchangeably". That parity is
the feature and was also the defect: ``{"success": true, "data":
{"started": true, "service": "vacuum.start"}}`` is what a real vacuum
returns, and there was no field, no note and no summary text that told a
caller nothing had been commanded.

The mock is kept. It now says what it is.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hardware.mock_roomba import (  # noqa: E402
    SIMULATED_NOTE,
    MockRoomba,
    is_enabled,
    register_with_mesh,
)


def test_the_mock_is_off_by_default(monkeypatch):
    """It is opt-in now. A fresh install has no vacuum in its device list."""
    monkeypatch.delenv("FERAL_MOCK_ROOMBA", raising=False)
    assert is_enabled() is False


def test_the_mock_is_opt_in_by_explicit_env(monkeypatch):
    monkeypatch.setenv("FERAL_MOCK_ROOMBA", "1")
    assert is_enabled() is True


def test_start_declares_itself_simulated():
    data = asyncio.run(MockRoomba().start())["data"]
    assert data["started"] is True
    assert data["simulated"] is True
    assert "No physical vacuum was commanded" in data["note"]


def test_stop_declares_itself_simulated():
    mock = MockRoomba()
    asyncio.run(mock.start())
    data = asyncio.run(mock.stop())["data"]
    assert data["stopped"] is True
    assert data["simulated"] is True


def test_status_declares_itself_simulated():
    status = MockRoomba().status()
    assert status["simulated"] is True
    assert status["note"] == SIMULATED_NOTE


def test_a_wrong_entity_is_still_refused():
    """The existing truthfulness gate must survive the change."""
    out = asyncio.run(MockRoomba().start(entity_id="vacuum.real_roomba"))
    assert out["success"] is False
    assert out["reason"] == "wrong_entity"


class _CapturingMemory:
    def __init__(self):
        self.saved: list[dict] = []

    async def episode_save(self, **kwargs):
        self.saved.append(kwargs)
        return {"id": "ep-1"}


def test_the_episode_summary_says_simulated():
    """Recall renders the summary, not the JSON detail.

    ``source: mock_roomba`` was already in ``detail``, but the timeline
    and every recall surface show ``summary``, where the event read as a
    real vacuum cycle.
    """
    memory = _CapturingMemory()
    asyncio.run(MockRoomba(memory=memory).start())
    assert memory.saved, "the actuator episode must still be written"
    summary = memory.saved[0]["summary"]
    assert "simulated" in summary.lower()
    detail = json.loads(memory.saved[0]["detail"])
    assert detail["source"] == "mock_roomba"


def test_a_failed_episode_write_is_logged_at_warning(caplog):
    class _Broken:
        async def episode_save(self, **kwargs):
            raise RuntimeError("store down")

    with caplog.at_level("WARNING", logger="feral.hardware.mock_roomba"):
        asyncio.run(MockRoomba(memory=_Broken()).start())
    assert "episode_save failed" in caplog.text


def test_the_mesh_entry_is_labelled():
    class _Mesh:
        def __init__(self):
            self._announced_devices = {}

    mesh = _Mesh()
    mock = MockRoomba()
    register_with_mesh(mesh, mock)
    entry = mesh._announced_devices[mock.entity_id]
    assert entry["metadata"]["mock"] is True
    assert "demo" in entry["name"].lower()
