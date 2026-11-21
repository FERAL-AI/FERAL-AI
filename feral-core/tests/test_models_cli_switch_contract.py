"""Model selection preserves same-provider overrides without cross-provider leaks."""

from __future__ import annotations

import json

import pytest

from cli.model_commands import cmd_models_set
from config.loader import ConfigLoader


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.setenv("FERAL_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")


def save(tmp_path, provider="openai"):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "llm": {"provider": provider, "model": "previous-model",
                "base_url": "https://fixture.invalid/v1",
                "fallback_providers": ["openai", "anthropic"],
                "models": ["previous-model"]},
        "unrelated": {"preserved": True},
    }))
    return path


def test_switch_to_local_resets_endpoint_and_fallbacks(tmp_path, capsys, monkeypatch):
    path = save(tmp_path)
    assert cmd_models_set(provider="ollama", model="fixture-local:latest") == 0
    result = json.loads(path.read_text())
    assert result["llm"]["base_url"] == "http://localhost:11434/v1"
    assert result["llm"]["fallback_providers"] == []
    assert result["llm"]["models"] == ["previous-model", "fixture-local:latest"]
    assert result["unrelated"] == {"preserved": True}
    output = capsys.readouterr().out
    assert "fallback" in output.lower() and "Restart" in output
    # A restarted reader has no previous brain's exported provider overrides.
    for name in ("FERAL_LLM_PROVIDER", "FERAL_LLM_MODEL", "FERAL_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    loader = ConfigLoader(project_dir=str(tmp_path / "empty-project"))
    loader.discover(load_credentials=False)
    assert loader.get("llm", "provider") == "ollama"
    assert loader.get("llm", "base_url") == "http://localhost:11434/v1"
    assert loader.get("llm", "fallback_providers") == []


def test_same_provider_preserves_explicit_endpoint_and_fallbacks(tmp_path):
    path = save(tmp_path, "ollama")
    assert cmd_models_set(provider="ollama", model="other-local") == 0
    result = json.loads(path.read_text())
    assert result["llm"]["base_url"] == "https://fixture.invalid/v1"
    assert result["llm"]["fallback_providers"] == ["openai", "anthropic"]


def test_alias_of_same_provider_keeps_custom_config(tmp_path):
    path = save(tmp_path)
    assert cmd_models_set(provider="openai api", model="fixture-new") == 0
    result = json.loads(path.read_text())
    assert result["llm"]["provider"] == "openai"
    assert result["llm"]["base_url"] == "https://fixture.invalid/v1"
    assert result["llm"]["fallback_providers"] == ["openai", "anthropic"]


@pytest.mark.parametrize("provider", ["unregistered-provider", "o", "together"])
def test_invalid_or_unsupported_provider_refuses_without_write(tmp_path, provider):
    path = save(tmp_path)
    before = path.read_bytes()
    assert cmd_models_set(provider=provider, model="fixture-model") != 0
    assert path.read_bytes() == before


def test_unknown_provider_does_not_create_settings(tmp_path):
    assert cmd_models_set(provider="unregistered-provider", model="fixture-model") != 0
    assert not (tmp_path / "settings.json").exists()


def test_corrupt_existing_settings_refused_without_write(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("not json")
    assert cmd_models_set(provider="ollama", model="fixture-model") != 0
    assert path.read_text() == "not json"


def test_empty_same_provider_endpoint_uses_runtime_default(tmp_path):
    path = save(tmp_path, "ollama")
    result = json.loads(path.read_text())
    result["llm"]["base_url"] = ""
    path.write_text(json.dumps(result))
    assert cmd_models_set(provider="ollama", model="fixture-model") == 0
    assert json.loads(path.read_text())["llm"]["base_url"] == "http://localhost:11434/v1"


@pytest.mark.parametrize("settings", [[], {"llm": []}, {"llm": {"provider": 42}}])
def test_malformed_settings_refuse_without_write(tmp_path, settings):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings))
    before = path.read_bytes()
    assert cmd_models_set(provider="ollama", model="fixture-model") != 0
    assert path.read_bytes() == before
