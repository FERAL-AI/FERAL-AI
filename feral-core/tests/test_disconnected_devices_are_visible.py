"""A device that stops reporting must read as DISCONNECTED, not vanish.

Owner complaint, verbatim: "devices that were connected and then
disconnected still show as connected; the same device appears many
times; ... glasses connected to an iPhone should be shown as a
sub-device of that iPhone rather than as a separate thing."

What was actually true before this module existed, measured against
the live install at ``~/.feral``:

* ``state.daemons`` is popped on WebSocketDisconnect (api/server.py:3533)
  and ``hardware_mesh.on_node_disconnected`` unregisters the node, so
  ``/api/devices/connected`` returns ``{"devices": []}`` the instant a
  phone drops. The node does not read "disconnected" anywhere -- it
  ceases to exist. Absence renders as "you have never owned a device",
  which is why the last thing the owner ever saw was a live green dot.
* ``memory.db.node_subdevices`` holds 7 rows across SIX distinct
  ``feral-iphone-*`` node ids (``SELECT count(*), count(DISTINCT
  node_id) FROM node_subdevices`` -> ``7|6``); 6 of the 7 are
  ``jw_health_glasses``. One physical pair of glasses presented as six.
* When the parent iPhone is offline those peripherals are reachable
  only via ``/api/devices/{node_id}/subdevices`` -- they hang off
  nothing, so nothing could nest them under their phone.

These tests pin the repaired contract. They exercise
``api.device_view`` (the single assembler every surface reads) plus
the three consumers: ``/api/devices/connected``, the
``connected_devices`` tool, and the ``## Connected Hardware`` prompt
block.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from memory.node_subdevices import NodeSubdeviceStore

pytestmark = pytest.mark.no_auto_feral_home


# The six node ids and seven rows below are copied from the owner's
# live ``~/.feral/memory.db``. Keeping the real shape means a fix that
# only works on tidy synthetic data fails here.
REAL_ROWS = [
    ("feral-iphone-6053b3cdc4ed", "jw_health_glasses", "ready", {"battery_pct": 69, "device_name": "W300"}, 5.4),
    ("feral-iphone-2a210fa1", "jw_health_glasses", "ready", {"device_name": "W300"}, 36.4),
    ("feral-iphone-79447a4cd1ed", "jw_health_glasses", "ready", {"battery_pct": 86, "device_name": ""}, 38.1),
    ("feral-iphone-c29f7fd3", "jw_health_glasses", "audio_ready", {"headphone_status": "3"}, 43.2),
    ("feral-iphone-415d9bef", "jw_health_glasses", "ready", {"device_name": "W300"}, 51.1),
    ("feral-iphone-2a210fa1", "veepoo_wristband", "ready", {"device_name": "VITRO"}, 65.4),
    ("feral-iphone-299994e5", "jw_health_glasses", "ready", {"device_name": "W300"}, 90.3),
]


def _seed_real_rows(store: NodeSubdeviceStore, now: float) -> None:
    for node_id, capability, status, attrs, days in REAL_ROWS:
        store.upsert(
            node_id=node_id,
            capability=capability,
            status=status,
            attrs=attrs,
            provenance="ble",
            observed_at=now - days * 86400.0,
        )


class _FakeWebSocket:
    def __init__(self, *, node_type="unknown", capabilities=None, platform="",
                 manufacturer="", model=""):
        self._feral_node_type = node_type
        self._feral_capabilities = list(capabilities or [])
        self._feral_platform = platform
        self._feral_manufacturer = manufacturer
        self._feral_model = model


# ──────────────────────────────────────────────────────────────
# 1. The heartbeat window is one number, stated once
# ──────────────────────────────────────────────────────────────

def test_node_heartbeat_window_is_three_hup_heartbeats():
    """One window, derived from the protocol, not invented per surface.

    HUP_SPEC.md keepalive row: "node_heartbeat every heartbeat_ms
    (default 10000). Brain MAY close ... if 3x interval elapses."
    models/protocol.py NodeAckPayload.heartbeat_ms defaults to 10000.
    30 s is therefore the protocol's own definition of a stale node.
    """
    from models.protocol import NodeAckPayload
    from api.device_view import NODE_HEARTBEAT_WINDOW_S

    spec_default_ms = NodeAckPayload.model_fields["heartbeat_ms"].default
    assert spec_default_ms == 10000
    assert NODE_HEARTBEAT_WINDOW_S == 3 * spec_default_ms / 1000.0


# ──────────────────────────────────────────────────────────────
# 2. Disconnect is visible, not absent
# ──────────────────────────────────────────────────────────────

def test_offline_node_is_reported_as_disconnected_not_omitted(tmp_path):
    from api.device_view import build_device_view

    now = time.time()
    store = NodeSubdeviceStore(db_path=str(tmp_path / "m.db"))
    _seed_real_rows(store, now)

    view = build_device_view(
        live_nodes=[],
        subdevice_rows=store.list_all(now=now),
        now=now,
    )
    assert view["devices"] == [], "no daemon holds a socket, so nothing is live"
    assert view["offline"], (
        "the phone that carried the glasses must still be listed, marked "
        "disconnected. Omitting it is what made a dropped device read as a "
        "device the user never owned."
    )
    for entry in view["offline"]:
        assert entry["connected"] is False
        assert entry["status"] == "disconnected"
        assert entry["last_seen"] is not None
        assert entry["last_seen_age_s"] > 0


def test_live_node_carries_an_explicit_connected_flag(tmp_path):
    from api.device_view import build_device_view

    now = time.time()
    store = NodeSubdeviceStore(db_path=str(tmp_path / "m.db"))
    store.upsert(node_id="feral-iphone-6053b3cdc4ed", capability="jw_health_glasses",
                 status="ready", attrs={"device_name": "W300"}, provenance="ble",
                 observed_at=now)

    view = build_device_view(
        live_nodes=[{"node_id": "feral-iphone-6053b3cdc4ed", "type": "iphone"}],
        subdevice_rows=store.list_all(now=now),
        now=now,
    )
    assert len(view["devices"]) == 1
    assert view["devices"][0]["connected"] is True
    assert view["devices"][0]["status"] == "connected"
    assert view["offline"] == []


# ──────────────────────────────────────────────────────────────
# 3. One physical device, once
# ──────────────────────────────────────────────────────────────

def test_seven_rows_across_six_node_ids_collapse_to_one_phone_two_peripherals(tmp_path):
    """The owner's exact data: 7 rows, 6 node ids, 1 phone, 2 peripherals."""
    from api.device_view import build_device_view

    now = time.time()
    store = NodeSubdeviceStore(db_path=str(tmp_path / "m.db"))
    _seed_real_rows(store, now)
    assert len(store.list_all(now=now)) == 7

    view = build_device_view(live_nodes=[], subdevice_rows=store.list_all(now=now), now=now)
    assert len(view["offline"]) == 1, (
        f"six feral-iphone-* installs are one physical phone; got "
        f"{[e['node_id'] for e in view['offline']]}"
    )
    phone = view["offline"][0]
    assert phone["node_id"] == "feral-iphone-6053b3cdc4ed", "most recent install wins"
    assert sorted(phone["also_known_as"]) == sorted([
        "feral-iphone-299994e5", "feral-iphone-2a210fa1", "feral-iphone-415d9bef",
        "feral-iphone-79447a4cd1ed", "feral-iphone-c29f7fd3",
    ])
    caps = sorted(s["capability"] for s in phone["subdevices"])
    assert caps == ["jw_health_glasses", "veepoo_wristband"], (
        "six jw_health_glasses rows are one pair of glasses"
    )


