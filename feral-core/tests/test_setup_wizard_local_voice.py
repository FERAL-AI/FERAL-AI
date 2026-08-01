"""The fully-local branch of the voice setup step.

Three things it must get right, none of which the cloud branch needed:

* it must say, in the sentence that offers the option, that the LLM is
  still remote. "Fully local voice" is a claim a privacy-motivated
  operator will over-read, and the audio being local is not the same
  as the conversation being local;
* it must never ask for an API key, because these engines have no
  account to have a key for;
* it must not offer realtime at all, because every realtime provider is
  a socket to a vendor and there is no local one.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from cli.setup.state import WizardState


@pytest.fixture
def feral_home(tmp_path, monkeypatch):
    home = tmp_path / "feral"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FERAL_HOME", str(home))
    return home


@pytest.fixture
def stub_probes(monkeypatch):
    """Local engines ready, cloud ones not configured."""
    from security import probe as probe_mod

    ready = {"whispercpp", "macos_say", "silero_vad"}

    async def _fake(pid, **_kw):
        return probe_mod.ProbeResult(
            provider=pid,
            ok=pid in ready,
            status_code=None,
            reason="" if pid in ready else "no_key",
            detail="ready" if pid in ready else "not configured",
            probed_at=time.time(),
            latency_ms=0.0,
        )

    monkeypatch.setattr(probe_mod, "probe", _fake)
    probe_mod.clear_probe_cache()


def _no_downloads(monkeypatch, vp):
    """The wizard may offer a download; a test must never take one."""
    monkeypatch.setattr(vp, "confirm", lambda *_a, **_kw: False)


def test_the_stack_prompt_says_the_llm_is_still_remote(monkeypatch):
    """The one place the caveat cannot be missed."""
    from cli.setup.steps import voice_preflight as vp

    printed: list[str] = []

    class FakeConsole:
        def print(self, *args, **_kw):
            printed.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(
        vp, "ask_choice",
        lambda _p, opts, default=None: next(o for o in opts if o.id == "skip"),
    )
    assert vp._ask_voice_stack(FakeConsole()) == "skip"

    blob = " ".join(printed).lower()
    assert "llm" in blob
    assert "remote" in blob
    assert "no audio is uploaded" in blob
    # It must name the escape hatch rather than imply there is none.
    assert "ollama" in blob or "lm studio" in blob


def test_the_stack_prompt_offers_exactly_local_cloud_and_skip(monkeypatch):
    from cli.setup.steps import voice_preflight as vp

    seen: list[str] = []

    class FakeConsole:
        def print(self, *_a, **_kw):
            return None

    def _capture(_prompt, opts, default=None):
        seen.extend(o.id for o in opts)
        return next(o for o in opts if o.id == default)

    monkeypatch.setattr(vp, "ask_choice", _capture)
    chosen = vp._ask_voice_stack(FakeConsole())

    assert set(seen) == {"local", "cloud", "skip"}
    assert chosen == "cloud", "enter must not silently pick local"


def test_local_branch_persists_local_engines_and_skips_realtime(
    feral_home, stub_probes, monkeypatch,
):
    from cli.setup.steps import voice_preflight as vp

    monkeypatch.setattr(vp, "_ask_voice_stack", lambda *_a, **_kw: "local")
    _no_downloads(monkeypatch, vp)

    prompts: list[str] = []

    def _ask(prompt, opts, default=None):
        prompts.append(prompt)
        return next(o for o in opts if o.id == default)

    monkeypatch.setattr(vp, "ask_choice", _ask)

    def _no_ask_text(*a, **kw):
        raise AssertionError(f"local voice must never prompt for a key: {a!r}")

    monkeypatch.setattr(vp, "ask_text", _no_ask_text)

    state = WizardState.load(feral_home)
    asyncio.run(vp.run(state))

    assert not any("realtime" in p.lower() for p in prompts), prompts

    assert state.get_setting("audio", "realtime_primary") == ""
    assert state.get_setting("audio", "chained_stt_provider") == "whispercpp"
    assert state.get_setting("audio", "chained_tts_provider") == "macos_say"
    assert state.get_setting("audio", "fallback_mode") == "chained"
    assert state.get_setting("audio", "configured_via_wizard") is True


def test_local_picks_land_in_the_block_the_router_reads_first(
    feral_home, stub_probes, monkeypatch,
):
    """``_resolve_chained_config`` reads ``voice.chained.*`` before
    ``audio.chained_fallback.*``. A local pick written only to the
    latter would be overridden by anything the phone Settings panel
    later wrote to the former."""
    from cli.setup.steps import voice_preflight as vp

    monkeypatch.setattr(vp, "_ask_voice_stack", lambda *_a, **_kw: "local")
    _no_downloads(monkeypatch, vp)
    monkeypatch.setattr(
        vp, "ask_choice",
        lambda _p, opts, default=None: next(o for o in opts if o.id == default),
    )

    state = WizardState.load(feral_home)
    asyncio.run(vp.run(state))

    voice_chained = (state.settings.get("voice") or {}).get("chained") or {}
    assert voice_chained.get("stt_provider") == "whispercpp"
    assert voice_chained.get("tts_provider") == "macos_say"

    audio_chained = (state.settings.get("audio") or {}).get("chained_fallback") or {}
    assert audio_chained.get("stt_provider") == "whispercpp"
    assert audio_chained.get("tts_provider") == "macos_say"


def test_the_router_then_resolves_those_picks(feral_home, stub_probes, monkeypatch):
    """End to end through the resolver Wave 1 landed, not a re-read of
    the keys this step just wrote."""
    from cli.setup.steps import voice_preflight as vp
    from voice.router import VoiceRouter

    monkeypatch.setattr(vp, "_ask_voice_stack", lambda *_a, **_kw: "local")
    _no_downloads(monkeypatch, vp)
    monkeypatch.setattr(
        vp, "ask_choice",
        lambda _p, opts, default=None: next(o for o in opts if o.id == default),
    )

    state = WizardState.load(feral_home)
    asyncio.run(vp.run(state))
    state.save()

    from config import loader as loader_mod

    loader_mod.load_settings.cache_clear() if hasattr(
        loader_mod.load_settings, "cache_clear"
    ) else None

    resolved = VoiceRouter()._resolve_chained_config()
    assert resolved["stt_provider"] == "whispercpp"
    assert resolved["tts_provider"] == "macos_say"


def test_cloud_branch_does_not_offer_local_engines(
    feral_home, stub_probes, monkeypatch,
):
    """The cloud branch never downloads weights, so listing an engine
    that needs them would offer a pick it cannot complete."""
    from cli.setup.steps import voice_preflight as vp

    monkeypatch.setattr(vp, "_ask_voice_stack", lambda *_a, **_kw: "cloud")
    monkeypatch.setattr(vp, "confirm", lambda *_a, **_kw: True)

    offered: list[str] = []

    def _ask(_prompt, opts, default=None):
        offered.extend(o.id for o in opts)
        return next(o for o in opts if o.id == "__none__")

    monkeypatch.setattr(vp, "ask_choice", _ask)
    monkeypatch.setattr(vp, "ask_text", lambda *a, **kw: "")

    state = WizardState.load(feral_home)
    asyncio.run(vp.run(state))

    for local in ("whispercpp", "macos_say", "piper", "faster_whisper"):
        assert local not in offered, f"{local} offered on the cloud branch"
    assert "deepgram" in offered
