"""The computer-use VLM has never initialised.

AUDIT-FIXES F-16. ``AgenticComputerUseSkill._get_vlm`` called
``LLMProvider(provider=..., model=..., api_key=...)``. The real
``LLMProvider.__init__`` takes ``(self)`` and accepts none of those, so
the call raised ``TypeError`` on every invocation. The surrounding
``except Exception`` logged a warning and returned ``None``, and the
caller renders ``None`` as "No VLM available. Set OPENAI_API_KEY or
FERAL_VLM_PROVIDER."

So a user with a perfectly good API key was told to set the API key they
had already set, and the whole capability was dead. That message is why
this never got reported as a bug: it reads like configuration.

Same shape as F-01 (wrong kwargs, swallowed by a broad handler) with a
larger consequence. Unlike F-01 there is no lying test double here, there
was simply no coverage of this path at all: nothing under tests/ so much
as names ``_get_vlm``.

Two properties are pinned:

1. With a key present, a provider is returned. That is the regression.
2. "Not configured" and "constructed but failed" stay distinguishable.
   Collapsing them into one silent ``None`` is what made a hard failure
   look like a missing setting for the life of the feature.
"""

from __future__ import annotations

import inspect

import pytest

from agents.llm_provider import LLMProvider
from skills.impl.agentic_computer_use import AgenticComputerUseSkill


@pytest.fixture
def skill():
    return AgenticComputerUseSkill()


class TestTheConstructorIsCalledCorrectly:
    def test_llmprovider_takes_no_configuration_kwargs(self):
        """Pins the fact the fix rests on. If LLMProvider ever grows these
        parameters, this fails and the call site should be revisited
        rather than quietly becoming redundant."""
        params = inspect.signature(LLMProvider.__init__).parameters
        assert set(params) == {"self"}, (
            f"LLMProvider.__init__ now takes {sorted(params)}; revisit F-16"
        )

    @pytest.mark.asyncio
    async def test_a_vlm_is_returned_when_a_key_is_present(self, skill, monkeypatch):
        """The regression. Before the fix this returns None, because
        constructing LLMProvider with kwargs raises TypeError."""
        monkeypatch.delenv("FERAL_VLM_PROVIDER", raising=False)
        monkeypatch.delenv("FERAL_VLM_MODEL", raising=False)

        switched: dict = {}

        async def _fake_switch(self, provider, model="", api_key="", base_url=""):
            switched.update(provider=provider, model=model, api_key=api_key)

        monkeypatch.setattr(LLMProvider, "switch_provider", _fake_switch)

        vlm = await skill._get_vlm({"OPENAI_API_KEY": "sk-test"})

        assert vlm is not None, "VLM still fails to initialise with a key present"
        assert switched["api_key"] == "sk-test"

    @pytest.mark.asyncio
    async def test_the_env_overrides_reach_the_provider(self, skill, monkeypatch):
        """FERAL_VLM_PROVIDER and FERAL_VLM_MODEL are documented knobs.
        They were read into locals and then thrown away with the
        TypeError, so neither has ever taken effect."""
        monkeypatch.setenv("FERAL_VLM_PROVIDER", "anthropic")
        monkeypatch.setenv("FERAL_VLM_MODEL", "claude-opus-4-7")

        switched: dict = {}

        async def _fake_switch(self, provider, model="", api_key="", base_url=""):
            switched.update(provider=provider, model=model)

        monkeypatch.setattr(LLMProvider, "switch_provider", _fake_switch)

        await skill._get_vlm({"ANTHROPIC_API_KEY": "sk-ant"})

        assert switched["provider"] == "anthropic"
        assert switched["model"] == "claude-opus-4-7"


class TestNotConfiguredIsNotTheSameAsBroken:
    @pytest.mark.asyncio
    async def test_no_key_returns_none_quietly(self, skill, monkeypatch):
        """A genuinely unconfigured install is not an error and must not
        warn. This is the case the old message described."""
        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(var, raising=False)

        assert await skill._get_vlm({}) is None

    @pytest.mark.asyncio
    async def test_a_construction_failure_is_loud(self, skill, monkeypatch, caplog):
        """The failure mode that hid for the life of the feature. A key is
        present, so this is not a configuration problem, and it must not
        be reported as one."""
        async def _boom(self, provider, model="", api_key="", base_url=""):
            raise RuntimeError("adapter exploded")

        monkeypatch.setattr(LLMProvider, "switch_provider", _boom)

        with caplog.at_level("WARNING"):
            result = await skill._get_vlm({"OPENAI_API_KEY": "sk-test"})

        assert result is None
        assert "adapter exploded" in caplog.text

    @pytest.mark.asyncio
    async def test_a_typeerror_is_not_mistaken_for_missing_configuration(
        self, skill, monkeypatch, caplog
    ):
        """The exact bug: a TypeError from calling our own constructor
        wrongly must not be indistinguishable from an absent API key."""
        async def _wrong_signature(self, *a, **kw):
            raise TypeError("unexpected keyword argument 'provider'")

        monkeypatch.setattr(LLMProvider, "switch_provider", _wrong_signature)

        with caplog.at_level("WARNING"):
            await skill._get_vlm({"OPENAI_API_KEY": "sk-test"})

        assert caplog.text, "a construction failure must not be silent"