def test_grouped_peripheral_keeps_every_node_it_was_seen_through(tmp_path):
    """Grouping must not destroy provenance -- the user may need it."""
    from api.device_view import build_device_view

    now = time.time()
    store = NodeSubdeviceStore(db_path=str(tmp_path / "m.db"))
    _seed_real_rows(store, now)

    phone = build_device_view(live_nodes=[], subdevice_rows=store.list_all(now=now), now=now)["offline"][0]
    glasses = next(s for s in phone["subdevices"] if s["capability"] == "jw_health_glasses")
    assert glasses["observations"] == 6
    assert glasses["via_node_id"] == "feral-iphone-6053b3cdc4ed"
    assert len(glasses["also_seen_via"]) == 5
    assert glasses["live"] is False
    assert glasses["last_seen_age_s"] > 5 * 86400


def test_two_genuinely_different_units_of_one_capability_do_not_merge(tmp_path):
    from api.device_view import build_device_view

    now = time.time()
    store = NodeSubdeviceStore(db_path=str(tmp_path / "m.db"))
    store.upsert(node_id="feral-iphone-aaaaaaaa", capability="jw_health_glasses",
                 status="ready", attrs={"device_name": "W300"}, provenance="ble",
                 observed_at=now - 60)
    store.upsert(node_id="feral-iphone-bbbbbbbb", capability="jw_health_glasses",
                 status="ready", attrs={"device_name": "W610"}, provenance="ble",
                 observed_at=now - 30)

    phone = build_device_view(live_nodes=[], subdevice_rows=store.list_all(now=now), now=now)["offline"][0]
    names = sorted(s["name"] for s in phone["subdevices"])
    assert names == ["W300", "W610"], "distinct device_name means distinct hardware"


