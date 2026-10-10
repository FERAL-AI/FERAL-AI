"""Real ASGI OAuth route maps locked-vault refusal without side effects."""

from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import httpx
import pytest


@pytest.mark.asyncio
async def test_real_oauth_route_refuses_without_cache_file_or_network_change(
    tmp_path, monkeypatch
):
    from api.server import app
    from api.routes import integrations_webhooks
    from integrations import oauth_manager
    from security.vault_coordinator import VaultCoordinator, DeferredBootVaultFacade

    monkeypatch.setenv("FERAL_NATIVE_DEFER_VAULT", "1")
    token_path = tmp_path / "oauth_state.json"
    pending_path = tmp_path / "oauth_pending.json"
    token_path.write_bytes(b'{"spotify":{"access_token":"legacy-private-sentinel"}}')
    pending_path.write_bytes(b'{"legacy":{"created":99999999999}}')
    monkeypatch.setattr(oauth_manager, "OAUTH_STATE_PATH", token_path)
    monkeypatch.setattr(oauth_manager, "OAUTH_PENDING_PATH", pending_path)
    factory = Mock(
        side_effect=AssertionError("HTTP authorization cannot unlock keychain")
    )
    coordinator = VaultCoordinator(vault_factory=factory)
    manager = oauth_manager.OAuthManager(
        vault=DeferredBootVaultFacade(coordinator), probe_on_add=False
    )
    network = AsyncMock(
        side_effect=AssertionError("locked authorization cannot send network requests")
    )
    monkeypatch.setattr(manager._http, "post", network)
    monkeypatch.setattr(integrations_webhooks, "state", SimpleNamespace(oauth=manager))
    before = (token_path.read_bytes(), pending_path.read_bytes())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.get("/api/oauth/authorize/spotify?redirect=1")
        assert response.status_code == 503
        assert response.json() == {
            "detail": {
                "code": "vault_locked",
                "message": "Unlock the vault explicitly before using stored credentials.",
            }
        }
        assert "legacy-private-sentinel" not in response.text
        assert "location" not in response.headers
        assert manager._tokens == {} and manager._pending_states == {}
        assert (token_path.read_bytes(), pending_path.read_bytes()) == before
        factory.assert_not_called()
        network.assert_not_awaited()
    finally:
        await manager._http.aclose()


@pytest.mark.asyncio
async def test_vault_refusal_handler_never_echoes_exception_context(monkeypatch):
    from api.server import app
    from api.routes import integrations_webhooks
    from security.vault_coordinator import VaultLockedRefusal

    error = VaultLockedRefusal()
    error.args = ("private-fixture-system-path-and-token",)
    manager = SimpleNamespace(build_authorize_response=Mock(side_effect=error))
    monkeypatch.setattr(integrations_webhooks, "state", SimpleNamespace(oauth=manager))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.get("/api/oauth/authorize/spotify")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "vault_locked"
    assert "private-fixture" not in response.text
