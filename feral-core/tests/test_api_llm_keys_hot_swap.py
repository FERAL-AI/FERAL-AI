"""Cross-cut #1 (v2026.5.42) — WebUI key hot-swap pins.

Pins the REST hooks added in
``ASOS/AUDIT-r14/round3/findings/lane4-vault-keys-hot-path.md`` fix
#6:

* ``POST /api/llm/providers/{pid}/keys`` (with ``set_active=true``)
  reconfigures the running LLMProvider so the next chat turn uses
  the new key without a brain restart.
* ``POST /api/llm/providers/{pid}/keys/active`` flips the labeled
  selection AND propagates the swap to the running provider.
* Failure of the hot-swap (e.g. probe rejects) surfaces in the
  response under ``reconfigured`` instead of bubbling a 500.

The runtime probe is stubbed to "ok" so the assertions are about the
slot write + the rebuilt httpx client's ``Authorization`` header,
not the upstream handshake.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from providers.catalog import ProviderCatalog


pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def fake_keychain(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.delenv("FERAL_VAULT_RECOVERY_CODE", raising=False)
    for _envvar in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "GROQ_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
        "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY", "GOOGLE_API_KEY",
        "FERAL_LLM_BASE_URL",
    ):
        monkeypatch.delenv(_envvar, raising=False)

    store: dict[tuple[str, str], str] = {}
    import security.vault as vault_mod
    monkeypatch.setattr(
        vault_mod, "_keyring_get_password",
        lambda service, username: store.get((service, username)),
    )
    monkeypatch.setattr(
        vault_mod, "_keyring_set_password",
        lambda service, username, password: store.__setitem__(
            (service, username), password,
        ),
    )
    monkeypatch.setattr(
        vault_mod, "_keyring_delete_password",
        lambda service, username: store.pop((service, username), None),
    )
    # Block local-model auto-detection so the brain_client LLMProvider
    # boot lands on an empty key slot (the test wants to drive the
    # slot through REST POSTs) rather than falling back to a real
    # Ollama / LM Studio the developer happens to have running.
    from agents import llm_provider as _llm_mod
    monkeypatch.setattr(
        _llm_mod.LLMProvider, "_detect_ollama", staticmethod(lambda: None),
    )
    monkeypatch.setattr(
        _llm_mod.LLMProvider, "_detect_lmstudio", staticmethod(lambda: None),
    )
    vault_mod.reset_vault()
    yield store
    vault_mod.reset_vault()


@pytest.fixture
def stub_chat_probe(monkeypatch):
    async def _ok(self):
        return True, "ok"

    from agents import llm_provider as _llm_mod
    monkeypatch.setattr(
        _llm_mod.LLMProvider, "_probe_chat_availability", _ok,
    )


@pytest.fixture
def brain_client(tmp_path, fake_keychain, stub_chat_probe, monkeypatch):
    """Boot a TestClient with a real LLMProvider on a mock orchestrator
    so the hot-swap REST hooks can write through to a live instance."""
    monkeypatch.setenv("FERAL_LLM_PROVIDER", "openai")
    monkeypatch.setenv("FERAL_LLM_MODEL", "gpt-4o")

    catalog = ProviderCatalog(cache_path=tmp_path / "cache.json")

    mock_config = MagicMock()
    _store: dict = {"llm": {"provider": "openai", "model": "gpt-4o", "base_url": ""}}
    mock_config.get.side_effect = lambda s, k, d=None: _store.get(s, {}).get(k, d)
    mock_config.update_settings.side_effect = (
        lambda s, k, v: _store.setdefault(s, {}).__setitem__(k, v)
    )

    from security.vault import get_vault
    vault = get_vault()

    from agents.llm_provider import LLMProvider
    llm = LLMProvider()

    orchestrator = MagicMock()
    orchestrator.llm = llm

    mock_state = MagicMock()
    mock_state.provider_catalog = catalog
    mock_state.config = mock_config
    mock_state.vault = vault
    mock_state.orchestrator = orchestrator

    with patch("api.state.state", mock_state), patch(
        "api.routes.llm.state", mock_state,
    ):
        from api.server import app
        client = TestClient(app, raise_server_exceptions=False)
        yield client, llm, vault


def test_post_keys_set_active_hot_swaps_running_provider(brain_client):
    """``POST /api/llm/providers/openai/keys`` with
    ``set_active=true`` must land the new secret on the running
    ``LLMProvider`` so the next chat turn ships with the new key —
    no restart, no Save-&-switch round-trip."""
    client, llm, _ = brain_client

    r = client.post(
        "/api/llm/providers/openai/keys",
        json={"label": "prod", "api_key": "sk-hot-swap-prod", "set_active": True},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["key"]["label"] == "prod"
    assert body["key"]["is_active"] is True
    assert body["reconfigured"]["ok"] is True

    assert llm.api_key == "sk-hot-swap-prod"
    auth = llm.client.headers.get("Authorization", "")
    assert auth == "Bearer sk-hot-swap-prod"


def test_post_keys_active_flips_running_provider(brain_client):
    """Add two labeled keys with one active, then flip the active
    pointer via ``POST .../keys/active``. The running ``LLMProvider``
    must follow the swap on the very next request — no restart."""
    client, llm, _ = brain_client

    r1 = client.post(
        "/api/llm/providers/openai/keys",
        json={"label": "dev", "api_key": "sk-dev-1", "set_active": False},
    )
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        "/api/llm/providers/openai/keys",
        json={"label": "prod", "api_key": "sk-prod-1", "set_active": True},
    )
    assert r2.status_code == 200, r2.text
    assert llm.api_key == "sk-prod-1"

    r3 = client.post(
        "/api/llm/providers/openai/keys/active",
        json={"label": "dev"},
    )
    assert r3.status_code == 200, r3.text
    body = r3.json()
    assert body["active_label"] == "dev"
    assert body["reconfigured"]["ok"] is True

    assert llm.api_key == "sk-dev-1"
    auth = llm.client.headers.get("Authorization", "")
    assert auth == "Bearer sk-dev-1"


def test_post_keys_no_set_active_does_not_touch_running_provider(brain_client):
    """When the operator stores a key without flipping the active
    pointer (the "save it for later" flow), the running provider's
    key must NOT change — otherwise we'd surprise-swap on every
    add."""
    client, llm, _ = brain_client

    r0 = client.post(
        "/api/llm/providers/openai/keys",
        json={"label": "prod", "api_key": "sk-active", "set_active": True},
    )
    assert r0.status_code == 200, r0.text
    assert llm.api_key == "sk-active"

    r = client.post(
        "/api/llm/providers/openai/keys",
        json={"label": "spare", "api_key": "sk-spare", "set_active": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["key"]["is_active"] is False
    assert "reconfigured" not in body

    assert llm.api_key == "sk-active"
