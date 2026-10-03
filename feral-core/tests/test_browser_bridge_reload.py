"""Actual BrainState browser bridge contracts with an inert browser fixture."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from skills.base import BaseSkill
from skills.impl import get_implementation, register_instance
from skills.registry import SkillRegistry


class ControlledBrowser:
    """No browser process, socket, account or model is started."""

    def __init__(self, result, *, connected=True, initialize_ok=True):
        self.result = result
        self.connected = connected
        self.initialize_ok = initialize_ok
        self.initializations = 0
        self.calls = []

    async def initialize(self):
        self.initializations += 1
        self.connected = self.initialize_ok
        return self.initialize_ok

    async def execute(self, endpoint_id, args):
        self.calls.append((endpoint_id, args))
        return self.result


def bind(browser):
    from api.state import BrainState
    # Use the actual production class and binding/dispatch methods without its
    # full subsystem constructor, which starts unrelated bootstrap services.
    state = BrainState.__new__(BrainState)
    state.browser = browser
    state.skill_registry = SkillRegistry()
    state.skill_registry.load_builtin_skills()
    state._register_browser_skill()
    bridge = state.skill_registry.get_skill("browser")
    assert isinstance(bridge, BaseSkill) and bridge.skill_id == "browser"
    assert inspect.iscoroutinefunction(bridge.execute)
    assert bridge is get_implementation("browser")
    return state, bridge


@pytest.mark.asyncio
@pytest.mark.parametrize("result,success,error", [
    ({"tabs": ["controlled"]}, True, None),
    ({"success": False, "error": "controlled browser failure"}, False, "controlled browser failure"),
])
async def test_actual_brain_state_bridge_dispatch_preserves_envelope(result, success, error):
    browser = ControlledBrowser(result)
    state, bridge = bind(browser)
    observed = await bridge.execute("get_tabs", {}, {})
    assert observed == {"success": success, "status_code": 200 if success else 500,
                        "data": result, "error": error}
    assert bridge._state is state and browser.calls == [("get_tabs", {})]
    assert browser.initializations == 0


@pytest.mark.asyncio
async def test_disconnected_controlled_browser_failure_is_not_backing_unavailability():
    browser = ControlledBrowser({}, connected=False, initialize_ok=False)
    state, bridge = bind(browser)
    result = await bridge.execute("get_tabs", {}, {})
    assert not result["success"] and result["status_code"] == 500
    assert "Cannot connect to Chrome" in result["error"]
    assert browser.initializations == 1 and browser.calls == []
    assert state.skill_registry.get_skill("browser") is bridge


@pytest.mark.asyncio
async def test_actual_shipped_reload_retains_exact_controller_bridge_and_discloses_inventory(monkeypatch):
    from api.routes import skills as routes
    browser = ControlledBrowser({"success": True, "tabs": []})
    state, bridge = bind(browser)
    manifest = state.skill_registry.skills["browser"]
    old_terms = manifest.model_dump_json()
    generation = state.skill_registry.generation
    monkeypatch.setattr(routes, "state", state)
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.post("/api/skills/reload", params={"skill_id": "browser"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "skill_id": "browser",
                               "refresh_kind": "manifest_inventory", "implementation_ready": True}
    assert state.skill_registry.generation == generation + 1
    assert state.skill_registry.skills["browser"].model_dump_json() == old_terms
    assert state.skill_registry.get_skill("browser") is bridge
    assert bridge._state is state and state.browser is browser
    assert browser.calls == [] and browser.initializations == 0
    # A readiness marker for the Python wrapper has performed no browser,
    # account or transport verification. Actual dispatch still uses the same
    # injected controller after reload.
    result = await bridge.execute("get_tabs", {}, {})
    assert result["success"] and browser.calls == [("get_tabs", {})]


def test_actual_web_actions_injection_shares_the_controlled_browser():
    class SharingFixture(BaseSkill):
        def __init__(self):
            super().__init__("web_actions")
            self.browser = None
        def set_browser(self, browser):
            self.browser = browser
    injected = SharingFixture()
    register_instance("web_actions", injected)
    browser = ControlledBrowser({})
    state, bridge = bind(browser)
    assert injected.browser is browser and bridge._state is state


@pytest.mark.asyncio
async def test_unbound_shipped_inventory_is_distinct_from_connected_browser_readiness(monkeypatch):
    from api.routes import skills as routes
    from skills.impl import SKILL_IMPLEMENTATIONS
    monkeypatch.delitem(SKILL_IMPLEMENTATIONS, "browser", raising=False)
    registry = SkillRegistry()
    monkeypatch.setattr(routes, "state", SimpleNamespace(skill_registry=registry))
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.post("/api/skills/reload", params={"skill_id": "browser"})
    assert response.status_code == 200 and response.json()["refresh_kind"] == "manifest_inventory"
    assert response.json()["implementation_ready"] is False
    assert registry.get_skill("browser") is None
