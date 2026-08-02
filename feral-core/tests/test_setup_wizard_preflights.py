"""audit-r14 / lane-07 () — wizard voice + TCC preflight steps.

Voice preflight reads the Wave 2 Lane 05 catalogue and lets the
operator pick a primary realtime + chained STT/TTS. TCC preflight is
macOS-only, read-only, and surfaces deeplinks.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def feral_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def stub_voice_probes(monkeypatch):
    from security import probe as probe_mod

    async def _fake(pid, **_kw):
        if pid == "deepgram":
            return probe_mod.ProbeResult(
                provider=pid, ok=True, status_code=200, reason="ok",
                detail="OK", probed_at=time.time(), latency_ms=20.0,
            )
        if pid == "openai_realtime":
            return probe_mod.ProbeResult(
                provider=pid, ok=True, status_code=200, reason="ok",
                detail="OK", probed_at=time.time(), latency_ms=15.0,
            )
        return probe_mod.ProbeResult(
            provider=pid, ok=False, status_code=None, reason="no_key",
            detail="not configured", probed_at=time.time(), latency_ms=0.0,
        )

    monkeypatch.setattr(probe_mod, "probe", _fake)
    probe_mod.clear_probe_cache()


# ----------------------------------------------------------------------
# voice_preflight
# ----------------------------------------------------------------------


def test_voice_preflight_skipped_when_user_declines(
    feral_home, stub_voice_probes, monkeypatch,
):
    """`Configure voice now? No` raises SkipStep so the wizard moves
    on without persisting picks."""
    from cli.setup.steps import voice_preflight as vp
    from cli.setup.helpers import SkipStep
    from cli.setup.state import WizardState

    monkeypatch.setattr(vp, "_ask_voice_stack", lambda *a, **kw: "skip")

    state = WizardState.load(feral_home)
    with pytest.raises(SkipStep):
        asyncio.run(vp.run(state))

    # We still leave a marker so the wizard knows the operator made a
    # deliberate choice.
    assert state.get_setting("audio", "configured_via_wizard") is False


def test_voice_preflight_persists_realtime_and_chained_picks(
    feral_home, stub_voice_probes, monkeypatch,
):
    """Pin: when the operator confirms + picks providers, the choices
    land under ``audio.realtime_primary`` /
    ``audio.chained_stt_provider`` / ``audio.chained_tts_provider``."""
    from cli.setup.steps import voice_preflight as vp
    from cli.setup.helpers import Option
    from cli.setup.state import WizardState

    monkeypatch.setattr(vp, "confirm", lambda *a, **kw: True)
    # The step now opens with a three-way "how should voice run?"
    # (cloud / fully local / skip) before any provider picker, so a
    # test that drives the cloud flow has to say so. Patching the
    # helper rather than threading an extra Option through every
    # pick sequence keeps each test's intent readable.
    monkeypatch.setattr(vp, "_ask_voice_stack", lambda *a, **kw: "cloud")

    pick_seq = iter([
        # 1) realtime → openai_realtime
        Option(id="openai_realtime", label="OpenAI Realtime"),
        # 1a) realtime model — Lane U2 surfaces the catalogue model
        # list right after the provider pick when one is present.
        Option(id="gpt-realtime", label="gpt-realtime"),
        # 2) STT → deepgram
        Option(id="deepgram", label="Deepgram"),
        # 3) TTS → user skips
        Option(id="__none__", label="(skip TTS)"),
    ])

    def _ask(_prompt, _opts, default=None):
        return next(pick_seq)

    monkeypatch.setattr(vp, "ask_choice", _ask)

    state = WizardState.load(feral_home)
    asyncio.run(vp.run(state))

    assert state.get_setting("audio", "realtime_primary") == "openai_realtime"
    assert state.get_setting("audio", "chained_stt_provider") == "deepgram"
    # User skipped TTS → setting NOT persisted (the value stays unset).
    assert state.get_setting("audio", "chained_tts_provider") is None
    assert state.get_setting("audio", "configured_via_wizard") is True


def test_voice_preflight_asks_model_after_openai_realtime(
    feral_home, stub_voice_probes, monkeypatch,
):
    """Lane U2 — after the operator picks ``openai_realtime`` the
    wizard MUST ask for a realtime model and persist it under
    ``audio.realtime_model``. Pre-Lane-U2 the wizard stopped at the
    provider step and the runtime silently defaulted to
    ``gpt-realtime`` with no operator visibility."""
    from cli.setup.steps import voice_preflight as vp
    from cli.setup.helpers import Option
    from cli.setup.state import WizardState

    monkeypatch.setattr(vp, "confirm", lambda *a, **kw: True)
    # The step now opens with a three-way "how should voice run?"
    # (cloud / fully local / skip) before any provider picker, so a
    # test that drives the cloud flow has to say so. Patching the
    # helper rather than threading an extra Option through every
    # pick sequence keeps each test's intent readable.
    monkeypatch.setattr(vp, "_ask_voice_stack", lambda *a, **kw: "cloud")

    pick_seq = iter([
        Option(id="openai_realtime", label="OpenAI Realtime"),
        # The new realtime-model picker — the catalogue advertises a
        # populated ``models`` list so the wizard offers an in-list
        # ask_choice and the user picks the GA default.
        Option(id="gpt-realtime", label="gpt-realtime"),
        Option(id="__none__", label="(skip STT)"),
        Option(id="__none__", label="(skip TTS)"),
    ])

    def _ask(_prompt, _opts, default=None):
        return next(pick_seq)

    monkeypatch.setattr(vp, "ask_choice", _ask)

    state = WizardState.load(feral_home)
    asyncio.run(vp.run(state))

    assert state.get_setting("audio", "realtime_primary") == "openai_realtime"
    assert state.get_setting("audio", "realtime_model") == "gpt-realtime"


# ----------------------------------------------------------------------
# Bug 3 — realtime model picker (no ask_text typed prompt)
# Bug 4 — shared key reuse between chat + realtime voice
# ----------------------------------------------------------------------


def test_voice_step_uses_picker_not_ask_text_for_realtime_model(
    feral_home, stub_voice_probes, monkeypatch,
):
    """The realtime-model selection must always use ``ask_choice``
    (picker) when the catalogue carries a ``models`` list — never the
    typed ``ask_text`` fallback. Operator complaint: 'voice setup
    asks me to type the model name.'"""
    from cli.setup.steps import voice_preflight as vp
    from cli.setup.helpers import Option

    monkeypatch.setattr(vp, "confirm", lambda *a, **kw: True)
    # The step now opens with a three-way "how should voice run?"
    # (cloud / fully local / skip) before any provider picker, so a
    # test that drives the cloud flow has to say so. Patching the
    # helper rather than threading an extra Option through every
    # pick sequence keeps each test's intent readable.
    monkeypatch.setattr(vp, "_ask_voice_stack", lambda *a, **kw: "cloud")

    pick_seq = iter([
        Option(id="openai_realtime", label="OpenAI Realtime"),
        Option(id="gpt-realtime", label="gpt-realtime"),  # model picker
        Option(id="__none__", label="(skip STT)"),
        Option(id="__none__", label="(skip TTS)"),
    ])

    def _ask(_prompt, _opts, default=None):
        return next(pick_seq)

    monkeypatch.setattr(vp, "ask_choice", _ask)

    # Loud assertion: if any voice-preflight path falls through to
    # ask_text for a model id, fail the test.
    #
    # Scoped to MODEL prompts on purpose. The step legitimately calls
    # ask_text(secret=True) to collect an API key, which is a different
    # question, and failing on that made this test assert something it
    # never meant. It only stayed green because the key prompt is
    # reached solely when no key is discoverable: in a full-suite run a
    # key was in scope so the branch never ran, and in isolation (or on
    # CI, which has no key) it did and the test failed. Order-dependent
    # for a reason that had nothing to do with model pickers.
    def _no_ask_text(*a, **kw):
        if kw.get("secret"):
            return ""          # the key prompt, skipped, not a model pick
        raise AssertionError(
            f"ask_text must NOT be used to pick a realtime model; got {a!r} {kw!r}"
        )
    monkeypatch.setattr(vp, "ask_text", _no_ask_text)

    from cli.setup.state import WizardState

    state = WizardState.load(feral_home)
    asyncio.run(vp.run(state))

    assert state.get_setting("audio", "realtime_model") == "gpt-realtime"


