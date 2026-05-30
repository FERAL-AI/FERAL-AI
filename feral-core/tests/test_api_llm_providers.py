"""Contract tests for /api/llm/providers + /api/llm/config routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from providers.catalog import ProviderCatalog


pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def client(tmp_path):
    catalog = ProviderCatalog(cache_path=tmp_path / "cache.json")
    mock_config = MagicMock()
    _store: dict = {"llm": {"provider": "", "model": "", "base_url": ""}, "audio": {}}

    def _get(section, key, default=None):
        return _store.get(section, {}).get(key, default)

    def _update(section, key, value):
        _store.setdefault(section, {})[key] = value

    mock_config.get.side_effect = _get
    mock_config.update_settings.side_effect = _update

    mock_vault = MagicMock()

    mock = MagicMock()
    mock.provider_catalog = catalog
    mock.config = mock_config
    mock.vault = mock_vault
    mock.orchestrator = None

    with patch("api.state.state", mock), patch("api.routes.llm.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), catalog, mock_config, mock_vault, _store


def test_list_providers_returns_all_builtin(client):
    c, catalog, _cfg, _vault, _store = client
    r = c.get("/api/llm/providers")
    assert r.status_code == 200
    body = r.json()
    ids = {p["id"] for p in body["providers"]}
    # Every built-in descriptor must be exposed to the v2 UI — not a
    # hardcoded subset. This protects against regressions where a
    # provider is added to BUILT_IN_DESCRIPTORS but the UI silently
    # ignores it (the whole reason Settings → Providers was skinny).
    required = {
        "openai", "anthropic", "gemini", "groq", "deepseek",
        "openrouter", "together", "fireworks", "bedrock",
        "ollama", "lmstudio",
    }
    missing = required - ids
    assert not missing, f"catalog is missing providers: {missing}"


def test_provider_descriptor_includes_alias_list(client):
    c, _, _, _, _ = client
    r = c.get("/api/llm/providers")
    entries = {p["id"]: p for p in r.json()["providers"]}
    assert "open ai" in entries["openai"]["aliases"]
    assert entries["openai"]["requires_api_key"] is True
    assert entries["ollama"]["supports_local"] is True


def test_get_provider_unknown_404(client):
    c, _, _, _, _ = client
    r = c.get("/api/llm/providers/not-real")
    assert r.status_code == 404


def test_list_provider_models_uses_catalog(client):
    c, catalog, _, _, _ = client
    from providers.catalog import CachedModelList
    import time as _time
    catalog._models["openai"] = CachedModelList(
        models=["gpt-x", "gpt-y"], last_refresh=_time.time(), source="cache",
    )
    r = c.get("/api/llm/providers/openai/models?live=false")
    assert r.status_code == 200
    body = r.json()
    assert body["models"] == ["gpt-x", "gpt-y"]
    assert body["source"] == "cache"


def test_list_provider_models_unknown_404(client):
    c, _, _, _, _ = client
    r = c.get("/api/llm/providers/not-real/models")
    assert r.status_code == 404


def test_probe_calls_adapter(client):
    c, catalog, _, _, _ = client
    fake = MagicMock()
    fake.refresh_models = AsyncMock(return_value=["reachable-model"])
    catalog.register_adapter(type("X", (), {"provider_id": "openai",
                                             "refresh_models": fake.refresh_models,
                                             "list_models": lambda self: []})())
    r = c.post("/api/llm/providers/openai/probe")
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is True
    assert body["error"] == ""


def test_probe_unreachable_shape(client):
    c, catalog, _, _, _ = client
    from providers.base import BaseProvider

    class Broken(BaseProvider):
        provider_id = "openai"
        _models: list = []

        async def chat(self, *a, **k):
            raise NotImplementedError

        async def refresh_models(self):
            raise RuntimeError("boom")

    catalog.register_adapter(Broken())
    r = c.post("/api/llm/providers/openai/probe")
    body = r.json()
    assert body["reachable"] is False
    assert "boom" in body["error"]


def test_configure_stores_key_in_vault(client):
    c, catalog, _, vault, _ = client
    r = c.post(
        "/api/llm/providers/openai/configure",
        json={"api_key": "sk-live", "base_url": "https://openai.example"},
    )
    assert r.status_code == 200
    # Commit 1 widened "stored_by" from "setup_wizard" to "settings" — the
    # key routing is now a single pipe used by setup + Settings alike.
    vault.store.assert_called_with("OPENAI_API_KEY", "sk-live", stored_by="settings")
    body = r.json()
    assert body["persisted"]["vault"] is True


def test_configure_unknown_provider_404(client):
    c, _, _, _, _ = client
    r = c.post("/api/llm/providers/fake/configure", json={"api_key": "x"})
    assert r.status_code == 404


def test_configure_non_active_provider_does_not_touch_global_base_url(client):
    """Saving credentials for a provider that is NOT currently active
    must persist the key but must not clobber the global
    ``llm.base_url``. Otherwise, adding a key for a second provider
    (e.g. anthropic while openai is active) would silently repoint
    the active adapter at the wrong endpoint.
    """
    c, _catalog, cfg, _vault, store = client
    cfg.update_settings("llm", "provider", "openai")
    cfg.update_settings("llm", "base_url", "https://api.openai.com/v1")

    r = c.post(
        "/api/llm/providers/anthropic/configure",
        json={"api_key": "sk-ant", "base_url": "https://api.anthropic.com"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body.get("active_provider") is False
    # Key was still persisted for the non-active provider.
    assert body["persisted"]["vault"] is True
    # Global active base_url is UNCHANGED.
    assert store["llm"]["base_url"] == "https://api.openai.com/v1"
    # Active provider is UNCHANGED.
    assert store["llm"]["provider"] == "openai"


def test_configure_active_provider_updates_global_base_url(client):
    """When the user reconfigures the currently active provider, the
    global ``llm.base_url`` is fair game to update — that's the same
    flow the legacy "Reconfigure" button relied on."""
    c, _catalog, cfg, _vault, store = client
    cfg.update_settings("llm", "provider", "openai")
    cfg.update_settings("llm", "base_url", "https://api.openai.com/v1")

    r = c.post(
        "/api/llm/providers/openai/configure",
        json={"api_key": "sk-live", "base_url": "https://openai.example/v1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("active_provider") is True
    assert store["llm"]["base_url"] == "https://openai.example/v1"


def test_configure_non_active_resolves_through_active_alias(client):
    """The active-provider check must resolve aliases so the user's
    ``settings.json`` carrying ``"open ai"`` (a known alias) still
    matches the canonical ``openai`` descriptor when comparing.
    """
    c, _catalog, cfg, _vault, store = client
    cfg.update_settings("llm", "provider", "open ai")  # alias
    cfg.update_settings("llm", "base_url", "https://api.openai.com/v1")

    r = c.post(
        "/api/llm/providers/openai/configure",
        json={"base_url": "https://openai.example/v1"},
    )
    assert r.status_code == 200
    assert r.json().get("active_provider") is True
    assert store["llm"]["base_url"] == "https://openai.example/v1"


def test_get_config_returns_safe_shape(client):
    c, _, cfg, _, _ = client
    cfg.update_settings("llm", "provider", "openai")
    cfg.update_settings("llm", "model", "gpt-4o-mini")
    r = c.get("/api/llm/config")
    body = r.json()
    assert body["provider"] == "openai"
    assert body["model"] == "gpt-4o-mini"
    assert "api_key" not in body  # never return the key


def test_set_config_persists_and_stores_key(client):
    c, _, cfg, vault, store = client
    r = c.post(
        "/api/llm/config",
        json={
            "provider": "open ai",  # fuzzy alias
            "model": "gpt-new-model-not-yet-released",
            "api_key": "sk-new",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "openai"
    assert body["model"] == "gpt-new-model-not-yet-released"
    assert store["llm"]["provider"] == "openai"
    # See note in test_configure_stores_key_in_vault — stored_by unified.
    vault.store.assert_called_with("OPENAI_API_KEY", "sk-new", stored_by="settings")


def test_set_config_rejects_unknown_provider(client):
    c, _, _, _, _ = client
    r = c.post(
        "/api/llm/config",
        json={"provider": "not-real", "model": "x"},
    )
    assert r.status_code == 400


def test_models_endpoint_exposes_warning_field(client):
    """A 401-driven fallback must surface ``warning`` so the v2 picker
    can render a "key rejected" chip instead of silently lying."""
    c, catalog, _, _, _ = client
    from providers.catalog import CachedModelList
    import time as _time

    catalog._models["openai"] = CachedModelList(
        models=["gpt-fallback"],
        last_refresh=_time.time(),
        source="fallback",
        warning="provider rejected the API key (HTTP 401)",
    )
    r = c.get("/api/llm/providers/openai/models?live=false")
    assert r.status_code == 200
    body = r.json()
    assert body["warning"] == "provider rejected the API key (HTTP 401)"
    assert body["source"] == "fallback"


def test_force_refresh_bypasses_cache(client):
    """``?force=true`` (the Refresh button) must do a live fetch even
    when the disk cache is fresh."""
    c, catalog, _, _, _ = client
    from providers.base import BaseProvider
    from providers.catalog import CachedModelList
    import time as _time

    # Warm cache with stale-looking models that a regression-prone code
    # path might erroneously serve.
    catalog._models["openai"] = CachedModelList(
        models=["stale-cached"], last_refresh=_time.time(), source="cache",
    )

    class _LiveAdapter(BaseProvider):
        provider_id = "openai"
        _models: list = []
        refreshed = 0

        async def chat(self, *a, **kw):
            raise NotImplementedError

        async def refresh_models(self):
            type(self).refreshed += 1
            return ["forced-fresh-model"]

    catalog.register_adapter(_LiveAdapter())
    r = c.get("/api/llm/providers/openai/models?force=true")
    assert r.status_code == 200
    body = r.json()
    assert body["models"] == ["forced-fresh-model"]
    assert body["source"] == "live"
    assert _LiveAdapter.refreshed == 1


def test_configure_invalidates_cache_so_next_models_call_goes_live(client):
    """After saving a key the next /models call must hit the wire.

    Without invalidation the v2 picker keeps rendering the pre-key
    model list even after the user pasted a working key — that's the
    user-facing bug we're fixing.
    """
    c, catalog, _, _, _ = client
    from providers.base import BaseProvider
    from providers.catalog import CachedModelList
    import time as _time

    catalog._models["openai"] = CachedModelList(
        models=["pre-key-model"], last_refresh=_time.time(), source="cache",
    )

    r = c.post("/api/llm/providers/openai/configure", json={"api_key": "sk-fresh"})
    assert r.status_code == 200
    # configure() must have wiped the warm cache.
    assert "openai" not in catalog._models

    # Re-bind a deterministic adapter so the next call produces a known
    # live result instead of touching the real OpenAI API.
    class _PostKeyAdapter(BaseProvider):
        provider_id = "openai"
        _models: list = []

        async def chat(self, *a, **kw):
            raise NotImplementedError

        async def refresh_models(self):
            return ["post-key-model"]

    catalog.register_adapter(_PostKeyAdapter())
    r = c.get("/api/llm/providers/openai/models?live=true")
    body = r.json()
    assert body["models"] == ["post-key-model"]
    assert body["source"] == "live"


def test_models_endpoint_accepts_recommended_and_model_class(client):
    """The v2 pickers default to ``recommended=true&model_class=chat``
    so the dropdown surfaces the curated chat-ready subset. The route
    must thread both params into the catalog so embeddings / audio /
    image ids never reach the picker, and recommended-only shortlists
    surface the 2026-era flagships in tier order."""
    c, catalog, _, _, _ = client
    from providers.catalog import CachedModelList
    import time as _time

    # Mixed list: chat + embedding + audio + image — the exact shape
    # of raw /v1/models output that used to leak into the picker.
    raw = [
        "gpt-5.5-pro",                # recommended chat (flagship)
        "gpt-5.4",                    # recommended chat
        "gpt-4-turbo-preview",        # chat but NOT recommended
        "text-embedding-3-large",     # embedding — must drop
        "whisper-1",                  # audio — must drop
        "dall-e-3",                   # image — must drop
    ]
    catalog._models["openai"] = CachedModelList(
        models=list(raw), last_refresh=_time.time(), source="cache",
    )

    # model_class=chat alone — drops embeddings/audio/image, keeps
    # the non-recommended chat id.
    r = c.get("/api/llm/providers/openai/models?live=false&model_class=chat")
    assert r.status_code == 200
    chat_only = r.json()["models"]
    assert "text-embedding-3-large" not in chat_only
    assert "whisper-1" not in chat_only
    assert "dall-e-3" not in chat_only
    assert "gpt-5.5-pro" in chat_only
    assert "gpt-4-turbo-preview" in chat_only  # chat but not recommended

    # recommended=true further narrows to the curated shortlist and
    # tier-sorts the result (gpt-5.5-pro is rank 0 for openai).
    r = c.get(
        "/api/llm/providers/openai/models"
        "?live=false&model_class=chat&recommended=true"
    )
    body = r.json()
    assert body["models"][0] == "gpt-5.5-pro"
    assert "gpt-5.4" in body["models"]
    assert "gpt-4-turbo-preview" not in body["models"]


def test_models_endpoint_filter_is_projection_only(client):
    """Filtered responses must not mutate the catalog's canonical
    cached raw list — a subsequent unfiltered call still sees every
    id the provider advertised."""
    c, catalog, _, _, _ = client
    from providers.catalog import CachedModelList
    import time as _time

    raw = [
        "gpt-5.5-pro",
        "text-embedding-3-large",
        "whisper-1",
    ]
    catalog._models["openai"] = CachedModelList(
        models=list(raw), last_refresh=_time.time(), source="cache",
    )

    # Filtered call.
    r = c.get(
        "/api/llm/providers/openai/models"
        "?live=false&model_class=chat&recommended=true"
    )
    assert r.status_code == 200
    assert "whisper-1" not in r.json()["models"]

    # Canonical cache still holds every id the provider advertised,
    # in insertion order. No mutation during the filtered view.
    assert catalog._models["openai"].models == raw

    # Unfiltered call still returns the full raw list.
    r2 = c.get("/api/llm/providers/openai/models?live=false")
    assert r2.json()["models"] == raw


def test_models_endpoint_default_params_preserve_legacy_shape(client):
    """When ``model_class`` and ``recommended`` are absent the endpoint
    must behave exactly as before — full raw list, untouched. Pins the
    backward-compat contract so older callers (CLI wizard, legacy
    desktop builds) don't regress the moment the v2 pickers start
    sending filter params."""
    c, catalog, _, _, _ = client
    from providers.catalog import CachedModelList
    import time as _time

    raw = ["alpha", "beta", "gamma"]
    catalog._models["openai"] = CachedModelList(
        models=list(raw), last_refresh=_time.time(), source="cache",
    )
    r = c.get("/api/llm/providers/openai/models?live=false")
    assert r.status_code == 200
    assert r.json()["models"] == raw


def test_503_when_catalog_missing():
    mock = MagicMock()
    mock.provider_catalog = None
    mock.config = None
    with patch("api.state.state", mock), patch("api.routes.llm.state", mock):
        from api.server import app
        c = TestClient(app, raise_server_exceptions=False)
        r = c.get("/api/llm/providers")
        assert r.status_code == 503


# ----------------------------------------------------------------------
# Unsupported / unknown provider handling ( A3)
# ----------------------------------------------------------------------


def _orchestrated_client(tmp_path):
    """TestClient fixture with a real LLMProvider attached via
    orchestrator. Used to exercise ``/api/llm/status`` and
    ``/api/llm/switch`` against the runtime's unsupported-provider
    path end-to-end.
    """
    import os as _os
    from unittest.mock import patch as _patch
    from providers.catalog import ProviderCatalog
    from agents.llm_provider import LLMProvider

    catalog = ProviderCatalog(cache_path=tmp_path / "cache.json")
    _store: dict = {"llm": {"provider": "", "model": "", "base_url": ""}, "audio": {}}
    mock_config = MagicMock()
    mock_config.get.side_effect = lambda s, k, d=None: _store.get(s, {}).get(k, d)
    mock_config.update_settings.side_effect = (
        lambda s, k, v: _store.setdefault(s, {}).__setitem__(k, v)
    )

    with _patch.dict(_os.environ, {"FERAL_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"}, clear=False):
        with _patch.object(LLMProvider, "_detect_ollama", return_value=None):
            llm = LLMProvider()

    orchestrator = MagicMock()
    orchestrator.llm = llm

    mock = MagicMock()
    mock.provider_catalog = catalog
    mock.config = mock_config
    mock.vault = MagicMock()
    mock.orchestrator = orchestrator
    return mock, catalog, llm, _store


@pytest.fixture
def orchestrated(tmp_path):
    mock, catalog, llm, store = _orchestrated_client(tmp_path)
    with patch("api.state.state", mock), patch("api.routes.llm.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), catalog, llm, store


def test_llm_status_flags_unsupported_provider(orchestrated):
    c, _cat, llm, _store = orchestrated
    # Simulate a stale settings.json that named a catalog-only
    # descriptor: the runtime must report supported=False + available=False
    # even if OPENAI_API_KEY is still in the environment.
    llm.provider = "bedrock"
    llm.available = True  # lying available flag on purpose
    r = c.get("/api/llm/status")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "bedrock"
    assert body["supported"] is False
    assert body["available"] is False
    assert "no runtime adapter" in body["reason"]


def test_llm_status_marks_openai_supported(orchestrated):
    c, _cat, _llm, _store = orchestrated
    r = c.get("/api/llm/status")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "openai"
    assert body["supported"] is True


def test_llm_switch_rejects_catalog_only_provider(orchestrated):
    """``bedrock`` is in the catalog (so ``/api/llm/providers`` lists
    it) but has no runtime adapter. ``/api/llm/switch`` must reject
    it with 400 unless the caller supplies an explicit ``base_url``
    override — otherwise the legacy path would silently swap the
    adapter over to OpenAI's endpoint."""
    c, _cat, llm, _store = orchestrated
    r = c.post("/api/llm/switch", json={"provider": "bedrock"})
    assert r.status_code == 400
    assert "no runtime adapter" in r.json()["detail"]
    # Runtime state unchanged.
    assert llm.provider == "openai"