def test_two_live_phones_are_never_collapsed(tmp_path):
    from api.device_view import build_device_view

    now = time.time()
    store = NodeSubdeviceStore(db_path=str(tmp_path / "m.db"))
    view = build_device_view(
        live_nodes=[
            {"node_id": "feral-iphone-aaaaaaaa", "type": "iphone"},
            {"node_id": "feral-iphone-bbbbbbbb", "type": "iphone"},
        ],
        subdevice_rows=store.list_all(now=now),
        now=now,
    )
    assert len(view["devices"]) == 2, "two open sockets are two phones, always"


# ──────────────────────────────────────────────────────────────
# 4. Nesting: peripherals hang off the phone they arrive through
# ──────────────────────────────────────────────────────────────

def test_offline_installs_fold_into_the_live_phone_of_the_same_family(tmp_path):
    from api.device_view import build_device_view

    now = time.time()
    store = NodeSubdeviceStore(db_path=str(tmp_path / "m.db"))
    _seed_real_rows(store, now)
    # The user reinstalled again; the current install is live.
    store.upsert(node_id="feral-iphone-ffffffff", capability="jw_health_glasses",
                 status="ready", attrs={"device_name": "W300"}, provenance="ble",
                 observed_at=now)

    view = build_device_view(
        live_nodes=[{"node_id": "feral-iphone-ffffffff", "type": "iphone"}],
        subdevice_rows=store.list_all(now=now),
        now=now,
    )
    assert len(view["devices"]) == 1
    assert view["offline"] == [], "old installs of the live phone are not separate devices"
    phone = view["devices"][0]
    assert len(phone["also_known_as"]) == 6
    caps = sorted(s["capability"] for s in phone["subdevices"])
    assert caps == ["jw_health_glasses", "veepoo_wristband"]
    glasses = next(s for s in phone["subdevices"] if s["capability"] == "jw_health_glasses")
    assert glasses["live"] is True, "the current install just reported it"


def test_peripherals_are_never_top_level_devices(tmp_path):
    """Glasses connect THROUGH a phone. They must not orbit the brain."""
    from api.device_view import build_device_view

    now = time.time()
    store = NodeSubdeviceStore(db_path=str(tmp_path / "m.db"))
    _seed_real_rows(store, now)
    view = build_device_view(live_nodes=[], subdevice_rows=store.list_all(now=now), now=now)
    top_level_ids = [e["node_id"] for e in view["devices"] + view["offline"]]
    for cap in ("jw_health_glasses", "veepoo_wristband"):
        assert cap not in top_level_ids


# ──────────────────────────────────────────────────────────────
# 5. Reconnect honesty
# ──────────────────────────────────────────────────────────────

def test_offline_entry_states_the_brain_cannot_initiate_reconnection(tmp_path):
    """No button that does nothing. The brain has no outbound channel to
    a node that is not holding a WebSocket: there is no APNs/FCM token
    registered (``~/.feral/data/push_tokens.db`` does not exist on the
    owner's install) and the pairing transport is phone-initiated.
    """
    from api.device_view import build_device_view

    now = time.time()
    store = NodeSubdeviceStore(db_path=str(tmp_path / "m.db"))
    _seed_real_rows(store, now)
    phone = build_device_view(live_nodes=[], subdevice_rows=store.list_all(now=now), now=now)["offline"][0]
    assert phone["reconnect"]["brain_can_initiate"] is False
    assert phone["reconnect"]["steps"], "say what the user must do instead"


