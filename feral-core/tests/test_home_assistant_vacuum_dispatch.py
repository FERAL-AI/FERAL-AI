"""
W11 (V1.0 cut-list #11 → S5 thesis) — vacuum_* round-trip is exposed
via the smart_home manifest AND wired into the HomeAssistant dispatch
table.

Pre-v2026.5.43 the dispatch handlers (``vacuum_start``,
``vacuum_stop``, ``vacuum_return_to_base``) existed in the integration
backend (added in v2026.5.38) but the skill manifest at
``skills/manifests/smart_home.json`` never declared them, so the LLM
tool-planner could not see them. The manifest now ships the three
vacuum endpoints with ``entity_id`` as the lone required param,
matching the backend signature.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ──────────────────────────────────────────────────────────────────────
# Manifest contract
# ──────────────────────────────────────────────────────────────────────


MANIFEST_PATH = ROOT / "skills" / "manifests" / "smart_home.json"


def _manifest():
    return json.loads(MANIFEST_PATH.read_text())


def test_smart_home_manifest_includes_vacuum_endpoints():
    endpoint_names = {ep["id"] for ep in _manifest()["endpoints"]}
    assert "vacuum_start" in endpoint_names
    assert "vacuum_stop" in endpoint_names
    assert "vacuum_return_to_base" in endpoint_names


@pytest.mark.parametrize("endpoint_id", ["vacuum_start", "vacuum_stop", "vacuum_return_to_base"])
def test_vacuum_endpoints_declare_entity_id_param(endpoint_id):
    endpoint = next(
        ep for ep in _manifest()["endpoints"] if ep["id"] == endpoint_id
    )
    param_names = {p["name"] for p in endpoint.get("params", [])}
    assert "entity_id" in param_names
    entity_id_param = next(
        p for p in endpoint["params"] if p["name"] == "entity_id"
    )
    assert entity_id_param.get("required") is True
    assert entity_id_param.get("type") == "string"


# ──────────────────────────────────────────────────────────────────────
# Dispatch round-trip — backend returns the wrapped structured envelope
# ──────────────────────────────────────────────────────────────────────


class _FakeResp:
    status_code = 200
    text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return {}


@pytest.mark.asyncio
async def test_vacuum_stop_dispatch_returns_stopped_envelope(monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "fake-token")
    from integrations.home_assistant import HomeAssistantIntegration

    ha = HomeAssistantIntegration()

    captured = []

    async def fake_post(self, path, json=None, **kwargs):
        captured.append({"path": path, "json": json})
        return _FakeResp()

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)

    result = await ha.execute(
        "vacuum_stop", {"entity_id": "vacuum.living_room"},
    )
    assert result["success"] is True
    assert result["data"] == {
        "stopped": True,
        "entity_id": "vacuum.living_room",
        "service": "vacuum.stop",
    }
    assert captured[-1]["path"] == "/api/services/vacuum/stop"


@pytest.mark.asyncio
async def test_vacuum_return_to_base_dispatch_returns_returning_envelope(monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "fake-token")
    from integrations.home_assistant import HomeAssistantIntegration

    ha = HomeAssistantIntegration()

    captured = []

    async def fake_post(self, path, json=None, **kwargs):
        captured.append({"path": path, "json": json})
        return _FakeResp()

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)

    result = await ha.execute(
        "vacuum_return_to_base", {"entity_id": "vacuum.living_room"},
    )
    assert result["success"] is True
    assert result["data"] == {
        "returning": True,
        "entity_id": "vacuum.living_room",
        "service": "vacuum.return_to_base",
    }
    assert captured[-1]["path"] == "/api/services/vacuum/return_to_base"


# ──────────────────────────────────────────────────────────────────────
# Manifest ↔ dispatch contract gate
# ──────────────────────────────────────────────────────────────────────


def test_manifest_dispatch_contract_clean_for_vacuum_endpoints():
    """Lane 05's ToolDispatchValidator walks every manifest endpoint
    and asserts the backend dispatch table accepts its params.
    Confirm the three vacuum endpoints round-trip cleanly."""
    from agents.tool_dispatch_validator import ToolDispatchValidator

    validator = ToolDispatchValidator()
    for endpoint_id in ("vacuum_start", "vacuum_stop", "vacuum_return_to_base"):
        violations = validator.contract_violations("smart_home_hue", endpoint_id)
        assert violations == [], (
            f"smart_home_hue.{endpoint_id} contract violations: {violations}"
        )