def test_llm_switch_rejects_totally_unknown_id(orchestrated):
    c, _cat, _llm, _store = orchestrated
    r = c.post("/api/llm/switch", json={"provider": "definitely-not-real"})
    assert r.status_code == 400
    # Error either says unknown provider or no runtime adapter.
    detail = r.json()["detail"]
    assert "unknown provider" in detail or "no runtime adapter" in detail


def test_llm_switch_accepts_custom_gateway_with_base_url(orchestrated):
    """Operator escape hatch: explicit ``base_url`` for a custom
    OpenAI-compatible gateway should round-trip through switch."""
    c, _cat, llm, _store = orchestrated
    r = c.post(
        "/api/llm/switch",
        json={
            "provider": "my-gateway",
            "base_url": "https://gw.example/v1",
            "api_key": "sk-gw",
            "model": "custom",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["supported"] is False  # honest: not in the runtime registry
    assert llm.base_url == "https://gw.example/v1"


def test_llm_switch_accepts_known_provider(orchestrated):
    c, _cat, llm, _store = orchestrated
    r = c.post(
        "/api/llm/switch",
        json={"provider": "anthropic", "api_key": "sk-ant", "model": "claude-test"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["provider"] == "anthropic"
    assert body["supported"] is True
    assert llm.provider == "anthropic"
    assert "anthropic.com" in llm.base_url


def test_set_llm_config_rejects_catalog_only_runtime_unsupported(client):
    """``/api/llm/config`` must also block catalog-only descriptors
    with no runtime adapter, so the Save-&-switch button never lands
    on a provider the runtime can't actually call."""
    c, _cat, _cfg, _vault, _store = client
    r = c.post(
        "/api/llm/config",
        json={"provider": "bedrock", "model": "claude-3"},
    )
    assert r.status_code == 400
    assert "no runtime adapter" in r.json()["detail"]


def test_set_llm_config_allows_catalog_only_with_base_url(client):
    """When the operator supplies an explicit base_url we treat it as
    a custom OpenAI-compatible gateway and accept the save."""
    c, _cat, _cfg, _vault, store = client
    r = c.post(
        "/api/llm/config",
        json={
            "provider": "bedrock",
            "model": "some-model",
            "base_url": "https://gw.example/v1",
        },
    )
    assert r.status_code == 200
    assert store["llm"]["provider"] == "bedrock"
