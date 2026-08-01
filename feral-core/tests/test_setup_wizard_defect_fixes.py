"""Regression tests for the verified setup-wizard / config-layer defects.

Each test in this module pins one defect that was reproduced live
against an isolated ``FERAL_HOME`` before the fix landed:

* F-122 Ollama ``llm.base_url`` shipped without the OpenAI-compat
  ``/v1`` suffix, so a brain that reported ``LLM: ready`` 404'd on
  every chat turn.
* F-110 ``llm.base_url`` was never cleared when the provider changed,
  so a local URL poisoned a later cloud provider.
* F-131 the Ollama model picker defaulted to a ``:cloud`` model that
  needs an ollama.com key (403) on a provider the wizard never prompts
  a key for.
* F-103/129 ``settings.security.autonomy_mode`` was a dead write; the
  operator picked "strict" and the brain booted "hybrid".
* F-117/121/123 the enter-through-defaults path preselected a provider
  that probed "not configured" and then dead-ended on a mandatory API
  key prompt with no skip escape.
* F-104/130 the agent's name never reached the system prompt.
* F-000 pairing's "back (change network mode)" landed on the wrong
  step.
* F-112 the vision toggle could turn vision on but not off.
* F-114 "skip voice" still wrote ``audio.*_provider``.
* F-116 two adjacent steps asked "Configure voice now?" verbatim with
  opposite defaults.
* F-118 ``SkipStep`` did not record completion or advance the resume
  sidecar.
* F-126 ``macos.tcc_snapshot`` was written and read by nothing.
* F-101 ``feral_data_home()`` ignored ``FERAL_HOME``.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from cli.setup.state import WizardState


# ----------------------------------------------------------------------
# F-122: Ollama base_url must carry the OpenAI-compat /v1 suffix
# ----------------------------------------------------------------------


class TestOllamaBaseUrl:
    def test_catalog_descriptor_carries_v1(self):
        """The value the wizard copies into ``llm.base_url`` verbatim."""
        from providers.catalog import get_shared_catalog

        desc = get_shared_catalog().get_descriptor("ollama")
        assert desc is not None
        assert desc.default_base_url.endswith("/v1"), desc.default_base_url

    def test_catalog_matches_the_runtime_default(self):
        """Catalog and runtime must not disagree about the same URL."""
        from config.runtime import ollama_openai_base_url
        from providers.catalog import get_shared_catalog

        desc = get_shared_catalog().get_descriptor("ollama")
        assert desc.default_base_url == ollama_openai_base_url()

    def test_every_local_provider_base_url_has_a_path(self):
        """No local provider may ship a bare host as its base URL.

        The LLM client posts the relative path ``/chat/completions``
        against whatever base it is handed, so a bare host resolves to
        ``http://host:port/chat/completions``, which 404s.
        """
        from providers.catalog import get_shared_catalog

        for desc in get_shared_catalog().list_providers():
            if not desc.default_base_url:
                continue
            path = desc.default_base_url.split("://", 1)[-1]
            assert "/" in path, (
                f"{desc.provider_id} default_base_url has no path: "
                f"{desc.default_base_url}"
            )

    def test_wizard_provider_step_persists_a_v1_url(self, tmp_path, monkeypatch):
        """End to end through the step that writes the setting."""
        from cli.setup.helpers import Option
        from cli.setup.steps import llm as llm_step

        state = WizardState.load(tmp_path / "feral")

        monkeypatch.setattr(
            llm_step, "ask_choice",
            lambda *_a, **_kw: Option(id="ollama", label="Ollama (local)"),
        )

        async def _no_probe(_catalog):
            return {}

        monkeypatch.setattr(llm_step, "_probe_all", _no_probe)

        async def _unreachable(_console):
            return None

        monkeypatch.setattr(llm_step, "_handle_ollama_unreachable", _unreachable)

        asyncio.run(llm_step.run_provider_step(state))

        assert state.get_setting("llm", "provider") == "ollama"
        assert state.get_setting("llm", "base_url").endswith("/v1")


class TestPersistedBareBaseUrlIsRepaired:
    """A broken value is already on disk for every existing install."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for key in list(os.environ):
            if key.startswith("FERAL_"):
                monkeypatch.delenv(key, raising=False)

    @staticmethod
    def _loader_over(home, settings):
        from config.loader import ConfigLoader

        home.mkdir(parents=True, exist_ok=True)
        (home / "settings.json").write_text(json.dumps(settings))
        loader = ConfigLoader(project_dir=str(home.parent / "project"))
        loader.user_home = home
        loader.discover()
        return loader

    def test_bare_ollama_url_gains_v1_on_load(self, tmp_path):
        loader = self._loader_over(
            tmp_path / "feral",
            {"llm": {"provider": "ollama", "base_url": "http://localhost:11434"}},
        )
        assert loader.get("llm", "base_url") == "http://localhost:11434/v1"

    def test_repaired_url_is_what_gets_exported_to_the_runtime(self, tmp_path):
        """``FERAL_LLM_BASE_URL`` is what ``agents/llm_provider`` seeds
        itself from, and it only fills the slot when it is EMPTY, so the
        repair has to be visible in the export."""
        loader = self._loader_over(
            tmp_path / "feral",
            {"llm": {"provider": "ollama", "base_url": "http://localhost:11434"}},
        )
        env = loader.export_as_env()
        assert env["FERAL_LLM_BASE_URL"].endswith("/v1")

    def test_trailing_slash_is_repaired_too(self, tmp_path):
        loader = self._loader_over(
            tmp_path / "feral",
            {"llm": {"provider": "ollama", "base_url": "http://localhost:11434/"}},
        )
        assert loader.get("llm", "base_url") == "http://localhost:11434/v1"

    def test_non_default_ollama_host_is_repaired(self, tmp_path):
        loader = self._loader_over(
            tmp_path / "feral",
            {"llm": {"provider": "ollama", "base_url": "http://192.168.1.9:11434"}},
        )
        assert loader.get("llm", "base_url") == "http://192.168.1.9:11434/v1"

    def test_url_that_already_names_a_path_is_left_alone(self, tmp_path):
        """An operator pointing Ollama at a gateway prefix meant it."""
        loader = self._loader_over(
            tmp_path / "feral",
            {"llm": {"provider": "ollama", "base_url": "http://gw.local/ollama/v1"}},
        )
        assert loader.get("llm", "base_url") == "http://gw.local/ollama/v1"

    def test_cloud_provider_base_url_is_never_touched(self, tmp_path):
        loader = self._loader_over(
            tmp_path / "feral",
            {"llm": {"provider": "openai", "base_url": "http://localhost:11434"}},
        )
        assert loader.get("llm", "base_url") == "http://localhost:11434"

    def test_empty_base_url_stays_empty(self, tmp_path):
        """An empty slot is what lets the runtime apply its own default."""
        loader = self._loader_over(
            tmp_path / "feral", {"llm": {"provider": "ollama", "base_url": ""}},
        )
        assert loader.get("llm", "base_url") == ""

    def test_env_supplied_bare_url_is_repaired(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FERAL_LLM_BASE_URL", "http://localhost:11434")
        loader = self._loader_over(
            tmp_path / "feral", {"llm": {"provider": "ollama"}},
        )
        assert loader.get("llm", "base_url") == "http://localhost:11434/v1"


# ----------------------------------------------------------------------
# F-110: base_url must not survive a provider change
# ----------------------------------------------------------------------


class TestBaseUrlFollowsTheProvider:
    @staticmethod
    def _desc(provider_id, base_url):
        from providers.catalog import get_shared_catalog

        return get_shared_catalog().get_descriptor(provider_id), base_url

    def test_switching_local_to_cloud_replaces_the_url(self, tmp_path):
        from cli.setup.steps.llm import _apply_base_url
        from providers.catalog import get_shared_catalog

        state = WizardState.load(tmp_path / "feral")
        state.set_setting("llm", "base_url", "http://localhost:11434/v1")

        openai = get_shared_catalog().get_descriptor("openai")
        _apply_base_url(state, openai, provider_changed=True)

        assert "11434" not in state.get_setting("llm", "base_url")
        assert state.get_setting("llm", "base_url") == openai.default_base_url

    def test_provider_without_a_default_clears_the_url(self, tmp_path):
        """Otherwise the stale value wins over the runtime's own default."""
        from cli.setup.steps.llm import _apply_base_url

        state = WizardState.load(tmp_path / "feral")
        state.set_setting("llm", "base_url", "http://localhost:11434/v1")

        class _NoDefault:
            default_base_url = ""

        _apply_base_url(state, _NoDefault(), provider_changed=True)
        assert state.get_setting("llm", "base_url") == ""

    def test_same_provider_keeps_a_customised_url(self, tmp_path):
        from cli.setup.steps.llm import _apply_base_url
        from providers.catalog import get_shared_catalog

        state = WizardState.load(tmp_path / "feral")
        state.set_setting("llm", "base_url", "http://my-proxy.local/v1")

        openai = get_shared_catalog().get_descriptor("openai")
        _apply_base_url(state, openai, provider_changed=False)

        assert state.get_setting("llm", "base_url") == "http://my-proxy.local/v1"

    def test_full_step_back_then_forward_does_not_poison_openai(
        self, tmp_path, monkeypatch,
    ):
        """The exact reachable repro: pick Ollama, go back, pick OpenAI."""
        from cli.setup.helpers import Option
        from cli.setup.steps import llm as llm_step

        state = WizardState.load(tmp_path / "feral")

        async def _no_probe(_catalog):
            return {}

        monkeypatch.setattr(llm_step, "_probe_all", _no_probe)

        async def _unreachable(_console):
            return None

        monkeypatch.setattr(llm_step, "_handle_ollama_unreachable", _unreachable)

        async def _skip_key(**_kw):
            return None

        monkeypatch.setattr(llm_step, "_configure_provider_key", _skip_key)

        class _Probe:
            reachable = True
            error = ""

        async def _probe_one(_pid):
            return _Probe()

        picks = iter([
            Option(id="ollama", label="Ollama (local)"),
            Option(id="openai", label="OpenAI"),
        ])
        monkeypatch.setattr(llm_step, "ask_choice", lambda *_a, **_kw: next(picks))

        asyncio.run(llm_step.run_provider_step(state))
        assert "11434" in state.get_setting("llm", "base_url")

        catalog = llm_step._catalog(state)
        monkeypatch.setattr(catalog, "probe", _probe_one)
        asyncio.run(llm_step.run_provider_step(state))

        assert state.get_setting("llm", "provider") == "openai"
        assert "11434" not in state.get_setting("llm", "base_url")


# ----------------------------------------------------------------------
# F-131: the Ollama model default must not be a key-gated cloud model
# ----------------------------------------------------------------------


class TestOllamaCloudModelsAreDemoted:
    def test_cloud_models_sort_below_local_ones(self):
        from cli.setup.steps.llm import _demote_hosted_local_models

        ordered = _demote_hosted_local_models(
            "ollama", ["deepseek-v4-flash:cloud", "llava:latest", "moondream:latest"],
        )
        assert ordered[0] == "llava:latest"
        assert ordered[-1] == "deepseek-v4-flash:cloud"

    def test_relative_order_within_each_group_is_preserved(self):
        from cli.setup.steps.llm import _demote_hosted_local_models

        assert _demote_hosted_local_models(
            "ollama", ["a:cloud", "b:latest", "c:cloud", "d:latest"],
        ) == ["b:latest", "d:latest", "a:cloud", "c:cloud"]

    def test_other_providers_are_untouched(self):
        from cli.setup.steps.llm import _demote_hosted_local_models

        models = ["gpt-5.5", "gpt-5.5-mini"]
        assert _demote_hosted_local_models("openai", models) == models


# ----------------------------------------------------------------------
# F-103 / F-129: autonomy_mode must reach the runtime
# ----------------------------------------------------------------------


class TestAutonomyModeReachesTheRuntime:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("FERAL_AUTONOMY", raising=False)

    @staticmethod
    def _loader_over(home, settings):
        from config.loader import ConfigLoader

        home.mkdir(parents=True, exist_ok=True)
        (home / "settings.json").write_text(json.dumps(settings))
        loader = ConfigLoader(project_dir=str(home.parent / "project"))
        loader.user_home = home
        loader.discover()
        return loader

    def test_strict_is_exported_as_feral_autonomy(self, tmp_path):
        loader = self._loader_over(
            tmp_path / "feral", {"security": {"autonomy_mode": "strict"}},
        )
        assert loader.export_as_env()["FERAL_AUTONOMY"] == "strict"

    def test_exec_mode_sees_the_wizard_choice(self, tmp_path, monkeypatch):
        """``security/exec_mode.current_autonomy_mode()`` reads ONLY the
        env var, which is why the export is the fix. Before it existed
        this returned "hybrid" for a settings file saying "strict"."""
        from security.exec_mode import current_autonomy_mode

        loader = self._loader_over(
            tmp_path / "feral", {"security": {"autonomy_mode": "strict"}},
        )
        for key, value in loader.export_as_env().items():
            monkeypatch.setenv(key, value)

        assert current_autonomy_mode() == "strict"

    def test_tool_runner_reads_the_same_env_var(self, tmp_path, monkeypatch):
        """Pin the assumption this fix rests on: ToolRunner resolves the
        tier from ``FERAL_AUTONOMY`` ahead of its ctor default, which the
        orchestrator never passes."""
        from agents.tool_runner import ToolRunner

        loader = self._loader_over(
            tmp_path / "feral", {"security": {"autonomy_mode": "strict"}},
        )
        for key, value in loader.export_as_env().items():
            monkeypatch.setenv(key, value)

        runner = ToolRunner(orchestrator=None)
        assert runner._autonomy_mode == "strict"

    def test_env_var_still_wins_over_settings(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FERAL_AUTONOMY", "loose")
        loader = self._loader_over(
            tmp_path / "feral", {"security": {"autonomy_mode": "strict"}},
        )
        assert loader.export_as_env()["FERAL_AUTONOMY"] == "loose"

    def test_unknown_value_falls_back_to_hybrid(self, tmp_path):
        loader = self._loader_over(
            tmp_path / "feral", {"security": {"autonomy_mode": "yolo"}},
        )
        assert loader.export_as_env()["FERAL_AUTONOMY"] == "hybrid"

    def test_default_install_exports_hybrid(self, tmp_path):
        loader = self._loader_over(tmp_path / "feral", {})
        assert loader.export_as_env()["FERAL_AUTONOMY"] == "hybrid"

    def test_wizard_write_survives_a_round_trip_to_the_runtime(
        self, tmp_path, monkeypatch,
    ):
        """Wizard step -> settings.json -> loader -> env -> exec_mode."""
        from cli.setup.helpers import Option
        from cli.setup.steps import capabilities
        from security.exec_mode import current_autonomy_mode

        home = tmp_path / "feral"
        state = WizardState.load(home)
        monkeypatch.setattr(
            capabilities, "ask_choice",
            lambda *_a, **_kw: Option(id="strict", label="Strict"),
        )
        capabilities._run_autonomy(state, capabilities.get_console())
        state.save()

        loader = self._loader_over(home, json.loads((home / "settings.json").read_text()))
        for key, value in loader.export_as_env().items():
            monkeypatch.setenv(key, value)

        assert current_autonomy_mode() == "strict"


# ----------------------------------------------------------------------
# F-117 / F-121 / F-123: the enter-through-defaults path must terminate
# ----------------------------------------------------------------------


class TestVoicePreflightDefaultsTerminate:
    def test_first_ready_returns_none_when_nothing_probed_ready(self):
        from cli.setup.helpers import Option
        from cli.setup.steps.voice_preflight import _first_ready

        opts = [
            Option(id="openai_realtime", label="OpenAI Realtime", status="needs_api_key"),
            Option(id="gemini_live", label="Gemini Live", status="needs_api_key"),
            Option(id="__none__", label="(skip realtime)", status=""),
        ]
        assert _first_ready(opts) == "__none__"

    def test_first_ready_still_prefers_a_ready_provider(self):
        from cli.setup.helpers import Option
        from cli.setup.steps.voice_preflight import _first_ready

        opts = [
            Option(id="openai_realtime", label="OpenAI Realtime", status="needs_api_key"),
            Option(id="gemini_live", label="Gemini Live", status="ready"),
            Option(id="__none__", label="(skip realtime)", status=""),
        ]
        assert _first_ready(opts) == "gemini_live"

    def test_enter_through_defaults_does_not_pick_an_unconfigured_provider(
        self, tmp_path, monkeypatch,
    ):
        """The live repro: no keys anywhere, operator presses enter at
        every prompt. ``audio.realtime_primary`` must not name a
        provider whose probe said "not configured"."""
        import time

        from cli.setup.steps import voice_preflight as vp
        from security import probe as probe_mod

        monkeypatch.setenv("FERAL_HOME", str(tmp_path / "feral"))

        async def _all_unconfigured(pid, **_kw):
            return probe_mod.ProbeResult(
                provider=pid, ok=False, status_code=None, reason="no_key",
                detail="not configured", probed_at=time.time(), latency_ms=0.0,
            )

        monkeypatch.setattr(probe_mod, "probe", _all_unconfigured)
        probe_mod.clear_probe_cache()
        monkeypatch.setattr(vp, "confirm", lambda *_a, **kw: kw.get("default", False))
        # The step now opens with "how should voice run?" (cloud /
        # fully local / skip), whose default is legitimately "cloud"
        # rather than "__none__" - it is not a provider picker and the
        # assertion below is about provider pickers.
        monkeypatch.setattr(vp, "_ask_voice_stack", lambda *_a, **_kw: "cloud")

        seen_defaults: list[str] = []

        def _ask_choice(_prompt, opts, default=None):
            seen_defaults.append(default)
            return next(o for o in opts if o.id == default)

        monkeypatch.setattr(vp, "ask_choice", _ask_choice)

        def _no_ask_text(*a, **kw):
            raise AssertionError(f"default path must not prompt for a key: {a!r}")

        monkeypatch.setattr(vp, "ask_text", _no_ask_text)

        state = WizardState.load(tmp_path / "feral")
        asyncio.run(vp.run(state))

        assert seen_defaults and all(d == "__none__" for d in seen_defaults)
        assert not state.get_setting("audio", "realtime_primary")

    def test_empty_key_is_accepted_as_skip(self, tmp_path, monkeypatch):
        """``allow_empty=False`` here looped forever: both the helper and
        ``ui_kit.password`` re-prompt on an empty value, and the attempt
        counter only bounds REJECTED keys. 400 blank lines produced 400
        "value cannot be empty" messages and then killed the wizard."""
        from cli.setup.steps import voice_preflight as vp

        calls = {"n": 0}

        def _blank(prompt, **kwargs):
            calls["n"] += 1
            assert kwargs.get("allow_empty") is True, (
                "an empty answer must be allowed or the prompt cannot terminate"
            )
            assert "skip" in prompt.lower(), "the skip escape must be advertised"
            if calls["n"] > 5:
                raise AssertionError("prompt looped instead of accepting the skip")
            return ""

        monkeypatch.setattr(vp, "ask_text", _blank)

        state = WizardState.load(tmp_path / "feral")
        ok = asyncio.run(vp._prompt_and_verify_key(
            state=state,
            vendor_id="openai",
            env_var="OPENAI_API_KEY",
            voice_provider_id="openai_realtime",
            console=vp.get_console(),
            prompt="  Enter your openai API key",
        ))

        assert ok is False
        assert calls["n"] == 1
        assert not state.credentials.get("OPENAI_API_KEY")


class TestLLMStepDefaultsTerminate:
    """The same dead-end class as F-121, found in the LLM step.

    On a keyless machine the provider picker's own default is a
    key-requiring cloud provider, so pressing enter through the wizard
    landed on a mandatory API-key prompt (step 1) and then on an
    unbounded model retry loop (step 2). Neither could be escaped with
    the keyboard, so the all-defaults run never reached step 3.
    """

    def test_missing_key_prompt_accepts_an_empty_answer(
        self, tmp_path, monkeypatch,
    ):
        from cli.setup.steps import llm as llm_step

        state = WizardState.load(tmp_path / "feral")
        monkeypatch.setattr(
            llm_step, "existing_provider_key", lambda *_a, **_kw: ("", "", []),
        )

        seen: dict = {}

        def _blank(prompt, **kwargs):
            seen["prompt"] = prompt
            seen["allow_empty"] = kwargs.get("allow_empty")
            return ""

        monkeypatch.setattr(llm_step, "ask_text", _blank)

        def _must_not_persist(**_kw):
            raise AssertionError("an empty answer must not be stored as a key")

        monkeypatch.setattr(llm_step, "_persist_new_key", _must_not_persist)

        asyncio.run(llm_step._configure_provider_key(
            state=state, catalog=None, provider_id="anthropic",
            env_var="ANTHROPIC_API_KEY", display_name="Anthropic",
            console=llm_step.get_console(),
        ))

        assert seen["allow_empty"] is True
        assert "skip" in seen["prompt"].lower()

    def test_smoke_test_is_skipped_when_no_key_is_configured(
        self, tmp_path, monkeypatch,
    ):
        """Without a key every model id fails identically, so a failure
        verdict re-asks a question the operator's answer cannot fix."""
        from cli.setup.steps import llm as llm_step
        from providers.catalog import get_shared_catalog

        monkeypatch.setattr(
            llm_step, "existing_provider_key", lambda *_a, **_kw: ("", "", []),
        )

        catalog = get_shared_catalog()

        def _no_adapter(_pid):
            raise AssertionError("the smoke test must not run without a key")

        monkeypatch.setattr(catalog, "get_adapter", _no_adapter)

        ok = asyncio.run(llm_step._smoke_test_model(
            catalog, "anthropic", "claude-fable-5", llm_step.get_console(),
        ))
        assert ok is True

    def test_model_retry_loop_is_bounded(self, tmp_path, monkeypatch):
        """It was ``while True`` with "Pick a different model?" defaulting
        to yes, so a failing smoke test re-asked until stdin ran out."""
        from cli.setup.steps import llm as llm_step

        state = WizardState.load(tmp_path / "feral")
        state.set_setting("llm", "provider", "anthropic")

        calls = {"n": 0}

        async def _always_fail(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] > 20:
                raise AssertionError("model prompt looped without a bound")
            return False

        monkeypatch.setattr(llm_step, "_smoke_test_model", _always_fail)
        monkeypatch.setattr(llm_step, "ask_text", lambda *_a, **_kw: "some-model")
        monkeypatch.setattr(llm_step, "confirm", lambda *_a, **kw: True)
        monkeypatch.setattr(
            llm_step.ui_kit, "is_inquirer_available", lambda: False,
        )

        async def _models(*_a, **_kw):
            class _Cached:
                models = ["some-model"]
                source = "test"
            return _Cached()

        monkeypatch.setattr(llm_step._catalog(state), "list_models", _models)

        asyncio.run(llm_step.run_model_step(state))

        assert calls["n"] == llm_step._MAX_MODEL_ATTEMPTS
        assert state.get_setting("llm", "model") == "some-model"


# ----------------------------------------------------------------------
# F-104 / F-130: the agent name must reach the system prompt
# ----------------------------------------------------------------------


class TestAgentNameReachesTheIdentity:
    def test_personality_step_writes_identity_yaml(self, tmp_path, monkeypatch):
        from cli.setup.helpers import Option
        from cli.setup.steps import personality

        home = tmp_path / "feral"
        state = WizardState.load(home)

        monkeypatch.setattr(personality, "confirm", lambda *_a, **_kw: True)
        monkeypatch.setattr(
            personality, "ask_choice",
            lambda *_a, **_kw: Option(id="assistant", label="Assistant"),
        )
        monkeypatch.setattr(personality, "ask_text", lambda *_a, **_kw: "Jarvis")

        personality.run(state)

        assert (home / "IDENTITY.yaml").is_file()
        import yaml

        data = yaml.safe_load((home / "IDENTITY.yaml").read_text())
        assert data["name"] == "Jarvis"

    def test_named_agent_shows_up_in_the_system_prompt(self, tmp_path, monkeypatch):
        """The live symptom: naming the agent Jarvis still produced
        "You are FERAL, a personal AI operating system."."""
        from agents.identity_loader import IdentityLoader
        from cli.setup.helpers import Option
        from cli.setup.steps import personality

        home = tmp_path / "feral"
        monkeypatch.setenv("FERAL_HOME", str(home))
        state = WizardState.load(home)

        monkeypatch.setattr(personality, "confirm", lambda *_a, **_kw: True)
        monkeypatch.setattr(
            personality, "ask_choice",
            lambda *_a, **_kw: Option(id="assistant", label="Assistant"),
        )
        monkeypatch.setattr(personality, "ask_text", lambda *_a, **_kw: "Jarvis")
        personality.run(state)

        prompt = IdentityLoader().load_identity()
        assert prompt.splitlines()[0].startswith("You are Jarvis")

    def test_existing_install_with_only_the_settings_key_is_honoured(
        self, tmp_path, monkeypatch,
    ):
        """Installs created before the IDENTITY.yaml fix have the name
        only at ``settings.identity.agent_name``. Recover it rather than
        making the operator re-run setup."""
        from agents.identity_loader import IdentityLoader

        home = tmp_path / "feral"
        home.mkdir(parents=True)
        monkeypatch.setenv("FERAL_HOME", str(home))
        (home / "settings.json").write_text(
            json.dumps({"identity": {"agent_name": "Jarvis"}})
        )

        prompt = IdentityLoader().load_identity()
        assert prompt.splitlines()[0].startswith("You are Jarvis")

    def test_untouched_install_still_says_feral(self, tmp_path, monkeypatch):
        from agents.identity_loader import IdentityLoader

        home = tmp_path / "feral"
        home.mkdir(parents=True)
        monkeypatch.setenv("FERAL_HOME", str(home))

        prompt = IdentityLoader().load_identity()
        assert prompt.splitlines()[0].startswith("You are FERAL")


# ----------------------------------------------------------------------
# F-000: pairing "back (change network mode)" must land on `network`
# ----------------------------------------------------------------------


class TestPairingBackNavigation:
    def test_back_button_requests_the_network_step_by_name(
        self, tmp_path, monkeypatch,
    ):
        """A bare BackNavigation is ``idx -= 1``, which landed on
        ``channels`` because ``network`` is five steps earlier."""
        from cli.setup.helpers import JumpToStep
        from cli.setup.steps import pairing

        async def _no_snapshot():
            return None

        monkeypatch.setattr(pairing.network, "get_snapshot", _no_snapshot)
        monkeypatch.setattr(pairing.ui_kit, "pick", lambda *_a, **_kw: "__back__")

        state = WizardState.load(tmp_path / "feral")
        with pytest.raises(JumpToStep) as excinfo:
            asyncio.run(pairing.run(state))
        assert excinfo.value.target == "network"

    def test_continue_does_not_navigate(self, tmp_path, monkeypatch):
        from cli.setup.steps import pairing

        async def _no_snapshot():
            return None

        monkeypatch.setattr(pairing.network, "get_snapshot", _no_snapshot)
        monkeypatch.setattr(pairing.ui_kit, "pick", lambda *_a, **_kw: "__continue__")

        state = WizardState.load(tmp_path / "feral")
        asyncio.run(pairing.run(state))

    @pytest.mark.asyncio
    async def test_state_machine_lands_on_network_not_channels(self, tmp_path):
        """Drive the real jump through the state machine."""
        from cli.setup.helpers import JumpToStep
        from cli.setup.state_machine import StateMachine

        visited: list[str] = []
        fired = {"done": False}

        def _record(name):
            def _step(_s):
                visited.append(name)
            return _step

        def _pairing(_s):
            visited.append("pairing")
            if not fired["done"]:
                fired["done"] = True
                raise JumpToStep("network")

        state = WizardState.load(tmp_path / "feral")
        await StateMachine(
            state=state,
            steps=[
                ("network", _record("network")),
                ("integrations", _record("integrations")),
                ("home_assistant", _record("home_assistant")),
                ("tool_keys", _record("tool_keys")),
                ("channels", _record("channels")),
                ("pairing", _pairing),
            ],
        ).run()

        assert visited[visited.index("pairing") + 1] == "network"


# ----------------------------------------------------------------------
# F-112: the vision toggle must be able to turn vision OFF
# ----------------------------------------------------------------------


class TestVisionToggleCanTurnVisionOff:
    def test_unticking_vision_writes_both_keys(self, tmp_path, monkeypatch):
        """``ConfigLoader._unify_feature_flags`` ORs ``features.vision``
        with ``vision.enabled``, so writing one key can only ever turn
        vision ON."""
        from cli.setup.steps import capabilities

        state = WizardState.load(tmp_path / "feral")
        state.set_setting("features", "vision", True)
        state.set_setting("vision", "enabled", True)

        monkeypatch.setattr(
            capabilities.ui_kit, "multi_select", lambda *_a, **_kw: [],
        )
        capabilities._run_toggles(state, capabilities.get_console())

        assert state.get_setting("vision", "enabled") is False
        assert state.get_setting("features", "vision") is False

    def test_loader_agrees_that_vision_is_off(self, tmp_path, monkeypatch):
        from config.loader import ConfigLoader

        from cli.setup.steps import capabilities

        home = tmp_path / "feral"
        state = WizardState.load(home)
        state.set_setting("features", "vision", True)
        state.set_setting("vision", "enabled", True)
        monkeypatch.setattr(
            capabilities.ui_kit, "multi_select", lambda *_a, **_kw: [],
        )
        capabilities._run_toggles(state, capabilities.get_console())
        state.save()

        for key in list(os.environ):
            if key.startswith("FERAL_VISION"):
                monkeypatch.delenv(key, raising=False)
        loader = ConfigLoader(project_dir=str(tmp_path / "project"))
        loader.user_home = home
        loader.discover()

        assert loader.get("vision", "enabled") is False
        assert loader.export_as_env()["FERAL_VISION_ENABLED"] == "false"

    def test_web_ui_enabled_vision_pre_marks_the_row(self, tmp_path, monkeypatch):
        """The settings UI writes ``features.vision``; the row must not
        look already-off when only that key is set."""
        from cli.setup.steps import capabilities

        state = WizardState.load(tmp_path / "feral")
        state.set_setting("features", "vision", True)

        seen: dict = {}

        def _multi_select(_title, choices):
            seen["choices"] = choices
            return [c["value"] for c in choices if c["enabled"]]

        monkeypatch.setattr(capabilities.ui_kit, "multi_select", _multi_select)
        capabilities._run_toggles(state, capabilities.get_console())

        row = next(c for c in seen["choices"] if c["value"] == "vision.enabled")
        assert row["enabled"] is True
        assert state.get_setting("vision", "enabled") is True


# ----------------------------------------------------------------------
# F-116: one "configure voice?" question, not two with opposite defaults
# ----------------------------------------------------------------------


class TestVoiceQuestionIsAskedOnce:
    def test_audio_step_does_not_reask_after_a_decline(self, tmp_path, monkeypatch):
        from cli.setup.steps import audio as audio_step

        state = WizardState.load(tmp_path / "feral")
        state.set_setting("audio", "configured_via_wizard", False)

        def _no_confirm(*a, **kw):
            raise AssertionError(f"voice was already declined; asked again: {a!r}")

        monkeypatch.setattr(audio_step, "confirm", _no_confirm)
        asyncio.run(audio_step.run(state))

        assert state.get_setting("audio", "stt_provider") is None

    def test_audio_step_does_not_reask_after_an_accept(self, tmp_path, monkeypatch):
        """Preflight already asked. The next prompt this step shows must
        be a real speech question, not "Configure voice now?" again."""
        from cli.setup.steps import audio as audio_step

        state = WizardState.load(tmp_path / "feral")
        state.set_setting("audio", "configured_via_wizard", True)

        monkeypatch.setattr(
            audio_step, "detect_local_audio_capabilities",
            lambda: {
                "local_stt": True, "local_tts": True,
                "stt_models": ["base"], "tts_voices": ["en_US-lessac-medium"],
            },
        )
        prompts: list[str] = []

        def _confirm(prompt, **kw):
            prompts.append(prompt)
            return True

        monkeypatch.setattr(audio_step, "confirm", _confirm)
        asyncio.run(audio_step.run(state))

        assert not any("configure voice" in p.lower() for p in prompts), prompts
        assert state.get_setting("audio", "stt_provider") == "faster-whisper"

    def test_the_two_voice_prompts_agree(self, tmp_path, monkeypatch):
        """The prompts were verbatim identical with opposite defaults, so
        pressing enter said yes to voice on one step and no on the next.

        They must no longer collide: either the wording differs, or the
        defaults agree. Assert both.
        """
        from cli.setup.helpers import SkipStep
        from cli.setup.steps import audio as audio_step
        from cli.setup.steps import voice_preflight as vp

        asked: list[tuple[str, object]] = []

        def _record(prompt, **kw):
            asked.append((prompt.strip().lower(), kw.get("default")))
            return False

        # The preflight's gate is now a three-way choice (cloud /
        # fully local / skip) rather than a yes/no, because "fully
        # local" is a different shape of setup and not a provider
        # inside the same one. The collision this test guards against
        # is unchanged: the two steps must not ask the operator the
        # same question with opposite defaults.
        def _record_choice(prompt, opts, default=None):
            asked.append((prompt.strip().lower(), default))
            return next(o for o in opts if o.id == "skip")

        monkeypatch.setattr(vp, "ask_choice", _record_choice)
        with pytest.raises(SkipStep):
            asyncio.run(vp.run(WizardState.load(tmp_path / "feral")))

        monkeypatch.setattr(audio_step, "confirm", _record)
        # Fresh state: no ``configured_via_wizard`` marker, so the audio
        # step falls back to asking (the voice catalogue was unavailable
        # path). That fallback is the only prompt collision left.
        asyncio.run(audio_step.run(WizardState.load(tmp_path / "feral2")))

        assert len(asked) == 2, asked
        (preflight_prompt, preflight_default), (audio_prompt, audio_default) = asked
        assert preflight_prompt != audio_prompt
        # Pressing enter proceeds with voice on BOTH steps rather than
        # yes on one and no on the other.
        assert preflight_default == "cloud"
        assert audio_default is True


# ----------------------------------------------------------------------
# F-118: SkipStep must record completion + advance the resume sidecar
# ----------------------------------------------------------------------


class TestSkipStepBookkeeping:
    @pytest.mark.asyncio
    async def test_skipped_step_is_marked_complete_and_persisted(self, tmp_path):
        """Otherwise a Ctrl+C on the NEXT step rewinds to the step the
        operator explicitly skipped."""
        from cli.setup.helpers import SkipStep
        from cli.setup.state_machine import StateMachine

        state = WizardState.load(tmp_path / "feral")

        def _skipper(_s):
            raise SkipStep()

        def _next(_s):
            pass

        await StateMachine(
            state=state,
            steps=[("voice_preflight", _skipper), ("audio", _next)],
        ).run()

        assert "voice_preflight" in state.completed_steps
        sidecar = WizardState.read_setup_state(state.home)
        assert sidecar["last_step"] == "audio"
        assert "voice_preflight" in sidecar["completed_steps"]


# ----------------------------------------------------------------------
# F-101: FERAL_HOME must relocate data as well as config
# ----------------------------------------------------------------------


class TestFeralDataHomeHonoursFeralHome:
    def test_feral_home_relocates_the_data_dir_too(self, tmp_path, monkeypatch):
        from config.loader import feral_data_home, feral_home

        monkeypatch.setenv("FERAL_HOME", str(tmp_path / "relocated"))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

        assert feral_home() == tmp_path / "relocated"
        assert feral_data_home() == tmp_path / "relocated"

    def test_xdg_data_home_still_applies_without_feral_home(
        self, tmp_path, monkeypatch,
    ):
        from config.loader import feral_data_home

        monkeypatch.delenv("FERAL_HOME", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        assert feral_data_home() == tmp_path / "xdg" / "feral"
