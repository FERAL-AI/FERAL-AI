"""Scripted live walkthrough of the full setup-wizard happy path.

This test drives the InquirerPy mock seam the other setup tests
already exercise to walk the operator-facing flow end-to-end and
prove the bug fixes hold together:

Happy path (``test_scripted_happy_path_chat_then_voice_reuse_then_jumpback``):

1. Pick OpenAI as the chat provider.
2. Confirm the OpenAI key is REUSED on the voice step (no
   re-prompt for the same vendor's key).
3. Pick the realtime model via the picker — no typed ``ask_text``.
4. Trigger ``JumpToStep`` from a late step; confirm the wizard hops
   back to the provider step and the masked key is displayed
   (because the labeled-key vault still has it).
5. The startup banner only renders once across the whole jump-back
   round-trip (Bug 4).

Rejected-key path (``test_scripted_voice_warns_when_existing_key_rejected``):

1. Same labeled OpenAI key is present.
2. The voice probe for ``openai_realtime`` returns a 401-rejected
   ``ProbeResult``.
3. The voice step must NOT silently reuse — it surfaces a warning
   and offers Replace / Keep anyway / Skip. The operator picks
   Replace, types a new key, and the wizard proceeds (Bug 1).

Default-namespace badge path
(``test_scripted_default_namespace_key_renders_ready_badge``):

1. The labeled-key vault is empty but ``state.credentials`` carries
   the legacy default-namespace ``OPENAI_API_KEY``.
2. The provider picker badge must read READY (not "needs API key"),
   matching what the operator sees one prompt later (Bug 2).

A true TTY-driven test (pexpect against ``feral setup``) is not
feasible from the sandbox because the InquirerPy + prompt_toolkit
pair needs a real controlling terminal — the existing wizard tests
all drive the prompt seam, not a pexpect child. The scripted
walkthroughs below prove the same invariants the operator would
see in a manual run.
"""

from __future__ import annotations

import asyncio
import time

import pytest


@pytest.fixture
def feral_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    return tmp_path


def _stub_voice_probe(monkeypatch, *, openai_realtime_ok: bool = True,
                      openai_realtime_status: int | None = 200,
                      openai_realtime_reason: str = "ok"):
    """Stub ``security.probe.probe`` so the scripted test doesn't
    depend on whatever ``OPENAI_API_KEY`` happens to be in the host
    env. ``openai_realtime_ok=False`` lets the rejected-key test
    flip the verdict for that one provider only."""
    from security import probe as probe_mod

    async def _fake(pid, **_kw):
        if pid == "openai_realtime":
            return probe_mod.ProbeResult(
                provider=pid,
                ok=openai_realtime_ok,
                status_code=openai_realtime_status,
                reason=openai_realtime_reason,
                detail="stubbed",
                probed_at=time.time(),
                latency_ms=1.0,
            )
        return probe_mod.ProbeResult(
            provider=pid, ok=False, status_code=None, reason="no_key",
            detail="not configured", probed_at=time.time(), latency_ms=0.0,
        )

    monkeypatch.setattr(probe_mod, "probe", _fake)
    probe_mod.clear_probe_cache()


def _seed_labeled_openai_key(monkeypatch):
    """Pretend the labeled-key vault has an active OpenAI key, with
    no labeled keys for any other vendor."""
    from security import vault_keys as vk_mod

    class _FakeEntry:
        provider_id = "openai"
        label = "default"
        is_active = True
        fingerprint = "sk-c…1234(beefface)"
        created_at = 0.0
        last_used_at = None
        last_probe_at = None
        last_probe_ok = True

    monkeypatch.setattr(
        vk_mod, "list_provider_keys",
        lambda pid, **kw: [_FakeEntry()] if pid == "openai" else [],
    )
    monkeypatch.setattr(
        vk_mod, "get_active_provider_key",
        lambda pid, **kw: "sk-chat-1234567890" if pid == "openai" else None,
    )
    monkeypatch.setattr(vk_mod, "add_provider_key", lambda *a, **kw: None)
    monkeypatch.setattr(vk_mod, "get_active_label", lambda pid, **kw: "default")


def _fake_catalog(monkeypatch):
    """Stub the LLM ProviderCatalog so we don't hit any network."""
    from cli.setup.steps import llm as llm_step
    from unittest.mock import AsyncMock, MagicMock

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

    class _FakeStatus:
        configured = True
        reachable = True
        error = ""

    fake_catalog.status_for = MagicMock(return_value=_FakeStatus())

    async def _list_models(*a, **kw):
        from providers.catalog import CachedModelList
        return CachedModelList(models=["gpt-4o-mini"], last_refresh=0.0, source="cache")

    fake_catalog.list_models = AsyncMock(side_effect=_list_models)
    monkeypatch.setattr(llm_step, "get_shared_catalog", lambda: fake_catalog)
    return fake_catalog


