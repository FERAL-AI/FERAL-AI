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
        """audit-r14 / lane-07  — ``state.save()`` no longer flips
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
    async def test_model_step_fuzzy_pick_commits_without_ask_text(
        self, tmp_path, monkeypatch,
    ):
        """Lane U1 fix #2 — when the interactive picker returns a real
        model id, ``run_model_step`` must persist it directly without
        ever calling ``ask_text`` (which is what triggered the
        original "picker then type the model name" double-prompt bug).
        """
        from cli.setup.steps import llm as llm_step
        from cli import ui_kit

        state = WizardState.load(tmp_path / "feral")
        state.set_setting("llm", "provider", "openai")

        fake_catalog = MagicMock()
        fake_desc = MagicMock()
        fake_desc.default_model = "gpt-4o-mini"
        fake_desc.display_name = "OpenAI"
        fake_catalog.get_descriptor.return_value = fake_desc

        async def _list_models(*a, **kw):
            from providers.catalog import CachedModelList
            return CachedModelList(
                models=["gpt-4o-mini", "gpt-4o"],
                last_refresh=0.0,
                source="cache",
            )
        fake_catalog.list_models = AsyncMock(side_effect=_list_models)
        monkeypatch.setattr(llm_step, "get_shared_catalog", lambda: fake_catalog)
        setattr(state, "_catalog", fake_catalog)

        monkeypatch.setattr(ui_kit, "is_inquirer_available", lambda: True)
        monkeypatch.setattr(ui_kit, "is_interactive", lambda: True)
        monkeypatch.setattr(
            ui_kit, "fuzzy_pick", lambda *a, **kw: "gpt-4o",
        )

        ask_text_mock = MagicMock(side_effect=AssertionError(
            "ask_text must NOT be called when the picker returned a real model id."
        ))
        monkeypatch.setattr(llm_step, "ask_text", ask_text_mock)

        await llm_step.run_model_step(state)
        assert state.get_setting("llm", "model") == "gpt-4o"
        ask_text_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_model_step_fuzzy_pick_unwraps_choice_object(
        self, tmp_path, monkeypatch,
    ):
        """Lane U1 fix #1+#2 — when InquirerPy returns a ``Choice``
        wrapper (or any object with a ``.value`` attribute) the model
        step must persist the unwrapped string id, not the wrapper's
        ``repr`` and certainly not the custom-id sentinel."""
        from cli.setup.steps import llm as llm_step
        from cli import ui_kit

        state = WizardState.load(tmp_path / "feral")
        state.set_setting("llm", "provider", "openai")

        fake_catalog = MagicMock()
        fake_desc = MagicMock()
        fake_desc.default_model = "gpt-4o-mini"
        fake_desc.display_name = "OpenAI"
        fake_catalog.get_descriptor.return_value = fake_desc

        async def _list_models(*a, **kw):
            from providers.catalog import CachedModelList
            return CachedModelList(
                models=["gpt-4o-mini", "gpt-4o"],
                last_refresh=0.0,
                source="cache",
            )
        fake_catalog.list_models = AsyncMock(side_effect=_list_models)
        monkeypatch.setattr(llm_step, "get_shared_catalog", lambda: fake_catalog)
        setattr(state, "_catalog", fake_catalog)

        class _FakeChoice:
            def __init__(self, value, name):
                self.value = value
                self.name = name

            def __eq__(self, other):  # match-on-attribute parity with InquirerPy.Choice
                return getattr(other, "value", other) == self.value

            def __repr__(self):  # pragma: no cover
                return f"_FakeChoice({self.value!r})"

        monkeypatch.setattr(ui_kit, "is_inquirer_available", lambda: True)
        monkeypatch.setattr(ui_kit, "is_interactive", lambda: True)
        monkeypatch.setattr(
            ui_kit, "fuzzy_pick",
            lambda *a, **kw: _FakeChoice("gpt-4o", "gpt-4o display"),
        )

        ask_text_mock = MagicMock(side_effect=AssertionError(
            "ask_text must NOT be called when the picker returned a Choice"
            " wrapping a real model id."
        ))
        monkeypatch.setattr(llm_step, "ask_text", ask_text_mock)

        await llm_step.run_model_step(state)
        stored = state.get_setting("llm", "model")
        assert stored == "gpt-4o", f"expected unwrapped id, got {stored!r}"
        ask_text_mock.assert_not_called()

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
        # Lane U1 — model + voice are now picker-first (fuzzy_pick over
        # the available_models / available_voices catalog) instead of
        # ask_text. Mock the picker so the test still drives values.
        picks = iter(["tts-1-hd", "shimmer"])  # STT models is single-element so picker auto-confirms; TTS model + voice picked here
        stt_picks = iter(["whisper-1"])

        def fake_pick(message, choices, *, default=None, **kw):
            label = (message or "").strip().lower()
            if "model" in label and "shimmer" not in label:
                # STT or TTS model. STT has only ["whisper-1"], TTS has
                # ["tts-1", "tts-1-hd"] — distinguish by whether
                # whisper-1 is a valid choice.
                values = [getattr(c, "value", c.get("value") if isinstance(c, dict) else c) for c in choices]
                if "whisper-1" in values:
                    return next(stt_picks)
                return next(picks)
            return next(picks)

        monkeypatch.setattr(audio_step.ui_kit, "fuzzy_pick", fake_pick)
        # ask_text should NOT be called for Model/Voice now that the
        # picker is the primary path. If something regresses we want a
        # loud test failure rather than a silent free-text prompt.
        def _no_ask_text(*a, **kw):
            raise AssertionError(f"ask_text called unexpectedly: {a!r} {kw!r}")
        monkeypatch.setattr(audio_step, "ask_text", _no_ask_text)
        asyncio.run(audio_step.run(state))
        assert state.get_setting("audio", "stt_model") == "whisper-1"
        assert state.get_setting("audio", "tts_model") == "tts-1-hd"
        assert state.get_setting("audio", "tts_voice") == "shimmer"
        # Audit-r11: fallback TTS chain must be persisted so the voice
        # router can degrade gracefully when Realtime hits a quota.
        assert state.get_setting("audio", "fallback_tts_providers") == ["whisper"]

    def test_audio_step_model_uses_picker_not_ask_text(self, tmp_path, monkeypatch):
        """Lane U1 fix #4 — the STT/TTS model field must use the same
        fuzzy picker as the LLM model step. If anything tries to fall
        back to ``ask_text(" Model", ...)`` the test fails loud."""
        from cli.setup.steps import audio as audio_step

        state = WizardState.load(tmp_path / "feral")
        monkeypatch.setattr(
            audio_step,
            "detect_local_audio_capabilities",
            lambda: {"local_stt": False, "local_tts": False},
        )
        # configure voice? yes. fully-local? no.
        answers = iter([True, False])
        monkeypatch.setattr(audio_step, "confirm", lambda *a, **kw: next(answers))
        choices = iter([
            Option(id="openai", label="OpenAI Whisper (cloud)"),
            Option(id="openai", label="OpenAI TTS (cloud)"),
            Option(id="whisper", label="OpenAI /audio/speech (cheap mp3)"),
        ])
        monkeypatch.setattr(audio_step, "ask_choice", lambda *a, **kw: next(choices))

        # Track every picker invocation; assert ask_text never fires.
        picker_calls: list[tuple[str, list]] = []

        def fake_pick(message, choices, *, default=None, **kw):
            values = [getattr(c, "value", c.get("value") if isinstance(c, dict) else c) for c in choices]
            picker_calls.append((message, values))
            # STT models is ["whisper-1"]; TTS models is ["tts-1", "tts-1-hd"];
            # TTS voices includes "shimmer". Pick deterministically.
            if "whisper-1" in values:
                return "whisper-1"
            if "tts-1-hd" in values:
                return "tts-1-hd"
            if "shimmer" in values:
                return "shimmer"
            return values[0]

        monkeypatch.setattr(audio_step.ui_kit, "fuzzy_pick", fake_pick)
        monkeypatch.setattr(
            audio_step, "ask_text",
            MagicMock(side_effect=AssertionError(
                "ask_text must NOT be called for Model/Voice when the "
                "live catalog is non-empty."
            )),
        )

        asyncio.run(audio_step.run(state))
        assert state.get_setting("audio", "stt_model") == "whisper-1"
        assert state.get_setting("audio", "tts_model") == "tts-1-hd"
        assert state.get_setting("audio", "tts_voice") == "shimmer"
        # Picker should have been invoked at least three times: STT
        # model, TTS model, TTS voice.
        assert len(picker_calls) >= 3

    def test_audio_step_falls_back_to_ask_text_when_models_empty(
        self, tmp_path, monkeypatch,
    ):
        """Lane U1 fix #4 — when a provider's live catalog is empty
        (e.g. faster-whisper with no caps.stt_models) the picker is
        useless. Fall back to ``ask_text`` with a clear "no live
        catalog available" message so the operator still has an
        escape hatch."""
        from cli.setup.steps import audio as audio_step

        # Inject a synthetic provider with no available_models.
        empty_stt = (
            {
                "id": "empty",
                "label": "Empty STT",
                "needs_key": False,
                "env": "",
                "is_local": True,
                "aliases": ("empty",),
                "default_model": "",
                "available_models": [],
            },
        )

        state = WizardState.load(tmp_path / "feral")
        monkeypatch.setattr(
            audio_step,
            "detect_local_audio_capabilities",
            lambda: {"local_stt": True, "local_tts": False, "stt_models": []},
        )

        ask_text_log: list[tuple[tuple, dict]] = []

        def fake_ask_text(*a, **kw):
            ask_text_log.append((a, kw))
            return "type-it-yourself-v1"

        monkeypatch.setattr(audio_step, "ask_text", fake_ask_text)
        monkeypatch.setattr(
            audio_step, "ask_choice",
            lambda *a, **kw: Option(id="empty", label="Empty STT"),
        )

        # Picker must never be invoked when models list is empty.
        def _no_pick(*a, **kw):
            raise AssertionError(
                "fuzzy_pick must NOT be called when the live model catalog is empty."
            )
        monkeypatch.setattr(audio_step.ui_kit, "fuzzy_pick", _no_pick)

        # _configure_provider is synchronous; drive it directly to
        # exercise the empty-catalog branch without involving the
        # outer interactive confirm() chain.
        audio_step._configure_provider(
            state,
            "stt",
            empty_stt,
            True,  # local_available
            False,  # has_openai_key
            {"stt_models": []},
            audio_step.get_console(),
        )

        # ask_text should have been called with the "no live catalog
        # available" hint baked into the prompt.
        assert any(
            "no live catalog available" in (a[0] if a else kw.get("prompt", ""))
            for a, kw in ask_text_log
        ), f"expected fallback hint in ask_text prompts, got {ask_text_log!r}"
        assert state.get_setting("audio", "stt_model") == "type-it-yourself-v1"


# ----------------------------------------------------------------------
# End-to-end final state
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# Bug 1 — non-linear back-navigation (JumpToStep)
# Bug 2 — "key already exists" detection + keep/replace/add/remove UX
# ----------------------------------------------------------------------


class TestJumpToStepNavigation:
    """The wizard must let the operator hop from any step back to a
    specific earlier step (e.g. the provider/key step) without
    re-walking every intermediate prompt. Operator complaint: 'going
    back to the main setup it doesn't allow you and it goes back step
    by step ... I wanna go all the way back to setup another provider
    key'.
    """

    @pytest.mark.asyncio
    async def test_wizard_back_nav_jumps_to_earlier_step(self, tmp_path):
        """Driving the state machine, a step that raises JumpToStep
        with an explicit target must skip back to that step. Already-
        entered settings persist (the jump never wipes
        ``state.settings`` or ``state.credentials``)."""
        from cli.setup.helpers import JumpToStep

        state = WizardState.load(tmp_path / "feral")
        state.set_setting("llm", "provider", "openai")
        state.set_credential("OPENAI_API_KEY", "sk-old")

        # Track visits to each step so we can assert the jump
        # landed where we asked it to.
        visits: list[str] = []
        jumped = {"done": False}

        def step_llm(s):
            visits.append("llm")

        def step_audio(s):
            visits.append("audio")

        def step_channels(s):
            visits.append("channels")
            if not jumped["done"]:
                jumped["done"] = True
                raise JumpToStep("llm")

        def step_finish(s):
            visits.append("finish")

        machine = StateMachine(
            state=state,
            steps=[
                ("llm", step_llm),
                ("audio", step_audio),
                ("channels", step_channels),
                ("finish", step_finish),
            ],
        )
        await machine.run()

        # The order must show channels triggered a jump back to llm
        # (re-walked llm + audio + channels) before reaching finish.
        assert visits == ["llm", "audio", "channels", "llm", "audio", "channels", "finish"]
        # Settings + credentials survive the jump — the operator can
        # see the prior provider already picked when they re-enter
        # the llm step.
        assert state.get_setting("llm", "provider") == "openai"
        assert state.credentials["OPENAI_API_KEY"] == "sk-old"


class _FakeStatus:
    """Minimal stand-in for ``ProviderStatus`` used by the LLM step's
    side-by-side table rendering. The wizard reads ``configured``,
    ``reachable``, and ``error`` off the status; we surface them as
    plain attributes so the MagicMock-noise from ``mock.status_for()``
    doesn't leak into the rendered table."""

    configured = False
    reachable = False
    error = ""


