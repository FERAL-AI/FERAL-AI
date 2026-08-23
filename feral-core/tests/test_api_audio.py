"""Contract tests for /api/audio/providers + /api/audio/config routes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def client():
    mock_config = MagicMock()
    store: dict = {"audio": {}}

    def _get(section, key, default=None):
        return store.get(section, {}).get(key, default)

    def _update(section, key, value):
        store.setdefault(section, {})[key] = value

    mock_config.get.side_effect = _get
    mock_config.update_settings.side_effect = _update

    mock = MagicMock()
    mock.config = mock_config

    with patch("api.state.state", mock), patch("api.routes.audio.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), mock_config, store


def test_list_providers_returns_stt_and_tts(client):
    c, _, _ = client
    r = c.get("/api/audio/providers")
    assert r.status_code == 200
    body = r.json()
    stt_ids = {p["id"] for p in body["stt"]}
    tts_ids = {p["id"] for p in body["tts"]}
    assert {"openai", "openai-compatible", "faster-whisper"} <= stt_ids
    assert {"openai", "piper"} <= tts_ids


def test_list_providers_marks_local_vs_cloud(client):
    c, _, _ = client
    body = c.get("/api/audio/providers").json()
    local_stt = next(p for p in body["stt"] if p["id"] == "faster-whisper")
    cloud_stt = next(p for p in body["stt"] if p["id"] == "openai")
    assert local_stt["is_local"] is True
    assert local_stt["needs_api_key"] is False
    assert cloud_stt["needs_api_key"] is True
    assert cloud_stt["credential_env_var"] == "OPENAI_API_KEY"
    compatible = next(
        p for p in body["stt"] if p["id"] == "openai-compatible"
    )
    assert compatible["needs_api_key"] is False
    assert compatible["needs_endpoint"] is True
    assert compatible["endpoint_env_var"] == "FERAL_STT_ENDPOINT"


def test_list_stt_models(client):
    c, _, _ = client
    r = c.get("/api/audio/providers/stt/faster-whisper/models")
    assert r.status_code == 200
    assert "base" in r.json()["models"]


def test_list_tts_voices(client):
    c, _, _ = client
    r = c.get("/api/audio/providers/openai/voices")
    assert r.status_code == 200
    body = r.json()
    assert "nova" in body["voices"]


def test_unknown_provider_404(client):
    c, _, _ = client
    assert c.get("/api/audio/providers/stt/not-real/models").status_code == 404
    assert c.get("/api/audio/providers/not-real/voices").status_code == 404


def test_get_config_defaults(client):
    c, cfg, _ = client
    body = c.get("/api/audio/config").json()
    assert body["stt_provider"] == "openai"
    assert body["tts_voice"] == "nova"


def test_set_config_persists_each_field(client):
    c, _, store = client
    r = c.post(
        "/api/audio/config",
        json={
            "stt_provider": "faster-whisper",
            "stt_model": "small",
            "tts_provider": "piper",
            "tts_voice": "en_US-lessac-medium",
        },
    )
    assert r.status_code == 200
    assert store["audio"]["stt_provider"] == "faster-whisper"
    assert store["audio"]["tts_voice"] == "en_US-lessac-medium"


def test_set_private_stt_endpoint_and_timeout(client):
    c, _, store = client
    endpoint = "http://speech.internal/v1/audio/transcriptions"
    r = c.post(
        "/api/audio/config",
        json={
            "stt_provider": "openai-compatible",
            "stt_model": "whisper-v3:turbo",
            "stt_endpoint": endpoint,
            "stt_timeout_seconds": 900,
        },
    )
    assert r.status_code == 200
    assert r.json()["stt_endpoint"] == endpoint
    assert r.json()["stt_timeout_seconds"] == 900
    assert store["audio"]["stt_provider"] == "openai-compatible"


def test_private_stt_timeout_is_bounded(client):
    c, _, _ = client
    r = c.post("/api/audio/config", json={"stt_timeout_seconds": 3601})
    assert r.status_code == 422


def test_private_stt_endpoint_rejects_embedded_credentials(client):
    c, _, _ = client
    r = c.post(
        "/api/audio/config",
        json={"stt_endpoint": "http://user:secret@speech.internal/v1/audio/transcriptions"},
    )
    assert r.status_code == 400


def test_set_config_rejects_unknown_stt_provider(client):
    c, _, _ = client
    r = c.post("/api/audio/config", json={"stt_provider": "google-cloud-stt"})
    assert r.status_code == 400


def test_set_config_rejects_unknown_tts_provider(client):
    c, _, _ = client
    r = c.post("/api/audio/config", json={"tts_provider": "not-real-tts"})
    assert r.status_code == 400
