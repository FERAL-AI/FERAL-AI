"""Scripted live walkthrough of the full setup-wizard happy path.

This test drives the InquirerPy mock seam the other setup tests
already exercise to walk the operator-facing flow end-to-end and
prove the four bug fixes hold together:

1. Pick OpenAI as the chat provider.
2. Enter the chat key once.
3. Reach voice preflight; confirm the OpenAI key is REUSED (no
   re-prompt for the same vendor's key).
4. Pick the realtime model via the picker — no typed ``ask_text``.
5. Trigger ``JumpToStep`` from a late step; confirm the wizard hops
   back to the provider step and the masked key is displayed
   (because the labeled-key vault still has it).
6. Continue forward to the finish step.

A true TTY-driven test (pexpect against ``feral setup``) is not
feasible from the sandbox because the InquirerPy + prompt_toolkit
pair needs a real controlling terminal — the existing wizard tests
all drive the prompt seam, not a pexpect child. The scripted
walkthrough below proves the same invariants the operator would see
in a manual run.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def feral_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    return tmp_path


def test_scripted_happy_path_chat_then_voice_reuse_then_jumpback(
    feral_home, monkeypatch, capsys,
):
    """Walk the operator-visible flow as a single scripted run."""
    transcript: list[str] = []

    # ----------------------------------------------------------------
    # Seed an already-stored OpenAI labeled key so the provider step
    # exercises the "key already exists → Keep" branch directly.
    # ----------------------------------------------------------------
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
        vk_mod, "list_provider_keys", lambda pid, **kw: [_FakeEntry()] if pid == "openai" else [],
    )
    monkeypatch.setattr(
        vk_mod, "get_active_provider_key",
        lambda pid, **kw: "sk-chat-1234567890" if pid == "openai" else None,
    )
    monkeypatch.setattr(vk_mod, "add_provider_key", lambda *a, **kw: None)
    monkeypatch.setattr(vk_mod, "get_active_label", lambda pid, **kw: "default")

    # ----------------------------------------------------------------
    # 1) Provider step (chat) — pick OpenAI then "Keep current key"
    # ----------------------------------------------------------------
    from cli.setup.steps import llm as llm_step
    from cli.setup.steps import voice_preflight as vp
    from cli.setup.helpers import JumpToStep, Option
    from cli.setup.state import WizardState
    from cli.setup.state_machine import StateMachine
    from unittest.mock import AsyncMock, MagicMock

    # Fake the ProviderCatalog so we don't hit network.
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

    # Scripted ask_choice answers for the provider step + the
    # downstream voice / jumpback steps.
    answers = iter([
        # 1a) provider picker → OpenAI
        Option(id="openai", label="OpenAI"),
        # 1b) key menu  → "Keep current key" (the masked-key path)
        Option(id="keep", label="Keep current key"),
        # 2a) realtime provider → openai_realtime
        Option(id="openai_realtime", label="OpenAI Realtime"),
        # 2b) realtime model picker → gpt-realtime
        Option(id="gpt-realtime", label="gpt-realtime"),
        # 2c) skip STT
        Option(id="__none__", label="(skip STT)"),
        # 2d) skip TTS
        Option(id="__none__", label="(skip TTS)"),
        # 3a) after jump, second pass through the provider step:
        #     re-pick OpenAI again → still shows existing key masked.
        Option(id="openai", label="OpenAI"),
        Option(id="keep", label="Keep current key"),
        # 3b) re-walk voice path
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

    # confirm — yes to "configure voice now?".
    monkeypatch.setattr(vp, "confirm", lambda *a, **kw: True)
    monkeypatch.setattr(llm_step, "confirm", lambda *a, **kw: True)

    # Pin ask_text to a loud failure — no Keep-path or reuse-path
    # should ever ask for a key in this scripted run.
    def _no_ask_text(*a, **kw):
        raise AssertionError(
            f"ask_text fired unexpectedly during the scripted run: {a!r} {kw!r}"
        )

    monkeypatch.setattr(llm_step, "ask_text", _no_ask_text)
    monkeypatch.setattr(vp, "ask_text", _no_ask_text)

    # ----------------------------------------------------------------
    # State machine — minimal step ladder.
    # ----------------------------------------------------------------
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

    # A "late" step that requests a jump back to the provider step
    # the first time it runs — simulates the operator hitting "↺
    # jump to a previous step…" from any later prompt.
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

    # The wizard MUST have re-walked the provider + voice steps after
    # the jump. Order: llm_provider → voice → channels (jump) →
    # llm_provider → voice → channels → finish.
    assert visits == [
        "llm_provider", "voice", "channels",
        "llm_provider", "voice", "channels", "finish",
    ], f"unexpected step order: {visits}"

    # The reuse path must have written the OpenAI key through to
    # state.credentials.
    assert state.credentials.get("OPENAI_API_KEY") == "sk-chat-1234567890"
    assert state.get_setting("audio", "realtime_primary") == "openai_realtime"
    assert state.get_setting("audio", "realtime_model") == "gpt-realtime"

    # The masked key (or its rich-formatted variant) must have been
    # printed at the provider step at least once.
    assert "sk-c…1234" in captured.out or "✓ OpenAI key already configured" in captured.out, (
        f"expected masked-key display in the transcript, got:\n{captured.out}"
    )

    # The "Reusing existing openai key" hint must have fired on the
    # voice step (Bug 4 confirmation).
    assert "Reusing existing openai" in captured.out, (
        f"expected the reuse hint in the transcript, got:\n{captured.out}"
    )

    # Print the transcript so the operator can eyeball it.
    print("\n".join(transcript))
