"""Actual registered local-view routes reject remote and malformed admission."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api.browser_view import BrowserViewManager
from api.routes import marketplace_browser as routes


class Browser:
    connected = True
    _attached_target_id = "fixture-tab"
    _page = None
    _cdp = None

    async def capture_view_frame(self, target):
        return {"success": True, "target_id": target, "format": "jpeg",
                "image_b64": "synthetic", "width": 10, "height": 10,
                "captured_at": 1000.0, "masked_password_fields": True}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(routes, "state", SimpleNamespace(browser=Browser()))
    monkeypatch.setattr(routes, "browser_views", BrowserViewManager(lambda: routes.state.browser))
    app = FastAPI()
    app.include_router(routes.router)
    return app


HEADERS = {"X-FERAL-Browser-View": "native-v1"}


@pytest.mark.parametrize("peer,headers", [
    ("192.0.2.1", HEADERS), ("127.0.0.1", {}),
    ("127.0.0.1", {**HEADERS, "Forwarded": "for=192.0.2.1"}),
    ("127.0.0.1", {**HEADERS, "X-Forwarded-For": "127.0.0.1"}),
    ("127.0.0.1", {**HEADERS, "X-Real-IP": "127.0.0.1"}),
    ("127.0.0.1", {**HEADERS, "Origin": "http://localhost:5173"}),
    ("127.0.0.1", {**HEADERS, "Origin": "null"}),
    ("127.0.0.1", {**HEADERS, "Sec-Fetch-Site": "same-origin"}),
])
def test_local_header_is_not_remote_authority(app, peer, headers):
    with TestClient(app, client=(peer, 20000)) as client:
        response = client.get("/api/browser/view/targets", headers=headers)
        assert response.status_code == 403
        assert response.json()["error_code"] == "view_local_operator_required"
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("body", [
    {"target_id": "fixture-tab", "consent": "true"},
    {"target_id": "fixture-tab", "consent": 1},
    {"target_id": "fixture-tab", "consent": True, "action": "click"},
    {"target_id": "../tab", "consent": True},
])
def test_strict_request_never_coerces_consent_or_accepts_actions(app, body):
    with TestClient(app, client=("127.0.0.1", 20000)) as client:
        assert client.post("/api/browser/view/start", headers=HEADERS, json=body).status_code == 422


def test_registered_contract_start_frame_stop(app):
    with TestClient(app, client=("127.0.0.1", 20000)) as client:
        response = client.get("/api/browser/view/targets", headers=HEADERS)
        assert response.json()["active_target_id"] == "fixture-tab"
        assert response.headers["cache-control"] == "no-store"
        assert client.post("/api/browser/view/start", headers=HEADERS,
                           json={"target_id": "fixture-tab", "consent": False}).status_code == 400
        start = client.post("/api/browser/view/start", headers=HEADERS,
                            json={"target_id": "fixture-tab", "consent": True})
        assert start.status_code == 200
        value = start.json()
        body = {key: value[key] for key in ("view_id", "token")}
        frame = client.post("/api/browser/view/frame", headers=HEADERS, json=body)
        assert frame.status_code == 200 and frame.json()["sequence"] == 1
        assert "token" not in frame.json()
        assert frame.headers["cache-control"] == "no-store"
        assert client.post("/api/browser/view/stop", headers=HEADERS, json=body).json() == {"success": True}
        assert client.post("/api/browser/view/frame", headers=HEADERS, json=body).status_code == 403