def test_voice_step_reuses_existing_openai_key(
    feral_home, stub_voice_probes, monkeypatch,
):
    """Bug 4 — when ``security.vault_keys.get_active_provider_key('openai')``
    returns a key, the voice preflight step picks up the OpenAI
    realtime provider WITHOUT re-prompting for the key. The masked
    "Reusing existing openai key …" hint must appear in the output.
    """
    from cli.setup.steps import voice_preflight as vp
    from cli.setup.helpers import Option

    monkeypatch.setattr(vp, "confirm", lambda *a, **kw: True)
    # The step now opens with a three-way "how should voice run?"
    # (cloud / fully local / skip) before any provider picker, so a
    # test that drives the cloud flow has to say so. Patching the
    # helper rather than threading an extra Option through every
    # pick sequence keeps each test's intent readable.
    monkeypatch.setattr(vp, "_ask_voice_stack", lambda *a, **kw: "cloud")

    pick_seq = iter([
        Option(id="openai_realtime", label="OpenAI Realtime"),
        Option(id="gpt-realtime", label="gpt-realtime"),
        Option(id="__none__", label="(skip STT)"),
        Option(id="__none__", label="(skip TTS)"),
    ])
    monkeypatch.setattr(vp, "ask_choice", lambda *a, **kw: next(pick_seq))

    # Stub vault_keys so the test isn't dependent on a real vault.
    from security import vault_keys as vk_mod

    monkeypatch.setattr(vk_mod, "get_active_provider_key", lambda pid, **kw: "sk-chat-1234567890")
    monkeypatch.setattr(vk_mod, "list_provider_keys", lambda pid, **kw: [])
    monkeypatch.setattr(vk_mod, "add_provider_key", lambda *a, **kw: None)

    def _no_ask_text(*a, **kw):
        raise AssertionError(
            f"ask_text must NOT prompt for the OpenAI key when one already exists"
            f" — got {a!r} {kw!r}"
        )

    monkeypatch.setattr(vp, "ask_text", _no_ask_text)

    from cli.setup.state import WizardState

    state = WizardState.load(feral_home)
    asyncio.run(vp.run(state))

    assert state.get_setting("audio", "realtime_primary") == "openai_realtime"
    # The reuse path writes the key through to ``state.credentials``
    # so the voice router boot hydration sees it on the env var path.
    assert state.credentials.get("OPENAI_API_KEY") == "sk-chat-1234567890"