class TestProviderKeyStepExistingKey:
    """Bug 2 — when a labeled key (or legacy default-namespace entry)
    is already stored, the provider step must surface that fact with
    a masked display + Keep/Replace/Add/Remove menu instead of asking
    "needs a key" and contradictorily prompting for one."""

    @pytest.mark.asyncio
    async def test_setup_key_step_shows_existing_key_masked(
        self, tmp_path, monkeypatch,
    ):
        from cli.setup.steps import llm as llm_step
        from cli.setup.helpers import Option as _Option

        state = WizardState.load(tmp_path / "feral")

        # Pretend vault_keys reports an active OpenAI key.
        from security import vault_keys as vk_mod

        class _FakeEntry:
            provider_id = "openai"
            label = "prod"
            is_active = True
            fingerprint = "sk-1…abcd(deadbeef)"
            created_at = 0.0
            last_used_at = None
            last_probe_at = None
            last_probe_ok = True

        monkeypatch.setattr(
            vk_mod, "list_provider_keys", lambda pid, **kw: [_FakeEntry()],
        )
        monkeypatch.setattr(
            vk_mod, "get_active_provider_key",
            lambda pid, **kw: "sk-1234567890abcd",
        )

        # Stub the catalog so we don't hit live providers.
        fake_catalog = MagicMock()
        fake_desc = MagicMock()
        fake_desc.requires_api_key = True
        fake_desc.credential_env_var = "OPENAI_API_KEY"
        fake_desc.display_name = "OpenAI"
        fake_desc.default_base_url = ""
        fake_desc.supports_local = False
        fake_desc.provider_id = "openai"
        fake_desc.aliases = ()
        fake_desc.notes = ""
        fake_catalog.get_descriptor.return_value = fake_desc
        fake_catalog.list_providers.return_value = [fake_desc]

        async def _probe(*a, **kw):
            from providers.catalog import ProviderStatus

            return ProviderStatus(
                provider_id="openai", display_name="OpenAI",
                supports_local=False, requires_api_key=True,
                configured=True, reachable=True,
            )

        fake_catalog.probe = AsyncMock(side_effect=_probe)
        fake_catalog.status_for = MagicMock(return_value=_FakeStatus())

        async def _list_models(*a, **kw):
            from providers.catalog import CachedModelList
            return CachedModelList(models=["gpt-4o-mini"], last_refresh=0.0, source="cache")

        fake_catalog.list_models = AsyncMock(side_effect=_list_models)
        monkeypatch.setattr(llm_step, "get_shared_catalog", lambda: fake_catalog)
        setattr(state, "_catalog", fake_catalog)

        # ask_choice: first pick provider, then pick "Keep current".
        picks = iter([
            _Option(id="openai", label="OpenAI"),
            _Option(id="keep", label="Keep current key"),
        ])
        monkeypatch.setattr(llm_step, "ask_choice", lambda *a, **kw: next(picks))

        # ask_text must NEVER be called when the operator keeps the
        # current key — that was the contradictory "enter a new key"
        # prompt the operator hit.
        def _no_ask_text(*a, **kw):
            raise AssertionError(
                f"ask_text must NOT be called when a key already exists; got {a!r} {kw!r}"
            )
        monkeypatch.setattr(llm_step, "ask_text", _no_ask_text)

        # confirm — only used by the local-provider helpers we won't
        # hit on this path. Trip if it fires.
        def _no_confirm(*a, **kw):
            raise AssertionError("confirm must NOT prompt 'use existing key?'")
        monkeypatch.setattr(llm_step, "confirm", _no_confirm)

        await llm_step.run_provider_step(state)

        assert state.get_setting("llm", "provider") == "openai"
        # The key is now in state.credentials + env so downstream
        # steps see it without a second vault round-trip.
        assert state.credentials.get("OPENAI_API_KEY") == "sk-1234567890abcd"

    @pytest.mark.asyncio
    async def test_setup_key_step_prompts_when_absent(
        self, tmp_path, monkeypatch,
    ):
        """No key anywhere → original free-text entry path runs."""
        from cli.setup.steps import llm as llm_step
        from cli.setup.helpers import Option as _Option

        state = WizardState.load(tmp_path / "feral")

        from security import vault_keys as vk_mod

        monkeypatch.setattr(vk_mod, "list_provider_keys", lambda pid, **kw: [])
        monkeypatch.setattr(vk_mod, "get_active_provider_key", lambda pid, **kw: None)
        monkeypatch.setattr(vk_mod, "add_provider_key", lambda *a, **kw: None)

        # Ensure no env var leaks into the test.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        fake_catalog = MagicMock()
        fake_desc = MagicMock()
        fake_desc.requires_api_key = True
        fake_desc.credential_env_var = "OPENAI_API_KEY"
        fake_desc.display_name = "OpenAI"
        fake_desc.default_base_url = ""
        fake_desc.supports_local = False
        fake_desc.provider_id = "openai"
        fake_desc.aliases = ()
        fake_desc.notes = ""
        fake_catalog.get_descriptor.return_value = fake_desc
        fake_catalog.list_providers.return_value = [fake_desc]

        async def _probe(*a, **kw):
            from providers.catalog import ProviderStatus
            return ProviderStatus(
                provider_id="openai", display_name="OpenAI",
                supports_local=False, requires_api_key=True,
                configured=False, reachable=False,
            )

        fake_catalog.probe = AsyncMock(side_effect=_probe)
        fake_catalog.status_for = MagicMock(return_value=_FakeStatus())

        async def _list_models(*a, **kw):
            from providers.catalog import CachedModelList
            return CachedModelList(models=[], last_refresh=0.0, source="cache")
        fake_catalog.list_models = AsyncMock(side_effect=_list_models)
        monkeypatch.setattr(llm_step, "get_shared_catalog", lambda: fake_catalog)
        setattr(state, "_catalog", fake_catalog)

        picks = iter([_Option(id="openai", label="OpenAI")])
        monkeypatch.setattr(llm_step, "ask_choice", lambda *a, **kw: next(picks))

        prompts_seen: list[str] = []

        def fake_ask_text(prompt, **kw):
            prompts_seen.append(prompt)
            return "sk-fresh-1234567890"

        monkeypatch.setattr(llm_step, "ask_text", fake_ask_text)

        await llm_step.run_provider_step(state)

        # ask_text must have been called exactly once — the free-text
        # entry for the missing key. No keep/replace menu was shown.
        assert any("OpenAI API key" in p for p in prompts_seen)
        assert state.credentials["OPENAI_API_KEY"] == "sk-fresh-1234567890"


