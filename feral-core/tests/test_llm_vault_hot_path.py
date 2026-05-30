"""Cross-cut #1 (v2026.5.42) — labeled vault keys land on the LLM hot path.

Pins the resolver wired in
``ASOS/AUDIT-r14/round3/findings/lane4-vault-keys-hot-path.md``:

* ``LLMProvider.__init__`` consults the labeled-keys vault when env
  is unset (boot hydration parity).
* ``set_active_label`` + ``reconfigure(api_key=get_active_key(...))``
  swap the running provider's key without a process restart.
* ``_build_client`` re-resolves an empty slot from the vault before
  baking the ``Authorization`` header.
* ``_get_provider_config`` (failover candidate path) prefers the
  labeled secret over a bare ``os.getenv`` so failover keeps working
  when the operator only ever set the key via ``feral key add``.

These tests intentionally do NOT exercise the network — the runtime
probe in ``switch_provider`` is monkey-patched to "ok" so the
hot-swap path can be observed deterministically. Real upstream
auth is covered by ``test_api_llm_providers.py`` and the  probe
suite.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


pytestmark = pytest.mark.no_auto_feral_home


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_keychain(tmp_path, monkeypatch):
    """Per-test in-memory keychain + isolated FERAL_HOME.

    Mirrors ``test_vault_encryption.py``'s pattern so the BlindVault
    that ``security.vault_keys`` writes through lands on disk in
    ``tmp_path`` and decrypts via a dict-backed master key.

    Also resets the ``security.vault._vault`` singleton so each test
    gets a fresh on-disk vault rooted at its own tmp_path. The
    autouse ``api.state`` module-level boot reads
    ``~/.feral/credentials.json`` on first import and writes its
    contents into ``os.environ`` — we delenv every known provider
    key here so the upstream snapshot can never bleed into a test
    that asserts on the resolver's fallback order.
    """
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.delenv("FERAL_VAULT_RECOVERY_CODE", raising=False)
    for _envvar in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "GROQ_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
        "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY", "GOOGLE_API_KEY",
        "FERAL_LLM_BASE_URL", "FERAL_LLM_PROVIDER", "FERAL_LLM_MODEL",
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

    # Local-model detection is best-effort over HTTP; in tests we want
    # an empty key slot to fall through to the labeled-vault resolver
    # rather than discover a real Ollama / LM Studio that happens to
    # be running on the developer's box.
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
    """Bypass the real ``_probe_chat_availability`` so ``switch_provider``
    / ``reconfigure`` don't hit the network. Tests that assert on
    hot-swap timing care about the slot write, not the upstream
    handshake."""
    async def _ok(self):
        return True, "ok"

    from agents import llm_provider as _llm_mod
    monkeypatch.setattr(
        _llm_mod.LLMProvider, "_probe_chat_availability", _ok,
    )


# ─────────────────────────────────────────────────────────────────────
# Resolver order — labeled active → legacy default-namespace → env
# ─────────────────────────────────────────────────────────────────────


def test_get_active_key_returns_labeled_active_secret(fake_keychain, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from security.vault_keys import add_provider_key, get_active_key

    add_provider_key("openai", "dev", "sk-dev")
    add_provider_key("openai", "prod", "sk-prod", set_active=True)

    assert get_active_key("openai") == "sk-prod"


def test_get_active_key_falls_back_to_legacy_namespace(fake_keychain, monkeypatch):
    """No labeled keys; legacy default-namespace OPENAI_API_KEY entry
    must still win over an unset env var."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from security.vault import get_vault
    from security.vault_keys import get_active_key

    get_vault().set_credential("OPENAI_API_KEY", "sk-legacy")

    assert get_active_key("openai") == "sk-legacy"


