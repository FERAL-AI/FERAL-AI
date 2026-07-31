"""An unset NODE_API_KEY must mean "refuse", never "allow everyone".

The `/v1/node` gate used to be a single comparison::

    if paired_device_id is None and credential != NODE_API_KEY:

With `NODE_API_KEY` defaulting to `""` (api/server.py) and a node supplying
no credential, that reduced to `"" != ""` — False — so the connection was
ADMITTED. An auditor registered a node called `attacker-node` with no
credential at all and received a full `node_ack` with granted capabilities.
From there a node can inject `text_command` (LLM spend), `telemetry` and
`device_event` (poisons baselines and health answers), and `device_announce`
(writes the knowledge graph).

`brain_bind_host()` defaults to 127.0.0.1, which limits the blast radius,
but the setup wizard's LAN profile and docker-compose.yml both produce a
LAN-exposed bind with an empty key.

These tests pin both halves of the fix: unauthenticated connections are
refused when no key is configured, AND legitimately paired devices still
connect in exactly that configuration.
"""

from __future__ import annotations

import logging
import sys
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from security.device_pairing import DevicePairingStore

from tests import test_server_websocket as ws_harness


pytestmark = pytest.mark.no_auto_feral_home


@contextmanager
def _node_client(tmp_path, node_api_key: str):
    """TestClient for /v1/node with a real pairing store and a chosen key."""
    store = DevicePairingStore(db_path=str(tmp_path / "pair.db"))
    mock = ws_harness._make_ws_mock_state()
    mock.device_pairing_store = store

    if "api.server" in sys.modules:
        del sys.modules["api.server"]
    with ExitStack() as stack:
        for patcher in ws_harness._brain_patchers(mock):
            stack.enter_context(patcher)
        stack.enter_context(patch("api.server.NODE_API_KEY", node_api_key))
        from api.server import app

        yield TestClient(app, raise_server_exceptions=False), store


def _assert_refused(ws, node_id: str = "probe-node") -> None:
    """Assert the socket was refused rather than admitted.

    Deliberately sends a `node_register` first and asserts on the reply.
    Waiting on a bare `ws.receive()` would block forever against the
    pre-fix server (which admitted the connection and sent nothing), so the
    regression would only show up as a test-suite timeout. Registering makes
    the pre-fix behaviour answer immediately with a `node_ack` — which is
    exactly the bug, reported as a fast, legible assertion failure.
    """
    ws.send_json(
        {
            "type": "node_register",
            "payload": {
                "node_id": node_id,
                "node_type": "robot",
                "platform": "linux",
                "capabilities": ["telemetry", "robot_move"],
            },
        }
    )
    msg = ws.receive()
    body = str(msg.get("text") or msg.get("bytes") or "")
    assert "node_ack" not in body, f"connection was ADMITTED, not refused: {msg}"
    assert msg.get("type") == "websocket.close" or msg.get("code") == 4003, msg


def _register_and_expect_ack(ws, node_id: str) -> dict:
    ws.send_json(
        {
            "type": "node_register",
            "payload": {
                "node_id": node_id,
                "node_type": "robot",
                "platform": "linux",
                "capabilities": ["telemetry", "robot_move"],
            },
        }
    )
    ack = ws.receive_json()
    assert ack["type"] == "node_ack"
    assert ack["payload"]["node_id"] == node_id
    return ack


# ── The bypass ──────────────────────────────────────────────────


def test_no_credential_is_refused_when_no_key_configured(tmp_path):
    """The exact auditor repro: no credential, no configured key."""
    with _node_client(tmp_path, "") as (client, _store):
        with client.websocket_connect("/v1/node") as ws:
            _assert_refused(ws)


def test_attacker_node_cannot_register_when_no_key_configured(tmp_path):
    """Registration must not succeed — no node_ack, no granted capabilities."""
    with _node_client(tmp_path, "") as (client, _store):
        with client.websocket_connect("/v1/node") as ws:
            ws.send_json(
                {
                    "type": "node_register",
                    "payload": {
                        "node_id": "attacker-node",
                        "node_type": "robot",
                        "platform": "linux",
                        "capabilities": ["telemetry", "robot_move"],
                    },
                }
            )
            # The gate closes before any frame is read, so the first thing
            # back must be the close — not a node_ack.
            first = ws.receive()
            assert first.get("type") == "websocket.close", first
            assert "node_ack" not in str(
                first.get("text") or first.get("bytes") or ""
            ), first


def test_empty_string_credential_is_refused_when_no_key_configured(tmp_path):
    """`"" == NODE_API_KEY` must not be treated as a valid credential."""
    with _node_client(tmp_path, "") as (client, _store):
        with client.websocket_connect(
            "/v1/node", headers={"x-api-key": ""}
        ) as ws:
            _assert_refused(ws)


def test_arbitrary_credential_is_refused_when_no_key_configured(tmp_path):
    with _node_client(tmp_path, "") as (client, _store):
        with client.websocket_connect(
            "/v1/node", headers={"authorization": "Bearer anything-at-all"}
        ) as ws:
            _assert_refused(ws)


def test_refusal_tells_the_operator_how_to_configure_a_key(tmp_path, caplog):
    caplog.set_level(logging.ERROR, logger="feral.brain")
    with _node_client(tmp_path, "") as (client, _store):
        with client.websocket_connect("/v1/node") as ws:
            _assert_refused(ws)

    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        "feral.security.node_api_key_unset" in m for m in messages
    ), messages
    assert any("NODE_API_KEY" in m and "config.yaml" in m for m in messages), messages


# ── …without breaking anyone legitimate ─────────────────────────


def test_paired_device_still_connects_when_no_key_configured(tmp_path):
    """The other half of the fix: pairing-token auth is untouched.

    A device that completed pairing must keep working with NODE_API_KEY
    unset — that is the normal local-first configuration.
    """
    with _node_client(tmp_path, "") as (client, store):
        issued = store.pair_device("phone-legit", kind="browser")
        with client.websocket_connect(
            "/v1/node",
            headers={"authorization": f"Bearer {issued['token']}"},
        ) as ws:
            _register_and_expect_ack(ws, "legit-paired-node")


def test_paired_device_via_subprotocol_still_connects_when_no_key(tmp_path):
    with _node_client(tmp_path, "") as (client, store):
        issued = store.pair_device("phone-legit-2", kind="browser_node_v2")
        with client.websocket_connect(
            "/v1/node",
            subprotocols=[f"feral-token-{issued['phone_bearer']}"],
        ) as ws:
            _register_and_expect_ack(ws, "legit-subprotocol-node")


def test_configured_key_still_admits_matching_credential(tmp_path):
    """Regression guard for the legacy shared-key path."""
    with _node_client(tmp_path, "configured-node-key") as (client, _store):
        with client.websocket_connect(
            "/v1/node", headers={"x-api-key": "configured-node-key"}
        ) as ws:
            _register_and_expect_ack(ws, "legacy-key-node")


def test_configured_key_still_refuses_missing_credential(tmp_path):
    with _node_client(tmp_path, "configured-node-key") as (client, _store):
        with client.websocket_connect("/v1/node") as ws:
            _assert_refused(ws)
