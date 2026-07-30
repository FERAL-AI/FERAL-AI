"""The LLM failover chain must only contain providers the runtime can dial.

``SUPPORTED_RUNTIME_PROVIDERS`` is the set with a real adapter in
``agents.llm_provider``. Before this, ``ConfigLoader._derive_fallback_providers``
returned an operator-supplied ``llm.fallback_providers`` verbatim, so an entry
like ``bedrock`` (which has a catalog descriptor but no runtime binding) entered
the chain and burned a hop on every turn, logging "Provider 'bedrock' has no
runtime adapter". The auto-derived branch had the same hole: its ``_KEY_MAP``
lists credentials we can read (``cohere``, ``mistral``, ``xai``) that are wider
than the set we can dial.

Live symptom on the maintainer's brain, 2026-07: 14 consecutive
``chat_with_failover exhausted`` against 1 successful call.
"""

from __future__ import annotations

from agents.llm_provider import SUPPORTED_RUNTIME_PROVIDERS
from config.loader import ConfigLoader


class TestFailoverChainValidation:
    def test_unrunnable_provider_is_dropped_from_operator_chain(self):
        """The exact chain from the maintainer's settings.json."""
        chain = ["openai", "bedrock", "openrouter", "deepseek", "gemini", "kimi"]
        kept = ConfigLoader._drop_unrunnable_providers(chain, source="test")

        assert "bedrock" not in kept
        assert kept == ["openai", "openrouter", "deepseek", "gemini", "kimi"]

    def test_credential_readable_but_undialable_providers_are_dropped(self):
        """_KEY_MAP can read these; the runtime has no adapter for them."""
        kept = ConfigLoader._drop_unrunnable_providers(
            ["cohere", "mistral", "xai", "anthropic"], source="test",
        )
        assert kept == ["anthropic"]

    def test_supported_providers_survive_untouched(self):
        chain = ["openai", "anthropic", "gemini", "groq", "deepseek", "openrouter"]
        assert ConfigLoader._drop_unrunnable_providers(chain, source="test") == chain

    def test_order_is_preserved(self):
        """Failover order is a priority list; filtering must not reorder it."""
        chain = ["gemini", "bedrock", "openai", "cohere", "anthropic"]
        assert ConfigLoader._drop_unrunnable_providers(chain, source="test") == [
            "gemini", "openai", "anthropic",
        ]

    def test_empty_chain_is_a_noop(self):
        assert ConfigLoader._drop_unrunnable_providers([], source="test") == []

    def test_every_kept_provider_is_actually_supported(self):
        """The filter's contract, stated against the source of truth."""
        chain = ["openai", "bedrock", "cohere", "mistral", "anthropic", "nonsense"]
        for prov in ConfigLoader._drop_unrunnable_providers(chain, source="test"):
            assert prov in SUPPORTED_RUNTIME_PROVIDERS

    def test_drop_is_logged_not_silent(self, caplog):
        """A dropped provider must be visible, or a typo is undiagnosable."""
        import logging

        with caplog.at_level(logging.WARNING):
            ConfigLoader._drop_unrunnable_providers(["openai", "bedrock"], source="test")
        assert "bedrock" in caplog.text
