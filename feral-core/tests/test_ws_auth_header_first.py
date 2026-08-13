from __future__ import annotations

import json
import logging
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from security.device_pairing import DevicePairingStore

from tests import test_server_websocket as ws_harness


pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def node_client(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    store = DevicePairingStore(db_path=str(tmp_path / "pair.db"))
    mock = ws_harness._make_ws_mock_state()
    mock.device_pairing_store = store

    ws_harness._reimport_api_server_before_patching()
    with ExitStack() as stack:
        for patcher in ws_harness._brain_patchers(mock):
            stack.enter_context(patcher)
        stack.enter_context(patch("api.server.NODE_API_KEY", "expected-node-key"))
        from api.server import app

        client = TestClient(app, raise_server_exceptions=False)
        yield client, store


def _send_register_and_expect_ack(ws, node_id: str):
    ws.send_json(
        {
            "type": "node_register",
            "payload": {
                "node_id": node_id,
                "node_type": "browser_node",
                "platform": "ios-browser",
                "capabilities": ["camera", "mic"],
            },
        }
    )
    ack = ws.receive_json()
    assert ack["type"] == "node_ack"
    assert ack["payload"]["node_id"] == node_id


def test_ws_accepts_authorization_bearer_pair_token(node_client):
    client, store = node_client
    issued = store.pair_device("phone-a", kind="browser")

    with client.websocket_connect(
        "/v1/node",
        headers={"authorization": f"Bearer {issued['token']}"},
    ) as ws:
        _send_register_and_expect_ack(ws, "auth-header-node")


def test_ws_accepts_sec_websocket_protocol_phone_bearer(node_client):
    client, store = node_client
    issued = store.pair_device("phone-b", kind="browser_node_v2")

    with client.websocket_connect(
        "/v1/node",
        subprotocols=[f"feral-token-{issued['phone_bearer']}"],
    ) as ws:
        _send_register_and_expect_ack(ws, "subprotocol-node")


def test_ws_query_auth_is_still_accepted_with_deprecation_warning(node_client, caplog):
    client, store = node_client
    issued = store.pair_device("phone-c", kind="browser")

    caplog.set_level(logging.WARNING, logger="feral.brain")
    with client.websocket_connect(f"/v1/node?api_key={issued['token']}") as ws:
        _send_register_and_expect_ack(ws, "query-auth-node")

    assert any(
        "feral.security.deprecated_query_auth" in rec.getMessage()
        for rec in caplog.records
    )


def test_ws_unknown_bearer_closes_4003(node_client):
    client, _store = node_client

    with client.websocket_connect(
        "/v1/node",
        headers={"authorization": "Bearer not-a-real-bearer"},
    ) as ws:
        # An unauthorized error frame (HUP §8 code 1001) precedes the
        # close, so a client can distinguish a rejected credential from a
        # dropped network and stop reconnecting.
        msg = ws.receive()
        if msg.get("type") == "websocket.send":
            payload = json.loads(msg["text"])["payload"]
            assert payload["code"] == 1001, payload
            assert payload["name"] == "unauthorized", payload
            msg = ws.receive()
        assert msg.get("type") == "websocket.close" or msg.get("code") == 4003
