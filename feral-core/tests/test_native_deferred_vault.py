"""Native deferral leaves HTTP reachable without keychain or plaintext fallback."""

from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from security.vault_coordinator import (
    VaultCoordinator,
    VaultLockedRefusal,
    DeferredBootVaultFacade,
)


@pytest.fixture
def deferred_mode(monkeypatch):
    from security import vault

    monkeypatch.setenv("FERAL_NATIVE_DEFER_VAULT", "1")
    monkeypatch.setattr(vault, "_deferred_coordinator", None)
    reads = Mock(side_effect=AssertionError("no automatic keychain read"))
    writes = Mock(side_effect=AssertionError("no automatic keychain write"))
    monkeypatch.setattr(vault, "_keyring_get_password", reads)
    monkeypatch.setattr(vault, "_keyring_set_password", writes)
    return reads, writes


def test_legacy_construction_and_singleton_fail_closed(deferred_mode):
    from security.vault import BlindVault, get_vault

    with pytest.raises(VaultLockedRefusal):
        BlindVault()
    with pytest.raises(VaultLockedRefusal):
        get_vault()
    deferred_mode[0].assert_not_called()
    deferred_mode[1].assert_not_called()


def test_config_settings_discovery_preserves_setup_without_credentials(
    deferred_mode, tmp_path, monkeypatch
):
    import json
    from config.loader import ConfigLoader

    settings = tmp_path / ".feral-isolation" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "meta": {"setup_complete": True},
                "llm": {"fallback_providers": ["openai"]},
            }
        )
    )
    loader = ConfigLoader()
    loader._load_credentials = Mock(side_effect=AssertionError("must remain deferred"))
    loader.discover()
    assert loader.setup_complete is True
    assert loader._merged["llm"]["fallback_providers"] == ["openai"]
    loader._load_credentials.assert_not_called()
    with pytest.raises(VaultLockedRefusal):
        loader.save_credentials({"OPENAI_API_KEY": "fixture"})
    assert loader._credentials == {}
    assert not (settings.parent / "credentials.json").exists()


def test_encrypted_memory_does_not_open_stale_or_create_plaintext(
    deferred_mode, tmp_path
):
    from memory.store import MemoryStore

    path = tmp_path / "memory.db"
    encrypted = tmp_path / "memory.db.enc"
    encrypted.write_bytes(b"private ciphertext fixture")
    with pytest.raises(VaultLockedRefusal):
        MemoryStore(db_path=str(path))
    assert not path.exists()
    path.write_bytes(b"stale plaintext must remain untouched")
    with pytest.raises(VaultLockedRefusal):
        MemoryStore(db_path=str(path))
    assert path.read_bytes() == b"stale plaintext must remain untouched"
    assert encrypted.read_bytes() == b"private ciphertext fixture"
    deferred_mode[0].assert_not_called()


def test_authenticated_memory_injection_uses_supplied_vault_only(
    deferred_mode, tmp_path, monkeypatch
):
    from memory.store import MemoryStore
    from memory import at_rest

    path = tmp_path / "memory.db"
    path.with_name("memory.db.enc").write_bytes(b"ciphertext")
    opened = object()
    restore = Mock()
    monkeypatch.setattr(at_rest, "ensure_plaintext_db", restore)
    memory = MemoryStore(db_path=str(path), authenticated_vault=opened)
    try:
        restore.assert_called_once_with(vault=opened, db_path=path)
        assert path.exists()
    finally:
        memory.close()
    deferred_mode[0].assert_not_called()


def test_nonnative_encrypted_memory_failure_preserves_existing_fallback(
    tmp_path, monkeypatch
):
    from memory.store import MemoryStore
    from memory import at_rest

    monkeypatch.delenv("FERAL_NATIVE_DEFER_VAULT", raising=False)
    path = tmp_path / "memory.db"
    path.with_name("memory.db.enc").write_bytes(b"ciphertext")
    restore = Mock(side_effect=RuntimeError("fixture failure"))
    monkeypatch.setattr(at_rest, "ensure_plaintext_db", restore)
    memory = MemoryStore(db_path=str(path))
    try:
        assert path.exists()
    finally:
        memory.close()


