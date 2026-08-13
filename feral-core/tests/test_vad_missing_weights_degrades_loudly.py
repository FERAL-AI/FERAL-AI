"""What a fresh install does when the Silero VAD weights are absent.

Established, not assumed. On the audit machine ``~/.feral/models`` does
not exist at all after 137 boots, so this is the branch every install
that has not run ``feral setup`` takes.

The finding is mostly a negative one and that is worth recording:
``load_endpointer`` does not crash and does not pretend. It returns
``None``, the chained pipeline falls back to its packet-absence timer,
and the conversation still works. The only defect was actionability -
the message named the missing file but not the command that produces
it, and the single visible symptom is about 2.3 seconds of extra latency
per voice turn, which nobody attributes to a missing 2.2MB file on their
own.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture()
def empty_model_store(monkeypatch, tmp_path):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))


def test_vad_available_names_the_remedy(empty_model_store):
    pytest.importorskip("onnxruntime")
    from voice.vad import vad_available

    ready, reason = vad_available()
    assert ready is False
    assert "silero_vad.onnx" in reason
    assert "fetch-vad" in reason, (
        "the operator is told what is missing but not how to get it"
    )


def test_load_endpointer_degrades_rather_than_crashing(empty_model_store, caplog):
    from voice.vad import load_endpointer

    with caplog.at_level(logging.INFO, logger="feral.voice.vad"):
        endpointer = load_endpointer(sample_rate=24000)

    assert endpointer is None, "a missing model must not produce a half-live VAD"
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "silence timer" in text, "the degradation was not announced"
    assert "fetch-vad" in text


def test_no_download_is_attempted_at_load_time(empty_model_store, monkeypatch):
    """A voice turn must never be the thing that decides to pull 2MB."""
    from voice import local_models, vad

    def _forbidden(*_a, **_k):
        raise AssertionError("load_endpointer downloaded weights mid-session")

    monkeypatch.setattr(local_models, "download", _forbidden)
    assert vad.load_endpointer(sample_rate=24000) is None
