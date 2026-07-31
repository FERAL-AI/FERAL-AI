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
        """``_KEY_MAP`` can read a credential the runtime cannot dial.

        ``cohere`` is the live example: the loader knows how to read
        ``COHERE_API_KEY`` but no adapter exists, so a user with a Cohere key
        would otherwise get an undialable chain entry without ever editing
        settings.json. ``bedrock`` has a catalog descriptor and a boto3
        adapter but no ``_PROVIDER_REGISTRY`` binding.

        NOTE: xai / mistral / minimax / zai were in this list until runtime
        bindings were added for them, which is why the assertion below is
        written against ``SUPPORTED_RUNTIME_PROVIDERS`` rather than a
        hardcoded expectation that goes stale the same way.
        """
        kept = ConfigLoader._drop_unrunnable_providers(
            ["cohere", "bedrock", "anthropic", "openai"], source="test",
        )
        assert "cohere" not in kept
        assert "bedrock" not in kept
        assert kept == ["anthropic", "openai"]
        for prov in kept:
            assert prov in SUPPORTED_RUNTIME_PROVIDERS

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

    def test_non_chat_models_are_not_dialed(self):
        """A non-chat model can only 404; classify it before the wire call.

        Live 2026-07-30: the ``openai`` fallback hop resolved to a non-chat id
        and burned a round trip on
          HTTP 404 "This is not a chat model and thus not supported in the
          v1/chat/completions endpoint"
        on every turn before falling through.
        """
        from agents.llm_provider import _chat_capability_of

        for model in ("text-embedding-3-small", "dall-e-3", "whisper-1", "babbage-002"):
            ok, cls = _chat_capability_of("openai", model)
            assert ok is False, f"{model} classified {cls}, should not be dialed"

    def test_chat_models_are_dialed(self):
        from agents.llm_provider import _chat_capability_of

        assert _chat_capability_of("openai", "gpt-4o")[0] is True

    def test_unrecognised_models_are_dialed(self):
        """Critical: an id the stale catalog cannot classify must still dial.

        `claude-opus-5` and `gpt-5.6-sol` classify as "unknown" until the
        catalog is refreshed. Excluding unknown would break every chain the
        moment a new frontier model is configured, which is the opposite of
        the intent. This mirrors the documented policy on
        ``providers.model_classes.classify``.
        """
        from agents.llm_provider import _chat_capability_of

        for provider, model in (("anthropic", "claude-opus-5"),
                                ("openai", "gpt-5.6-sol"),
                                ("openai", "some-unreleased-model-2027")):
            assert _chat_capability_of(provider, model)[0] is True, model

    def test_blank_model_is_dialed(self):
        """Empty means "use the provider default"; do not pre-emptively skip."""
        from agents.llm_provider import _chat_capability_of

        assert _chat_capability_of("openai", "")[0] is True

    def test_drop_is_logged_not_silent(self, caplog):
        """A dropped provider must be visible, or a typo is undiagnosable."""
        import logging

        with caplog.at_level(logging.WARNING):
            ConfigLoader._drop_unrunnable_providers(["openai", "bedrock"], source="test")
        assert "bedrock" in caplog.text
