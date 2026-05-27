"""Lane 05 W8 — /api/voice/providers REST surface (THESIS_SCENARIOS S4).

Pins the contract Lane 11 (iOS Settings → Voice picker) and Lane 12
(WebUI Settings → Voice panel) consume:

  * GET  /api/voice/providers — flat list of every realtime/STT/TTS
    provider with ``configured`` (probe-derived) and ``probe_status``.
  * POST /api/voice/providers/probe — force-refresh one or all probes.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def client(monkeypatch):
    mock_config = MagicMock()
    mock_config.get.return_value = None
    mock = MagicMock()
    mock.config = mock_config

    # Strip every voice-provider env var so the probes consistently
    # report ``no_key`` in this test.
    for env_key in (
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "DEEPGRAM_API_KEY",
        "GROQ_API_KEY",
        "ELEVENLABS_API_KEY",
        "CARTESIA_API_KEY",
    ):
        monkeypatch.delenv(env_key, raising=False)

    from security.probe import clear_probe_cache
    clear_probe_cache()

    with patch("api.state.state", mock), patch("api.routes.audio.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False)


def test_voice_providers_lists_eight_with_kinds(client):
    r = client.get("/api/voice/providers")
    assert r.status_code == 200
    body = r.json()
    rows = body["providers"]
    assert len(rows) == 8

    ids = {p["id"] for p in rows}
    assert ids == {
        "openai_realtime",
        "gemini_live",
        "deepgram",
        "groq_whisper",
        "openai_whisper",
        "elevenlabs",
        "cartesia",
        "openai_tts",
    }
    kinds = {p["kind"] for p in rows}
    assert kinds == {"realtime", "stt", "tts"}


def test_voice_providers_marks_unconfigured_when_no_keys(client):
    body = client.get("/api/voice/providers").json()
    for row in body["providers"]:
        assert row["configured"] is False
        assert row["probe_status"] == "no_key"
        # latency_ms is reported (0.0 for the no_key short-circuit).
        assert "latency_ms" in row


def test_voice_providers_displays_human_names(client):
    body = client.get("/api/voice/providers").json()
    by_id = {p["id"]: p for p in body["providers"]}
    assert by_id["cartesia"]["name"] == "Cartesia"
    assert by_id["deepgram"]["name"] == "Deepgram (streaming)"
    assert by_id["openai_realtime"]["name"] == "OpenAI Realtime"


def test_probe_specific_provider(client):
    r = client.post(
        "/api/voice/providers/probe",
        json={"provider_id": "deepgram"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["provider_id"] == "deepgram"
    assert body["ok"] is False
    assert body["reason"] == "no_key"


def test_probe_unknown_provider_returns_404(client):
    r = client.post(
        "/api/voice/providers/probe",
        json={"provider_id": "spotify"},  # exists but isn't a voice provider
    )
    assert r.status_code == 404
    assert "valid" in r.json()["detail"]


def test_probe_all_when_provider_id_omitted(client):
    r = client.post("/api/voice/providers/probe", json={})
    assert r.status_code == 200
    rows = r.json()["providers"]
    assert len(rows) == 8
    assert all(row["reason"] == "no_key" for row in rows)


# ----------------------------------------------------------------------
# Lane U2 — realtime model picker contract
# ----------------------------------------------------------------------


def test_openai_realtime_entry_has_models(client):
    """Pin: ``/api/voice/providers`` attaches a ``models[]`` and
    ``default_model`` to the OpenAI Realtime entry so the WebUI Voice
    card + CLI preflight can render an in-list picker instead of the
    LLM free-text fallback (Lane U2)."""
    rows = client.get("/api/voice/providers").json()["providers"]
    row = next(p for p in rows if p["id"] == "openai_realtime")
    assert "models" in row and isinstance(row["models"], list)
    assert len(row["models"]) >= 1
    assert "gpt-realtime" in row["models"]
    assert row.get("default_model") == "gpt-realtime"


def test_non_realtime_entries_omit_models(client):
    """Pin: catalogue entries without a curated model list MUST NOT
    invent one. The route + clients treat absence as "use the runtime
    default — no picker"."""
    rows = client.get("/api/voice/providers").json()["providers"]
    for row in rows:
        if row["id"] == "openai_realtime":
            continue
        assert "models" not in row, row
