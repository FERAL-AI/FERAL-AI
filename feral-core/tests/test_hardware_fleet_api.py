"""Tests for the unified /api/hardware/fleet server-driven fleet view."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hardware.protocol import DeviceCapability, DeviceManifest, DeviceRegistry


def _registry() -> DeviceRegistry:
    reg = DeviceRegistry()
    reg.register_device(
        DeviceManifest(
            device_id="cutebot-usb-0",
            device_type="robot",
            name="QtBot (CuteBot)",
            manufacturer="Elecfreaks",
            connection_type="serial",
            capabilities=[
                DeviceCapability(
                    id="drive", name="Manual Drive", description="wheels",
                    category="actuator", permission_tier="dangerous",
                    requires_confirmation=True,
                    parameters=[{"name": "left", "type": "integer"}],
                ),
                DeviceCapability(
                    id="follow_line", name="Follow Line", description="line",
                    category="actuator", permission_tier="active",
                    verify={"via": "read_telemetry", "field": "mode",
                            "expect": ["line_follow"]},
                ),
                DeviceCapability(
                    id="read_telemetry", name="Telemetry", description="snapshot",
                    category="sensor", permission_tier="passive",
                ),
            ],
        )
    )
    reg.record_verification("cutebot-usb-0", {
        "capability": "follow_line", "verified": True, "observed": "line_follow",
    })
    return reg


@pytest.fixture
def client():
    from api.routes.security_and_hardware import router

    app = FastAPI()
    app.include_router(router)

    mock_state = SimpleNamespace(
        device_registry=_registry(),
        hardware_mesh=SimpleNamespace(
            connected_nodes=[{"node_id": "iphone-1", "node_type": "phone"}],
            list_announced_devices=lambda: [{"device_id": "airpods", "name": "AirPods"}],
        ),
        primary_session_id="sess-1",
    )
    with patch("api.routes.security_and_hardware.state", mock_state):
        yield TestClient(app)


def test_fleet_lists_devices_with_safety_tiers(client):
    resp = client.get("/api/hardware/fleet")
    assert resp.status_code == 200
    data = resp.json()
    assert {d["device_id"] for d in data["devices"]} == {"cutebot-usb-0"}
    cute = data["devices"][0]
    caps = {c["id"]: c for c in cute["capabilities"]}
    # Generic safety tiers match what the LLM layer enforces.
    assert caps["drive"]["safety_tier"] == "confirm"
    assert caps["drive"]["requires_approval"] is True
    assert caps["read_telemetry"]["read_only"] is True
    assert caps["follow_line"]["has_verify"] is True


def test_fleet_includes_last_verified_and_mesh(client):
    data = client.get("/api/hardware/fleet").json()
    cute = data["devices"][0]
    assert cute["last_verified"]["verified"] is True
    assert cute["last_verified"]["observed"] == "line_follow"
    assert data["verifications"]["cutebot-usb-0"]["capability"] == "follow_line"
    assert data["mesh"]["nodes"][0]["node_id"] == "iphone-1"
    assert data["mesh"]["announced_devices"][0]["device_id"] == "airpods"


def test_fleet_empty_without_registry():
    from api.routes.security_and_hardware import router

    app = FastAPI()
    app.include_router(router)
    with patch("api.routes.security_and_hardware.state",
               SimpleNamespace(device_registry=None, hardware_mesh=None,
                               primary_session_id=None)):
        c = TestClient(app)
        data = c.get("/api/hardware/fleet").json()
    assert data["devices"] == []