# ──────────────────────────────────────────────────────────────
# 6. /api/devices/connected
# ──────────────────────────────────────────────────────────────

@pytest.fixture()
def api(tmp_path):
    now = time.time()
    mock = MagicMock()
    mock.session_handoff = None
    mock.skill_executor = None
    mock.node_subdevices = NodeSubdeviceStore(db_path=str(tmp_path / "memory.db"))
    _seed_real_rows(mock.node_subdevices, now)
    mock.daemons = {}
    with patch("api.state.state", mock), patch("api.routes.devices.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False)


def test_api_devices_connected_reports_the_offline_phone(api):
    body = api.get("/api/devices/connected").json()
    assert body["devices"] == [], "live-daemon contract is unchanged"
    assert body["heartbeat_window_s"] == 30.0
    assert len(body["offline"]) == 1
    phone = body["offline"][0]
    assert phone["status"] == "disconnected"
    assert phone["type"] == "iphone", (
        "a phone must present as a phone; feral-iphone-* is this SDK's own prefix"
    )
    assert sorted(s["capability"] for s in phone["subdevices"]) == [
        "jw_health_glasses", "veepoo_wristband",
    ]


def test_api_devices_connected_live_row_declares_connected(api):
    from api.routes import devices as devices_route
    devices_route.state.daemons = {
        "feral-iphone-6053b3cdc4ed": _FakeWebSocket(node_type="iphone", platform="iOS"),
    }
    body = api.get("/api/devices/connected").json()
    assert len(body["devices"]) == 1
    assert body["devices"][0]["connected"] is True
    assert body["offline"] == []


# ──────────────────────────────────────────────────────────────
# 7. The connected_devices tool
# ──────────────────────────────────────────────────────────────

def test_connected_devices_tool_reports_the_disconnected_phone(tmp_path):
    import asyncio
    from skills.impl.self_introspection import SelfIntrospectionSkill

    now = time.time()
    store = NodeSubdeviceStore(db_path=str(tmp_path / "m.db"))
    _seed_real_rows(store, now)

    fake = MagicMock()
    fake.node_subdevices = store
    fake.device_registry.list_devices.return_value = []

    with patch.object(SelfIntrospectionSkill, "_state", staticmethod(lambda: fake)):
        result = asyncio.run(SelfIntrospectionSkill().execute("connected_devices", {}, {}))

    data = result["data"]
    assert data["heartbeat_window_s"] == 30.0
    offline = data.get("offline") or []
    assert len(offline) == 1, (
        "the tool the model reads must know the phone dropped; before this "
        "fix it read state.device_registry only, which is emptied on "
        "disconnect, so the model answered 'nothing is connected'"
    )
    assert offline[0]["status"] == "disconnected"
    assert sorted(s["capability"] for s in offline[0]["subdevices"]) == [
        "jw_health_glasses", "veepoo_wristband",
    ]


# ──────────────────────────────────────────────────────────────
# 8. The ## Connected Hardware prompt block
# ──────────────────────────────────────────────────────────────

def test_prompt_block_groups_under_the_phone_and_says_disconnected(tmp_path):
    from agents.identity_loader import IdentityLoader

    now = time.time()
    store = NodeSubdeviceStore(db_path=str(tmp_path / "m.db"))
    _seed_real_rows(store, now)

    loader = IdentityLoader.__new__(IdentityLoader)
    loader.subdevice_store = store
    block = loader._build_connected_hardware_section()

    assert "## Connected Hardware" in block
    assert block.count("jw_health_glasses") == 1, (
        "six rows for one pair of glasses told the model the user owns six "
        f"pairs. Block was:\n{block}"
    )
    assert "feral-iphone-6053b3cdc4ed" in block
    assert "disconnected" in block.lower()
    # Peripherals must be presented as hanging off the phone, indented
    # under it, not as a flat list of seven unrelated things.
    lines = [ln for ln in block.splitlines() if ln.strip().startswith("-")]
    assert any(ln.startswith("  -") for ln in lines), (
        f"no nested peripheral lines; block was:\n{block}"
    )
