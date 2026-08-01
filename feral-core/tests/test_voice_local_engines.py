"""Local voice engines: registration, refusal, and the model store.

The rule these tests exist to enforce is the one an operator cannot
check for themselves: **a local engine that cannot run must refuse, not
quietly become a cloud call.** Someone who picked local STT so their
voice never leaves the machine is worse off with a working session on
Deepgram than with a broken one that says why.

``perception/audio_pipeline.py`` does the opposite (it flips to cloud on
local failure) and that behaviour is deliberately not copied here.
"""

from __future__ import annotations

import platform

import pytest

from voice import local_models
from voice.provider_registry import (
    LOCAL_STT_PROVIDERS,
    LOCAL_TTS_PROVIDERS,
    register_voice_providers,
    requires_credential,
)


# --- registration must not be able to break boot ------------------------


def test_registration_never_raises_when_an_optional_module_explodes(monkeypatch):
    """``api/state.py`` imports these at boot.

    An unguarded ``import voice.tts_providers.piper`` would turn "the
    operator did not install an optional extra" into "the brain does
    not start". The guard is what makes the one-line change in
    ``api/state.py`` safe.
    """
    import importlib

    real_import = importlib.import_module

    def _exploding(name, *args, **kwargs):
        if name == "voice.tts_providers.piper":
            raise ImportError("simulated broken native wheel")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _exploding)

    report = register_voice_providers()

    assert not report.ok
    assert "voice.tts_providers.piper" in report.failed
    # Everything else still registered.
    assert "voice.stt_providers.deepgram" in report.registered
    assert "voice.tts_providers.macos_say" in report.registered
    assert "piper" in report.summary()


def test_registration_registers_the_local_engines():
    report = register_voice_providers()
    assert report.ok, report.failed

    from voice.stt_providers import _PROVIDER_REGISTRY as stt_registry
    from voice.tts_providers import _PROVIDER_REGISTRY as tts_registry

    for name in LOCAL_STT_PROVIDERS:
        assert name in stt_registry, f"{name} not registered"
    for name in LOCAL_TTS_PROVIDERS:
        assert name in tts_registry, f"{name} not registered"


def test_local_providers_declare_they_need_no_credential():
    for name in LOCAL_STT_PROVIDERS:
        assert requires_credential("stt", name) is False
    for name in LOCAL_TTS_PROVIDERS:
        assert requires_credential("tts", name) is False
    assert requires_credential("stt", "deepgram") is True
    assert requires_credential("tts", "elevenlabs") is True


# --- the router's key table ---------------------------------------------


def test_unknown_provider_no_longer_demands_a_deepgram_key():
    """Regression pin.

    ``provider_env.get(name, ("deepgram", "DEEPGRAM_API_KEY"))`` meant
    any name the router did not recognise aborted the session
    demanding a Deepgram key. That silently made every local engine
    unusable, and told the operator to go get a credential for a vendor
    they had not chosen.
    """
    from voice.router import _provider_credential, _is_local_provider

    assert _provider_credential("stt", "whispercpp") == ("", "")
    assert _provider_credential("tts", "macos_say") == ("", "")
    assert _provider_credential("stt", "totally_made_up") == ("", "")
    assert _provider_credential("stt", "deepgram") == ("deepgram", "DEEPGRAM_API_KEY")

    assert _is_local_provider("stt", "whispercpp") is True
    assert _is_local_provider("tts", "macos_say") is True
    assert _is_local_provider("stt", "deepgram") is False


# --- local engines accept and ignore api_key ----------------------------


def test_local_stt_accepts_the_api_key_kwarg_the_router_always_sends():
    """``voice/router.py`` passes ``api_key=`` to every STT constructor.

    A local provider that rejected the kwarg would be unconstructible
    through the only path that constructs them.
    """
    from voice.stt_providers.whispercpp import WhisperCppSTTProvider

    with pytest.raises(RuntimeError) as excinfo:
        WhisperCppSTTProvider(api_key="sk-should-be-ignored", model="base.en")
    # It got far enough to check availability, i.e. the kwarg was
    # accepted; the failure is the missing engine, not a TypeError.
    assert "unavailable" in str(excinfo.value)


def test_local_engines_refuse_rather_than_falling_back_to_cloud():
    from voice.stt_providers.whispercpp import WhisperCppSTTProvider

    with pytest.raises(RuntimeError) as excinfo:
        WhisperCppSTTProvider(api_key="", model="base.en")
    message = str(excinfo.value)
    assert "will not silently fall back" in message
    assert "feral setup" in message


def test_faster_whisper_refuses_metal_explicitly():
    """CTranslate2 has no MPS backend.

    ``device="auto"`` would demote to CPU while every log line claimed
    local acceleration, so the wrong-looking-right case is refused by
    name and pointed at whisper.cpp.
    """
    from voice.stt_providers.faster_whisper_local import FasterWhisperSTTProvider

    with pytest.raises(RuntimeError) as excinfo:
        FasterWhisperSTTProvider(api_key="", device="mps")
    assert "Metal" in str(excinfo.value)
    assert "whispercpp" in str(excinfo.value)


# --- the model store ----------------------------------------------------


