"""The LLM provider step must never strand the operator.

Reported live: ``feral setup`` reached "Step 1 of 16 - LLM Provider"
with the cursor on ``Codex (ChatGPT sign-in) - unreachable``. Selecting
it printed "Install the Codex CLI, run `codex login`, then re-run
`feral setup`" and then **continued** the wizard with a provider that
cannot answer a single chat turn: the model step found no models, fell
through to "type the exact model name", and the smoke test could not
pass. The operator was marched through steps that were structurally
incapable of succeeding, and the only instruction on screen was to
restart all sixteen.

The rule these tests pin is the distinction between:

* "this can still work" - a cloud provider whose key was just entered
  and probed unreachable transiently. That path prints a note and
  continues on purpose, and must keep doing so.
* "this cannot work until you do something outside the wizard" -
  Ollama not serving, LM Studio not serving, Codex with no `codex
  login` session. Those must offer a different provider right there
  and loop back to the picker.

Everything here drives the step function directly with the prompt
helpers monkeypatched, the way ``tests/test_setup_wizard_defect_fixes``
does; there is no terminal to drive.
"""

from __future__ import annotations

import asyncio

import pytest

from cli.setup.state import WizardState


def _status(provider_id: str, *, reachable: bool, configured: bool = False,
            error: str = "", supports_local: bool = False,
            requires_api_key: bool = False):
    from providers.catalog import ProviderStatus

    return ProviderStatus(
        provider_id=provider_id,
        display_name=provider_id,
        supports_local=supports_local,
        requires_api_key=requires_api_key,
        configured=configured,
        reachable=reachable,
        error=error,
    )


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    """A state + the real shared catalog with the network stubbed out.

    The real catalog is deliberate: the picker's option list, the
    descriptors and the ``supports_local`` / ``requires_api_key`` flags
    are exactly what the operator saw. Only the two calls that would
    touch the network (``probe`` and ``list_models``) are replaced.
    """
    from cli.setup.steps import llm as llm_step

    state = WizardState.load(tmp_path / "feral")
    catalog = llm_step._catalog(state)

    async def _probe_one(provider_id, *_a, **_kw):
        return _status(provider_id, reachable=True, configured=True)

    async def _list_models(*_a, **_kw):
        from providers.catalog import CachedModelList

        return CachedModelList(models=[], last_refresh=0.0, source="cache")

    monkeypatch.setattr(catalog, "probe", _probe_one)
    monkeypatch.setattr(catalog, "list_models", _list_models)

    # The re-pick offer is only made when a human can answer it. Under
    # pytest stdin is not a tty, so without this the step would (rightly)
    # decline to ask and these tests would pin nothing.
    #
    # ``raising=False`` on purpose: on the pre-fix module this name does
    # not exist, and a fixture that errored there would prove only that
    # a helper is missing. What has to fail before the fix is the
    # BEHAVIOUR below - the wizard walking on with a dead provider.
    monkeypatch.setattr(llm_step, "_can_prompt", lambda: True, raising=False)

    # No cloud key prompts in these tests - the provider choice is what
    # is under test, not the key UX.
    async def _skip_key(**_kw):
        return None

    monkeypatch.setattr(llm_step, "_configure_provider_key", _skip_key)

    return state, catalog, llm_step


def _script_choices(monkeypatch, llm_step, ids):
    """Answer every ``ask_choice`` from *ids*, recording the prompts.

    Runs off the end deliberately: a step that asks one question more
    than the script expects raises ``StopIteration`` rather than
    silently picking something, which is what a scripted-TUI test
    should do.
    """
    from cli.setup.helpers import Option

    prompts: list[str] = []
    answers = iter(ids)

    def fake_ask_choice(prompt, options, **_kw):
        prompts.append(prompt)
        picked = next(answers)
        for opt in options:
            if opt.id == picked:
                return opt
        return Option(id=picked, label=picked)

    monkeypatch.setattr(llm_step, "ask_choice", fake_ask_choice)
    return prompts


