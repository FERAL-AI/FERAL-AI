"""The OAuth round trip has to be usable by a human in a browser.

Both ends of the flow used to answer a real browser with a JSON dict.
Clicking Authorize opened a popup showing `{"success": true, "url": ...}`
rather than the provider's consent screen, and after consenting the user
landed on `{"success": true, "provider": "whoop"}` with no indication of
what to do next and no signal back to the tab that opened the popup.

Everything behind those two routes worked. The flow was unreachable
anyway, which is why Whoop could not be connected at all.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import integrations_webhooks as mod


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(mod.router)

    oauth = MagicMock()
    oauth.build_authorize_response = MagicMock(
        return_value={
            "success": True,
            "provider": "whoop",
            "url": "https://api.prod.whoop.com/oauth/oauth2/auth?client_id=x&state=y",
            "state": "y",
        }
    )
    oauth.handle_callback = AsyncMock(
        return_value={"success": True, "provider": "whoop"}
    )
    monkeypatch.setattr(mod.state, "oauth", oauth, raising=False)
    return TestClient(app), oauth


def test_authorize_redirects_to_the_provider_when_asked(client):
    c, _ = client
    r = c.get("/api/oauth/authorize/whoop?redirect=1", follow_redirects=False)

    assert r.status_code == 302
    assert r.headers["location"].startswith("https://api.prod.whoop.com/oauth")


def test_authorize_still_returns_json_by_default(client):
    """The v1 wizard fetches this and opens `data.url` itself."""
    c, _ = client
    r = c.get("/api/oauth/authorize/whoop")

    assert r.status_code == 200
    assert r.json()["url"].startswith("https://api.prod.whoop.com/oauth")


def test_setup_required_never_redirects(client):
    """A provider with no client_id has no URL to redirect to.

    Redirecting to an empty string would strand the user; the UI needs
    the body so it can render the "configure your own app" panel.
    """
    c, oauth = client
    oauth.build_authorize_response.return_value = {
        "success": False,
        "provider": "whoop",
        "reason": "provider_setup_required",
        "setup_status": "provider_setup_required",
    }

    r = c.get("/api/oauth/authorize/whoop?redirect=1", follow_redirects=False)

    assert r.status_code == 200
    assert r.json()["setup_status"] == "provider_setup_required"


def test_callback_renders_html_for_a_browser(client):
    c, _ = client
    r = c.get("/api/oauth/callback?state=y&code=abc")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Connected" in r.text
    assert "close this window" in r.text
    assert "feral:oauth" in r.text, "the opener needs a signal to refresh"


def test_callback_failure_says_what_went_wrong(client):
    c, oauth = client
    oauth.handle_callback.return_value = {
        "success": False,
        "provider": "whoop",
        "error": "token exchange HTTP 400",
    }

    r = c.get("/api/oauth/callback?state=y&code=abc")

    assert r.status_code == 400
    assert "Connection failed" in r.text
    assert "token exchange HTTP 400" in r.text


def test_callback_still_returns_json_on_request(client):
    c, _ = client
    r = c.get("/api/oauth/callback?state=y&code=abc&format=json")

    assert r.json() == {"success": True, "provider": "whoop"}


def test_provider_error_text_is_escaped(client):
    """The error string comes from the provider, so it is untrusted."""
    c, oauth = client
    oauth.handle_callback.return_value = {
        "success": False,
        "provider": "whoop",
        "error": "<img src=x onerror=alert(1)>",
    }

    r = c.get("/api/oauth/callback?state=y&code=abc")

    assert "<img src=x" not in r.text
    assert "&lt;img" in r.text
