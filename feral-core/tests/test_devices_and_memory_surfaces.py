"""Guards for the four HTTP surfaces the v2 Devices + Memory pages depend on.

Every test here was written after reproducing the defect against a live
brain on 127.0.0.1:9454 and re-running it after the fix.

1. ``/api/devices/connected`` erased every live HUP daemon.
   ``state.session_handoff`` is always set after boot, and the route took
   that branch and returned ONLY the handoff registry. The only place in
   the brain that calls ``SessionHandoffManager.register_device`` is the
   messaging-channel bridge in ``api/state.py``, so a HUP node registered
   over ``/v1/node`` was never in it. Measured with three daemons
   attached (visible in /api/hardware/mesh, /api/hardware/devices and
   /api/hardware/fleet), this endpoint answered
   ``{"devices": [], "offline": []}`` — which per its own docstring means
   "nothing has ever paired". The v2 Live pane could not render.

2. ``/internal/memory/search`` declared its parameter as ``query`` while
   the v2 Memory page had always called it as ``?q=``. FastAPI bound
   ``query`` to "" and the ``if not query: return []`` guard fired on
   every search. ``?q=quokka`` -> ``[]``; ``?query=quokka`` -> two
   matching notes with scores.

3. ``MemoryStore.search_all`` — the four-tier hybrid recall path — had no
   HTTP route at all. It was reachable from the gateway RPC and from
   taskflow only.

4. ``/api/hardware/invoke`` reads ``{node_id, command, params}``. The v2
   detail modal posted ``{device_id, method, args}``: all three keys
   missed, so the brain invoked ``node_id="" command=""`` and answered
   ``{"success": false, "error": "Node not connected: "}`` with nothing
   after the colon. Every Invoke click had always failed, and the message
   blamed the device.

5. ``/api/hardware/device/{id}`` answered ``200 {"error": ...}`` for an
   unknown id, so a client that does ``.then(json).then(setDetail)``
   replaced its good row with an error object.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.no_auto_feral_home


class _FakeWebSocket:
    """Mirrors the attributes /v1/node sets on the socket at register."""

    def __init__(self, *, node_type, capabilities=None, platform="", manufacturer="", model=""):
        self._feral_node_type = node_type
        self._feral_capabilities = list(capabilities or [])
        self._feral_platform = platform
        self._feral_manufacturer = manufacturer
        self._feral_model = model


# ─────────────────────────────────────────────
# 1. Live daemons survive a populated session_handoff
# ─────────────────────────────────────────────

@pytest.fixture()
def handoff_client(tmp_path):
    """A brain whose session_handoff exists, as it always does after boot."""
    from memory.node_subdevices import NodeSubdeviceStore

    mock = MagicMock()
    mock.skill_executor = None
    mock.node_subdevices = NodeSubdeviceStore(db_path=str(tmp_path / "memory.db"))
    mock.daemons = {
        "spd-phone-01": _FakeWebSocket(
            node_type="phone",
            capabilities=["camera", "gps"],
            platform="ios",
            manufacturer="Apple",
            model="iPhone 16 Pro",
        ),
        "spd-band-01": _FakeWebSocket(
            node_type="wearable",
            capabilities=["heart_rate"],
            platform="veepoo",
        ),
    }
    handoff = MagicMock()
    # What the real manager holds after boot: messaging-channel sessions
    # only. Never a HUP node.
    handoff.get_active_devices.return_value = [
        {
            "session_id": "channel_telegram_42",
            "node_type": "channel",
            "node_id": "telegram_42",
            "connected_at": 0,
        },
    ]
    mock.session_handoff = handoff

    with patch("api.state.state", mock), patch("api.routes.devices.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False)


def test_live_daemons_are_reported_even_when_session_handoff_is_populated(handoff_client):
    body = handoff_client.get("/api/devices/connected").json()
    ids = {d.get("node_id") for d in body["devices"]}
    assert "spd-phone-01" in ids, (
        "a daemon holding an open /v1/node socket vanished from "
        f"/api/devices/connected; got {body['devices']!r}"
    )
    assert "spd-band-01" in ids


def test_daemon_row_keeps_its_node_register_identity(handoff_client):
    body = handoff_client.get("/api/devices/connected").json()
    phone = next(d for d in body["devices"] if d.get("node_id") == "spd-phone-01")
    assert phone["type"] == "phone"
    assert phone["manufacturer"] == "Apple"
    assert "camera" in phone["capabilities"]


def test_handoff_only_rows_are_still_reported(handoff_client):
    """The merge must not drop the rows the old branch did return."""
    body = handoff_client.get("/api/devices/connected").json()
    ids = {d.get("node_id") for d in body["devices"]}
    assert "telegram_42" in ids


def test_no_daemons_and_no_handoff_rows_is_still_empty(tmp_path):
    from memory.node_subdevices import NodeSubdeviceStore

    mock = MagicMock()
    mock.skill_executor = None
    mock.node_subdevices = NodeSubdeviceStore(db_path=str(tmp_path / "m.db"))
    mock.daemons = {}
    handoff = MagicMock()
    handoff.get_active_devices.return_value = []
    mock.session_handoff = handoff
    with patch("api.state.state", mock), patch("api.routes.devices.state", mock):
        from api.server import app
        c = TestClient(app, raise_server_exceptions=False)
        assert c.get("/api/devices/connected").json()["devices"] == []


# ─────────────────────────────────────────────
# 2 + 3. Memory search surfaces
# ─────────────────────────────────────────────

@pytest.fixture()
def memory_client():
    mock = MagicMock()
    memory = MagicMock()
    memory.search = AsyncMock(return_value=[
        {"id": "n1", "content": "The quokka lives on Rottnest Island", "relevance_score": 0.75},
    ])
    memory.search_all = AsyncMock(return_value=[
        {"tier": "note", "score": 0.75, "id": "n1", "content": "The quokka lives on Rottnest Island"},
        {"tier": "entity", "score": 0.82, "id": "e1", "name": "quokka", "summary": "Entity: quokka (thing)"},
        {"tier": "knowledge", "score": 0.5, "subject": "quokka", "predicate": "lives_on", "object": "Rottnest Island"},
    ])
    memory.last_search_degradations = []
    mock.memory = memory

    with patch("api.state.state", mock), patch("api.routes.memory.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), memory


def test_internal_memory_search_accepts_the_q_spelling(memory_client):
    client, memory = memory_client
    r = client.get("/internal/memory/search", params={"q": "quokka"})
    assert r.status_code == 200
    assert r.json(), "?q= returned an empty list; the v2 Search tab reads that as 'No results'"
    memory.search.assert_awaited()
    assert memory.search.await_args.kwargs["query"] == "quokka"


def test_internal_memory_search_still_accepts_query(memory_client):
    client, _ = memory_client
    assert client.get("/internal/memory/search", params={"query": "quokka"}).json()


def test_internal_memory_search_empty_input_is_still_empty(memory_client):
    client, _ = memory_client
    assert client.get("/internal/memory/search").json() == []


def test_cross_tier_search_route_exists_and_reports_tiers(memory_client):
    client, _ = memory_client
    body = client.get("/api/memory/search", params={"q": "quokka"}).json()
    assert body["count"] == 3
    assert body["tiers"] == {"note": 1, "entity": 1, "knowledge": 1}
    assert body["degraded"] is False


def test_cross_tier_search_requires_a_query(memory_client):
    client, _ = memory_client
    assert client.get("/api/memory/search", params={"q": ""}).status_code == 400


def test_cross_tier_search_declares_partial_answers(memory_client):
    """A tier that raised is not a tier that matched nothing."""
    client, memory = memory_client

    async def _search_all(query, limit=20):
        memory.last_search_degradations = [
            {"tier": "episode", "error": "EmbeddingDimensionMismatch: 1536 != 384"},
        ]
        return [{"tier": "note", "score": 0.9, "id": "n1", "content": "x"}]

    memory.search_all = AsyncMock(side_effect=_search_all)
    body = client.get("/api/memory/search", params={"q": "anything"}).json()
    assert body["degraded"] is True
    assert body["degradations"][0]["tier"] == "episode"


def test_cross_tier_search_does_not_leak_a_previous_querys_degradations(memory_client):
    client, memory = memory_client
    memory.last_search_degradations = [{"tier": "note", "error": "stale from an older call"}]
    body = client.get("/api/memory/search", params={"q": "quokka"}).json()
    assert body["degradations"] == []
    assert body["degraded"] is False


def test_degradations_reach_the_response_from_a_real_store(tmp_path):
    """End-to-end, no mock in the middle: a real ``MemoryStore``, a real
    tier failure, the real route.

    ``last_search_degradations`` was reported in a dead-code audit as
    "written with only test readers". It is not: ``search_all`` in
    ``memory/context_builder.py`` writes it and ``GET /api/memory/search``
    returns it as ``degradations`` / ``degraded``. The tests above mock
    ``search_all``, so they pin the read but not the write. This one
    exercises both halves and is the reason the field was kept."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from fastapi.testclient import TestClient

    from memory.store import MemoryStore

    store = MemoryStore(db_path=str(tmp_path / "degraded.db"))
    boom = AsyncMock(side_effect=RuntimeError("EmbeddingDimensionMismatch: 1536 != 384"))
    store.episode_search_hybrid = boom

    mock = MagicMock()
    mock.memory = store
    with patch("api.state.state", mock), patch("api.routes.memory.state", mock):
        from api.server import app

        client = TestClient(app, raise_server_exceptions=False)
        body = client.get("/api/memory/search", params={"q": "quokka"}).json()

    assert body["degraded"] is True, body
    assert [d["tier"] for d in body["degradations"]] == ["episode"], body
    assert "EmbeddingDimensionMismatch" in body["degradations"][0]["error"], body