def test_voice_step_prompts_key_for_different_vendor(
    feral_home, stub_voice_probes, monkeypatch,
):
    """Bug 4 — only OpenAI is configured but the operator picks
    Gemini Live; the wizard must prompt for the missing
    ``GEMINI_API_KEY`` instead of silently advancing to a broken
    realtime session."""
    from cli.setup.steps import voice_preflight as vp
    from cli.setup.helpers import Option

    monkeypatch.setattr(vp, "confirm", lambda *a, **kw: True)
    # The step now opens with a three-way "how should voice run?"
    # (cloud / fully local / skip) before any provider picker, so a
    # test that drives the cloud flow has to say so. Patching the
    # helper rather than threading an extra Option through every
    # pick sequence keeps each test's intent readable.
    monkeypatch.setattr(vp, "_ask_voice_stack", lambda *a, **kw: "cloud")

    pick_seq = iter([
        Option(id="gemini_live", label="Gemini Live"),
        # No model picker for gemini_live — the catalogue doesn't
        # advertise a ``models[]`` list, so the wizard skips that
        # branch and lands on STT next.
        Option(id="__none__", label="(skip STT)"),
        Option(id="__none__", label="(skip TTS)"),
    ])
    monkeypatch.setattr(vp, "ask_choice", lambda *a, **kw: next(pick_seq))

    from security import vault_keys as vk_mod

    # Only the OpenAI key is configured. Gemini lookups return None.
    def _active(pid, **kw):
        return "sk-openai-only" if pid == "openai" else None

    monkeypatch.setattr(vk_mod, "get_active_provider_key", _active)
    monkeypatch.setattr(vk_mod, "list_provider_keys", lambda pid, **kw: [])
    added: list[tuple] = []
    monkeypatch.setattr(
        vk_mod, "add_provider_key",
        lambda pid, label, key, **kw: added.append((pid, label, key)),
    )

    # Make sure no Gemini env var is set so the prompt path triggers.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    # ask_text returns a fake Gemini key when prompted.
    asked: list[str] = []

    def fake_ask_text(prompt, **kw):
        asked.append(prompt)
        return "gem-fresh-9876543210"

    monkeypatch.setattr(vp, "ask_text", fake_ask_text)

    from cli.setup.state import WizardState

    state = WizardState.load(feral_home)
    asyncio.run(vp.run(state))

    # The wizard MUST have asked for a Gemini API key.
    assert any("gemini" in p.lower() for p in asked), (
        f"expected a Gemini key prompt; got {asked!r}"
    )
    assert state.credentials.get("GEMINI_API_KEY") == "gem-fresh-9876543210"
    # The labeled-key vault was written too so the next ``feral
    # voice`` invocation can find the key without re-running the
    # wizard.
    assert ("gemini", "default", "gem-fresh-9876543210") in added


