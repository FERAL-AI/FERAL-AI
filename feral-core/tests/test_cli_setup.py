"""Tests for the refactored CLI setup wizard (feral-core/cli/setup)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli.setup.helpers import (
    Option,
    STATUS_NEEDS_KEY,
    STATUS_READY,
    STATUS_UNREACHABLE,
    resolve_option,
    BackNavigation,
    QuitNavigation,
)
from cli.setup.state import WizardState
from cli.setup.state_machine import StateMachine


# ----------------------------------------------------------------------
# helpers.resolve_option
# ----------------------------------------------------------------------


OPENAI = Option(id="openai", label="OpenAI", aliases=("open ai", "gpt", "chatgpt"), status="needs_api_key")
ANTHROPIC = Option(id="anthropic", label="Anthropic", aliases=("claude",), status="needs_api_key")
OLLAMA = Option(id="ollama", label="Ollama (local)", aliases=("local-ollama",), status="ready")


class TestResolveOption:
    @pytest.mark.parametrize(
        "text,expected_id",
        [
            ("openai", "openai"),
            ("OpenAI", "openai"),
            ("open ai", "openai"),
            ("chatgpt", "openai"),
            ("claude", "anthropic"),
            ("Anthropic", "anthropic"),
            ("local-ollama", "ollama"),
            ("Ollama (local)", "ollama"),
        ],
    )
    def test_canonical_and_aliases(self, text, expected_id):
        assert resolve_option(text, [OPENAI, ANTHROPIC, OLLAMA]).id == expected_id

    def test_numeric_index(self):
        assert resolve_option("1", [OPENAI, ANTHROPIC, OLLAMA]).id == "openai"
        assert resolve_option("3", [OPENAI, ANTHROPIC, OLLAMA]).id == "ollama"
        assert resolve_option("9", [OPENAI, ANTHROPIC, OLLAMA]) is None

    def test_substring_unambiguous(self):
        assert resolve_option("seek", [Option(id="deepseek", label="DeepSeek")]).id == "deepseek"

    def test_ambiguous_returns_none(self):
        ambiguous = [
            Option(id="openai", label="OpenAI"),
            Option(id="openrouter", label="OpenRouter"),
            Option(id="ollama", label="Ollama"),
        ]
        # "o" hits all three via substring.
        assert resolve_option("o", ambiguous) is None

    def test_empty_returns_none(self):
        assert resolve_option("", [OPENAI]) is None
        assert resolve_option("   ", [OPENAI]) is None


# ----------------------------------------------------------------------
# WizardState persistence
# ----------------------------------------------------------------------


class TestWizardState:
    def test_load_nonexistent_home_returns_empty_state(self, tmp_path):
        state = WizardState.load(tmp_path / "feral")
        assert state.settings == {}
        assert state.credentials == {}
        assert state.home.exists()

    def test_load_reads_existing_files(self, tmp_path):
        home = tmp_path / "feral"
        home.mkdir()
        (home / "settings.json").write_text('{"llm": {"provider": "ollama"}}')
        (home / "credentials.json").write_text('{"OPENAI_API_KEY": "sk-old"}')
        state = WizardState.load(home)
        assert state.settings["llm"]["provider"] == "ollama"
        assert state.credentials["OPENAI_API_KEY"] == "sk-old"

    def test_save_writes_both_files_but_does_not_mark_complete(self, tmp_path):
        """audit-r14 / lane-07 W7 — ``state.save()`` no longer flips
        ``setup_complete`` (the wizard's finish step does, via
        :meth:`WizardState.mark_complete`). Quit / Ctrl+C / crash that
        ends in the finally block calling ``state.save()`` MUST NOT
        mark the install as complete."""
        state = WizardState.load(tmp_path / "feral")
        state.set_setting("llm", "provider", "openai")
        state.set_credential("OPENAI_API_KEY", "sk-new")
        state.save()
        import json
        saved = json.loads((state.home / "settings.json").read_text())
        assert saved["llm"]["provider"] == "openai"
        assert saved.get("meta", {}).get("setup_complete") is not True

        # Now invoke ``mark_complete()`` (what the finish step does)
        # and confirm the flag flips + the resume sidecar is gone.
        state.write_setup_state(last_step="audio", completed_steps=["welcome"])
        assert (state.home / "setup_state.json").is_file()
        state.mark_complete()
        saved = json.loads((state.home / "settings.json").read_text())
        assert saved["meta"]["setup_complete"] is True
        assert not (state.home / "setup_state.json").is_file()

        # A7 contract: setup credentials are encrypted at rest through
        # BlindVault and must not leave a plaintext credentials.json.
        from security.vault import BlindVault, reset_vault
        assert not (state.home / "credentials.json").exists()
        assert (state.home / "credentials.enc").exists()
        reset_vault()
        vault = BlindVault(vault_path=str(state.home / "credentials.json"))
        assert vault.retrieve("OPENAI_API_KEY") == "sk-new"


# ----------------------------------------------------------------------
# State machine navigation
# ----------------------------------------------------------------------


class TestStateMachine:
    @pytest.mark.asyncio
    async def test_runs_all_steps_in_order(self, tmp_path):
        state = WizardState.load(tmp_path / "feral")
        order: list[str] = []

        def step_a(s):
            order.append("a")

        def step_b(s):
            order.append("b")

        async def step_c(s):
            order.append("c")

        await StateMachine(state=state, steps=[("a", step_a), ("b", step_b), ("c", step_c)]).run()
        assert order == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_back_navigation_repeats_prior_step(self, tmp_path):
        state = WizardState.load(tmp_path / "feral")
        calls: list[str] = []
        back_once = {"done": False}

        def step_a(s):
            calls.append("a")

        def step_b(s):
            calls.append("b")
            if not back_once["done"]:
                back_once["done"] = True
                raise BackNavigation()

        def step_c(s):
            calls.append("c")

        await StateMachine(state=state, steps=[("a", step_a), ("b", step_b), ("c", step_c)]).run()
        assert calls == ["a", "b", "a", "b", "c"]

    @pytest.mark.asyncio
    async def test_quit_halts_without_running_remaining_steps(self, tmp_path):
        state = WizardState.load(tmp_path / "feral")
        calls: list[str] = []

        def step_a(s):
            calls.append("a")

        def step_b(s):
            raise QuitNavigation()

        def step_c(s):
            calls.append("c")

        await StateMachine(state=state, steps=[("a", step_a), ("b", step_b), ("c", step_c)]).run()
        assert calls == ["a"]

    @pytest.mark.asyncio
    async def test_step_exception_does_not_halt(self, tmp_path):
        state = WizardState.load(tmp_path / "feral")
        calls: list[str] = []

        def step_a(s):
            calls.append("a")
            raise RuntimeError("simulated")

        def step_b(s):
            calls.append("b")

        await StateMachine(state=state, steps=[("a", step_a), ("b", step_b)]).run()
        assert calls == ["a", "b"]


# ----------------------------------------------------------------------
# LLM step — integrates with ProviderCatalog
# ----------------------------------------------------------------------


class TestLLMStep:
    @pytest.mark.asyncio
    async def test_model_step_accepts_free_text_newer_than_catalog(self, tmp_path, monkeypatch):
        """Users typing a model string the bundled catalog doesn't know
        about (e.g. brand-new 'gpt-6-omega') should NOT be rejected."""
        from cli.setup.steps import llm as llm_step

        state = WizardState.load(tmp_path / "feral")
        state.set_setting("llm", "provider", "openai")

        fake_catalog = MagicMock()
        fake_desc = MagicMock()
        fake_desc.default_model = "gpt-4o-mini"
        fake_desc.display_name = "OpenAI"
        fake_catalog.get_descriptor.return_value = fake_desc

        async def _list_models(*a, **kw):
            from providers.catalog import CachedModelList
            return CachedModelList(models=["gpt-4o-mini", "gpt-4o"], last_refresh=0.0, source="cache")
        fake_catalog.list_models = AsyncMock(side_effect=_list_models)
        monkeypatch.setattr(llm_step, "get_shared_catalog", lambda: fake_catalog)
        setattr(state, "_catalog", fake_catalog)

        # ask_text returns the free-text model
        monkeypatch.setattr(llm_step, "ask_text", lambda *a, **kw: "gpt-6-omega")
        await llm_step.run_model_step(state)
        assert state.get_setting("llm", "model") == "gpt-6-omega"

    @pytest.mark.asyncio
    async def test_model_step_numeric_picker(self, tmp_path, monkeypatch):
        """Typing '2' should pick the second listed model."""
        from cli.setup.steps import llm as llm_step

        state = WizardState.load(tmp_path / "feral")
        state.set_setting("llm", "provider", "openai")

        fake_catalog = MagicMock()
        fake_desc = MagicMock()
        fake_desc.default_model = "gpt-4o-mini"
        fake_desc.display_name = "OpenAI"
        fake_catalog.get_descriptor.return_value = fake_desc

        async def _list_models(*a, **kw):
            from providers.catalog import CachedModelList
            return CachedModelList(models=["gpt-4o-mini", "gpt-4o", "o1"], last_refresh=0.0, source="cache")
        fake_catalog.list_models = AsyncMock(side_effect=_list_models)
        monkeypatch.setattr(llm_step, "get_shared_catalog", lambda: fake_catalog)
        setattr(state, "_catalog", fake_catalog)

        monkeypatch.setattr(llm_step, "ask_text", lambda *a, **kw: "2")
        await llm_step.run_model_step(state)
        assert state.get_setting("llm", "model") == "gpt-4o"


# ----------------------------------------------------------------------
# Audio step
# ----------------------------------------------------------------------


class TestAudioStep:
    def test_skip_step_keeps_defaults(self, tmp_path, monkeypatch):
        from cli.setup.steps import audio as audio_step

        state = WizardState.load(tmp_path / "feral")
        monkeypatch.setattr(audio_step, "confirm", lambda *a, **kw: False)
        asyncio.run(audio_step.run(state))
        assert state.get_setting("audio", "stt_provider") == "openai"

    def test_local_preset_picks_whisper_and_piper(self, tmp_path, monkeypatch):
        from cli.setup.steps import audio as audio_step

        state = WizardState.load(tmp_path / "feral")
        # Pretend faster-whisper + piper are both installed.
        monkeypatch.setattr(
            audio_step,
            "detect_local_audio_capabilities",
            lambda: {
                "local_stt": True, "local_tts": True,
                "stt_models": ["base"], "tts_voices": ["en_US-lessac-medium"],
            },
        )
        # confirm() calls: (1) configure voice? yes, (2) prefer fully local? yes
        answers = iter([True, True])
        monkeypatch.setattr(audio_step, "confirm", lambda *a, **kw: next(answers))
        asyncio.run(audio_step.run(state))
        assert state.get_setting("audio", "stt_provider") == "faster-whisper"
        assert state.get_setting("audio", "tts_provider") == "piper"
        assert state.get_setting("audio", "tts_voice") == "en_US-lessac-medium"

    def test_cloud_path_writes_openai(self, tmp_path, monkeypatch):
        from cli.setup.steps import audio as audio_step

        state = WizardState.load(tmp_path / "feral")
        monkeypatch.setattr(
            audio_step,
            "detect_local_audio_capabilities",
            lambda: {"local_stt": False, "local_tts": False},
        )
        # configure voice? yes. prefer fully local? no.
        answers = iter([True, False])
        monkeypatch.setattr(audio_step, "confirm", lambda *a, **kw: next(answers))
        # ask_choice first picks OpenAI STT, then OpenAI TTS, then
        # the audit-r11 fallback step asks for the whisper fallback
        # provider — supply the OpenAI /audio/speech option ("whisper").
        choices = iter([
            Option(id="openai", label="OpenAI Whisper (cloud)"),
            Option(id="openai", label="OpenAI TTS (cloud)"),
            Option(id="whisper", label="OpenAI /audio/speech (cheap mp3)"),
        ])
        monkeypatch.setattr(audio_step, "ask_choice", lambda *a, **kw: next(choices))
        # ask_text is called for model + voice
        texts = iter(["whisper-1", "tts-1-hd", "shimmer"])
        monkeypatch.setattr(audio_step, "ask_text", lambda *a, **kw: next(texts))
        asyncio.run(audio_step.run(state))
        assert state.get_setting("audio", "stt_model") == "whisper-1"
        assert state.get_setting("audio", "tts_model") == "tts-1-hd"
        assert state.get_setting("audio", "tts_voice") == "shimmer"
        # Audit-r11: fallback TTS chain must be persisted so the voice
        # router can degrade gracefully when Realtime hits a quota.
        assert state.get_setting("audio", "fallback_tts_providers") == ["whisper"]


# ----------------------------------------------------------------------
# End-to-end final state
# ----------------------------------------------------------------------


class TestEndToEndState:
    def test_after_wizard_run_all_keys_round_trip(self, tmp_path):
        """audit-r14 / lane-07 W7 — call ``mark_complete()`` to model
        the finish step running. Without it ``state.save()`` alone
        leaves ``setup_complete`` unset (the new contract)."""
        state = WizardState.load(tmp_path / "feral")
        state.set_setting("llm", "provider", "ollama")
        state.set_setting("llm", "model", "llama3.3:8b")
        state.set_setting("audio", "stt_provider", "faster-whisper")
        state.set_setting("audio", "stt_model", "small")
        state.set_setting("audio", "tts_provider", "piper")
        state.set_setting("audio", "tts_voice", "en_GB-alan-medium")
        state.set_credential("OPENAI_API_KEY", "")
        state.save()
        state.mark_complete()

        # Re-load into a second state; everything persists.
        reloaded = WizardState.load(tmp_path / "feral")
        assert reloaded.settings["llm"]["provider"] == "ollama"
        assert reloaded.settings["llm"]["model"] == "llama3.3:8b"
        assert reloaded.settings["audio"]["stt_model"] == "small"
        assert reloaded.settings["audio"]["tts_voice"] == "en_GB-alan-medium"
        assert reloaded.settings["meta"]["setup_complete"] is True