def test_scripted_happy_path_chat_then_voice_reuse_then_jumpback(
    feral_home, monkeypatch, capsys,
):
    """Walk the operator-visible flow as a single scripted run."""
    transcript: list[str] = []

    _seed_labeled_openai_key(monkeypatch)
    _stub_voice_probe(monkeypatch, openai_realtime_ok=True)
    _fake_catalog(monkeypatch)

    from cli.setup.steps import llm as llm_step
    from cli.setup.steps import voice_preflight as vp
    from cli.setup.steps import welcome as welcome_step
    from cli.setup.helpers import JumpToStep, Option
    from cli.setup.state import WizardState
    from cli.setup.state_machine import StateMachine

    answers = iter([
        Option(id="openai", label="OpenAI"),
        Option(id="keep", label="Keep current key"),
        # The voice step now opens with "how should voice run?"
        # (cloud / fully local / skip) before any provider picker.
        Option(id="cloud", label="Cloud providers"),
        Option(id="openai_realtime", label="OpenAI Realtime"),
        Option(id="gpt-realtime", label="gpt-realtime"),
        Option(id="__none__", label="(skip STT)"),
        Option(id="__none__", label="(skip TTS)"),
        Option(id="openai", label="OpenAI"),
        Option(id="keep", label="Keep current key"),
        Option(id="cloud", label="Cloud providers"),
        Option(id="openai_realtime", label="OpenAI Realtime"),
        Option(id="gpt-realtime", label="gpt-realtime"),
        Option(id="__none__", label="(skip STT)"),
        Option(id="__none__", label="(skip TTS)"),
    ])

    def fake_ask_choice(prompt, opts, default=None):
        transcript.append(f"ask_choice: {prompt}")
        return next(answers)

    monkeypatch.setattr(llm_step, "ask_choice", fake_ask_choice)
    monkeypatch.setattr(vp, "ask_choice", fake_ask_choice)

    monkeypatch.setattr(vp, "confirm", lambda *a, **kw: True)
    monkeypatch.setattr(llm_step, "confirm", lambda *a, **kw: True)

    def _no_ask_text(*a, **kw):
        raise AssertionError(
            f"ask_text fired unexpectedly during the scripted run: {a!r} {kw!r}"
        )

    monkeypatch.setattr(llm_step, "ask_text", _no_ask_text)
    monkeypatch.setattr(vp, "ask_text", _no_ask_text)

    visits: list[str] = []
    jumped = {"done": False}

    async def step_llm_provider(s):
        visits.append("llm_provider")
        await llm_step.run_provider_step(s)

    async def step_voice(s):
        visits.append("voice")
        await vp.run(s)

    def step_finish(s):
        visits.append("finish")

    def step_channels(s):
        visits.append("channels")
        if not jumped["done"]:
            jumped["done"] = True
            transcript.append("operator: jump back to llm_provider")
            raise JumpToStep("llm_provider")

    state = WizardState.load(feral_home)
    machine = StateMachine(
        state=state,
        steps=[
            ("welcome", welcome_step.run),
            ("llm_provider", step_llm_provider),
            ("voice_preflight", step_voice),
            ("channels", step_channels),
            ("finish", step_finish),
        ],
    )
    asyncio.run(machine.run())

    captured = capsys.readouterr()
    transcript.append("--- captured stdout ---")
    transcript.append(captured.out)

    assert visits == [
        "llm_provider", "voice", "channels",
        "llm_provider", "voice", "channels", "finish",
    ], f"unexpected step order: {visits}"

    assert state.credentials.get("OPENAI_API_KEY") == "sk-chat-1234567890"
    assert state.get_setting("audio", "realtime_primary") == "openai_realtime"
    assert state.get_setting("audio", "realtime_model") == "gpt-realtime"

    assert "sk-c…1234" in captured.out or "✓ OpenAI key already configured" in captured.out, (
        f"expected masked-key display in the transcript, got:\n{captured.out}"
    )

    assert "Reusing existing openai" in captured.out, (
        f"expected the reuse hint in the transcript, got:\n{captured.out}"
    )

    # Bug 4 — the FERAL banner must render at most once. The welcome
    # step is invoked once in the step ladder above; the jump-back
    # path re-enters the state machine loop but the banner guard in
    # welcome.py keeps the ASCII art from being re-emitted. We
    # count the "Unleashed AI" subtitle string because it appears
    # exactly once per banner render and never in any other step.
    banner_marker = "Unleashed AI"
    banner_count = captured.out.count(banner_marker)
    assert banner_count == 1, (
        f"banner rendered {banner_count} times — expected exactly 1.\n"
        f"transcript:\n{captured.out}"
    )

    print("\n".join(transcript))