# ─────────────────────────────────────────────
# 4 + 5. Hardware invoke + device lookup
# ─────────────────────────────────────────────

@pytest.fixture()
def hardware_client():
    mock = MagicMock()
    mesh = MagicMock()
    mesh.invoke = AsyncMock(return_value={"success": True, "result": {"lat": 1}})
    mock.hardware_mesh = mesh
    registry = MagicMock()
    registry.get_device.return_value = None
    mock.device_registry = registry

    with patch("api.state.state", mock), patch("api.routes.security_and_hardware.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), mesh


def test_invoke_uses_the_canonical_wire_keys(hardware_client):
    client, mesh = hardware_client
    r = client.post("/api/hardware/invoke", json={
        "node_id": "spd-phone-01", "command": "gps_location", "params": {"accuracy": "high"},
    })
    assert r.status_code == 200
    assert mesh.invoke.await_args.kwargs["node_id"] == "spd-phone-01"
    assert mesh.invoke.await_args.kwargs["command"] == "gps_location"
    assert mesh.invoke.await_args.kwargs["params"] == {"accuracy": "high"}


def test_invoke_accepts_the_v2_clients_pre_fix_key_names(hardware_client):
    """An older client sending device_id/method/args must stop silently failing."""
    client, mesh = hardware_client
    client.post("/api/hardware/invoke", json={
        "device_id": "spd-phone-01", "method": "buzz", "args": {"ms": 200},
    })
    assert mesh.invoke.await_args.kwargs["node_id"] == "spd-phone-01"
    assert mesh.invoke.await_args.kwargs["command"] == "buzz"
    assert mesh.invoke.await_args.kwargs["params"] == {"ms": 200}


def test_invoke_with_no_node_id_is_a_400_not_a_not_connected_lie(hardware_client):
    client, mesh = hardware_client
    r = client.post("/api/hardware/invoke", json={"command": "buzz"})
    assert r.status_code == 400
    assert "node_id" in str(r.json()["detail"])
    mesh.invoke.assert_not_awaited()


def test_invoke_with_no_command_is_a_400(hardware_client):
    client, mesh = hardware_client
    assert client.post("/api/hardware/invoke", json={"node_id": "n"}).status_code == 400
    mesh.invoke.assert_not_awaited()


def test_unknown_hardware_device_is_a_404(hardware_client):
    client, _ = hardware_client
    r = client.get("/api/hardware/device/not-a-real-device")
    assert r.status_code == 404, (
        "a 200 asserts the body IS the device, so a client that sets the "
        "response as its detail state wipes the row it already had"
    )


# ─────────────────────────────────────────────
# 6. Mesh rows state their liveness
# ─────────────────────────────────────────────

def test_connected_mesh_nodes_say_they_are_online():
    """The v2 mesh card reads `n.online`; an absent key is falsy."""
    from hardware.mesh import HardwareMesh

    mesh = HardwareMesh.__new__(HardwareMesh)
    mesh._node_metadata = {"spd-phone-01": {"node_type": "phone", "platform": "ios"}}
    mesh._daemons = {"spd-phone-01": object()}
    rows = mesh.connected_nodes
    assert rows[0]["online"] is True


def test_mesh_nodes_without_a_socket_are_not_listed():
    from hardware.mesh import HardwareMesh

    mesh = HardwareMesh.__new__(HardwareMesh)
    mesh._node_metadata = {"gone-01": {"node_type": "phone"}}
    mesh._daemons = {}
    assert mesh.connected_nodes == []
