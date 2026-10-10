"""Bounded typed registration and actual authenticated ASGI websocket ingress."""

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from unittest.mock import patch
import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient
from hardware.command_contract import CommandLedger
from hardware.mesh import HardwareMesh
from hardware.protocol import DeviceRegistry
from models.protocol import NodeRegisterPayload, hup_frame
from security.capability_grants import CapabilityGrantStore
from security.device_pairing import DevicePairingStore
from tests.test_reviewed_hardware_dispatch import Policy
from tests import test_server_websocket as harness

pytestmark = pytest.mark.no_auto_feral_home


def manifest():
    return {
        "device_id": "disposable-test-sensor",
        "device_type": "sensor",
        "name": "Disposable Test Sensor — synthetic fixture",
        "connection_type": "websocket",
        "capabilities": [
            {
                "id": "temperature",
                "name": "Read synthetic temperature",
                "description": "Fixed synthetic reading; no physical sensor",
                "category": "sensor",
                "permission_tier": "passive",
                "requires_confirmation": False,
                "action_type": "read",
                "parameters": [],
            }
        ],
    }


def test_typed_full_manifest_retained_and_legacy_none_preserved():
    item = NodeRegisterPayload(
        node_id="disposable-test-sensor", node_type="sensor", device_manifest=manifest()
    )
    assert item.device_manifest["name"] == manifest()["name"]
    assert item.device_manifest["capabilities"][0]["permission_tier"] == "passive"
    assert (
        NodeRegisterPayload(
            node_id="old-node", node_type="desktop", capabilities=["system.run"]
        ).device_manifest
        is None
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign",
        "string",
        "missing",
        "oversized",
        "caps",
        "duplicate",
        "number-bool",
        "unknown",
        "transport",
        "nan",
        "parameters",
    ],
)
def test_untrusted_manifest_is_strict_bounded_and_owner_matched(mutation):
    value = deepcopy(manifest())
    if mutation == "foreign":
        value["device_id"] = "other-node"
    elif mutation == "string":
        value = "serialized fallback"
    elif mutation == "missing":
        value.pop("name")
    elif mutation == "oversized":
        value["name"] = "x" * 65537
    elif mutation == "caps":
        value["capabilities"] *= 129
    elif mutation == "duplicate":
        value["capabilities"] *= 2
    elif mutation == "number-bool":
        value["capabilities"][0]["requires_confirmation"] = 0
    elif mutation == "unknown":
        value["actions"] = []
    elif mutation == "transport":
        value["connection_type"] = "ble"
    elif mutation == "nan":
        value["capabilities"][0]["returns"] = {"value": float("nan")}
    elif mutation == "parameters":
        value["capabilities"][0]["parameters"] = [{}] * 65
    with pytest.raises(ValidationError):
        NodeRegisterPayload(
            node_id="disposable-test-sensor", node_type="sensor", device_manifest=value
        )


@contextmanager
def actual_client(tmp_path):
    mock = harness._make_ws_mock_state()
    mock.primary_session_id = "fixture-primary"
    mock._native_bootstrap_required = False
    mock.device_pairing_store = DevicePairingStore(db_path=str(tmp_path / "pair.db"))
    mock.hardware_mesh = HardwareMesh(
        DeviceRegistry(),
        mock.daemons,
        ledger=CommandLedger(str(tmp_path / "ledger.db")),
    )
    grants = CapabilityGrantStore(str(tmp_path / "grants.db"))
    mock.hardware_mesh.reviewed_controller(
        policy=lambda: Policy(), grants=lambda: grants
    )
    harness._reimport_api_server_before_patching()
    with ExitStack() as stack:
        for patcher in harness._brain_patchers(mock):
            stack.enter_context(patcher)
        stack.enter_context(patch("api.server.NODE_API_KEY", "isolated-node-key"))
        stack.enter_context(patch("api.routes.hardware_reviewed.state", mock))
        from api.server import app

        yield (
            TestClient(
                app,
                raise_server_exceptions=True,
                base_url="http://127.0.0.1:9090",
                client=("127.0.0.1", 50000),
            ),
            mock,
        )


def test_actual_authenticated_websocket_registration_keeps_passive_manifest(tmp_path):
    with actual_client(tmp_path) as (client, mock):
        with client.websocket_connect(
            "/v1/node", headers={"authorization": "Bearer isolated-node-key"}
        ) as ws:
            ws.send_json(
                hup_frame(
                    "node_register",
                    {
                        "node_id": "disposable-test-sensor",
                        "node_type": "sensor",
                        "platform": "synthetic fixture",
                        "capabilities": ["temperature"],
                        "device_manifest": manifest(),
                    },
                )
            )
            ack = ws.receive_json()
            assert ack["type"] == "node_ack", ack
            actual = mock.hardware_mesh._registry.get_device("disposable-test-sensor")
            assert actual.name == manifest()["name"]
            assert actual.capabilities[0].category == "sensor"
            assert actual.capabilities[0].permission_tier == "passive"
            assert (
                mock.hardware_mesh.reviewed_binding("disposable-test-sensor")
                is not None
            )
            response = client.post(
                "/api/hardware/reviewed/review",
                json={
                    "node_id": "disposable-test-sensor",
                    "command": "temperature",
                    "params": {},
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["review"]["permission_tier"] == "passive"
            assert response.json()["separate_authorization_required"] is False


def test_actual_websocket_malformed_manifest_rejected_without_registration_then_legacy_works(
    tmp_path,
):
    with actual_client(tmp_path) as (client, mock):
        with client.websocket_connect(
            "/v1/node", headers={"authorization": "Bearer isolated-node-key"}
        ) as ws:
            bad = manifest()
            bad["device_id"] = "foreign"
            ws.send_json(
                hup_frame(
                    "node_register",
                    {
                        "node_id": "disposable-test-sensor",
                        "node_type": "sensor",
                        "capabilities": ["temperature"],
                        "device_manifest": bad,
                    },
                )
            )
            error = ws.receive_json()
            assert error["type"] == "error" and error["payload"]["code"] == 1003
            assert (
                mock.hardware_mesh._registry.get_device("disposable-test-sensor")
                is None
            )
            ws.send_json(
                hup_frame(
                    "node_register",
                    {
                        "node_id": "legacy-node",
                        "node_type": "desktop",
                        "capabilities": ["system.run"],
                    },
                )
            )
            ack = ws.receive_json()
            assert ack["type"] == "node_ack", ack
            actual = mock.hardware_mesh._registry.get_device("legacy-node")
            assert (
                actual.capabilities[0].category == "compute"
                and actual.capabilities[0].permission_tier == "active"
            )


def test_normalized_manifest_stays_within_network_bound():
    import json

    value = manifest()
    value["capabilities"] = [
        {
            **value["capabilities"][0],
            "id": "temperature_" + str(i),
            "description": "x" * 300,
        }
        for i in range(128)
    ]
    assert len(json.dumps(value, separators=(",", ":")).encode()) < 65536
    with pytest.raises(ValidationError, match="normalized device_manifest"):
        NodeRegisterPayload(
            node_id="disposable-test-sensor", node_type="sensor", device_manifest=value
        )
