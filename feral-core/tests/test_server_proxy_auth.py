"""Integration coverage for trusted-proxy auth at the server boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse

pytestmark = pytest.mark.no_auto_feral_home


def _scope(*, peer=("10.20.30.40", 443), method="GET", path="/api/dashboard"):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
        "client": peer,
        "scheme": "https",
        "server": ("feral.example", 443),
    }


def _headers(**values):
    return {key.lower().encode(): value.encode() for key, value in values.items()}


@pytest.mark.asyncio
async def test_http_proxy_identity_is_authenticated_from_socket_peer(monkeypatch):
    from api.server import APIKeyMiddleware

    monkeypatch.setenv("FERAL_PROXY_AUTH_ENABLED", "1")
    monkeypatch.setenv("FERAL_PROXY_AUTH_TRUSTED_PROXIES", "10.20.30.0/24")
    monkeypatch.setenv("FERAL_PROXY_AUTH_SECRET", "shared-secret")
    monkeypatch.setenv("FERAL_PROXY_AUTH_ALLOWED_ORIGINS", "https://dashboard.example")

    scope = _scope()
    scope["headers"] = list(_headers(
        **{
            "X-FERAL-Proxy-Secret": "shared-secret",
            "X-FERAL-Proxy-User": "noah",
            "Origin": "https://dashboard.example",
        }
    ).items())
    request = Request(scope)
    seen = {}

    async def call_next(req):
        seen["identity"] = req.state.proxy_identity
        return PlainTextResponse("ok")

    middleware = APIKeyMiddleware(SimpleNamespace())
    response = await middleware.dispatch(request, call_next)

    assert response.status_code == 200
    assert seen["identity"].user == "noah"
    assert seen["identity"].source == "trusted-proxy"


@pytest.mark.asyncio
async def test_http_proxy_identity_never_uses_forwarded_for(monkeypatch):
    from api.server import APIKeyMiddleware

    monkeypatch.setenv("FERAL_PROXY_AUTH_ENABLED", "1")
    monkeypatch.setenv("FERAL_PROXY_AUTH_TRUSTED_PROXIES", "10.20.30.0/24")
    monkeypatch.setenv("FERAL_PROXY_AUTH_SECRET", "shared-secret")
    monkeypatch.setenv("FERAL_PROXY_AUTH_ALLOWED_ORIGINS", "https://dashboard.example")

    scope = _scope(peer=("192.0.2.99", 443))
    scope["headers"] = list(_headers(
        **{
            "X-FERAL-Proxy-Secret": "shared-secret",
            "X-FERAL-Proxy-User": "noah",
            "X-Forwarded-For": "10.20.30.40",
            "Origin": "https://dashboard.example",
        }
    ).items())
    request = Request(scope)
    middleware = APIKeyMiddleware(SimpleNamespace())
    response = await middleware.dispatch(request, lambda req: PlainTextResponse("bad"))

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_websocket_bad_proxy_envelope_cannot_authenticate(monkeypatch):
    from api.server import client_session

    monkeypatch.setenv("FERAL_PROXY_AUTH_ENABLED", "1")
    monkeypatch.setenv("FERAL_PROXY_AUTH_TRUSTED_PROXIES", "10.20.30.0/24")
    monkeypatch.setenv("FERAL_PROXY_AUTH_SECRET", "shared-secret")
    monkeypatch.setenv("FERAL_PROXY_AUTH_ALLOWED_ORIGINS", "https://dashboard.example")

    ws = SimpleNamespace(
        client=("10.20.30.40", 443),
        scope={"type": "websocket", "client": ("10.20.30.40", 443)},
        headers={
            "X-FERAL-Proxy-Secret": "wrong",
            "X-FERAL-Proxy-User": "noah",
            "Origin": "https://dashboard.example",
        },
        state=SimpleNamespace(),
        close=AsyncMock(),
        accept=AsyncMock(),
        receive_json=AsyncMock(return_value={}),
    )

    with patch("api.server._session_auth_module.transport_is_trusted", return_value=False):
        await client_session(ws)

    ws.close.assert_awaited_once_with(code=4001, reason="Unauthorized")
    ws.accept.assert_awaited_once()