def test_oauth_locked_boot_never_loads_or_writes_plaintext_tokens(
    deferred_mode, tmp_path, monkeypatch
):
    from integrations import oauth_manager
    import json

    tokens = tmp_path / "oauth_state.json"
    tokens.write_text(json.dumps({"spotify": {"access_token": "legacy-private-token"}}))
    pending = tmp_path / "oauth_pending.json"
    pending.write_text(json.dumps({"legacy": {"created": 99999999999}}))
    monkeypatch.setattr(oauth_manager, "OAUTH_STATE_PATH", tokens)
    monkeypatch.setattr(oauth_manager, "OAUTH_PENDING_PATH", pending)
    coordinator = VaultCoordinator(vault_factory=Mock())
    manager = oauth_manager.OAuthManager(
        vault=DeferredBootVaultFacade(coordinator), probe_on_add=False
    )
    assert manager._tokens == {} and manager._pending_states == {}
    before = tokens.read_bytes()
    with pytest.raises(VaultLockedRefusal):
        manager._save_token("spotify", {"access_token": "new-token"})
    assert tokens.read_bytes() == before
    assert manager._tokens == {}
    pending_before = pending.read_bytes()
    with pytest.raises(VaultLockedRefusal):
        manager._persist_pending_state("new", {"created": 1})
    with pytest.raises(VaultLockedRefusal):
        manager._drop_pending_state("legacy")
    with pytest.raises(VaultLockedRefusal):
        manager.build_authorize_response("spotify")
    assert pending.read_bytes() == pending_before and manager._pending_states == {}
    import asyncio

    asyncio.run(manager._http.aclose())