def test_scripted_voice_warns_when_existing_key_rejected(
    feral_home, monkeypatch, capsys,
):
    """Bug 1 — the voice step must NOT silently reuse a key the
    probe just rejected. The operator must see a warning + the
    Replace / Keep anyway / Skip menu."""
    _seed_labeled_openai_key(monkeypatch)
    _stub_voice_probe(
        monkeypatch,
        openai_realtime_ok=False,
        openai_realtime_status=401,
        openai_realtime_reason="unauthorized",
    )
    _fake_catalog(monkeypatch)

    from cli.setup.steps import voice_preflight as vp
    from cli.setup.helpers import Option
    from cli.setup.state import WizardState

    answers = iter([
        # The voice step now opens with "how should voice run?"
        # (cloud / fully local / skip) before any provider picker.
        Option(id="cloud", label="Cloud providers"),
        Option(id="openai_realtime", label="OpenAI Realtime"),
        Option(id="replace", label="Replace it now"),
        Option(id="gpt-realtime", label="gpt-realtime"),
        Option(id="__none__", label="(skip STT)"),
        Option(id="__none__", label="(skip TTS)"),
    ])
    ask_choice_log: list[list[str]] = []

    def fake_ask_choice(prompt, opts, default=None):
        ask_choice_log.append([getattr(o, "id", None) for o in opts])
        return next(answers)

    monkeypatch.setattr(vp, "ask_choice", fake_ask_choice)
    monkeypatch.setattr(vp, "confirm", lambda *a, **kw: True)
    monkeypatch.setattr(
        vp, "ask_text", lambda *a, **kw: "sk-fresh-replacement-9876543210",
    )

    state = WizardState.load(feral_home)
    asyncio.run(vp.run(state))

    captured = capsys.readouterr().out
    assert "key was rejected" in captured or "401" in captured, (
        f"expected the rejected-key warning in transcript, got:\n{captured}"
    )
    menu_offered = any(set(opts) >= {"replace", "keep", "skip"} for opts in ask_choice_log)
    assert menu_offered, (
        f"expected ask_choice to offer Replace/Keep/Skip; "
        f"call log:\n{ask_choice_log!r}\ntranscript:\n{captured}"
    )

    print("\n--- rejected-key scripted-live transcript ---")
    print(captured)
    print(f"--- ask_choice call log: {ask_choice_log!r} ---")
    # Replace path → new key persisted to env + state.credentials.
    assert state.credentials.get("OPENAI_API_KEY") == "sk-fresh-replacement-9876543210"
    # Model picker must NOT render the provider as ready when the
    # probe was rejected — the model rows reflect the parent
    # provider's real status.
    assert "ready" not in captured.lower().split("openai_realtime")[-1][:200] or "unauthorized" in captured.lower(), (
        f"model picker should not advertise 'ready' under a "
        f"rejected provider; transcript:\n{captured}"
    )


def test_scripted_default_namespace_key_renders_ready_badge(
    feral_home, monkeypatch, capsys,
):
    """Bug 2 — when only the legacy default-namespace vault carries
    the key (no labeled key), the provider picker badge must read
    READY, never "needs API key". This matches the message the
    operator sees one prompt later from
    ``_configure_provider_key`` (✓ key already configured)."""
    # Labeled-key vault: empty for every provider.
    from security import vault_keys as vk_mod

    monkeypatch.setattr(vk_mod, "list_provider_keys", lambda pid, **kw: [])
    monkeypatch.setattr(vk_mod, "get_active_provider_key", lambda pid, **kw: None)
    monkeypatch.setattr(vk_mod, "add_provider_key", lambda *a, **kw: None)
    monkeypatch.setattr(vk_mod, "get_active_label", lambda pid, **kw: None)

    # No env-var leakage from the host shell either.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    fake_catalog = _fake_catalog(monkeypatch)

    # Force the catalog probe to say "configured but UNreachable" so
    # we know the READY badge can only come from the wizard
    # detecting state.credentials, not from a green probe.
    from providers.catalog import ProviderStatus

    async def _probe(*a, **kw):
        return ProviderStatus(
            provider_id="openai", display_name="OpenAI",
            supports_local=False, requires_api_key=True,
            configured=False, reachable=False,
        )

    fake_catalog.probe.side_effect = _probe

    class _FakeStatusUnreach:
        configured = False
        reachable = False
        error = ""

    fake_catalog.status_for.return_value = _FakeStatusUnreach()

    from cli.setup.helpers import STATUS_READY, STATUS_NEEDS_KEY
    from cli.setup.state import WizardState
    from cli.setup.steps.llm import _build_options

    state = WizardState.load(feral_home)
    state.credentials["OPENAI_API_KEY"] = "sk-from-default-namespace-vault"

    statuses = {"openai": _FakeStatusUnreach()}
    opts = _build_options(fake_catalog, statuses, state)
    assert opts, "expected at least one option from the fake catalog"
    openai_opt = next(o for o in opts if o.id == "openai")
    assert openai_opt.status == STATUS_READY, (
        f"default-namespace key must render as READY, got {openai_opt.status!r}"
    )
    assert openai_opt.status != STATUS_NEEDS_KEY

    capsys.readouterr()  # silence unrelated output