# ----------------------------------------------------------------------
# Bug 1 — never silent-reuse a probe-rejected key
# ----------------------------------------------------------------------


def test_voice_reuse_warns_when_existing_key_rejected(
    feral_home, monkeypatch, capsys,
):
    """Bug 1 — the openai_realtime probe came back HTTP 401 (key
    rejected). The voice step must NOT print the green ✓ silent
    reuse line; it MUST surface a warning + Replace / Keep anyway
    / Skip menu. Also: when the probe is rejected, the realtime
    model picker must not advertise the model rows as ``ready``."""
    from security import probe as probe_mod

    async def _fake(pid, **_kw):
        if pid == "openai_realtime":
            return probe_mod.ProbeResult(
                provider=pid, ok=False, status_code=401,
                reason="unauthorized", detail="401",
                probed_at=time.time(), latency_ms=10.0,
            )
        return probe_mod.ProbeResult(
            provider=pid, ok=False, status_code=None, reason="no_key",
            detail="not configured", probed_at=time.time(), latency_ms=0.0,
        )

    monkeypatch.setattr(probe_mod, "probe", _fake)
    probe_mod.clear_probe_cache()

    from cli.setup.steps import voice_preflight as vp
    from cli.setup.helpers import Option
    from cli.setup.state import WizardState
    from security import vault_keys as vk_mod

    # The labeled-key vault has an active OpenAI key — the same one
    # the probe just rejected.
    monkeypatch.setattr(
        vk_mod, "get_active_provider_key",
        lambda pid, **kw: "sk-rejected-1234567890" if pid == "openai" else None,
    )
    monkeypatch.setattr(vk_mod, "list_provider_keys", lambda pid, **kw: [])
    monkeypatch.setattr(vk_mod, "add_provider_key", lambda *a, **kw: None)

    monkeypatch.setattr(vp, "confirm", lambda *a, **kw: True)
    # The step now opens with a three-way "how should voice run?"
    # (cloud / fully local / skip) before any provider picker, so a
    # test that drives the cloud flow has to say so. Patching the
    # helper rather than threading an extra Option through every
    # pick sequence keeps each test's intent readable.
    monkeypatch.setattr(vp, "_ask_voice_stack", lambda *a, **kw: "cloud")

    # Capture the model picker's options so we can assert the badge
    # does NOT say ready.
    captured_model_opts: list = []

    answers = iter([
        Option(id="openai_realtime", label="OpenAI Realtime"),
        Option(id="replace", label="Replace it now"),
        Option(id="gpt-realtime", label="gpt-realtime"),
        Option(id="__none__", label="(skip STT)"),
        Option(id="__none__", label="(skip TTS)"),
    ])
    ask_choice_log: list[tuple[str, list]] = []

    def _ask(prompt, opts, default=None):
        prompt_l = (prompt or "").lower()
        ask_choice_log.append((prompt or "", [getattr(o, "id", None) for o in opts]))
        if "pick the" in prompt_l and "model" in prompt_l:
            captured_model_opts.append(list(opts))
        return next(answers)

    monkeypatch.setattr(vp, "ask_choice", _ask)

    # ask_text feeds the replacement key when the operator picks Replace.
    monkeypatch.setattr(vp, "ask_text", lambda *a, **kw: "sk-fresh-9876543210x")

    state = WizardState.load(feral_home)
    asyncio.run(vp.run(state))

    out = capsys.readouterr().out
    # Warning text + verdict.
    assert "key was rejected" in out or "rejected" in out.lower(), (
        f"expected the rejected-key warning, got:\n{out}"
    )
    # The three-way menu options must have been offered via
    # ask_choice (the prompt text isn't in stdout because the
    # picker is mocked, so we inspect the recorded call log).
    menu_offered = any(
        set(opt_ids) >= {"replace", "keep", "skip"}
        for _prompt, opt_ids in ask_choice_log
    )
    assert menu_offered, (
        f"expected ask_choice to offer Replace/Keep/Skip; "
        f"call log:\n{ask_choice_log!r}"
    )
    # Replace path persisted the new key.
    assert state.credentials.get("OPENAI_API_KEY") == "sk-fresh-9876543210x"

    # Model picker, when it ran, must NOT have rendered the model
    # rows with status="ready" (the probe was rejected — the rows
    # should mirror that).
    for opts in captured_model_opts:
        for opt in opts:
            assert opt.status != "ready", (
                f"model picker labelled {opt.id!r} as ready under a "
                f"rejected provider — that's the Bug-1 contradiction."
            )