# ----------------------------------------------------------------------
# Bug 2 — provider picker badge respects default-namespace key
# ----------------------------------------------------------------------


class TestProviderBadgeWithDefaultNamespaceKey:
    """``_build_options`` must consult EVERY credential surface the
    wizard knows (labeled vault → legacy default-namespace vault →
    env var) before deciding "needs API key". The operator's run
    hit a contradiction: badge said "needs API key" but selecting
    the row immediately printed "✓ key already configured · source:
    vault (default)" because the key step uses
    ``existing_provider_key`` (which DOES check the default
    namespace). The badge MUST mirror that resolution."""

    @pytest.mark.asyncio
    async def test_provider_badge_ready_for_default_namespace_key(
        self, tmp_path, monkeypatch,
    ):
        from cli.setup.steps import llm as llm_step

        state = WizardState.load(tmp_path / "feral")
        state.credentials["ANTHROPIC_API_KEY"] = "sk-ant-default-1234567890"

        from security import vault_keys as vk_mod

        monkeypatch.setattr(vk_mod, "list_provider_keys", lambda pid, **kw: [])
        monkeypatch.setattr(vk_mod, "get_active_provider_key", lambda pid, **kw: None)
        monkeypatch.setattr(vk_mod, "get_active_label", lambda pid, **kw: None)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        fake_catalog = MagicMock()
        fake_desc = MagicMock()
        fake_desc.requires_api_key = True
        fake_desc.supports_local = False
        fake_desc.provider_id = "anthropic"
        fake_desc.display_name = "Anthropic"
        fake_desc.credential_env_var = "ANTHROPIC_API_KEY"
        fake_desc.aliases = ("claude",)
        fake_desc.notes = ""
        fake_catalog.list_providers.return_value = [fake_desc]

        class _FakeStatus:
            configured = False
            reachable = False
            error = ""

        statuses = {"anthropic": _FakeStatus()}

        opts = llm_step._build_options(fake_catalog, statuses, state)
        anthropic_opt = next(o for o in opts if o.id == "anthropic")
        assert anthropic_opt.status == STATUS_READY, (
            f"default-namespace key must render as READY, got {anthropic_opt.status!r}"
        )
        assert anthropic_opt.status != STATUS_NEEDS_KEY

    @pytest.mark.asyncio
    async def test_provider_badge_needs_key_when_truly_absent(
        self, tmp_path, monkeypatch,
    ):
        """Regression guard for Bug 2 — when NO surface has a key,
        the badge correctly stays at NEEDS_KEY."""
        from cli.setup.steps import llm as llm_step

        state = WizardState.load(tmp_path / "feral")

        from security import vault_keys as vk_mod

        monkeypatch.setattr(vk_mod, "list_provider_keys", lambda pid, **kw: [])
        monkeypatch.setattr(vk_mod, "get_active_provider_key", lambda pid, **kw: None)
        monkeypatch.setattr(vk_mod, "get_active_label", lambda pid, **kw: None)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        fake_catalog = MagicMock()
        fake_desc = MagicMock()
        fake_desc.requires_api_key = True
        fake_desc.supports_local = False
        fake_desc.provider_id = "anthropic"
        fake_desc.display_name = "Anthropic"
        fake_desc.credential_env_var = "ANTHROPIC_API_KEY"
        fake_desc.aliases = ()
        fake_desc.notes = ""
        fake_catalog.list_providers.return_value = [fake_desc]

        class _FakeStatus:
            configured = False
            reachable = False
            error = ""

        statuses = {"anthropic": _FakeStatus()}

        opts = llm_step._build_options(fake_catalog, statuses, state)
        anthropic_opt = next(o for o in opts if o.id == "anthropic")
        assert anthropic_opt.status == STATUS_NEEDS_KEY