@pytest.fixture
def api_client(monkeypatch):
    from api.routes import config, llm, security_and_hardware

    opened = SimpleNamespace(list_keys=lambda: [], to_safe_summary=lambda: {})
    factory = Mock(return_value=opened)
    coordinator = VaultCoordinator(vault_factory=factory)
    configuration = Mock()
    configuration._merged = {"llm": {"provider": "ollama", "model": "fixture-local"}}
    configuration.get.return_value = "ollama"
    swap = AsyncMock()
    provider = SimpleNamespace(
        provider="ollama", switch_provider=swap, set_config=Mock()
    )
    fake = SimpleNamespace(
        vault_coordinator=coordinator,
        vault=None,
        memory=object(),
        _native_vault_deferred=True,
        _native_bootstrap_required=False,
        config=configuration,
        orchestrator=SimpleNamespace(llm=provider),
        provider_catalog=Mock(),
        oauth=None,
    )
    for module in (config, llm, security_and_hardware):
        monkeypatch.setattr(module, "state", fake)
    app = FastAPI()
    for module in (config, llm, security_and_hardware):
        app.include_router(module.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, fake, factory, swap


def test_passive_review_one_use_unlock_contract(api_client):
    client, state, factory, swap = api_client
    status = client.get("/api/security/vault/status").json()
    assert status["state"] == "locked" and status["unlock_supported"] is True
    assert status["initialization_supported"] is False
    factory.assert_not_called()
    review = client.post("/api/security/vault/unlock/review", json={}).json()
    assert review["previous_state"] == review["status"]["state"] == "locked"
    assert review["expires_in_seconds"] == 300
    factory.assert_not_called()
    response = client.post(
        "/api/security/vault/unlock", json={"review_token": review["review_token"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert (
        body["operation"] == "unlock" and body["review_token"] == review["review_token"]
    )
    assert body["ok"] is body["status"]["credentials_available"] is True
    assert (
        client.post(
            "/api/security/vault/unlock", json={"review_token": review["review_token"]}
        ).status_code
        == 409
    )
    factory.assert_called_once()
    swap.assert_not_called()


def test_cancelled_unlock_review_cannot_dispatch(api_client):
    client, state, factory, swap = api_client
    review = client.post("/api/security/vault/unlock/review", json={}).json()
    assert client.post(
        "/api/security/vault/unlock/cancel",
        json={"review_token": review["review_token"]},
    ).json()["ok"]
    assert (
        client.post(
            "/api/security/vault/unlock", json={"review_token": review["review_token"]}
        ).status_code
        == 409
    )
    factory.assert_not_called()


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/config/credentials", {"OPENAI_API_KEY": "private-request-key"}),
        (
            "/api/setup/complete",
            {
                "settings": {"meta": {"setup_complete": True}},
                "credentials": {"OPENAI_API_KEY": "private-request-key"},
            },
        ),
        (
            "/api/llm/config",
            {
                "provider": "openai",
                "model": "fixture",
                "api_key": "private-request-key",
            },
        ),
        ("/api/llm/providers/openai/configure", {"api_key": "private-request-key"}),
        (
            "/api/config/update",
            {"section": "llm", "key": "api_key", "value": "private-request-key"},
        ),
        (
            "/api/security/vault/store",
            {"key_name": "OPENAI_API_KEY", "value": "private-request-key"},
        ),
    ],
)
def test_locked_credential_requests_have_no_side_effects(
    api_client, monkeypatch, path, body
):
    import os

    client, state, factory, swap = api_client
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    response = client.post(path, json=body)
    assert (
        response.status_code == 503
        and response.json()["detail"]["code"] == "vault_locked"
    )
    assert "private-request-key" not in response.text
    assert "OPENAI_API_KEY" not in os.environ
    state.config.update_settings.assert_not_called()
    state.config.save_credentials.assert_not_called()
    state.provider_catalog.configure.assert_not_called()
    swap.assert_not_called()
    factory.assert_not_called()


def test_locked_vault_delete_returns_typed_refusal(api_client):
    client, state, factory, swap = api_client
    response = client.delete("/api/security/vault/fixture")
    assert (
        response.status_code == 503
        and response.json()["detail"]["code"] == "vault_locked"
    )
    factory.assert_not_called()


def test_credential_free_local_routing_stays_usable(api_client):
    client, state, factory, swap = api_client
    response = client.post(
        "/api/config/update",
        json={"section": "llm", "key": "call_site_tiers", "value": {"chat": "cheap"}},
    )
    assert response.status_code == 200 and response.json()["ok"]
    swap.assert_awaited_once()
    factory.assert_not_called()


def test_real_encrypted_memory_restores_only_with_authenticated_injection(
    deferred_mode, tmp_path
):
    import sqlite3
    from memory.store import MemoryStore
    from memory.at_rest import encrypt_memory_db

    path = tmp_path / "memory.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE fixture_payload(value TEXT)")
        db.execute(
            "INSERT INTO fixture_payload VALUES ('authenticated existing history')"
        )
    opened = SimpleNamespace(_master_key=lambda: bytes(range(32)))
    encrypt_memory_db(vault=opened, db_path=path, shred_plaintext=True)
    encrypted = path.with_name("memory.db.enc").read_bytes()
    assert not path.exists()
    with pytest.raises(VaultLockedRefusal):
        MemoryStore(db_path=str(path))
    assert not path.exists()
    memory = MemoryStore(db_path=str(path), authenticated_vault=opened)
    try:
        with sqlite3.connect(path) as db:
            assert (
                db.execute("SELECT value FROM fixture_payload").fetchone()[0]
                == "authenticated existing history"
            )
        assert path.with_name("memory.db.enc").read_bytes() == encrypted
    finally:
        memory.close()
    deferred_mode[0].assert_not_called()
    deferred_mode[1].assert_not_called()


def test_authenticated_conflicting_plaintext_memory_refuses_both_sources(
    deferred_mode, tmp_path
):
    from memory.store import MemoryStore

    path = tmp_path / "memory.db"
    path.write_bytes(b"unreviewed plaintext")
    encrypted = tmp_path / "memory.db.enc"
    encrypted.write_bytes(b"encrypted checkpoint")
    with pytest.raises(VaultLockedRefusal):
        MemoryStore(db_path=str(path), authenticated_vault=object())
    assert path.read_bytes() == b"unreviewed plaintext"
    assert encrypted.read_bytes() == b"encrypted checkpoint"


def test_fresh_native_state_constructor_keeps_local_memory_without_vault(
    deferred_mode, monkeypatch
):
    from api import state as module

    skipped = Mock(side_effect=AssertionError("legacy boot hydration must not run"))
    monkeypatch.setattr(module.BrainState, "_load_stored_credentials", skipped)
    brain = module.BrainState()
    try:
        assert brain.memory is not None and brain.vault is None
        assert brain.vault_coordinator.status()["state"] == "locked"
        assert brain._native_bootstrap_required is False
        skipped.assert_not_called()
        deferred_mode[0].assert_not_called()
        deferred_mode[1].assert_not_called()
    finally:
        brain.memory.close()


@pytest.mark.asyncio
async def test_encrypted_native_state_stays_reachable_and_bootstrap_dormant(
    deferred_mode, monkeypatch
):
    from api import state as module
    from config.loader import feral_data_home

    home = feral_data_home()
    home.mkdir(parents=True, exist_ok=True)
    encrypted = home / "memory.db.enc"
    encrypted.write_bytes(b"locked fixture ciphertext")
    skipped = Mock(side_effect=AssertionError("legacy hydration must not run"))
    monkeypatch.setattr(module.BrainState, "_load_stored_credentials", skipped)
    brain = module.BrainState()
    assert brain.memory is None and brain._native_bootstrap_required is True
    await brain.init()
    assert brain.orchestrator is None and brain.provider_catalog is None
    assert brain.vault_coordinator.status()["state"] == "locked"
    assert encrypted.read_bytes() == b"locked fixture ciphertext"
    assert not (home / "memory.db").exists()
    assert brain._boot_report.degraded_count >= 1
    deferred_mode[0].assert_not_called()
    deferred_mode[1].assert_not_called()