def test_voice_reuse_silent_when_probe_ok(
    feral_home, monkeypatch, capsys,
):
    """Regression guard for Bug 1 — when the probe came back OK,
    silent reuse is preserved (no Replace/Keep menu, just the
    green ✓ line)."""
    from security import probe as probe_mod

    async def _fake(pid, **_kw):
        if pid == "openai_realtime":
            return probe_mod.ProbeResult(
                provider=pid, ok=True, status_code=200, reason="ok",
                detail="OK", probed_at=time.time(), latency_ms=15.0,
            )
        return probe_mod.ProbeResult(
            provider=pid, ok=False, status_code=None, reason="no_key",
            detail="not configured", probed_at=time.time(), latency_ms=0.0,
        )

    monkeypatch.setattr(probe_mod, "probe", _fake)
    probe_mod.clear_probe_cache()

    from cli.setup.steps import voice_preflight as vp
    from cli.setup.helpers import Option
    from cli.setup.state import WizardState
    from security import vault_keys as vk_mod

    monkeypatch.setattr(
        vk_mod, "get_active_provider_key",
        lambda pid, **kw: "sk-good-1234567890" if pid == "openai" else None,
    )
    monkeypatch.setattr(vk_mod, "list_provider_keys", lambda pid, **kw: [])

    monkeypatch.setattr(vp, "confirm", lambda *a, **kw: True)
    # The step now opens with a three-way "how should voice run?"
    # (cloud / fully local / skip) before any provider picker, so a
    # test that drives the cloud flow has to say so. Patching the
    # helper rather than threading an extra Option through every
    # pick sequence keeps each test's intent readable.
    monkeypatch.setattr(vp, "_ask_voice_stack", lambda *a, **kw: "cloud")

    pick_seq = iter([
        Option(id="openai_realtime", label="OpenAI Realtime"),
        Option(id="gpt-realtime", label="gpt-realtime"),
        Option(id="__none__", label="(skip STT)"),
        Option(id="__none__", label="(skip TTS)"),
    ])
    monkeypatch.setattr(vp, "ask_choice", lambda *a, **kw: next(pick_seq))

    # ask_text must NEVER fire — silent reuse path doesn't prompt.
    monkeypatch.setattr(
        vp, "ask_text",
        lambda *a, **kw: pytest.fail(f"unexpected ask_text in silent-reuse path: {a}"),
    )

    state = WizardState.load(feral_home)
    asyncio.run(vp.run(state))

    out = capsys.readouterr().out
    assert "Reusing existing openai" in out, (
        f"expected the silent-reuse hint, got:\n{out}"
    )
    # The rejected-key warning MUST NOT appear when the probe was OK.
    assert "key was rejected" not in out


