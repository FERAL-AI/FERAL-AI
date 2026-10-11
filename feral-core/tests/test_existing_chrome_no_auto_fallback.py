"""No implicit browser launch or target substitution after attach failure.

Production controller and connection-manager paths, controlled CDP/Playwright
boundaries only. No Chrome process, real profiles, network, or input is used.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from api.existing_chrome_connection import ChromeConnectionManager
from api.state import BrainState
from skills.call_context import bind_context
from skills.impl.browser_use import BrowserController
from skills.impl.existing_chrome import ExistingChromeController


@pytest.fixture
def disconnected(monkeypatch):
    controller = BrowserController()
    controller._cdp.connect = AsyncMock(return_value=False)
    controller._auto_launch_chrome = AsyncMock(return_value=True)
    monkeypatch.setattr("skills.impl.browser_use.asyncio.sleep", AsyncMock())
    # A mistaken fallback fails without spawning a real browser.
    monkeypatch.setattr("subprocess.Popen", Mock(side_effect=AssertionError("no browser process")))
    return controller


@pytest.mark.asyncio
async def test_unselected_first_use_is_attach_only(disconnected):
    assert await disconnected.initialize() is False
    disconnected._auto_launch_chrome.assert_not_awaited()
    disconnected._cdp.connect.assert_awaited_once_with(prefer_page=True)
    receipt = disconnected.connection_failure
    assert receipt["success"] is False and receipt["browser_launched"] is False
    assert receipt["error_code"] == "browser_connection_unavailable"
    assert "existing Chrome" in receipt["error"] and "macos_ax" in receipt["error"]
    assert "--remote-debugging" not in receipt["error"]
    # Receipts are snapshots; a caller cannot make attachment look successful.
    receipt["success"] = True
    assert disconnected.connection_failure["success"] is False


@pytest.mark.asyncio
async def test_unavailable_receipt_prevents_action_after_failed_attachment(disconnected):
    await disconnected.initialize()
    disconnected.navigate = AsyncMock(return_value={"success": True})
    result = await disconnected.execute("navigate", {"url": "http://localhost/owned"})
    assert result == disconnected.connection_failure
    disconnected.navigate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("launch_isolated", [False, True])
async def test_lost_selected_target_never_reconnects_to_first_tab(disconnected, launch_isolated):
    disconnected._attached_target_id = "selected-tab"
    disconnected._page = Mock()
    assert await disconnected.initialize(launch_isolated=launch_isolated) is False
    disconnected._cdp.connect.assert_awaited_once_with(prefer_page=True, target_id="selected-tab")
    disconnected._auto_launch_chrome.assert_not_awaited()
    assert disconnected._page is None
    assert disconnected.connection_failure["requested_target_id"] == "selected-tab"
    assert disconnected.connection_failure["error_code"] == "browser_target_unavailable"


@pytest.mark.asyncio
async def test_explicit_isolated_policy_preserves_launcher(disconnected, monkeypatch):
    disconnected._cdp.connect = AsyncMock(side_effect=[False, False])
    sleep = AsyncMock()
    monkeypatch.setattr("skills.impl.browser_use.asyncio.sleep", sleep)
    assert await disconnected.initialize(launch_isolated=True) is False
    disconnected._auto_launch_chrome.assert_awaited_once()
    assert disconnected._cdp.connect.await_count == 2
    sleep.assert_awaited_once_with(2.0)
    assert disconnected.connection_failure["browser_launched"] is True


@pytest.mark.asyncio
async def test_explicit_launch_failure_is_truthful(disconnected):
    disconnected._auto_launch_chrome = AsyncMock(return_value=False)
    assert await disconnected.initialize(launch_isolated=True) is False
    disconnected._auto_launch_chrome.assert_awaited_once()
    disconnected._cdp.connect.assert_awaited_once()
    assert disconnected.connection_failure["browser_launched"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["true", 1, None])
async def test_launch_opt_in_is_strict(disconnected, value):
    with pytest.raises(ValueError, match="boolean"):
        await disconnected.initialize(launch_isolated=value)
    disconnected._cdp.connect.assert_not_awaited()
    disconnected._auto_launch_chrome.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_attachment_never_starts_browser(disconnected):
    import asyncio
    disconnected._cdp.connect = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await disconnected.initialize(launch_isolated=True)
    disconnected._auto_launch_chrome.assert_not_awaited()


class SelectedConnector:
    """External connector boundary; actual manager owns installation/rollback."""
    def __init__(self, owner):
        self.owner = owner
        self.connection = str(uuid4())
        self.connected = False
        self.controller = None

    def status(self):
        return {"connected": self.connected, "connection_id": self.connection,
                "owner_session_id": self.owner, "mode": "existing_chrome",
                "selected_target_id": "owned-tab" if self.controller else None}

    async def connect(self, consent):
        assert consent is True
        self.connected = True

    async def targets(self):
        return [{"id": "owned-tab", "label": "Chrome tab 1"}]

    async def select(self, target):
        assert target == "owned-tab"
        self.controller = SimpleNamespace(connected=True)

    async def disconnect(self):
        self.connected = False
        if self.controller:
            self.controller.connected = False
        self.controller = None
        return {"success": True, "browser_closed": False}


@pytest.mark.asyncio
async def test_actual_disconnect_restores_attach_only_generic(disconnected, monkeypatch):
    brain = SimpleNamespace(browser=disconnected,
        orchestrator=SimpleNamespace(tool_runner=SimpleNamespace(browser_resource_scope=lambda: None)))
    manager = ChromeConnectionManager(lambda: brain, factory=SelectedConnector)
    monkeypatch.setattr(manager, "_inject_web", lambda browser: None)
    monkeypatch.setenv("FERAL_TOOL_CALL_CONTEXT", "on")
    with bind_context(session_id="synthetic-owner", surface="http_api", tool_name="browser_connection"):
        receipt = await manager.connect("synthetic-owner", True)
        selected = await manager.select("synthetic-owner", receipt["connection_id"], "owned-tab")
        assert brain.browser is not disconnected
        result = await manager.disconnect("synthetic-owner", selected["connection_id"])
    assert result["browser_closed"] is False and brain.browser is disconnected
    assert await brain.browser.initialize() is False
    disconnected._auto_launch_chrome.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_attach_does_not_launch_or_use_wrong_playwright_page(disconnected, monkeypatch):
    # Playwright's first tab disagrees with the pinned CDP tab. CDP remains
    # usable while the unrelated Playwright page must never become an action target.
    disconnected._attached_target_id = "selected-tab"
    disconnected._cdp._connected = True
    disconnected._cdp._page_ws_url = "ws://localhost/devtools/page/selected-tab"
    disconnected._cdp.connect = AsyncMock(return_value=True)
    disconnected._cdp.send_command = AsyncMock(return_value={})
    disconnected._cdp.add_event_listener = Mock()
    disconnected.restore_cookies = AsyncMock()
    disconnected._playwright_page_for_target = AsyncMock(return_value=None)
    pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(
        return_value=SimpleNamespace(contexts=[SimpleNamespace(pages=[Mock()])]))), stop=AsyncMock())
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: SimpleNamespace(start=AsyncMock(return_value=pw)))
    assert await disconnected.initialize() is True
    disconnected._auto_launch_chrome.assert_not_awaited()
    assert disconnected.connection_failure is None
    assert disconnected._page is None
    disconnected._cdp.connect.assert_awaited_once_with(prefer_page=True, target_id="selected-tab")


@pytest.mark.asyncio
async def test_actual_state_dispatch_returns_actionable_failure(disconnected):
    brain = BrainState.__new__(BrainState)
    brain.browser = disconnected
    disconnected.execute = AsyncMock()
    result = await brain._dispatch_browser_action("navigate", {"url": "http://localhost/owned"})
    assert result == disconnected.connection_failure
    assert result["success"] is False and "macos_ax" in result["error"]
    disconnected.execute.assert_not_awaited()
    disconnected._auto_launch_chrome.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_existing_selection_returns_receipt_through_actual_state():
    cdp = SimpleNamespace(connected=False, target_id="owned-tab",
        _binding={"owner_session_id": "owner", "connection_id": str(uuid4()), "target_id": "owned-tab"})
    browser = ExistingChromeController(cdp)
    browser._auto_launch_chrome = AsyncMock()
    brain = BrainState.__new__(BrainState)
    brain.browser = browser
    result = await brain._dispatch_browser_action("snapshot", {})
    assert result["success"] is False and result["browser_launched"] is False
    assert result["error_code"] == "existing_chrome_connection_changed"
    assert result["requested_target_id"] == "owned-tab"
    browser._auto_launch_chrome.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, Mock(), {"success": True}])
async def test_state_legacy_mocks_keep_existing_failure_fallback(failure):
    brain = BrainState.__new__(BrainState)
    brain.browser = SimpleNamespace(connected=False, initialize=AsyncMock(return_value=False),
                                   connection_failure=failure)
    result = await brain._dispatch_browser_action("snapshot", {})
    assert result["success"] is False and "Cannot connect to Chrome" in result["error"]


@pytest.mark.asyncio
async def test_registered_init_route_returns_actionable_failure(disconnected, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from api.routes import marketplace_browser as routes
    monkeypatch.setattr(routes.state, "browser", disconnected)
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post("/api/browser/init")
    assert response.status_code == 200
    assert response.json() == disconnected.connection_failure
    disconnected._auto_launch_chrome.assert_not_awaited()


@pytest.mark.asyncio
async def test_registered_init_route_legacy_mock_keeps_connected_false(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from api.routes import marketplace_browser as routes
    # An unconfigured Mock attribute is truthy; it is not a failure receipt.
    browser = Mock(connected=False)
    browser.initialize = AsyncMock(return_value=False)
    monkeypatch.setattr(routes.state, "browser", browser)
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post("/api/browser/init")
    assert response.json() == {"connected": False}
