"""The wake word must be togglable, and must say what it listens for.

Three defects, all verified against the real ``WakeWordDetector``
rather than a double. ``tests/test_ambient_api.py`` covered both routes
with ``ww = MagicMock(); ww.enabled = False; ww.phrase = "hey feral"``.
A MagicMock accepts any attribute assignment and answers any attribute
read, so those tests passed against an object that has neither
behaviour. Trap 3 in CLAUDE.md, for the third time in this audit.

1. ``POST /api/ambient/wake_word/toggle`` does
   ``state.wake_word.enabled = not state.wake_word.enabled``.
   ``WakeWordDetector.enabled`` is a read-only ``@property``, so the
   real call raises ``AttributeError: property 'enabled' of
   'WakeWordDetector' object has no setter`` and the route 500s. The
   wake word could not be turned on from the API at all.

2. ``GET /api/ambient/wake_word/status`` reads
   ``getattr(state.wake_word, "phrase", "hey feral")``.
   ``WakeWordDetector`` stores the phrase on ``_config`` and exposes no
   ``phrase`` attribute, so the getattr default always won: the route
   answered "hey feral" no matter what ``FERAL_WAKE_PHRASE`` said.

3. The phrase FERAL reports is not the phrase it detects.
   ``FERAL_WAKE_MODEL`` defaults to ``hey_jarvis_v0.1``, the openwakeword
   pre-trained model, which fires on "hey jarvis". The detector logged
   ``phrase='hey feral'`` and reported it through ``stats`` and the API
   while listening for a different phrase entirely. A user saying "hey
   feral" was never heard, and nothing anywhere said why.

Plus: with openwakeword absent the detector silently degrades to
``_detect_energy``, whose own docstring calls it "not a true wake word
detector". Measured: 3200 bytes of uniform random noise, no speech of
any kind, activate it with confidence 1.00. That is a loudness gate
opening a microphone to STT, and it must announce itself.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from perception.wake_word import WakeWordConfig, WakeWordDetector


def _noise(n_samples: int = 1600) -> bytes:
    import numpy as np

    rng = np.random.default_rng(1234)
    return rng.integers(-30000, 30000, n_samples).astype("<i2").tobytes()


@pytest.fixture()
def no_openwakeword(monkeypatch):
    """Simulate the default install: ``openwakeword`` is an extra."""
    import builtins

    real_import = builtins.__import__

    def _guarded(name, *args, **kwargs):
        if name.split(".")[0] == "openwakeword":
            raise ImportError("No module named 'openwakeword'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _guarded)


# ---------------------------------------------------------------------------
# 1. The toggle
# ---------------------------------------------------------------------------


def test_enabled_is_settable(no_openwakeword):
    """``POST /api/ambient/wake_word/toggle`` assigns to this."""
    det = WakeWordDetector(WakeWordConfig(enabled=False))
    det.enabled = True
    assert det.enabled is True
    det.enabled = False
    assert det.enabled is False


async def test_toggle_route_flips_the_real_detector(monkeypatch):
    """The route, against the real object rather than a MagicMock."""
    from api.routes import ambient

    det = WakeWordDetector(WakeWordConfig(enabled=False))

    class _S:
        wake_word = det

    monkeypatch.setattr(ambient, "state", _S())
    body = await ambient.toggle_wake_word()
    assert body["enabled"] is True
    assert det.enabled is True


def test_enabling_at_runtime_loads_the_model(monkeypatch):
    """Turning the wake word on has to do more than flip a bool.

    The model is only loaded in ``__init__`` when the detector starts
    enabled. A detector that booted disabled (the default, for privacy)
    and was then switched on would otherwise run the energy fallback
    forever, whatever is installed.
    """
    pytest.importorskip("openwakeword")
    det = WakeWordDetector(WakeWordConfig(enabled=False))
    assert det._oww_model is None
    det.enabled = True
    assert det._oww_model is not None
    assert det.detector == "openwakeword"


# ---------------------------------------------------------------------------
# 2. The reported phrase
# ---------------------------------------------------------------------------


def test_phrase_is_readable_and_reflects_config(no_openwakeword):
    det = WakeWordDetector(WakeWordConfig(enabled=True, phrase="ok computer"))
    assert det.phrase == "ok computer"


async def test_status_route_reports_the_configured_phrase(monkeypatch):
    from api.routes import ambient

    det = WakeWordDetector(WakeWordConfig(enabled=False, phrase="ok computer"))

    class _S:
        wake_word = det

    monkeypatch.setattr(ambient, "state", _S())
    body = await ambient.wake_word_status()
    assert body["phrase"] == "ok computer", (
        "the route fell back to its hardcoded 'hey feral' default"
    )


# ---------------------------------------------------------------------------
# 3. The phrase it actually detects
# ---------------------------------------------------------------------------


def test_effective_phrase_is_the_models_phrase_not_the_configured_one(monkeypatch):
    """openwakeword's ``hey_jarvis_v0.1`` fires on "hey jarvis"."""
    pytest.importorskip("openwakeword")
    monkeypatch.setenv("FERAL_WAKE_MODEL", "hey_jarvis_v0.1")
    det = WakeWordDetector(WakeWordConfig(enabled=True, phrase="hey feral"))

    assert det.detector == "openwakeword"
    assert det.effective_phrase == "hey jarvis"
    assert det.stats["effective_phrase"] == "hey jarvis"
    assert det.stats["phrase_matches_model"] is False