def test_get_active_key_falls_back_to_env(fake_keychain, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    from security.vault_keys import get_active_key

    assert get_active_key("openai") == "sk-env"


def test_get_active_key_returns_empty_when_unconfigured(fake_keychain, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from security.vault_keys import get_active_key

    assert get_active_key("openai") == ""


# ─────────────────────────────────────────────────────────────────────
# LLMProvider hot path
# ─────────────────────────────────────────────────────────────────────


def test_boot_hydration_picks_up_labeled_key_when_env_unset(
    fake_keychain, monkeypatch,
):
    """Pre-Cross-cut-#1, ``LLMProvider.__init__`` snapshotted
    ``os.getenv(ANTHROPIC_API_KEY, "")`` and never consulted the
    labeled-keys vault. With the resolver wired in, an operator who
    ran ``feral key add --provider anthropic --set-active`` and
    rebooted must see the labeled secret on the running provider."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("FERAL_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("FERAL_LLM_MODEL", "claude-opus-4-7")

    from security.vault_keys import add_provider_key
    add_provider_key(
        "anthropic", "default", "sk-ant-test", set_active=True,
    )

    from agents.llm_provider import LLMProvider
    llm = LLMProvider()

    assert llm.provider == "anthropic"
    assert llm.api_key == "sk-ant-test"


async def test_two_labeled_keys_set_active_swaps_chat_key(
    fake_keychain, monkeypatch, stub_chat_probe,
):
    """Storing two labeled keys then flipping the active label must
    propagate through ``reconfigure(api_key=get_active_key(...))``
    without a process restart. This is the canonical "add a key, set
    it active, send a chat turn" flow the operator's CLI prompt does."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("FERAL_LLM_PROVIDER", "openai")
    monkeypatch.setenv("FERAL_LLM_MODEL", "gpt-4o")

    from security.vault_keys import (
        add_provider_key, get_active_key, set_active_label,
    )

    add_provider_key("openai", "dev", "sk-dev")
    add_provider_key("openai", "prod", "sk-prod", set_active=True)

    from agents.llm_provider import LLMProvider
    llm = LLMProvider()
    assert llm.api_key == "sk-prod"

    set_active_label("openai", "dev")
    result = await llm.reconfigure(
        provider="openai",
        model=llm.model,
        api_key=get_active_key("openai"),
        base_url=llm.base_url,
    )

    assert result["ok"] is True
    assert llm.api_key == "sk-dev"
    # Authorization header on the rebuilt client carries the new key.
    auth = llm.client.headers.get("Authorization", "")
    assert auth == "Bearer sk-dev"


def test_build_client_late_binds_from_vault_when_slot_empty(
    fake_keychain, monkeypatch,
):
    """An ``LLMProvider`` whose ``api_key`` slot is empty at the
    moment ``_build_client`` runs must consult the labeled-vault
    overlay rather than baking a bare ``Authorization: Bearer``
    header. Pre-fix, the slot stayed empty and the next chat turn
    sent a header with no token."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from security.vault_keys import add_provider_key
    add_provider_key("openai", "prod", "sk-late", set_active=True)

    from agents.llm_provider import LLMProvider
    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = "openai"
    llm.base_url = "https://api.openai.com/v1"
    llm.api_key = ""

    client = llm._build_client()
    try:
        assert llm.api_key == "sk-late"
        assert client.headers.get("Authorization") == "Bearer sk-late"
    finally:
        # AsyncClient close needs an event loop; sync close is fine.
        try:
            client.close()
        except Exception:
            pass


def test_get_provider_config_prefers_labeled_for_failover_candidate(
    fake_keychain, monkeypatch,
):
    """Cross-cut #1: failover candidates must read from the labeled
    vault too, otherwise an operator who only configured the key via
    ``feral key add`` loses cross-provider failover."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from security.vault_keys import add_provider_key
    add_provider_key("anthropic", "prod", "sk-fb", set_active=True)

    from agents.llm_provider import LLMProvider
    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = "openai"
    llm.base_url = "https://api.openai.com/v1"
    llm.api_key = "sk-primary"
    llm.model = "gpt-4o"

    cfg = llm._get_provider_config("anthropic")
    assert cfg["api_key"] == "sk-fb"
    assert cfg["supported"] is True
