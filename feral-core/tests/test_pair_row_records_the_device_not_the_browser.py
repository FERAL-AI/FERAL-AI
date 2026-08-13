"""A paired device must present as what it IS, not as the transport
that happened to carry its pairing token.

Owner complaint: "a phone shows up as a browser connection which he
says he never made and finds confusing".

Measured on the owner's live ``~/.feral/paired_devices.db``:

    sqlite> SELECT kind, count(*) FROM paired_devices GROUP BY kind;
    browser|61
    sqlite> SELECT count(*), sum(claimed_at IS NOT NULL), sum(node_id != '')
            FROM paired_devices;
    61|18|0

Every single row is ``kind='browser'`` with an EMPTY ``node_id``. Zero
hardware has ever been recorded as paired. Two separate defects
produced that:

1. ``GET /api/devices/pair/url`` and ``GET /api/devices/pair/qr`` both
   call ``store.pair_device(name, kind="browser")`` unconditionally.
   ``kind`` is stamped at TOKEN-ISSUE time, when the brain cannot
   possibly know what will claim the token -- so merely opening the
   pair modal mints a row that claims a browser was paired. 43 of the
   61 rows were never claimed by anything at all.
2. ``POST /api/devices/pair/complete`` receives the claimant's own
   declaration and throws it away: ``mark_claimed`` only writes
   ``claimed_at`` and ``last_seen``. An iPhone opening the /pair page
   sends ``kind: "browser_node_v2"`` -- a transport name -- and the
   row keeps saying "browser" forever.

These tests pin: the token row says "pending" until something claims
it, and the claim records what the claimant actually is.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from security.device_pairing import DevicePairingStore

pytestmark = pytest.mark.no_auto_feral_home


IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
)
MAC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


@pytest.fixture()
def store(tmp_path):
    return DevicePairingStore(db_path=str(tmp_path / "paired.db"))


# ──────────────────────────────────────────────────────────────
# 1. Issuing a token does not assert that a browser was paired
# ──────────────────────────────────────────────────────────────

def test_pair_url_mints_a_pending_row_not_a_browser(tmp_path, store):
    mock = MagicMock()
    mock.device_pairing_store = store
    # brain_id + access_pairing_mode land in the base64 QR blob and must
    # be JSON-serialisable; a bare MagicMock attribute is not.
    mock.config.brain_id = "brain-test"
    mock.config.access_pairing_mode = "lan"
    with patch("api.state.state", mock), patch("api.routes.devices.state", mock), \
         patch("api.routes.devices._resolve_pair_origin", return_value="http://192.168.1.9:9090"):
        from api.server import app
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/devices/pair/url")
    assert r.status_code == 200

    rows = store.list_devices(include_unclaimed=True)
    assert len(rows) == 1
    assert rows[0]["kind"] == "pending", (
        "the brain cannot know what will scan the QR. Stamping 'browser' at "
        "issue time is what put 61 browsers in the owner's device list."
    )


def test_pair_token_rows_are_labelled_as_tokens_not_devices(store):
    """An unclaimed token is not a device and must not be counted as one."""
    from api.device_view import describe_pairing_row

    issued = store.pair_device("unnamed", kind="pending")
    row = next(
        r for r in store.list_devices(include_unclaimed=True)
        if r["device_id"] == issued["device_id"]
    )
    described = describe_pairing_row(row)
    assert described["is_device"] is False
    assert described["label"] == "Pairing code (unclaimed)"


# ──────────────────────────────────────────────────────────────
# 2. The claim records what actually claimed it
# ──────────────────────────────────────────────────────────────

def test_claim_records_the_iphone_as_a_phone(store):
    issued = store.pair_device("unnamed", kind="pending")
    device_id = store.mark_claimed(
        issued["token"],
        kind="browser_node_v2",
        platform=IPHONE_UA,
        node_id="feral-iphone-6053b3cdc4ed",
    )
    assert device_id == issued["device_id"]

    row = next(r for r in store.list_devices() if r["device_id"] == device_id)
    assert row["kind"] == "phone", (
        f"an iPhone claimed the token and the row still says {row['kind']!r}"
    )
    assert row["node_id"] == "feral-iphone-6053b3cdc4ed", (
        "node_id is the join key between paired_devices and the HUP node; "
        "all 61 of the owner's rows have it empty, so no pairing could ever "
        "be matched to a device that connected"
    )


def test_claim_from_a_desktop_browser_still_says_browser(store):
    issued = store.pair_device("unnamed", kind="pending")
    device_id = store.mark_claimed(issued["token"], kind="browser_node_v2", platform=MAC_UA)
    row = next(r for r in store.list_devices() if r["device_id"] == device_id)
    assert row["kind"] == "browser", "a Mac browser genuinely is a browser node"


def test_mark_claimed_keeps_working_with_no_identity(store):
    """Backward compatibility: older clients POST only the token."""
    issued = store.pair_device("legacy", kind="browser")
    assert store.mark_claimed(issued["token"]) == issued["device_id"]
    row = next(r for r in store.list_devices() if r["device_id"] == issued["device_id"])
    assert row["kind"] == "browser", "an unknown claimant must not overwrite a known kind"


def test_pair_complete_endpoint_threads_the_claimant_identity(store):
    mock = MagicMock()
    mock.device_pairing_store = store
    issued = store.pair_device("unnamed", kind="pending")

    with patch("api.state.state", mock), patch("api.routes.devices.state", mock):
        from api.server import app
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/api/devices/pair/complete", json={
            "token": issued["token"],
            "kind": "browser_node_v2",
            "platform": IPHONE_UA,
            "node_id": "feral-iphone-6053b3cdc4ed",
        })
    assert r.status_code == 200, r.text
    row = next(r2 for r2 in store.list_devices() if r2["device_id"] == issued["device_id"])
    assert row["kind"] == "phone"
    assert row["node_id"] == "feral-iphone-6053b3cdc4ed"


# ──────────────────────────────────────────────────────────────
# 3. The device list stops calling everything a browser
# ──────────────────────────────────────────────────────────────

def test_paired_listing_labels_a_claimed_phone_as_a_phone(store):
    mock = MagicMock()
    mock.device_pairing_store = store
    issued = store.pair_device("unnamed", kind="pending")
    store.mark_claimed(issued["token"], kind="browser_node_v2", platform=IPHONE_UA,
                       node_id="feral-iphone-6053b3cdc4ed")

    with patch("api.state.state", mock), patch("api.routes.devices.state", mock):
        from api.server import app
        client = TestClient(app, raise_server_exceptions=False)
        body = client.get("/api/devices/paired").json()

    row = body["devices"][0]
    assert row["kind"] == "phone"
    assert row["is_device"] is True
    assert row["label"] == "iPhone"


def test_legacy_browser_rows_are_preserved_and_explained(store):
    """The owner's 61 existing rows are history and must not be deleted.

    They also must not keep masquerading as devices. A row with
    kind='browser', no node_id, no capabilities and no claim is a
    pairing token that was issued and abandoned -- say so.
    """
    from api.device_view import describe_pairing_row

    legacy_unclaimed = {
        "device_id": "0b28887a-3fa9-4cbf-a207-7aae692fca1c",
        "name": "unnamed", "kind": "browser", "node_id": "",
        "capabilities": [], "claimed_at": None, "last_seen": None,
        "platform": "",
    }
    legacy_claimed = {**legacy_unclaimed, "device_id": "x", "claimed_at": 1.0}

    assert describe_pairing_row(legacy_unclaimed)["is_device"] is False
    assert describe_pairing_row(legacy_unclaimed)["label"] == "Pairing code (unclaimed)"
    assert describe_pairing_row(legacy_claimed)["is_device"] is True
    assert describe_pairing_row(legacy_claimed)["label"] == "Browser"
