"""Lane 05  — /api/voice/providers REST surface (THESIS_SCENARIOS S4).

Pins the contract Lane 11 (iOS Settings → Voice picker) and Lane 12
(WebUI Settings → Voice panel) consume:

  * GET  /api/voice/providers — flat list of every realtime/STT/TTS
    provider with ``configured`` (probe-derived) and ``probe_status``.
  * POST /api/voice/providers/probe — force-refresh one or all probes.
"""

from __future__ import annotations

import re

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


def test_voice_providers_lists_cloud_and_local_with_kinds(client):
    r = client.get("/api/voice/providers")
    assert r.status_code == 200
    body = r.json()
    rows = body["providers"]

    ids = {p["id"] for p in rows}
    # The eight cloud vendors, unchanged.
    assert {
        "openai_realtime",
        "gemini_live",
        "deepgram",
        "groq_whisper",
        "openai_whisper",
        "elevenlabs",
        "cartesia",
        "openai_tts",
    } <= ids
    # Plus the local engines, which are selectable providers in their
    # own right rather than a mode of the cloud ones.
    assert {"whispercpp", "faster_whisper", "macos_say", "piper"} <= ids
    kinds = {p["kind"] for p in rows}
    assert kinds == {"realtime", "stt", "tts", "vad"}

    # Every row says whether readiness means "credential accepted" or
    # "installed and downloaded", so a UI never offers an API-key
    # prompt for an engine that has no account.
    by_id = {p["id"]: p for p in rows}
    assert by_id["deepgram"]["local"] is False
    assert by_id["whispercpp"]["local"] is True
    assert by_id["macos_say"]["local"] is True


def test_voice_providers_marks_unconfigured_when_no_keys(client):
    body = client.get("/api/voice/providers").json()
    # Scoped to the cloud rows on purpose. A local engine has no
    # credential to be missing, so "no_key" is not a verdict that can
    # apply to it: its probe answers a different question (is the code
    # importable and are the weights on disk) and reports
    # "not_configured" when the answer is no. macOS `say` needs
    # neither, so on a Mac it is legitimately ready with no key at all.
    cloud = [row for row in body["providers"] if not row["local"]]
    assert cloud
    for row in cloud:
        assert row["configured"] is False
        assert row["probe_status"] == "no_key"


def test_local_rows_are_never_reported_as_missing_a_key(client):
    body = client.get("/api/voice/providers").json()
    local = [row for row in body["providers"] if row["local"]]
    assert local
    for row in local:
        assert row["probe_status"] != "no_key", row
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
    cloud = [row for row in rows if not row.get("local")]
    assert len(cloud) == 8
    assert all(row["reason"] == "no_key" for row in cloud)


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


_DATED = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def test_openai_realtime_models_list_has_full_set(client):
    """v2026.5.43 Nit-1 — the OpenAI Realtime entry surfaces every
    realtime model the API will accept, not just the GA default.
    Operators on legacy mini quotas, dated snapshot pins, or the
    preview family all need an in-list option."""
    rows = client.get("/api/voice/providers").json()["providers"]
    row = next(p for p in rows if p["id"] == "openai_realtime")
    models = row["models"]
    assert "gpt-realtime" in models
    assert "gpt-realtime-mini" in models
    # This used to require a "preview" id, on the reasoning that
    # operators pinned to the preview family needed an in-list option.
    # OpenAI retired gpt-4o-realtime-preview and its siblings from the
    # models endpoint, and the 2026-09-04 catalog refresh removed them
    # from the bundled list, so requiring one now demands that the picker
    # offer a model the API will refuse. The intent behind the assertion
    # was "more than just the GA default", and that is what is checked:
    # a dated snapshot for anyone pinning one, and the mini line for
    # anyone on those quotas. voice/router.py already rewrites a pinned
    # gpt-4o-realtime* to the GA id with a warning, so nobody stranded on
    # the old name is stuck.
    assert any(_DATED.search(m) for m in models), models
    assert any(m.startswith("gpt-realtime-mini") for m in models), models
    assert row.get("default_model") == "gpt-realtime"
    # was 1 in v2026.5.42; v2026.5.43 ships the curated multi-entry list.
    assert len(models) >= 5
    # GA leads so the dropdown's first option matches the runtime default.
    assert models[0] == "gpt-realtime"


def test_non_realtime_entries_omit_models(client):
    """Pin: catalogue entries without a curated model list MUST NOT
    invent one. The route + clients treat absence as "use the runtime
    default — no picker"."""
    rows = client.get("/api/voice/providers").json()["providers"]
    for row in rows:
        if row["id"] == "openai_realtime":
            continue
        assert "models" not in row, row
