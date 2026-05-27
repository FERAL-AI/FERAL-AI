"""audit-r14 / lane-07 (W7) — wizard voice + TCC preflight steps.

Voice preflight reads the Wave 2 Lane 05 catalogue and lets the
operator pick a primary realtime + chained STT/TTS. TCC preflight is
macOS-only, read-only, and surfaces deeplinks.
"""

from __future__ import annotations

import asyncio
import json
import sys
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

    monkeypatch.setattr(vp, "confirm", lambda *a, **kw: False)

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


def test_tcc_preflight_persists_snapshot_and_uses_deeplinks(
    feral_home, monkeypatch, capsys,
):
    """On macOS, the step calls ``all_gui_permission_statuses``,
    renders deeplinks from ``TCC_CATALOG``, and persists a snapshot
    under ``settings.macos.tcc_snapshot``."""
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

    snapshot = state.get_setting("macos", "tcc_snapshot")
    assert isinstance(snapshot, list)
    assert {s["permission"] for s in snapshot} == {
        "accessibility", "screen_recording", "calendar",
    }
    # The snapshot must include the deeplink for at least the
    # screen_recording entry (denied → operator needs the URL).
    sr = next(s for s in snapshot if s["permission"] == "screen_recording")
    assert sr["deeplink"].startswith("x-apple.systempreferences:")

    out = capsys.readouterr().out
    # The deeplink for screen_recording must be printed when the
    # operator opts in to the deeplink list.
    assert "x-apple.systempreferences:" in out