def test_model_store_lives_under_feral_home(tmp_path, monkeypatch):
    """Never ``~/.feral`` when ``FERAL_HOME`` is set. Test isolation
    depends on this and so does an operator who relocated their install."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
    root = local_models.models_root()
    assert str(root).startswith(str(tmp_path))
    assert root.name == "models"
    assert local_models.model_path("vad", "silero_vad.onnx").parent == root / "vad"


def test_weights_are_never_downloaded_by_a_runtime_caller(tmp_path, monkeypatch):
    """The whole latency-honesty rule in one assertion.

    A voice turn that discovered a missing model and fetched 75MB
    inline would look exactly like a hang. Runtime callers get a
    refusal carrying the command that fixes it; only setup passes
    ``allow_download=True``.
    """
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))

    def _boom(*_args, **_kwargs):
        raise AssertionError("a runtime caller must never download weights")

    monkeypatch.setattr(local_models, "download", _boom)

    with pytest.raises(local_models.ModelUnavailable) as excinfo:
        local_models.ensure_silero_vad()
    assert "feral setup" in excinfo.value.remedy

    with pytest.raises(local_models.ModelUnavailable):
        local_models.ensure_piper_voice("en_US-lessac-medium")

    with pytest.raises(local_models.ModelUnavailable):
        local_models.ensure_whispercpp_model("base.en")


def test_a_truncated_download_is_not_reported_as_present(tmp_path, monkeypatch):
    """Guards against a 404 page landing on disk as if it were weights."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
    path = local_models.silero_vad_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"<html>404</html>")
    assert local_models.silero_vad_present() is False


def test_piper_voice_name_maps_to_its_upstream_path():
    weights, config = local_models.piper_voice_specs("en_US-lessac-medium")
    assert weights.filename == "en_US-lessac-medium.onnx"
    assert config.filename == "en_US-lessac-medium.onnx.json"
    assert weights.url.endswith("/en/en_US/lessac/medium/en_US-lessac-medium.onnx")

    with pytest.raises(ValueError):
        local_models.piper_voice_specs("not-a-piper-name-at-all-x")


# --- probes are readiness, not credentials ------------------------------


def test_local_probes_never_ask_for_a_key():
    from security.probe import LOCAL_VOICE_PROVIDERS, voice_provider_catalogue

    catalogue = {e["id"]: e for e in voice_provider_catalogue()}
    for pid in LOCAL_VOICE_PROVIDERS:
        assert pid in catalogue, f"{pid} missing from the catalogue"
        assert catalogue[pid]["local"] is True
    assert catalogue["deepgram"]["local"] is False


@pytest.mark.asyncio
async def test_local_probe_reports_missing_weights_as_not_configured(
    tmp_path, monkeypatch,
):
    """Not as a rejected credential.

    The wizard's ``_probe_indicates_bad_key`` treats "not_configured"
    as informational, which is what "the operator has not downloaded
    this optional engine yet" is. Reporting it as a rejection would
    make the wizard offer to replace an API key that does not exist.
    """
    from security import probe as probe_mod

    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))
    probe_mod.clear_probe_cache()

    result = await probe_mod.probe("silero_vad", force=True)
    assert result.ok is False
    assert result.reason == "not_configured"
    assert "weights not downloaded" in result.detail
    assert result.status_code is None


@pytest.mark.asyncio
@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS only")
async def test_macos_say_probes_ready_without_any_download():
    from security import probe as probe_mod

    probe_mod.clear_probe_cache()
    result = await probe_mod.probe("macos_say", force=True)
    assert result.ok is True, result.detail


# --- macOS say ----------------------------------------------------------


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS only")
@pytest.mark.asyncio
async def test_macos_say_produces_headerless_pcm16_at_the_requested_rate():
    """The pipeline's ``pcm16`` wire format is raw samples.

    Leaving the 44-byte RIFF header on the front of the first frame
    would decode as a burst of noise at the start of every reply.
    """
    from voice.tts_providers.macos_say import MacOSSayTTSProvider

    provider = MacOSSayTTSProvider(api_key="", sample_rate=24000)
    assert provider.output_format == "pcm"
    assert provider.is_local is True

    chunks = [chunk async for chunk in provider.synthesize("Testing one two three.")]
    assert chunks, "say produced no audio"
    audio = b"".join(chunks)
    assert not audio.startswith(b"RIFF"), "WAV header leaked into the PCM stream"
    assert len(audio) % 2 == 0
    # Roughly a second of speech at 24kHz mono PCM16 is ~48000 bytes;
    # anything under a tenth of that means the file was empty.
    assert len(audio) > 4800
    await provider.close()


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS only")
def test_macos_say_declares_a_chunk_floor():
    """Measured: ~1.07s fixed cost per invocation, ~2ms per word.

    Below that floor a chunk takes longer to synthesise than its audio
    takes to play and the speaker underruns mid-reply, so the provider
    tells the pipeline to accumulate more text before asking.
    """
    from voice.tts_providers.macos_say import MacOSSayTTSProvider

    provider = MacOSSayTTSProvider(api_key="")
    assert provider.min_chunk_chars >= 60


@pytest.mark.skipif(platform.system() == "Darwin", reason="non-macOS only")
def test_macos_say_refuses_off_darwin():
    from voice.tts_providers.macos_say import MacOSSayTTSProvider

    with pytest.raises(RuntimeError):
        MacOSSayTTSProvider(api_key="")


# --- piper licensing ----------------------------------------------------


def test_piper_is_behind_its_own_extra_and_never_a_transitive_dependency():
    """GPL-3.0-or-later in an Apache-2.0 project must be opt-in.

    Nothing may pull piper-tts implicitly, so it must not appear in the
    base dependency list or in any extra that is not about Piper.
    """
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text())
    project = data["project"]

    assert not any("piper" in dep for dep in project["dependencies"]), (
        "piper-tts must never be a base dependency"
    )
    carrying = {
        name for name, deps in project["optional-dependencies"].items()
        if any("piper" in dep for dep in deps)
    }
    assert carrying == {"tts", "tts-piper"}, carrying
    # The fully-local convenience extra must not drag GPL code in.
    assert not any(
        "piper" in dep for dep in project["optional-dependencies"]["voice-local"]
    )