class TestUnreachableProviderOffersARePick:
    """The reported case: Codex, unreachable, cursor on it, enter."""

    def test_codex_unreachable_loops_back_to_the_picker(
        self, wizard, monkeypatch,
    ):
        state, _catalog, llm_step = wizard

        async def _probe_all(_catalog):
            return {"codex": _status("codex", reachable=False,
                                     error="codex CLI not found")}

        monkeypatch.setattr(llm_step, "_probe_all", _probe_all)
        prompts = _script_choices(
            monkeypatch, llm_step, ["codex", "repick", "openai"],
        )

        asyncio.run(llm_step.run_provider_step(state))

        # The wizard must not finish the step holding a provider that
        # cannot answer a chat turn.
        assert state.get_setting("llm", "provider") == "openai"
        # Three prompts: the picker, the "what now?" offer, the picker
        # again. Before the fix there was exactly one.
        assert len(prompts) == 3, prompts

    def test_the_operator_may_still_keep_the_broken_provider(
        self, wizard, monkeypatch,
    ):
        """Insisting is legitimate. The wizard must not force a change."""
        state, _catalog, llm_step = wizard

        async def _probe_all(_catalog):
            return {"codex": _status("codex", reachable=False)}

        monkeypatch.setattr(llm_step, "_probe_all", _probe_all)
        prompts = _script_choices(monkeypatch, llm_step, ["codex", "keep"])

        asyncio.run(llm_step.run_provider_step(state))

        assert state.get_setting("llm", "provider") == "codex"
        assert len(prompts) == 2, prompts

    def test_ollama_not_serving_offers_a_re_pick(self, wizard, monkeypatch):
        state, _catalog, llm_step = wizard

        async def _probe_all(_catalog):
            return {"ollama": _status("ollama", reachable=False,
                                      supports_local=True)}

        monkeypatch.setattr(llm_step, "_probe_all", _probe_all)
        prompts = _script_choices(
            monkeypatch, llm_step, ["ollama", "repick", "openai"],
        )

        asyncio.run(llm_step.run_provider_step(state))

        assert state.get_setting("llm", "provider") == "openai"
        assert len(prompts) == 3, prompts

    def test_lmstudio_not_serving_offers_a_re_pick(self, wizard, monkeypatch):
        state, _catalog, llm_step = wizard

        async def _probe_all(_catalog):
            return {"lmstudio": _status("lmstudio", reachable=False,
                                        supports_local=True)}

        monkeypatch.setattr(llm_step, "_probe_all", _probe_all)
        prompts = _script_choices(
            monkeypatch, llm_step, ["lmstudio", "repick", "openai"],
        )

        asyncio.run(llm_step.run_provider_step(state))

        assert state.get_setting("llm", "provider") == "openai"
        assert len(prompts) == 3, prompts

    def test_ollama_serving_with_no_models_offers_a_re_pick(
        self, wizard, monkeypatch,
    ):
        """Reachable is not the same as usable.

        A running Ollama with an empty model list dead-ends the model
        step just as hard as one that is not running at all.
        """
        state, _catalog, llm_step = wizard

        async def _probe_all(_catalog):
            return {"ollama": _status("ollama", reachable=True,
                                      configured=True, supports_local=True)}

        monkeypatch.setattr(llm_step, "_probe_all", _probe_all)
        # Decline the "pull a starter model now?" offer.
        monkeypatch.setattr(llm_step, "confirm", lambda *_a, **_kw: False)
        prompts = _script_choices(
            monkeypatch, llm_step, ["ollama", "repick", "openai"],
        )

        asyncio.run(llm_step.run_provider_step(state))

        assert state.get_setting("llm", "provider") == "openai"
        assert len(prompts) == 3, prompts


class TestTheContinueAnywayPathSurvives:
    """A cloud key that probes unreachable is "can still work"."""

    def test_cloud_provider_unreachable_after_a_key_does_not_re_prompt(
        self, wizard, monkeypatch,
    ):
        state, catalog, llm_step = wizard

        async def _probe_all(_catalog):
            return {"openai": _status("openai", reachable=False,
                                      configured=True,
                                      requires_api_key=True,
                                      error="connection reset")}

        async def _probe_one(provider_id, *_a, **_kw):
            return _status(provider_id, reachable=False, configured=True,
                           requires_api_key=True, error="connection reset")

        monkeypatch.setattr(llm_step, "_probe_all", _probe_all)
        monkeypatch.setattr(catalog, "probe", _probe_one)
        prompts = _script_choices(monkeypatch, llm_step, ["openai"])

        asyncio.run(llm_step.run_provider_step(state))

        # One prompt only: the provider picker. A transient cloud probe
        # is not a reason to make the operator choose again, and the key
        # they just entered can be re-probed later without re-running
        # anything.
        assert state.get_setting("llm", "provider") == "openai"
        assert len(prompts) == 1, prompts

    def test_a_ready_provider_asks_nothing_extra(self, wizard, monkeypatch):
        state, _catalog, llm_step = wizard

        async def _probe_all(_catalog):
            return {"openai": _status("openai", reachable=True,
                                      configured=True,
                                      requires_api_key=True)}

        monkeypatch.setattr(llm_step, "_probe_all", _probe_all)
        prompts = _script_choices(monkeypatch, llm_step, ["openai"])

        asyncio.run(llm_step.run_provider_step(state))

        assert state.get_setting("llm", "provider") == "openai"
        assert len(prompts) == 1, prompts


