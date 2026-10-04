"""Provider-key persistence — every save route writes to vault + disk + env.

Regression guard for the "OpenAI key suddenly 401" bug: keys typed in
Settings for providers other than OpenAI/Groq/Anthropic used to drop
into a silent hole. Now they must land in every layer.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.no_auto_feral_home


def _make_mock_state(tmp_path, *, with_catalog=True, with_vault=True, with_orch=True):
    """Build a state mock with the pieces the config + llm routes touch."""
    mock = MagicMock()
    mock.config = MagicMock()
    settings = {("llm", "provider"): "openai", ("llm", "base_url"): "", ("llm", "fallback_providers"): []}
    mock.config.get.side_effect = lambda section, key, default=None: settings.get((section, key), default)
    mock.config.save_credentials = MagicMock(return_value=True)
    mock.config.update_settings = MagicMock(return_value=True)

    if with_vault:
        from security.vault import BlindVault
        mock.vault = BlindVault(vault_path=str(tmp_path / "credentials.json"))
    else:
        mock.vault = None

    if with_catalog:
        from providers.catalog import ProviderCatalog
        mock.provider_catalog = ProviderCatalog()
    else:
        mock.provider_catalog = None

    if with_orch:
        orch = MagicMock()
        llm = MagicMock()
        llm.reconfigure = AsyncMock(return_value={
            "ok": True, "provider": "openai", "model": "gpt-4o-mini",
            "available": True, "base_url": "", "reason": "ok",
        })
        llm.switch_provider = AsyncMock(return_value=None)
        llm.provider = "openai"
        orch.llm = llm
        mock.orchestrator = orch
    else:
        mock.orchestrator = None

    mock.channel_manager = None
    return mock


@pytest.fixture
def client(tmp_path):
    mock = _make_mock_state(tmp_path)
    with patch("api.state.state", mock), \
         patch("api.routes.config.state", mock), \
         patch("api.routes.llm.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=True), mock, tmp_path


# ── save_credentials accepts every catalog env var ───────────────


@pytest.mark.parametrize("env_var", [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
    "TOGETHER_API_KEY",
    "FIREWORKS_API_KEY",
    "GOOGLE_API_KEY",
])
def test_save_credentials_accepts_any_provider_key(client, env_var):
    c, mock, _tmp = client
    r = c.post("/api/config/credentials", json={env_var: "sk-test"})
    assert r.status_code == 200
    body = r.json()
    assert env_var in body["keys_saved"]
    assert env_var in body["persisted_to_vault"]


def test_save_credentials_rejects_random_keys(client):
    c, mock, _tmp = client
    r = c.post("/api/config/credentials", json={"RANDOM_NAME": "x"})
    assert r.status_code == 200
    body = r.json()
    assert body["keys_saved"] == []
    assert "RANDOM_NAME" in body["rejected"]


def test_save_credentials_skill_keys_still_work(client):
    c, _mock, _tmp = client
    r = c.post("/api/config/credentials", json={
        "skill_keys": {"weather": "w-key"},
    })
    assert r.status_code == 200


# ── /api/llm/config — vault + creds.json + env + reconfigure ─────


def test_llm_config_persists_gemini_key_everywhere(client):
    c, mock, _tmp = client
    r = c.post("/api/llm/config", json={
        "provider": "gemini",
        "model": "gemini-2.0-flash",
        "api_key": "g-test",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["persisted"]["ok"] is True
    assert body["persisted"]["vault"] is True
    assert body["persisted"]["credentials_json"] is True
    # Vault round-trip
    assert mock.vault.retrieve("GOOGLE_API_KEY") == "g-test"
    # Reconfigure coroutine was awaited with the new provider
    mock.orchestrator.llm.reconfigure.assert_awaited_once()
    kwargs = mock.orchestrator.llm.reconfigure.await_args.kwargs
    assert kwargs["provider"] == "gemini"
    assert kwargs["api_key"] == "g-test"
    mock.config.update_settings.assert_any_call("llm", "fallback_providers", ["openai"])


def test_llm_config_without_key_still_switches(client):
    c, mock, _tmp = client
    r = c.post("/api/llm/config", json={
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
    })
    assert r.status_code == 200
    mock.orchestrator.llm.reconfigure.assert_awaited_once()


# ── /api/llm/providers/{id}/configure persistence ────────────────


def test_provider_configure_persists_and_reports(client):
    c, mock, _tmp = client
    r = c.post("/api/llm/providers/deepseek/configure", json={
        "api_key": "dsk-test",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["persisted"]["vault"] is True
    assert body["persisted"]["credentials_json"] is True
    assert mock.vault.retrieve("DEEPSEEK_API_KEY") == "dsk-test"


# ── BlindVault survives corrupt credentials.json ────────────────


def test_blind_vault_survives_corrupt_json(tmp_path):
    from security.vault import BlindVault
    bad = tmp_path / "credentials.json"
    bad.write_text("{ this is not json")
    vault = BlindVault(vault_path=str(bad))
    assert vault._cache == {}
    # File should be moved aside, not deleted.
    assert (tmp_path / "credentials.corrupt").exists()
    # Store still works after the corrupt-backup swap.
    vault.store("OPENAI_API_KEY", "x")
    assert vault.retrieve("OPENAI_API_KEY") == "x"


def test_state_loads_env_from_vault_when_creds_missing(tmp_path, monkeypatch):
    """_load_stored_credentials should read vault when credentials.json is absent."""
    # Point FERAL_HOME at a fresh tmp dir and drop a vault file.
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    from security.vault import BlindVault
    vault = BlindVault(vault_path=str(tmp_path / "credentials_vault.json"))
    vault.store("OPENAI_API_KEY", "sk-from-vault")

    # Clear env first so the load path has to fire.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # And point _load_stored_credentials at the vault path via class.
    with patch("security.vault.BlindVault", return_value=vault):
        from api.state import BrainState
        BrainState._load_stored_credentials()

    import os
    assert os.environ.get("OPENAI_API_KEY") == "sk-from-vault"