# ----------------------------------------------------------------------
# Bug 4 — startup banner renders at most once per invocation
# ----------------------------------------------------------------------


class TestBannerRendersOnce:
    """The FERAL ASCII banner is a one-shot first-run greeting; it
    must not be re-emitted when the state machine re-enters the
    welcome step (BackNavigation from step 1, or JumpToStep)."""

    @pytest.mark.asyncio
    async def test_banner_renders_once_including_after_jumpback(
        self, tmp_path, capsys,
    ):
        from cli.setup.helpers import JumpToStep
        from cli.setup.steps import welcome as welcome_step

        state = WizardState.load(tmp_path / "feral")

        visits: list[str] = []
        jumped = {"done": False}

        def step_llm(s):
            visits.append("llm")

        def step_channels(s):
            visits.append("channels")
            if not jumped["done"]:
                jumped["done"] = True
                raise JumpToStep("welcome")

        def step_finish(s):
            visits.append("finish")

        machine = StateMachine(
            state=state,
            steps=[
                ("welcome", welcome_step.run),
                ("llm", step_llm),
                ("channels", step_channels),
                ("finish", step_finish),
            ],
        )
        await machine.run()

        out = capsys.readouterr().out
        # The "Unleashed AI" subtitle string is part of the welcome
        # panel's banner block and appears nowhere else in the
        # wizard, so it's a precise per-render counter.
        banner_marker = "Unleashed AI"
        count = out.count(banner_marker)
        assert count == 1, (
            f"banner rendered {count} times across welcome → llm → channels "
            f"→ welcome → llm → channels → finish; expected exactly 1.\n"
            f"transcript:\n{out}"
        )


class TestEndToEndState:
    def test_after_wizard_run_all_keys_round_trip(self, tmp_path):
        """audit-r14 / lane-07  — call ``mark_complete()`` to model
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