class TestTheRePickLoopTerminates:
    """``_MAX_MODEL_ATTEMPTS`` exists because a ``while True`` around a
    prompt defaulting to "ask me again" re-asked forever. The re-pick
    loop must not reintroduce that shape."""

    def test_an_operator_who_always_re_picks_still_finishes(
        self, wizard, monkeypatch,
    ):
        state, _catalog, llm_step = wizard

        async def _probe_all(_catalog):
            return {"codex": _status("codex", reachable=False)}

        monkeypatch.setattr(llm_step, "_probe_all", _probe_all)

        prompts: list[str] = []

        def fake_ask_choice(prompt, options, **_kw):
            prompts.append(prompt)
            # Always pick the broken provider, always ask to re-pick.
            wanted = "repick" if any(o.id == "repick" for o in options) else "codex"
            return next(o for o in options if o.id == wanted)

        monkeypatch.setattr(llm_step, "ask_choice", fake_ask_choice)

        asyncio.run(llm_step.run_provider_step(state))

        # Bounded: _MAX_PROVIDER_ATTEMPTS picks plus one fewer offers
        # (the final attempt keeps the pick instead of offering again).
        expected = llm_step._MAX_PROVIDER_ATTEMPTS * 2 - 1
        assert len(prompts) == expected, prompts
        assert state.get_setting("llm", "provider") == "codex"

    def test_the_bound_is_small_enough_to_be_a_bound(self):
        from cli.setup.steps import llm as llm_step

        assert 1 < llm_step._MAX_PROVIDER_ATTEMPTS <= 6


class TestNoStepTellsTheOperatorToRestartTheWizard:
    """The remediation text is user-visible and was the whole defect.

    "re-run `feral setup`" mid-wizard means "throw away the sixteen
    steps you are standing in". Every local-provider remediation string
    now points at the re-pick that is one keystroke away instead.
    """

    def test_local_provider_hints_do_not_say_re_run_setup(self):
        from cli.setup import local_providers

        for text in (
            local_providers.OLLAMA_INSTALL_HINT,
            local_providers.LMSTUDIO_INSTRUCTIONS,
        ):
            assert "feral setup" not in text, text

    def test_the_blocked_provider_handlers_do_not_say_re_run_setup(
        self, capsys,
    ):
        from cli.setup.steps import llm as llm_step

        console = llm_step.get_console()
        llm_step._show_codex_instructions(console, None)
        llm_step._show_lmstudio_instructions(console)
        llm_step._show_lmstudio_no_model(console)
        asyncio.run(llm_step._handle_ollama_unreachable(console))

        # Rich hard-wraps at the console width, so a phrase can be split
        # across two lines. Collapse whitespace before matching.
        printed = " ".join(capsys.readouterr().out.lower().split())
        assert "re-run `feral setup`" not in printed
        assert "re-run feral setup" not in printed
        assert "feral setup" not in printed


class TestBlockedHandlersReportTheBlock:
    """The handlers are the only thing that knows a provider is stuck."""

    def test_each_handler_returns_a_reason(self, capsys):
        from cli.setup.steps import llm as llm_step

        console = llm_step.get_console()
        assert llm_step._show_codex_instructions(console, None)
        assert llm_step._show_lmstudio_instructions(console)
        assert llm_step._show_lmstudio_no_model(console)
        assert asyncio.run(llm_step._handle_ollama_unreachable(console))
        capsys.readouterr()
