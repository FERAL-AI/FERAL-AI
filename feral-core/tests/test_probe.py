"""Tests for security.probe — credential validation helper."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from security.probe import (
    PROBE_CACHE_TTL_SECONDS,
    ProbeResult,
    clear_probe_cache,
    probe,
    probe_all,
    registered_probe_ids,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_probe_cache()
    yield
    clear_probe_cache()


def _mock_response(status_code: int, text: str = "") -> httpx.Response:
    request = httpx.Request("GET", "https://example.com")
    return httpx.Response(status_code, text=text, request=request)


@pytest.mark.asyncio
async def test_probe_openai_valid_key():
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=_mock_response(200, '{"data":[]}'))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    vault = MagicMock()
    vault.get_credential.return_value = "sk-valid"

    with patch("security.probe.httpx.AsyncClient", return_value=mock_client):
        result = await probe("openai", vault=vault)

    assert result.ok is True
    assert result.status_code == 200
    assert result.reason == "ok"
    assert result.provider == "openai"


@pytest.mark.asyncio
async def test_probe_openai_invalid_key():
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=_mock_response(401, "invalid"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    vault = MagicMock()
    vault.get_credential.return_value = "sk-bad"

    with patch("security.probe.httpx.AsyncClient", return_value=mock_client):
        result = await probe("openai", vault=vault)

    assert result.ok is False
    assert result.status_code == 401
    assert result.reason == "unauthorized"


@pytest.mark.asyncio
async def test_probe_openai_no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    vault = MagicMock()
    vault.get_credential.return_value = None

    with patch("security.probe.httpx.AsyncClient") as client_cls:
        result = await probe("openai", vault=vault)
        client_cls.assert_not_called()

    assert result.ok is False
    assert result.status_code is None
    assert result.reason == "no_key"


@pytest.mark.asyncio
async def test_probe_cache_within_ttl():
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=_mock_response(200))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    vault = MagicMock()
    vault.get_credential.return_value = "sk-valid"

    with patch("security.probe.httpx.AsyncClient", return_value=mock_client):
        first = await probe("openai", vault=vault)
        second = await probe("openai", vault=vault)

    assert first.ok is True
    assert second.ok is True
    assert mock_client.request.await_count == 1


@pytest.mark.asyncio
async def test_probe_force_bypasses_cache():
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=_mock_response(200))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    vault = MagicMock()
    vault.get_credential.return_value = "sk-valid"

    with patch("security.probe.httpx.AsyncClient", return_value=mock_client):
        await probe("openai", vault=vault)
        await probe("openai", vault=vault, force=True)

    assert mock_client.request.await_count == 2


@pytest.mark.asyncio
async def test_probe_prefers_the_stored_credential_over_the_env_var(monkeypatch):
    """The vault entry outranks the process env var, and that is deliberate.

    This test previously asserted the opposite (it was named
    ``test_probe_env_overrides_vault``) and had been failing since the
    resolution order was centralised in ``security.vault_keys.get_active_key``,
    whose documented order is: active labeled secret -> default-namespace
    vault entry -> process env var.

    The probe deliberately routes through that helper rather than reading
    ``os.getenv`` directly, so a key the operator just added through the UI
    probes correctly without restarting the brain. Probing is the operator's
    first "did I add this right?" check, so it has to reflect what the chat
    and voice paths will actually use -- which is the stored credential.
    Asserting env-wins here would have pinned the probe to a different
    credential than the one that serves real traffic.
    """
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=_mock_response(200))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    vault = MagicMock()
    vault.get_credential.return_value = "sk-from-vault"

    with patch("security.probe.httpx.AsyncClient", return_value=mock_client):
        await probe("openai", vault=vault)

    headers = mock_client.request.await_args.kwargs.get("headers") or {}
    assert headers.get("Authorization") == "Bearer sk-from-vault"


@pytest.mark.asyncio
async def test_probe_falls_back_to_env_when_nothing_is_stored(monkeypatch):
    """With no stored credential, the env var is still honoured."""
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=_mock_response(200))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    vault = MagicMock()
    vault.get_credential.return_value = None

    with patch("security.probe.httpx.AsyncClient", return_value=mock_client):
        await probe("openai", vault=vault)

    headers = mock_client.request.await_args.kwargs.get("headers") or {}
    assert headers.get("Authorization") == "Bearer sk-from-env"


@pytest.mark.asyncio
async def test_probe_all_runs_registered_providers():
    with patch("security.probe.probe", new_callable=AsyncMock) as mock_probe:
        mock_probe.return_value = ProbeResult(
            provider="openai",
            ok=True,
            status_code=200,
            reason="ok",
            detail="ok",
            probed_at=time.time(),
            latency_ms=1.0,
        )
        results = await probe_all(vault=MagicMock())

    assert set(results.keys()) == set(registered_probe_ids())
    assert mock_probe.await_count == len(registered_probe_ids())


@pytest.mark.asyncio
async def test_probe_ollama_reachability_no_key_required():
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=_mock_response(200, '{"models":[]}'))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("security.probe.httpx.AsyncClient", return_value=mock_client):
        result = await probe("ollama", vault=None)

    assert result.provider == "ollama"
    assert result.ok is True


@pytest.mark.asyncio
async def test_probe_bedrock_no_aws_creds():
    vault = MagicMock()
    vault.get_credential.return_value = None
    result = await probe("bedrock", vault=vault)
    assert result.reason == "no_key"


def test_registered_providers_include_expected_set():
    expected = {
        "openai", "anthropic", "gemini", "openrouter", "deepseek", "groq",
        "ollama", "lmstudio", "bedrock", "google", "notion", "spotify",
        "whoop", "oura",
    }
    assert expected.issubset(set(registered_probe_ids()))