def test_key_masking_uniform_across_steps(
    feral_home, monkeypatch, capsys,
):
    """Bug 3 — chat step + voice step must mask the SAME secret to
    the SAME ``sk-…XYZW`` string. Both surfaces route through
    ``security.vault_keys.mask_key``; this test pins that
    contract so any future ad-hoc masking regresses loudly."""
    from security.vault_keys import mask_key

    secret = "sk-uniform-mask-1234567890abcdef"
    expected = mask_key(secret)
    assert expected and "…" in expected, "mask_key contract changed"

    # --- voice surface ---
    from security import probe as probe_mod

    async def _fake(pid, **_kw):
        return probe_mod.ProbeResult(
            provider=pid, ok=True, status_code=200, reason="ok",
            detail="OK", probed_at=time.time(), latency_ms=10.0,
        )

    monkeypatch.setattr(probe_mod, "probe", _fake)
    probe_mod.clear_probe_cache()

    from cli.setup.steps import voice_preflight as vp
    from cli.setup.helpers import Option
    from cli.setup.state import WizardState
    from security import vault_keys as vk_mod

    monkeypatch.setattr(
        vk_mod, "get_active_provider_key",
        lambda pid, **kw: secret if pid == "openai" else None,
    )
    monkeypatch.setattr(vk_mod, "list_provider_keys", lambda pid, **kw: [])

    monkeypatch.setattr(vp, "confirm", lambda *a, **kw: True)
    # The step now opens with a three-way "how should voice run?"
    # (cloud / fully local / skip) before any provider picker, so a
    # test that drives the cloud flow has to say so. Patching the
    # helper rather than threading an extra Option through every
    # pick sequence keeps each test's intent readable.
    monkeypatch.setattr(vp, "_ask_voice_stack", lambda *a, **kw: "cloud")
    monkeypatch.setattr(
        vp, "ask_choice",
        lambda *a, **kw: next(iter([
            Option(id="openai_realtime", label="OpenAI Realtime"),
            Option(id="gpt-realtime", label="gpt-realtime"),
            Option(id="__none__", label="(skip STT)"),
            Option(id="__none__", label="(skip TTS)"),
        ])),
    )
    # The above lambda only yields the first element each call, so
    # build a proper iter:
    seq = iter([
        Option(id="openai_realtime", label="OpenAI Realtime"),
        Option(id="gpt-realtime", label="gpt-realtime"),
        Option(id="__none__", label="(skip STT)"),
        Option(id="__none__", label="(skip TTS)"),
    ])
    monkeypatch.setattr(vp, "ask_choice", lambda *a, **kw: next(seq))

    state = WizardState.load(feral_home)
    asyncio.run(vp.run(state))
    voice_out = capsys.readouterr().out

    assert expected in voice_out, (
        f"voice step did not display masked key {expected!r}; "
        f"got:\n{voice_out}"
    )
    # And critically: no fully-masked '***' literal for this same
    # secret — that was the operator-visible inconsistency in the
    # original transcript.
    assert " ***" not in voice_out and "*** " not in voice_out, (
        f"voice step rendered a *** placeholder for a long secret; "
        f"got:\n{voice_out}"
    )

    # --- chat surface (drive _configure_provider_key directly) ---
    from cli.setup.steps import llm as llm_step

    fake_catalog = MagicMock()
    fake_catalog.configure = MagicMock()
    fake_desc = MagicMock()
    fake_desc.requires_api_key = True
    fake_desc.credential_env_var = "OPENAI_API_KEY"
    fake_desc.display_name = "OpenAI"
    fake_desc.provider_id = "openai"

    monkeypatch.setattr(
        llm_step, "ask_choice",
        lambda *a, **kw: type("O", (), {"id": "keep"})(),
    )
    monkeypatch.setattr(
        llm_step, "ask_text",
        lambda *a, **kw: pytest.fail("ask_text must NOT fire in keep-path"),
    )

    asyncio.run(llm_step._configure_provider_key(
        state=state, catalog=fake_catalog,
        provider_id="openai", env_var="OPENAI_API_KEY",
        display_name="OpenAI", console=vp.get_console(),
    ))
    chat_out = capsys.readouterr().out

    assert expected in chat_out, (
        f"chat step did not display masked key {expected!r}; "
        f"got:\n{chat_out}"
    )


