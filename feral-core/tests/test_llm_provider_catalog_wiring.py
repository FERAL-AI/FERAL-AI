"""Runtime wiring tests — settings.json → env → LLMProvider / AudioPipeline.

The prior behavior was:
  * ``settings.json.audio.*`` was silently ignored because
    ``ConfigLoader.export_as_env`` didn't propagate it.
  * ``settings.json.llm.fallback_providers`` existed on disk but
    ``LLMProvider.set_config`` was never called.
  * ``_is_first_run()`` used credentials.json only, so an Ollama-only
    user hit the setup wizard on every ``feral start``.

These tests lock in the fixed contracts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.loader import ConfigLoader


pytestmark = pytest.mark.no_auto_feral_home


def _write_settings(home: Path, data: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(data))


def _clear_audio_env(monkeypatch):
    # Strip any leaked env from earlier tests in the same pytest run
    # so we exercise the settings.json → env path cleanly.
    for k in (
        "FERAL_STT_PROVIDER", "FERAL_STT_MODEL",
        "FERAL_TTS_PROVIDER", "FERAL_TTS_MODEL", "FERAL_TTS_VOICE",
    ):
        monkeypatch.delenv(k, raising=False)


class TestAudioExport:
    def test_audio_stt_provider_exported(self, tmp_path, monkeypatch):
        _clear_audio_env(monkeypatch)
        monkeypatch.setenv("FERAL_HOME", str(tmp_path / ".feral"))
        _write_settings(tmp_path / ".feral", {
            "audio": {
                "stt_provider": "faster-whisper",
                "stt_model": "small",
                "tts_provider": "piper",
                "tts_model": "piper",
                "tts_voice": "en_US-amy-low",
            },
        })
        cfg = ConfigLoader()
        cfg.discover()
        env = cfg.export_as_env()
        assert env.get("FERAL_STT_PROVIDER") == "faster-whisper"
        assert env.get("FERAL_STT_MODEL") == "small"
        assert env.get("FERAL_TTS_PROVIDER") == "piper"
        assert env.get("FERAL_TTS_VOICE") == "en_US-amy-low"

    def test_audio_defaults_do_not_clobber_missing_keys(self, tmp_path, monkeypatch):
        _clear_audio_env(monkeypatch)
        monkeypatch.setenv("FERAL_HOME", str(tmp_path / ".feral"))
        # Settings tree explicitly unsets the model keys so we can test
        # that export_as_env doesn't fabricate them.
        _write_settings(tmp_path / ".feral", {
            "audio": {
                "stt_provider": "openai",
                "stt_model": "",
                "tts_model": "",
                "tts_voice": "",
            },
        })
        cfg = ConfigLoader()
        cfg.discover()
        env = cfg.export_as_env()
        assert env.get("FERAL_STT_PROVIDER") == "openai"
        assert "FERAL_STT_MODEL" not in env
        assert "FERAL_TTS_MODEL" not in env

    def test_env_override_propagates_into_settings(self, tmp_path, monkeypatch):
        _clear_audio_env(monkeypatch)
        monkeypatch.setenv("FERAL_HOME", str(tmp_path / ".feral"))
        _write_settings(tmp_path / ".feral", {})
        monkeypatch.setenv("FERAL_STT_MODEL", "medium")
        monkeypatch.setenv("FERAL_TTS_MODEL", "tts-1-hd")
        cfg = ConfigLoader()
        cfg.discover()
        # Env overrides should feed back into the merged settings tree.
        assert cfg.get("audio", "stt_model") == "medium"
        assert cfg.get("audio", "tts_model") == "tts-1-hd"


class TestFirstRunDetection:
    def test_setup_complete_true_skips_wizard(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path / ".feral"))
        _write_settings(tmp_path / ".feral", {"meta": {"setup_complete": True}})
        from cli.main import _is_first_run
        assert _is_first_run() is False

    def test_ollama_only_setup_not_first_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path / ".feral"))
        _write_settings(tmp_path / ".feral", {
            "llm": {"provider": "ollama", "model": "llama3.3"},
        })
        # Clear any env keys that would auto-skip for other reasons.
        for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        from cli.main import _is_first_run
        assert _is_first_run() is False

    def test_lmstudio_only_setup_not_first_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path / ".feral"))
        _write_settings(tmp_path / ".feral", {
            "llm": {"provider": "lmstudio", "model": "local-model"},
        })
        for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        from cli.main import _is_first_run
        assert _is_first_run() is False

    def test_fresh_install_is_first_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path / ".feral"))
        # No settings.json, no creds, no env keys.
        for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        from cli.main import _is_first_run
        assert _is_first_run() is True

    def test_env_api_key_skips_wizard(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FERAL_HOME", str(tmp_path / ".feral"))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        from cli.main import _is_first_run
        assert _is_first_run() is False


class TestLLMProviderSetConfig:
    # These two used to call importlib.reload(agents.llm_provider) "so env
    # reading doesn't bleed across tests". Neither assertion reads env, and
    # the reload was not free: reload re-executes a module in place, so the
    # module object survives but every class it defines is rebuilt. Any test
    # module that did `from agents.llm_provider import LLMProvider` at import
    # time -- which is collection time, before any test runs -- kept the old
    # class, while production code importing inside a function got the new
    # one. monkeypatch.setattr(LLMProvider, ...) then patched a class nothing
    # would use.
    #
    # That is not theoretical. It is why four tests in
    # test_agentic_cu_vlm_init.py failed only in full runs: the fake
    # switch_provider was installed on the stale class, the real one ran, and
    # the suite made a genuine call to OpenAI that came back 401.

    def test_set_config_stores_fallback_providers(self):
        from agents.llm_provider import LLMProvider

        llm = LLMProvider()
        llm.set_config({"fallback_providers": ["groq", "deepseek"]})
        assert llm._config.get("fallback_providers") == ["groq", "deepseek"]

    def test_set_catalog_is_stored(self):
        from agents.llm_provider import LLMProvider

        llm = LLMProvider()
        sentinel = object()
        llm.set_catalog(sentinel)
        assert llm._catalog is sentinel
