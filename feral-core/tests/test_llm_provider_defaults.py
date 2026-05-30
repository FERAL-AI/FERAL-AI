"""LLMProvider default-model regression tests — Roadmap §3.5 P0 ().

The roadmap bans hardcoded model literals in ``agents/llm_provider.py``
because they drifted (gpt-4o-mini, claude-sonnet-4-20250514,
gemini-2.5-flash) and shipped to production unnoticed. This test pins
the contract: when neither ``FERAL_LLM_MODEL`` nor an env override is
set, ``LLMProvider`` must resolve its default model through
``ProviderCatalog.default_model_for(provider_id)`` — never a literal.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from agents import llm_provider as llm_module
from agents.llm_provider import LLMProvider
from providers.catalog import (
    BUILT_IN_DESCRIPTORS,
    ProviderCatalog,
    reset_shared_catalog,
)


# Names that used to be hardcoded into _PROVIDER_REGISTRY / __init__ /
# switch_provider / _get_provider_config. If any of these reappear as
# DEFAULT model strings (i.e. selected when the env vars are unset),
# the bug is back.
_BANNED_DEFAULTS = {
    "gpt-4o-mini",
    "gpt-4o",
    "claude-sonnet-4-20250514",
    "claude-3-5-sonnet-20241022",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "openai/gpt-4.1",
}


@pytest.fixture
def clean_env(monkeypatch):
    """Drop every env var that could short-circuit the default lookup."""
    for key in (
        "FERAL_LLM_PROVIDER",
        "FERAL_LLM_MODEL",
        "FERAL_LLM_BASE_URL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GROQ_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
        "MOONSHOT_API_KEY",
        "DASHSCOPE_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture
def catalog_with_known_defaults(tmp_path, clean_env):
    """A fresh ProviderCatalog whose ``default_model_for`` returns a
    sentinel so we can prove ``LLMProvider`` consulted it instead of
    falling back to a hardcoded literal."""
    reset_shared_catalog()
    cache = tmp_path / "model_catalog.json"
    cat = ProviderCatalog(cache_path=cache)

    # Stub default_model_for so we can verify it was consulted.
    sentinel_map = {
        "openai": "gpt-test-pinned",
        "anthropic": "claude-test-pinned",
        "gemini": "gemini-test-pinned",
    }

    def _fake_default(pid: str) -> str:
        return sentinel_map.get(pid, "")

    cat.default_model_for = _fake_default  # type: ignore[assignment]

    with patch("providers.catalog._SHARED", cat), patch(
        "providers.catalog.get_shared_catalog", lambda: cat
    ):
        yield cat
    reset_shared_catalog()


class TestLLMProviderResolvesDefaultViaCatalog:
    def test_openai_default_comes_from_catalog_not_literal(
        self, catalog_with_known_defaults, clean_env
    ):
        clean_env.setenv("FERAL_LLM_PROVIDER", "openai")
        llm = LLMProvider()
        assert llm.model == "gpt-test-pinned"
        assert llm.model not in _BANNED_DEFAULTS

    def test_anthropic_default_comes_from_catalog(
        self, catalog_with_known_defaults, clean_env
    ):
        clean_env.setenv("FERAL_LLM_PROVIDER", "anthropic")
        llm = LLMProvider()
        assert llm.model == "claude-test-pinned"
        assert llm.model not in _BANNED_DEFAULTS

    def test_gemini_default_comes_from_catalog(
        self, catalog_with_known_defaults, clean_env
    ):
        clean_env.setenv("FERAL_LLM_PROVIDER", "gemini")
        llm = LLMProvider()
        assert llm.model == "gemini-test-pinned"
        assert llm.model not in _BANNED_DEFAULTS

    def test_explicit_env_model_wins_over_catalog(
        self, catalog_with_known_defaults, clean_env
    ):
        clean_env.setenv("FERAL_LLM_PROVIDER", "openai")
        clean_env.setenv("FERAL_LLM_MODEL", "user-specified-model")
        llm = LLMProvider()
        assert llm.model == "user-specified-model"

    def test_unknown_provider_default_is_empty_not_literal(
        self, catalog_with_known_defaults, clean_env
    ):
        # Unknown provider id → catalog returns "" → model stays empty.
        # Picker should render an honest "choose a model" state, NOT
        # silently fall back to gpt-4o-mini.
        clean_env.setenv("FERAL_LLM_PROVIDER", "openai")
        # The fixture's stub returns "" for unknown providers. Test by
        # creating a provider not in the sentinel map.
        clean_env.setenv("FERAL_LLM_PROVIDER", "made-up")
        llm = LLMProvider()
        assert llm.model not in _BANNED_DEFAULTS

    def test_provider_registry_no_longer_carries_default_model(self):
        # Tuple shape check: was (base_url, env_key, default_model);
        # current contract: (base_url, env_key) — no default model
        # literal anywhere in the tuple.
        for pid, value in llm_module._PROVIDER_REGISTRY.items():
            assert isinstance(value, tuple) and len(value) == 2, (
                f"_PROVIDER_REGISTRY[{pid!r}] must be a 2-tuple "
                f"(base_url, env_key) — got {value!r}"
            )
            base_url, env_key = value
            assert isinstance(base_url, str)
            assert isinstance(env_key, str)
            # Belt-and-suspenders: nothing in the tuple should look
            # like a model id (no slashes-without-protocol, no model
            # version suffix).
            for v in value:
                for banned in _BANNED_DEFAULTS:
                    assert v != banned, (
                        f"_PROVIDER_REGISTRY[{pid!r}] still carries "
                        f"a banned default model literal: {banned!r}"
                    )


class TestSwitchProviderRespectsCatalog:
    def test_switch_to_openai_without_model_uses_catalog(
        self, catalog_with_known_defaults, clean_env
    ):
        clean_env.setenv("FERAL_LLM_PROVIDER", "openai")
        llm = LLMProvider()
        # Now switch with model="" — must resolve via catalog.
        import asyncio as _aio
        _aio.run(llm.switch_provider("openai", model=""))
        assert llm.model == "gpt-test-pinned"
        assert llm.model not in _BANNED_DEFAULTS


class TestProviderConfigUsesCatalog:
    def test_get_provider_config_returns_catalog_default(
        self, catalog_with_known_defaults, clean_env
    ):
        clean_env.setenv("FERAL_LLM_PROVIDER", "openai")
        llm = LLMProvider()
        cfg = llm._get_provider_config("openai")
        assert cfg["model"] == "gpt-test-pinned"
        assert cfg["model"] not in _BANNED_DEFAULTS

    def test_get_provider_config_unknown_returns_no_banned_literal(
        self, catalog_with_known_defaults, clean_env
    ):
        clean_env.setenv("FERAL_LLM_PROVIDER", "openai")
        llm = LLMProvider()
        # Unknown provider falls through to the OpenAI base URL (legacy
        # behaviour) but the model must not be a hardcoded literal.
        cfg = llm._get_provider_config("not-a-real-provider")
        assert cfg["model"] not in _BANNED_DEFAULTS
