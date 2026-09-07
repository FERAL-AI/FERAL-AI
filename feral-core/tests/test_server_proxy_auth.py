"""Integration coverage for trusted-proxy auth at the server boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from security.proxy_auth import RAW_SOCKET_PEER_SCOPE_KEY
from security.session_auth import TRUSTED_TRANSPORT_SCOPE_KEY

pytestmark = pytest.mark.no_auto_feral_home


def _scope(
    *,
    peer=("10.20.30.40", 443),
    raw_peer=None,
    method="GET",
    path="/api/dashboard",
    untrusted=False,
):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
        "client": peer,
        "scheme": "https",
        "server": ("feral.example", 443),
    }
    if raw_peer is not None:
        scope[RAW_SOCKET_PEER_SCOPE_KEY] = raw_peer
    if untrusted:
        scope[TRUSTED_TRANSPORT_SCOPE_KEY] = True
    return scope


def _headers(**values):
    return {key.lower().encode(): value.encode() for key, value in values.items()}


def _enable_proxy_auth(monkeypatch):
    monkeypatch.setenv("FERAL_PROXY_AUTH_ENABLED", "1")
    monkeypatch.setenv("FERAL_PROXY_AUTH_TRUSTED_PROXIES", "10.20.30.0/24")
    monkeypatch.setenv("FERAL_PROXY_AUTH_SECRET", "shared-secret")
    monkeypatch.setenv("FERAL_PROXY_AUTH_ALLOWED_ORIGINS", "https://dashboard.example")


def _proxy_headers(**extra):
    return {
        "X-FERAL-Proxy-Secret": "shared-secret",
        "X-FERAL-Proxy-User": "noah",
        "Origin": "https://dashboard.example",
        **extra,
    }


def _loopback_client(app):
    """Give TestClient a real IP-shaped ASGI peer across Starlette versions."""

    class SocketPeer:
        async def __call__(self, scope, receive, send):
            if scope.get("type") in {"http", "websocket"}:
                scope = dict(scope)
                scope["client"] = ("127.0.0.1", 43120)
            await app(scope, receive, send)

    return TestClient(SocketPeer())


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
async def test_http_proxy_identity_uses_raw_peer_after_uvicorn_client_rewrite(
    monkeypatch,
):
    from api.server import APIKeyMiddleware

    _enable_proxy_auth(monkeypatch)
    scope = _scope(
        peer=("203.0.113.8", 0),
        raw_peer=("10.20.30.40", 443),
    )
    scope["headers"] = list(_headers(**_proxy_headers()).items())
    seen = {}

    async def call_next(request):
        seen["identity"] = request.state.proxy_identity
        return PlainTextResponse("ok")

    response = await APIKeyMiddleware(SimpleNamespace()).dispatch(
        Request(scope), call_next
    )

    assert response.status_code == 200
    assert seen["identity"].user == "noah"


@pytest.mark.asyncio
async def test_http_missing_proxy_envelope_cannot_use_implicit_local_bypass(
    monkeypatch,
):
    from api.server import APIKeyMiddleware

    _enable_proxy_auth(monkeypatch)
    monkeypatch.setenv("FERAL_LOCAL_BYPASS", "1")
    scope = _scope(peer=("127.0.0.1", 43120), raw_peer=("127.0.0.1", 43120))
    call_next = AsyncMock(return_value=PlainTextResponse("bad"))

    response = await APIKeyMiddleware(SimpleNamespace()).dispatch(
        Request(scope), call_next
    )

    assert response.status_code == 401
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_proxy_envelope_is_rejected_on_untrusted_transport(monkeypatch):
    from api.server import APIKeyMiddleware

    _enable_proxy_auth(monkeypatch)
    scope = _scope(raw_peer=("10.20.30.40", 443), untrusted=True)
    scope["headers"] = list(_headers(**_proxy_headers()).items())
    call_next = AsyncMock(return_value=PlainTextResponse("bad"))

    response = await APIKeyMiddleware(SimpleNamespace()).dispatch(
        Request(scope), call_next
    )

    assert response.status_code == 401
    call_next.assert_not_awaited()


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
async def test_enabled_proxy_auth_preserves_api_key_without_envelope(monkeypatch):
    from api import server as server_module
    from api.server import APIKeyMiddleware

    _enable_proxy_auth(monkeypatch)
    scope = _scope(peer=("127.0.0.1", 43120), raw_peer=("127.0.0.1", 43120))
    scope["headers"] = list(
        _headers(
            **{"Authorization": f"Bearer {server_module.FERAL_API_KEY}"}
        ).items()
    )

    response = await APIKeyMiddleware(SimpleNamespace()).dispatch(
        Request(scope),
        AsyncMock(return_value=PlainTextResponse("ok")),
    )

    assert response.status_code == 200
    assert response.body == b"ok"


@pytest.mark.asyncio
async def test_enabled_proxy_auth_preserves_phone_bearer_without_envelope(
    monkeypatch,
):
    from api.server import APIKeyMiddleware

    _enable_proxy_auth(monkeypatch)
    scope = _scope(
        peer=("127.0.0.1", 43120),
        raw_peer=("127.0.0.1", 43120),
        path="/api/context/live",
    )
    scope["headers"] = list(
        _headers(**{"Authorization": "Bearer phone-token"}).items()
    )
    pairing_store = SimpleNamespace(
        verify_phone_bearer=lambda token: "phone-1" if token == "phone-token" else None
    )

    with patch("api.state.state", SimpleNamespace(device_pairing_store=pairing_store)):
        response = await APIKeyMiddleware(SimpleNamespace()).dispatch(
            Request(scope),
            AsyncMock(return_value=PlainTextResponse("ok")),
        )

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
    from api.server import _authenticate_client_session

    _enable_proxy_auth(monkeypatch)

    ws = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.8", port=0),
        scope={
            "type": "websocket",
            "client": ("203.0.113.8", 0),
            RAW_SOCKET_PEER_SCOPE_KEY: ("10.20.30.40", 443),
        },
        headers={
            "X-FERAL-Proxy-Secret": "wrong",
            "X-FERAL-Proxy-User": "noah",
            "Origin": "https://dashboard.example",
        },
        state=SimpleNamespace(),
        close=AsyncMock(),
        accept=AsyncMock(),
    )

    assert await _authenticate_client_session(ws, None) is False

    ws.close.assert_awaited_once_with(code=4001, reason="Unauthorized")
    ws.accept.assert_not_awaited()


@pytest.mark.asyncio
async def test_websocket_proxy_auth_uses_raw_peer_after_uvicorn_rewrite(monkeypatch):
    from api.server import _authenticate_client_session

    _enable_proxy_auth(monkeypatch)
    ws = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.8", port=0),
        scope={
            "type": "websocket",
            "client": ("203.0.113.8", 0),
            RAW_SOCKET_PEER_SCOPE_KEY: ("10.20.30.40", 443),
        },
        headers=_proxy_headers(),
        state=SimpleNamespace(),
        close=AsyncMock(),
        accept=AsyncMock(),
    )

    assert await _authenticate_client_session(ws, None) is True
    ws.accept.assert_awaited_once()
    ws.close.assert_not_awaited()
    assert ws.state.proxy_identity.user == "noah"


@pytest.mark.asyncio
async def test_websocket_missing_proxy_envelope_cannot_use_loopback_bypass(
    monkeypatch,
):
    from api.server import _authenticate_client_session

    _enable_proxy_auth(monkeypatch)
    monkeypatch.setenv("FERAL_LOCAL_BYPASS", "1")
    ws = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1", port=43120),
        scope={
            "type": "websocket",
            "client": ("127.0.0.1", 43120),
            RAW_SOCKET_PEER_SCOPE_KEY: ("127.0.0.1", 43120),
        },
        headers={},
        state=SimpleNamespace(),
        close=AsyncMock(),
        accept=AsyncMock(),
        receive_json=AsyncMock(return_value={"type": "chat", "text": "hello"}),
    )

    assert await _authenticate_client_session(ws, None) is False
    ws.accept.assert_awaited_once()
    ws.close.assert_awaited_once_with(code=4001, reason="Unauthorized")


@pytest.mark.asyncio
async def test_websocket_explicit_token_survives_enabled_proxy_auth(monkeypatch):
    from api import server as server_module

    _enable_proxy_auth(monkeypatch)
    ws = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1", port=43120),
        scope={
            "type": "websocket",
            "client": ("127.0.0.1", 43120),
            RAW_SOCKET_PEER_SCOPE_KEY: ("127.0.0.1", 43120),
        },
        headers={},
        state=SimpleNamespace(),
        close=AsyncMock(),
        accept=AsyncMock(),
    )

    assert (
        await server_module._authenticate_client_session(
            ws, server_module.FERAL_API_KEY
        )
        is True
    )
    ws.accept.assert_awaited_once()
    ws.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_websocket_proxy_assertion_is_rejected_on_untrusted_transport(
    monkeypatch,
):
    from api.server import _authenticate_client_session

    _enable_proxy_auth(monkeypatch)
    ws = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.8", port=0),
        scope={
            "type": "websocket",
            "client": ("203.0.113.8", 0),
            RAW_SOCKET_PEER_SCOPE_KEY: ("10.20.30.40", 443),
            TRUSTED_TRANSPORT_SCOPE_KEY: True,
        },
        headers=_proxy_headers(),
        state=SimpleNamespace(),
        close=AsyncMock(),
        accept=AsyncMock(),
    )

    assert await _authenticate_client_session(ws, None) is False
    ws.close.assert_awaited_once_with(code=4001, reason="Unauthorized")
    ws.accept.assert_not_awaited()


def test_full_http_boundary_preserves_raw_peer_before_uvicorn_rewrite(monkeypatch):
    from api import server

    _enable_proxy_auth(monkeypatch)
    monkeypatch.setenv("FERAL_PROXY_AUTH_TRUSTED_PROXIES", "127.0.0.1")
    client = _loopback_client(server.uvicorn_app)

    response = client.get(
        "/api/__proxy_auth_boundary_probe__",
        headers={
            **_proxy_headers(),
            "X-Forwarded-For": "203.0.113.8",
            "X-Forwarded-Proto": "https",
        },
    )

    # Authenticated requests reach routing; the probe route is intentionally
    # absent, so 404 proves the entire middleware boundary allowed it.
    assert response.status_code == 404


def test_full_http_boundary_rejects_missing_envelope_and_untrusted_assertion(
    monkeypatch,
):
    from api import server

    _enable_proxy_auth(monkeypatch)
    monkeypatch.setenv("FERAL_PROXY_AUTH_TRUSTED_PROXIES", "127.0.0.1")
    monkeypatch.setenv("FERAL_LOCAL_BYPASS", "1")
    trusted_client = _loopback_client(server.uvicorn_app)
    untrusted_client = _loopback_client(server.untrusted_uvicorn_app)

    assert trusted_client.get("/api/__proxy_auth_boundary_probe__").status_code == 401
    assert (
        untrusted_client.get(
            "/api/__proxy_auth_boundary_probe__",
            headers={
                **_proxy_headers(),
                "X-Forwarded-For": "203.0.113.8",
            },
        ).status_code
        == 401
    )


def test_full_websocket_boundary_uses_raw_peer_after_uvicorn_rewrite(monkeypatch):
    from api import server

    _enable_proxy_auth(monkeypatch)
    monkeypatch.setenv("FERAL_PROXY_AUTH_TRUSTED_PROXIES", "127.0.0.1")
    client = _loopback_client(server.uvicorn_app)

    with client.websocket_connect(
        "/v1/session",
        headers={
            **_proxy_headers(),
            "X-Forwarded-For": "203.0.113.8",
            "X-Forwarded-Proto": "https",
        },
    ) as websocket:
        assert websocket is not None


def test_full_websocket_boundary_rejects_missing_envelope_and_untrusted_assertion(
    monkeypatch,
):
    from api import server

    _enable_proxy_auth(monkeypatch)
    monkeypatch.setenv("FERAL_PROXY_AUTH_TRUSTED_PROXIES", "127.0.0.1")
    monkeypatch.setenv("FERAL_LOCAL_BYPASS", "1")
    trusted_client = _loopback_client(server.uvicorn_app)
    untrusted_client = _loopback_client(server.untrusted_uvicorn_app)

    with pytest.raises(WebSocketDisconnect) as missing:
        with trusted_client.websocket_connect("/v1/session") as websocket:
            websocket.send_json({"type": "chat", "text": "hello"})
            websocket.receive_json()
    assert missing.value.code == 4001

    with pytest.raises(WebSocketDisconnect) as untrusted:
        with untrusted_client.websocket_connect(
            "/v1/session",
            headers={
                **_proxy_headers(),
                "X-Forwarded-For": "203.0.113.8",
            },
        ):
            pass
    assert untrusted.value.code == 4001
