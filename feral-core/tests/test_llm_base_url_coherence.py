"""A base_url override must belong to the provider it overrides.

`~/.feral/settings.json` on a real install carried:

    provider: openrouter
    base_url: https://api.anthropic.com/v1
    model:    anthropic/claude-sonnet-5

The OpenRouter adapter honours base_url, so every call posted an
OpenRouter key to Anthropic. The brain logged 610 x
`HTTP 401 - authentication_error: Invalid Anthropic API Key`, failed
over silently to another provider, and reported
`provider: openrouter` as healthy the whole time. The boot report said
`LLMProvider OK`.

Nothing detected it because nothing ever compared the two fields. This
pins the check.
"""

from __future__ import annotations

import pytest

from providers.catalog import provider_base_url_mismatch


class TestMismatchIsDetected:
    def test_the_exact_configuration_that_broke_this_install(self):
        problem = provider_base_url_mismatch(
            "openrouter", "https://api.anthropic.com/v1"
        )
        assert problem is not None
        assert "openrouter" in problem and "anthropic" in problem

    @pytest.mark.parametrize(
        "provider, url",
        [
            ("openai", "https://api.anthropic.com/v1"),
            ("anthropic", "https://api.openai.com/v1"),
            ("deepseek", "https://openrouter.ai/api/v1"),
        ],
    )
    def test_any_cross_provider_override_is_flagged(self, provider, url):
        assert provider_base_url_mismatch(provider, url) is not None


class TestLegitimateOverridesArePermitted:
    """The check must not become a reason people stop using base_url.
    Proxies, gateways and self-hosted endpoints are the whole point of
    the field."""

    def test_no_override_is_fine(self):
        assert provider_base_url_mismatch("openrouter", "") is None
        assert provider_base_url_mismatch("openrouter", None) is None

    def test_the_providers_own_endpoint_is_fine(self):
        assert provider_base_url_mismatch(
            "openrouter", "https://openrouter.ai/api/v1"
        ) is None

    def test_a_local_or_private_endpoint_is_fine(self):
        """Ollama, LM Studio, a corporate gateway, a dev proxy."""
        for url in (
            "http://localhost:11434/v1",
            "http://127.0.0.1:8080/v1",
            "https://llm-gateway.internal.example.com/v1",
            "https://my-azure-proxy.example.net/openai/v1",
        ):
            assert provider_base_url_mismatch("openai", url) is None, url

    def test_an_unknown_provider_is_not_second_guessed(self):
        assert provider_base_url_mismatch("some-new-provider",
                                          "https://api.anthropic.com/v1") is None
