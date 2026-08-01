"""What running the local voice engines for real turned up.

Every provider in ``voice/stt_providers`` and ``voice/tts_providers``
that ends in "local" was written against documentation and shipped
without ever executing. Each of the assertions here corresponds to a
defect that only appeared once real audio went through the real class:

* whisper.cpp printed its whole ggml banner to stderr on every boot,
  because ``redirect_whispercpp_logs_to=False`` means "do not redirect"
  in pywhispercpp, not "suppress".
* Both Whisper providers could lose the first word of an utterance,
  because Whisper wants leading silence and VAD endpointing removes
  exactly that. Intermittent rather than universal, which is worse:
  short commands came back clean and longer ones did not.
* faster-whisper downloaded its weights from HuggingFace *inside the
  voice turn*, because ``WhisperModel(size, download_root=X)`` treats
  ``download_root`` as a cache dir and fetches on construction.
* faster-whisper had no fetch path at all, so choosing it in setup
  produced an install that refused every session.
* Piper's "real synthesis probe" only loaded the model and never
  touched espeak, so it reported ready on the one platform where the
  first synthesis kills the process.

The tests that need weights on disk are marked and skip cleanly. CI has
no models.
"""

from __future__ import annotations

import platform
import subprocess
import sys
import types

import pytest

from voice import local_models


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_feral_home(tmp_path, monkeypatch):
    """Never touch the operator's real ``~/.feral``."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / "home"))


def _fake_pywhispercpp(monkeypatch, recorder):
    """Install a stub ``pywhispercpp.model`` that records its kwargs.

    Injected rather than skipped so the wiring is pinned on CI, where
    the real optional dependency is not installed.
    """
    module = types.ModuleType("pywhispercpp.model")

    class _Model:
        def __init__(self, **kwargs):
            recorder.update(kwargs)

        def transcribe(self, samples, **params):
            recorder["samples"] = samples
            recorder["params"] = params
            return []

    module.Model = _Model
    parent = types.ModuleType("pywhispercpp")
    parent.model = module
    monkeypatch.setitem(sys.modules, "pywhispercpp", parent)
    monkeypatch.setitem(sys.modules, "pywhispercpp.model", module)
    return module


def _make_whispercpp_provider(monkeypatch):
    """Construct the provider with the availability gate satisfied."""
    from voice.stt_providers import whispercpp as mod

    monkeypatch.setattr(mod, "whispercpp_available", lambda _m=None: (True, "ready"))
    return mod.WhisperCppSTTProvider(model="tiny.en", sample_rate=16000)


# ----------------------------------------------------------------------
# whisper.cpp: the stderr banner
# ----------------------------------------------------------------------


def test_whispercpp_suppresses_the_ggml_banner_with_none_not_false(monkeypatch):
    """``False`` means "do not redirect". Only ``None`` reaches /dev/null.

    Before this fix the provider passed False, so every brain boot
    logged roughly thirty lines of ``whisper_model_load`` and
    ``ggml_metal_init`` output with no attribution, which is exactly
    what the comment beside the argument claimed it prevented.
    """
    recorded: dict = {}
    _fake_pywhispercpp(monkeypatch, recorded)
    provider = _make_whispercpp_provider(monkeypatch)

    provider._load_model_blocking()

    assert "redirect_whispercpp_logs_to" in recorded
    assert recorded["redirect_whispercpp_logs_to"] is None, (
        "False tells pywhispercpp not to redirect at all; the banner "
        "goes straight to stderr"
    )
    assert recorded["print_progress"] is False
    assert recorded["print_realtime"] is False


# ----------------------------------------------------------------------
# both Whisper providers: the lost first word
# ----------------------------------------------------------------------


def test_whispercpp_prepends_silence_so_the_first_word_survives(monkeypatch):
    """Whisper eats word one when audio starts at sample zero.

    Reproduced on real audio on both the native-16kHz and the resampled
    path: "The quick brown fox ..." came back as "quick brown fox ..."
    until 100ms of leading silence was prepended.
    """
    import numpy as np

    from voice.stt_providers import whispercpp as mod

    provider = _make_whispercpp_provider(monkeypatch)
    speech = np.ones(mod.WHISPER_SAMPLE_RATE, dtype=np.float32)

    padded = provider._pad_lead(speech)

    expected = int(mod.WHISPER_SAMPLE_RATE * mod.LEAD_PAD_MS / 1000)
    assert padded.size == speech.size + expected
    assert float(np.abs(padded[:expected]).max()) == 0.0
    assert np.array_equal(padded[expected:], speech)


def test_faster_whisper_prepends_the_same_silence(monkeypatch):
    import numpy as np

    from voice.stt_providers import faster_whisper_local as mod

    monkeypatch.setattr(
        mod, "faster_whisper_available", lambda _m=None: (True, "ready")
    )
    provider = mod.FasterWhisperSTTProvider(model="tiny.en", sample_rate=16000)
    speech = np.ones(mod.WHISPER_SAMPLE_RATE, dtype=np.float32)

    padded = provider._pad_lead(speech)

    expected = int(mod.WHISPER_SAMPLE_RATE * mod.LEAD_PAD_MS / 1000)
    assert padded.size == speech.size + expected
    assert float(np.abs(padded[:expected]).max()) == 0.0


@pytest.mark.asyncio
async def test_lead_pad_does_not_rescue_a_click_from_the_length_check(monkeypatch):
    """Order matters: check length, then pad.

    Padding first would lift a 20ms click over the 50ms floor and spend
    a whole inference producing a hallucinated sentence out of nothing.
    """
    recorded: dict = {}
    _fake_pywhispercpp(monkeypatch, recorded)
    provider = _make_whispercpp_provider(monkeypatch)

    # 20ms of PCM16 at 16kHz, well under the 50ms floor.
    await provider.send_audio(b"\x00\x01" * (16000 // 50))
    await provider.flush()

    assert "samples" not in recorded, "a sub-50ms click must not be decoded"
    assert provider._result_queue.empty()


# ----------------------------------------------------------------------
# faster-whisper: the mid-session download
# ----------------------------------------------------------------------


def test_faster_whisper_never_downloads_at_model_load(monkeypatch):
    """``download_root`` is a *cache dir*, so it fetches on construction.

    That fetch lands inside a voice turn in chained mode, which is the
    exact behaviour ``voice/local_models.py`` exists to forbid. The
    provider must name a directory and pass ``local_files_only``.
    """
    recorded: dict = {}

    class _WhisperModel:
        def __init__(self, model_path, **kwargs):
            recorded["path"] = model_path
            recorded.update(kwargs)

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = _WhisperModel
    module.download_model = lambda *a, **k: pytest.fail(
        "loading a model must never trigger a download"
    )
    monkeypatch.setitem(sys.modules, "faster_whisper", module)

    from voice.stt_providers import faster_whisper_local as mod

    monkeypatch.setattr(
        mod, "faster_whisper_available", lambda _m=None: (True, "ready")
    )
    provider = mod.FasterWhisperSTTProvider(model="tiny.en")
    provider._load_model_blocking()

    assert recorded.get("local_files_only") is True, (
        "without this the constructor reaches HuggingFace mid-turn"
    )
    assert "download_root" not in recorded
    assert recorded["path"] == str(local_models.faster_whisper_model_path("tiny.en"))


def test_ensure_faster_whisper_model_refuses_without_permission():
    """The fetch path that did not exist at all.

    Before this, nothing anywhere passed ``allow_download=True`` for
    faster-whisper and there was no function to pass it to, so a fresh
    install that picked it in setup refused every single session.
    """
    with pytest.raises(local_models.ModelUnavailable) as excinfo:
        local_models.ensure_faster_whisper_model("tiny.en")

    remedy = excinfo.value.remedy
    assert "feral setup" in remedy
    assert "fetch-faster-whisper tiny.en" in remedy, (
        "the refusal has to carry a command that actually works"
    )


def test_faster_whisper_presence_matches_where_the_fetch_puts_it(tmp_path):
    """Presence check and download destination must agree.

    ``download_model(output_dir=X)`` maps onto ``local_dir=X``, so the
    files land flat in ``<store>/<model>/``. A presence check looking
    anywhere else reports "not downloaded" forever.
    """
    assert local_models.faster_whisper_model_present("tiny.en") is False

    dest = local_models.faster_whisper_model_dir("tiny.en")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "model.bin").write_bytes(b"x" * 4096)

    assert local_models.faster_whisper_model_present("tiny.en") is True
    assert local_models.faster_whisper_model_path("tiny.en") == dest


def test_local_models_cli_exposes_both_whisper_fetches(capsys):
    monkeypatch_argv = ["voice.local_models", "--help"]
    original = sys.argv
    sys.argv = monkeypatch_argv
    try:
        local_models._cli()
    finally:
        sys.argv = original
    usage = capsys.readouterr().out
    assert "fetch-faster-whisper" in usage
    assert "fetch-whispercpp" in usage


# ----------------------------------------------------------------------
# Piper: the probe that could not fail safely
# ----------------------------------------------------------------------


def test_piper_probe_runs_out_of_process(monkeypatch, tmp_path):
    """It has to be a subprocess, and this pins that it is one.

    On macOS arm64 with piper-tts 1.5.0 or 1.6.0, espeak-ng calls
    ``exit(1)`` from native code on the first synthesis. That is not an
    exception; ``try``/``except Exception`` cannot contain it, and an
    in-process probe kills ``feral setup`` outright.
    """
    from voice.tts_providers import piper as mod

    mod.clear_piper_probe_cache()
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "PIPER_PROBE_OK 4096\n", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    ok, reason = mod._probe_synthesis_out_of_process("en_US-lessac-medium")

    assert ok is True and reason == "ready"
    assert calls, "the probe must spawn a child process"
    assert calls[0][0] == sys.executable
    assert "PiperVoice.load" in calls[0][2]
    assert "synthesize" in calls[0][2], "loading alone never touches espeak"


def test_piper_probe_reports_a_native_process_exit_as_unavailable(monkeypatch):
    """The child dying is the answer, not a crash of the parent."""
    from voice.tts_providers import piper as mod

    mod.clear_piper_probe_cache()

    def _fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, "",
            "Error processing file '/Users/runner/work/piper1-gpl/"
            "piper1-gpl/_skbuild/.../espeak-ng-data/phontab': "
            "No such file or directory.\n",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    ok, reason = mod._probe_synthesis_out_of_process("en_US-lessac-medium")

    assert ok is False
    assert "cannot synthesise here" in reason
    assert "phontab" in reason


def test_piper_probe_result_is_cached(monkeypatch):
    """``PiperTTSProvider.__init__`` probes on every session open.

    A process spawn plus a cold ONNX load per conversation is not
    acceptable, and the answer cannot change while the process lives.
    """
    from voice.tts_providers import piper as mod

    mod.clear_piper_probe_cache()
    runs = []

    def _fake_run(argv, **kwargs):
        runs.append(argv)
        return subprocess.CompletedProcess(argv, 0, "PIPER_PROBE_OK 1\n", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    for _ in range(4):
        mod._probe_synthesis_out_of_process("en_US-lessac-medium")

    assert len(runs) == 1


def test_piper_extras_pin_below_the_broken_macos_wheels():
    """piper-tts 1.5.0 and 1.6.0 abort on macOS arm64.

    Measured, not assumed: the espeak data path is linked into
    ``espeakbridge.so`` from the CI build machine and is overridden by
    nothing (the ``espeak_data_dir=`` kwarg, a direct
    ``EspeakPhonemizer(path)``, a bare ``espeakbridge.initialize(path)``,
    and ``ESPEAK_DATA_PATH`` / ``ESPEAK_DATA_DIR`` /
    ``PIPER_ESPEAK_DATA`` were all tried). 1.4.2 works end to end.
    Linux wheels are unaffected and keep the full range.
    """
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text())
    extras = data["project"]["optional-dependencies"]

    for name in ("tts-piper", "tts"):
        pins = extras[name]
        darwin = [p for p in pins if "darwin" in p and "!=" not in p]
        assert darwin, f"{name} must pin piper-tts on darwin"
        assert all("<1.5" in p for p in darwin), (
            f"{name} allows a macOS piper-tts wheel that aborts the process"
        )
        other = [p for p in pins if "!= 'darwin'" in p]
        assert other and all("<2.0" in p for p in other), (
            f"{name} must not restrict Linux, where the wheels are fine"
        )


# ----------------------------------------------------------------------
# real engines, real audio. Skipped without weights on disk.
# ----------------------------------------------------------------------


def _existing_whispercpp_model_dir(model: str = "tiny.en"):
    """A directory already holding ``ggml-<model>.bin``, or None.

    Resolved once at import time and deliberately *not* through the
    autouse ``FERAL_HOME`` fixture, which points at a fresh tmpdir that
    is empty by definition. This asks the different question of whether
    this machine has the weights anywhere at all: in the operator's
    ambient FERAL store, or in pywhispercpp's own platform cache.

    Returning the directory rather than a bool means the test can point
    the store straight at it and is therefore incapable of downloading.
    """
    import os
    from pathlib import Path

    try:
        import pywhispercpp  # noqa: F401
    except Exception:
        return None

    filename = local_models.whispercpp_model_filename(model)
    candidates = []
    ambient = os.environ.get("FERAL_HOME")
    if ambient:
        candidates.append(Path(ambient) / "models" / local_models.WHISPERCPP_FAMILY)
    cached = local_models._pywhispercpp_cached_path(model)
    if cached is not None:
        candidates.append(cached.parent)
    for directory in candidates:
        if (directory / filename).is_file():
            return directory
    return None


WHISPERCPP_MODEL_DIR = _existing_whispercpp_model_dir()

requires_say = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="test audio is generated with macOS `say`",
)
requires_whispercpp_model = pytest.mark.skipif(
    WHISPERCPP_MODEL_DIR is None,
    reason="no whisper.cpp tiny.en weights on this machine (CI has none)",
)


@requires_say
@requires_whispercpp_model
@pytest.mark.asyncio
async def test_whispercpp_transcribes_real_speech_end_to_end(tmp_path, monkeypatch):
    """The run that had never happened.

    Generates speech with macOS ``say``, pushes PCM16 through
    ``send_audio``/``flush`` at the session's 24kHz (so the resampler
    is in the path too) and asserts the transcript.

    The sentence is not arbitrary. First-word loss is intermittent, not
    universal: short utterances survive without a lead pad, and this
    two-sentence one reproducibly does not. Measured on tiny.en with
    ``say -v Samantha``, unpadded, it comes back as "quick-brown fox
    jumps over ..." and padded it comes back whole. Swapping in a
    shorter sentence would make this test pass against the unfixed
    provider and prove nothing.
    """
    import wave

    from voice.stt_providers.whispercpp import WhisperCppSTTProvider

    sentence = (
        "The quick brown fox jumps over the lazy dog. "
        "Turn on the kitchen lights at seven thirty."
    )
    wav = tmp_path / "probe.wav"
    subprocess.run(
        ["say", "-v", "Samantha", "-o", str(wav),
         "--data-format=LEI16@24000", sentence],
        check=True, timeout=120,
    )
    with wave.open(str(wav)) as handle:
        pcm = handle.readframes(handle.getnframes())
        assert handle.getframerate() == 24000

    # Point the store at the weights this machine already has, so the
    # test is structurally incapable of downloading.
    monkeypatch.setattr(
        local_models, "whispercpp_model_dir", lambda: WHISPERCPP_MODEL_DIR
    )
    monkeypatch.setattr(
        local_models, "whispercpp_model_present", lambda _m: True
    )

    provider = WhisperCppSTTProvider(model="tiny.en", sample_rate=24000)
    fragments = []

    await provider.send_audio(pcm)
    await provider.flush()
    while not provider._result_queue.empty():
        fragments.append(provider._result_queue.get_nowait())
    await provider.close()

    assert fragments, "real speech produced no transcript"
    text = fragments[0].text.lower()
    assert fragments[0].is_final and fragments[0].speech_final
    assert text.startswith("the"), (
        f"the leading word was dropped: {fragments[0].text!r}"
    )
    for word in ("quick", "brown", "fox", "lazy", "dog", "kitchen"):
        assert word in text, f"{word!r} missing from {fragments[0].text!r}"
