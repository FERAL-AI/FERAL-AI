"""One bad adapter must cost one provider, never the brain.

PR #206 added a `codex` provider whose ``__init__`` raised ``ValueError``
on an unrecognised ``FERAL_CODEX_SANDBOX``. ``ProviderCatalog`` builds
every adapter from its own ``__init__`` and caught only ``ImportError``,
so a typo in an env var documented in .env.example aborted catalog
construction for all sixteen providers and the brain did not boot.

Fixed in two places on purpose: the provider stops raising, and the
catalog stops trusting adapter constructors not to. Either alone would
close this instance; both are needed so the next adapter that validates
in its constructor cannot reopen it.
"""

from __future__ import annotations

import logging


class TestABadSandboxValueCannotStopTheBrain:
    def test_an_unrecognised_value_falls_back_instead_of_raising(self, monkeypatch):
        from providers.codex_provider import CodexProvider

        monkeypatch.setenv("FERAL_CODEX_SANDBOX", "workspace_write")  # underscore, not hyphen

        provider = CodexProvider()

        assert provider.sandbox == "read-only", (
            "an unusable sandbox value must land on the safe mode, so the "
            "failure direction is 'less capable than asked', never more"
        )

    def test_the_catalog_still_builds_with_a_bad_value(self, monkeypatch):
        from providers.catalog import ProviderCatalog

        monkeypatch.setenv("FERAL_CODEX_SANDBOX", "nonsense")

        catalog = ProviderCatalog()

        assert len(catalog._adapters) == len(catalog._descriptors), (
            "one misspelled env var used to abort construction for every provider"
        )

    def test_a_valid_value_is_still_honoured(self, monkeypatch):
        from providers.codex_provider import CodexProvider

        monkeypatch.setenv("FERAL_CODEX_SANDBOX", "workspace-write")

        assert CodexProvider().sandbox == "workspace-write"


class TestDangerFullAccessNeedsASecondKey:
    """Codex runs with ``approvalPolicy: "never"``. The sandbox is the only
    thing between a model-chosen command and the machine, and
    ``danger-full-access`` removes it, bypassing security/dangerous_tools.py
    entirely. One env var is too little friction for that.
    """

    def test_the_env_var_alone_does_not_grant_it(self, monkeypatch):
        from providers.codex_provider import CodexProvider

        monkeypatch.setenv("FERAL_CODEX_SANDBOX", "danger-full-access")
        monkeypatch.delenv("FERAL_CODEX_ALLOW_DANGEROUS_SANDBOX", raising=False)

        assert CodexProvider().sandbox == "read-only"

    def test_the_explicit_opt_in_grants_it(self, monkeypatch):
        from providers.codex_provider import CodexProvider

        monkeypatch.setenv("FERAL_CODEX_SANDBOX", "danger-full-access")
        monkeypatch.setenv("FERAL_CODEX_ALLOW_DANGEROUS_SANDBOX", "1")

        assert CodexProvider().sandbox == "danger-full-access", (
            "the escape hatch must still work for someone who means it"
        )

    def test_the_refusal_is_logged_loudly(self, monkeypatch, caplog):
        from providers.codex_provider import CodexProvider

        monkeypatch.setenv("FERAL_CODEX_SANDBOX", "danger-full-access")
        monkeypatch.delenv("FERAL_CODEX_ALLOW_DANGEROUS_SANDBOX", raising=False)

        with caplog.at_level(logging.ERROR, logger="feral.providers.codex"):
            CodexProvider()

        assert any("danger-full-access" in record.getMessage() for record in caplog.records), (
            "silently downgrading a security setting teaches nothing"
        )


class TestTheSubprocessDoesNotInheritOurSecrets:
    """Codex authenticates itself. It needs none of FERAL's credentials,
    and the subprocess is the one place in this provider where they would
    leave the process.
    """

    def test_provider_keys_are_stripped(self, monkeypatch):
        from providers.codex_provider import _child_env

        monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
        monkeypatch.setenv("SOME_SERVICE_TOKEN", "t")
        monkeypatch.setenv("DB_PASSWORD", "p")

        env = _child_env()

        for leaked in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SOME_SERVICE_TOKEN", "DB_PASSWORD"):
            assert leaked not in env, f"{leaked} reached the Codex subprocess"

    def test_codex_own_configuration_survives(self, monkeypatch):
        from providers.codex_provider import _child_env

        monkeypatch.setenv("CODEX_HOME", "/tmp/codex")
        monkeypatch.setenv("PATH", "/usr/bin")

        env = _child_env()

        assert env.get("CODEX_HOME") == "/tmp/codex", "stripping must not break Codex's own config"
        assert "PATH" in env, "a subprocess with no PATH cannot find anything"


class TestNoAdapterConstructorCanKillTheCatalog:
    def test_a_raising_constructor_costs_only_that_provider(self, monkeypatch):
        import providers.anthropic_provider as anthropic_mod
        from providers.catalog import ProviderCatalog

        class Exploding:
            def __init__(self, *args, **kwargs):
                raise ValueError("adapter constructor exploded")

        monkeypatch.setattr(anthropic_mod, "AnthropicProvider", Exploding)

        catalog = ProviderCatalog()

        assert "anthropic" not in catalog._adapters, "the broken adapter should be dropped"
        assert len(catalog._adapters) == len(catalog._descriptors) - 1, (
            "every other provider must survive one adapter's constructor raising"
        )


class TestPlanBilledProvidersAreNotBilledPerToken:
    """Review of #206 reported that codex would be billed at a fallback
    per-token rate because its default model is the empty string. It is
    not: the pricing lookup short-circuits on an empty model and returns
    zero, which is the right answer for a subscription-billed provider.
    Pinned so a future 'fix' to the empty default does not start charging
    for a plan the user already pays for.
    """

    def test_an_empty_model_prices_at_zero(self):
        from agents.llm_provider import LLMProvider

        pricing = LLMProvider()._pricing_for_model("codex", "")

        assert pricing == {"input": 0.0, "output": 0.0}
