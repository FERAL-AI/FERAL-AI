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
    monkeypatch.setenv("FERAL_PROXY_AUTH_ALLOWED_GROUPS", "axiom-owner")

    scope = _scope()
    scope["headers"] = list(_headers(
        **{
            "X-FERAL-Proxy-Secret": "shared-secret",
            "X-FERAL-Proxy-User": "noah",
            "X-FERAL-Proxy-Groups": "axiom-owner|axiom-operator",
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
    assert seen["identity"].groups == ("axiom-owner", "axiom-operator")
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
async def test_bad_proxy_envelope_is_not_rescued_by_api_key(monkeypatch):
    from api import server as server_module
    from api.server import APIKeyMiddleware

    monkeypatch.setenv("FERAL_PROXY_AUTH_ENABLED", "1")
    monkeypatch.setenv("FERAL_PROXY_AUTH_TRUSTED_PROXIES", "10.20.30.0/24")
    monkeypatch.setenv("FERAL_PROXY_AUTH_SECRET", "shared-secret")
    monkeypatch.setenv("FERAL_PROXY_AUTH_ALLOWED_ORIGINS", "https://dashboard.example")

    scope = _scope()
    scope["headers"] = list(_headers(
        **{
            "Authorization": f"Bearer {server_module.FERAL_API_KEY}",
            "X-FERAL-Proxy-Secret": "wrong",
            "X-FERAL-Proxy-User": "noah",
            "Origin": "https://dashboard.example",
        }
    ).items())
    request = Request(scope)
    call_next = AsyncMock(return_value=PlainTextResponse("bad"))
    response = await APIKeyMiddleware(SimpleNamespace()).dispatch(request, call_next)

    assert response.status_code == 401
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_misconfigured_proxy_auth_does_not_break_api_key_without_envelope(
    monkeypatch,
):
    from api import server as server_module
    from api.server import APIKeyMiddleware

    monkeypatch.setenv("FERAL_PROXY_AUTH_ENABLED", "1")
    monkeypatch.delenv("FERAL_PROXY_AUTH_SECRET", raising=False)
    monkeypatch.delenv("FERAL_PROXY_AUTH_TRUSTED_PROXIES", raising=False)
    monkeypatch.delenv("FERAL_PROXY_AUTH_ALLOWED_ORIGINS", raising=False)

    scope = _scope()
    scope["headers"] = list(_headers(
        **{"Authorization": f"Bearer {server_module.FERAL_API_KEY}"}
    ).items())
    request = Request(scope)

    async def call_next(_request):
        return PlainTextResponse("ok")

    response = await APIKeyMiddleware(SimpleNamespace()).dispatch(request, call_next)
    assert response.status_code == 200
    assert response.body == b"ok"


@pytest.mark.asyncio
async def test_cross_site_proxy_request_is_rejected(monkeypatch):
    from api.server import APIKeyMiddleware

    monkeypatch.setenv("FERAL_PROXY_AUTH_ENABLED", "1")
    monkeypatch.setenv("FERAL_PROXY_AUTH_TRUSTED_PROXIES", "10.20.30.0/24")
    monkeypatch.setenv("FERAL_PROXY_AUTH_SECRET", "shared-secret")
    monkeypatch.setenv("FERAL_PROXY_AUTH_ALLOWED_ORIGINS", "https://dashboard.example")

    scope = _scope(method="POST")
    scope["headers"] = list(_headers(
        **{
            "X-FERAL-Proxy-Secret": "shared-secret",
            "X-FERAL-Proxy-User": "noah",
            "Origin": "https://dashboard.example",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "cors",
        }
    ).items())
    response = await APIKeyMiddleware(SimpleNamespace()).dispatch(
        Request(scope), AsyncMock(return_value=PlainTextResponse("bad"))
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_websocket_bad_proxy_envelope_is_rejected_before_accept(monkeypatch):
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
    )

    with patch("api.server._session_auth_module.transport_is_trusted", return_value=False):
        await client_session(ws)

    ws.close.assert_awaited_once_with(code=4001, reason="Unauthorized")
    ws.accept.assert_not_awaited()