def test_a_phrase_the_model_cannot_detect_is_reported_at_boot(monkeypatch, caplog):
    pytest.importorskip("openwakeword")
    monkeypatch.setenv("FERAL_WAKE_MODEL", "hey_jarvis_v0.1")
    with caplog.at_level(logging.WARNING, logger="feral.wake_word"):
        WakeWordDetector(WakeWordConfig(enabled=True, phrase="hey feral"))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "hey jarvis" in text and "hey feral" in text


async def test_status_route_exposes_the_effective_phrase(monkeypatch):
    from api.routes import ambient

    pytest.importorskip("openwakeword")
    monkeypatch.setenv("FERAL_WAKE_MODEL", "hey_jarvis_v0.1")
    det = WakeWordDetector(WakeWordConfig(enabled=True, phrase="hey feral"))

    class _S:
        wake_word = det

    monkeypatch.setattr(ambient, "state", _S())
    body = await ambient.wake_word_status()
    assert body["effective_phrase"] == "hey jarvis"
    assert body["detector"] == "openwakeword"


# ---------------------------------------------------------------------------
# 4. The energy fallback announces that it is not a wake word detector
# ---------------------------------------------------------------------------


def test_energy_fallback_is_labelled(no_openwakeword, caplog):
    with caplog.at_level(logging.WARNING, logger="feral.wake_word"):
        det = WakeWordDetector(WakeWordConfig(enabled=True, phrase="hey feral"))

    assert det.detector == "energy-fallback"
    assert det.stats["using_ml"] is False
    assert det.effective_phrase == "", (
        "the energy fallback detects no phrase; reporting one is a lie"
    )
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "loud" in text.lower() or "loudness" in text.lower()
    assert "openwakeword" in text


def test_energy_fallback_still_opens_on_loud_non_speech(no_openwakeword):
    """Not a regression test - a record of why the warning above exists.

    The behaviour is deliberately unchanged: removing the fallback would
    remove wake-word support entirely for anyone without the ``[wake]``
    extra. It is now labelled instead of being passed off as a wake
    word detector.
    """
    det = WakeWordDetector(WakeWordConfig(enabled=True, sensitivity=0.5))
    assert asyncio.run(det.process_frame("s", _noise())) is True


def test_disabled_detector_reports_disabled(no_openwakeword):
    det = WakeWordDetector(WakeWordConfig(enabled=False))
    assert det.detector == "disabled"
    assert det.stats["using_ml"] is False


def test_model_download_failure_is_not_swallowed(monkeypatch, caplog):
    """``_try_load_oww`` wrapped the model fetch in ``except Exception:
    pass``. A machine that could not reach the model host dropped to the
    loudness gate with no trace of why."""
    pytest.importorskip("openwakeword")
    import openwakeword.utils

    def _boom(*_a, **_k):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(openwakeword.utils, "download_models", _boom)
    with caplog.at_level(logging.WARNING, logger="feral.wake_word"):
        WakeWordDetector(WakeWordConfig(enabled=True))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "network unreachable" in text