# ----------------------------------------------------------------------
# tcc_preflight
# ----------------------------------------------------------------------


def test_tcc_preflight_no_op_off_darwin(feral_home, monkeypatch):
    """The step is a no-op (SkipStep) on non-Darwin platforms."""
    from cli.setup.steps import tcc_preflight
    from cli.setup.helpers import SkipStep
    from cli.setup.state import WizardState

    monkeypatch.setattr(tcc_preflight.platform, "system", lambda: "Linux")

    state = WizardState.load(feral_home)
    with pytest.raises(SkipStep):
        tcc_preflight.run(state)


def test_tcc_preflight_is_read_only_and_uses_deeplinks(
    feral_home, monkeypatch, capsys,
):
    """On macOS, the step calls ``all_gui_permission_statuses`` and
    renders deeplinks from ``TCC_CATALOG``.

    It must NOT persist anything. It used to write a
    ``settings.macos.tcc_snapshot`` list "so the doctor + dashboard can
    reflect the wizard's last reading without re-probing", but nothing
    in the codebase ever read that key, and TCC grants change outside
    FERAL so a cached copy is stale on arrival."""
    from cli.setup.steps import tcc_preflight
    from cli.setup.state import WizardState
    from security.macos_permissions import TCCStatus

    monkeypatch.setattr(tcc_preflight.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(tcc_preflight, "confirm", lambda *a, **kw: True)

    fake_statuses = [
        TCCStatus(
            permission="accessibility", status="granted",
            api="AXIsProcessTrustedWithOptions", setup_step="ok",
        ),
        TCCStatus(
            permission="screen_recording", status="denied",
            api="CGPreflightScreenCaptureAccess", setup_step="open settings",
        ),
        TCCStatus(
            permission="calendar", status="unknown",
            api="EKEventStore", setup_step="install pyobjc",
            error="not importable",
        ),
    ]
    import security.macos_permissions as macos_mod
    monkeypatch.setattr(macos_mod, "all_gui_permission_statuses", lambda: fake_statuses)

    state = WizardState.load(feral_home)
    tcc_preflight.run(state)

    # Regression: the step must leave settings untouched. The dead
    # ``macos.tcc_snapshot`` write is gone, and it was the step's only
    # write, so nothing at all should be persisted.
    assert state.get_setting("macos", "tcc_snapshot") is None
    assert state.settings == {}

    out = capsys.readouterr().out
    # The deeplink for screen_recording must be printed when the
    # operator opts in to the deeplink list.
    assert "x-apple.systempreferences:" in out
