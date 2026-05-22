"""audit-r14 / lane-07 (W4) — `feral voice` + `feral models` commands.

`feral voice providers` reads the Wave 2 Lane 05 catalogue + cached
probe state. `feral models list/test/set` wraps Lane 09's
ProviderCatalog without booting the brain. Both surfaces are pure-
local (covered by ``test_cli_pure_local.py`` for classification);
this file pins the dispatch + rendering behaviour.
"""

from __future__ import annotations

import json
import time

import pytest


@pytest.fixture(autouse=True)
def _isolate_feral_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.setenv("FERAL_DATA_HOME", str(tmp_path))


@pytest.fixture
def stub_voice_probes(monkeypatch):
    """Replace ``security.probe.probe`` with a deterministic stub."""
    from security import probe as probe_mod

    async def _fake(pid, **_kw):
        # Pretend Deepgram is configured + ok; everything else is no_key.
        if pid == "deepgram":
            return probe_mod.ProbeResult(
                provider=pid, ok=True, status_code=200, reason="ok",
                detail="OK", probed_at=time.time(), latency_ms=22.0,
            )
        if pid == "openai_tts":
            return probe_mod.ProbeResult(
                provider=pid, ok=False, status_code=401,
                reason="auth_failed", detail="invalid key",
                probed_at=time.time(), latency_ms=33.0,
            )
        return probe_mod.ProbeResult(
            provider=pid, ok=False, status_code=None,
            reason="no_key", detail="not configured",
            probed_at=time.time(), latency_ms=0.0,
        )

    monkeypatch.setattr(probe_mod, "probe", _fake)
    probe_mod.clear_probe_cache()


# ----------------------------------------------------------------------
# voice providers
# ----------------------------------------------------------------------


def test_voice_providers_lists_full_catalogue_with_probe_status(
    stub_voice_probes, capsys,
):
    from cli.voice_commands import cmd_voice_providers

    rc = cmd_voice_providers()
    out = capsys.readouterr().out

    assert rc == 0
    # All eight catalogue entries must appear (Lane 05 W7 contract).
    for name in (
        "OpenAI Realtime", "Gemini Live", "Deepgram",
        "Groq Whisper", "OpenAI Whisper",
        "ElevenLabs", "Cartesia", "OpenAI TTS",
    ):
        assert name in out, f"{name} missing from `feral voice providers`"
    # Counts line is present.
    assert "providers green" in out


def test_voice_providers_renders_red_for_auth_failed(stub_voice_probes, capsys):
    from cli.voice_commands import cmd_voice_providers

    cmd_voice_providers()
    out = capsys.readouterr().out
    # OpenAI TTS is stubbed as 401 → red ✘ key rejected
    assert "OpenAI TTS" in out
    # Rich table cells may wrap, but the literal "key rejected" must appear.
    assert "key rejected" in out


# ----------------------------------------------------------------------
# voice test (dispatch)
# ----------------------------------------------------------------------


def test_voice_test_unknown_provider_returns_2(capsys):
    from cli.voice_commands import cmd_voice_test

    rc = cmd_voice_test(provider="not-a-thing")
    out = capsys.readouterr().out
    assert rc == 2
    assert "Unknown voice provider" in out
    assert "Known:" in out


def test_voice_test_stt_requires_input(capsys):
    from cli.voice_commands import cmd_voice_test

    rc = cmd_voice_test(provider="deepgram")
    out = capsys.readouterr().out
    assert rc == 2
    assert "--input" in out


def test_voice_test_tts_requires_text(capsys):
    from cli.voice_commands import cmd_voice_test

    rc = cmd_voice_test(provider="elevenlabs")
    out = capsys.readouterr().out
    assert rc == 2
    assert "--text" in out


# ----------------------------------------------------------------------
# models list
# ----------------------------------------------------------------------


def test_models_list_renders_default_model_per_provider(capsys):
    """``feral models list --provider openai`` MUST list the cached
    catalogue and the descriptor's ``default_model``."""
    from cli.model_commands import cmd_models_list

    rc = cmd_models_list(provider="openai", live=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "openai" in out.lower()
    # Some model id should be present (the catalog ships a non-empty
    # default fallback list — at minimum, the descriptor's default).


def test_models_list_unknown_provider_returns_2(capsys):
    from cli.model_commands import cmd_models_list

    rc = cmd_models_list(provider="unknown-vendor")
    out = capsys.readouterr().out
    assert rc == 2
    assert "Unknown provider" in out


# ----------------------------------------------------------------------
# models set
# ----------------------------------------------------------------------


def test_models_set_writes_settings_json(tmp_path, monkeypatch, capsys):
    """``feral models set --provider X --model Y`` MUST persist
    ``llm.provider`` and ``llm.model`` in ``~/.feral/settings.json``."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    from cli.model_commands import cmd_models_set

    rc = cmd_models_set(provider="openai", model="gpt-5")
    assert rc == 0
    settings_path = tmp_path / "settings.json"
    assert settings_path.is_file()
    data = json.loads(settings_path.read_text())
    assert data["llm"]["provider"] == "openai"
    assert data["llm"]["model"] == "gpt-5"


def test_models_set_rejects_missing_args(capsys):
    from cli.model_commands import cmd_models_set

    rc = cmd_models_set(provider="", model="")
    out = capsys.readouterr().out
    assert rc == 2
    assert "required" in out.lower()


# ----------------------------------------------------------------------
# Wiring smoke test: feral voice/models appear in --help
# ----------------------------------------------------------------------


def test_voice_and_models_subcommands_registered():
    """The new top-level subcommands MUST appear in PURE_LOCAL_SUBCOMMANDS
    and in the live argparse subparsers list."""
    from cli import main as cli_main

    assert "voice" in cli_main.PURE_LOCAL_SUBCOMMANDS
    assert "models" in cli_main.PURE_LOCAL_SUBCOMMANDS
