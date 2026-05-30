"""
Tests for cli.setup_wizard — constants, helpers, Rich/plain wizards, and I/O mocks.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

import cli.setup_wizard as sw
from cli.setup_wizard import (
    TOOL_KEYS,
    PERSONALITY_PRESETS,
    PROVIDERS,
    OnboardWizard,
    OnboardWizardPlain,
    _get_local_ip,
    _looks_like_vision_model,
    run_setup,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def wizard_home(tmp_path, monkeypatch):
    """Point module-level FERAL_HOME at an isolated directory."""
    home = tmp_path / "feral-wizard"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sw, "FERAL_HOME", home)
    return home


@pytest.fixture
def mock_console():
    return MagicMock()


class _FakeHttpxAsyncClient:
    """Minimal async context manager — avoids MagicMock teardown RuntimeWarnings."""

    def __init__(self, inner):
        self._inner = inner

    async def __aenter__(self):
        return self._inner

    async def __aexit__(self, *args):
        return None


def _patch_httpx_async_client(inner_client):
    """Patch httpx.AsyncClient to yield inner_client from async with."""

    def _factory(*_a, **_kw):
        return _FakeHttpxAsyncClient(inner_client)

    return patch("httpx.AsyncClient", _factory)


class _HttpxInnerStub:
    """Minimal async client with `get` for patching httpx.AsyncClient."""

    def __init__(self, response=None, side_effect: BaseException | None = None):
        self.calls: list[tuple] = []
        self._response = response
        self._side_effect = side_effect

    async def get(self, url, headers=None):
        self.calls.append((url, headers))
        if self._side_effect is not None:
            raise self._side_effect
        return self._response


# ── _looks_like_vision_model ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name, expected",
    [
        ("llava:7b", True),
        ("some/Moondream-v1", True),
        ("Qwen2-VL-7B-Instruct", True),
        ("minicpm-v-2.5", True),
        ("bakllava:latest", True),
        ("gemma3:12b", True),
        ("gpt-4.1", False),
        ("claude-sonnet-4", False),
        ("deepseek-chat", False),
        ("", False),
        (None, False),
    ],
)
def test_looks_like_vision_model_variants(name, expected):
    assert _looks_like_vision_model(name) is expected


# ── _get_local_ip ─────────────────────────────────────────────────────────────


def test_get_local_ip_success():
    fake_sock = MagicMock()
    fake_sock.getsockname.return_value = ("192.168.1.42", 12345)

    with patch("cli.setup_wizard.socket.socket", return_value=fake_sock) as mock_socket:
        ip = _get_local_ip()

    assert ip == "192.168.1.42"
    mock_socket.assert_called_once()
    fake_sock.connect.assert_called_once_with(("8.8.8.8", 80))
    fake_sock.close.assert_called_once()


def test_get_local_ip_socket_error_returns_placeholder():
    with patch("cli.setup_wizard.socket.socket", side_effect=OSError("no network")):
        assert _get_local_ip() == "YOUR_IP"


# ── PROVIDERS / TOOL_KEYS / PERSONALITY_PRESETS ───────────────────────────────


EXPECTED_PROVIDER_KEYS = (
    "openai",
    "anthropic",
    "gemini",
    "openrouter",
    "deepseek",
    "kimi",
    "qwen",
    "groq",
    "ollama",
    "lmstudio",
)


def test_providers_has_all_expected_keys_and_required_fields():
    assert set(PROVIDERS.keys()) == set(EXPECTED_PROVIDER_KEYS)
    for pid, meta in PROVIDERS.items():
        assert "name" in meta
        assert "env_key" in meta
        assert "base_url" in meta
        assert "desc" in meta
        assert "models" in meta
        assert "default_model" in meta
        assert "voice" in meta
        assert "key_hint" in meta
        assert isinstance(meta["models"], list)


def test_tool_keys_count_and_env_names():
    assert len(TOOL_KEYS) == 8
    envs = [t["env"] for t in TOOL_KEYS]
    assert "EXA_API_KEY" in envs
    assert "GITHUB_TOKEN" in envs
    assert "SPOTIFY_CLIENT_ID" in envs
    spotify = next(t for t in TOOL_KEYS if t["env"] == "SPOTIFY_CLIENT_ID")
    assert spotify.get("extra_keys") == ["SPOTIFY_CLIENT_SECRET"]


def test_personality_presets_expected_keys():
    assert set(PERSONALITY_PRESETS.keys()) == {
        "assistant",
        "engineer",
        "coach",
        "minimal",
        "custom",
    }
    for key, preset in PERSONALITY_PRESETS.items():
        assert "label" in preset
        assert "desc" in preset
        assert "soul" in preset
        if key != "custom":
            assert len(preset["soul"].strip()) > 0


# ── OnboardWizard __init__ / _load_existing_creds ─────────────────────────────


def test_onboard_wizard_init_sets_console_and_empty_dicts(mock_console):
    w = OnboardWizard(mock_console)
    assert w.c is mock_console
    assert w.config == {}
    assert w.creds == {}


def test_load_existing_creds_reads_json(wizard_home, mock_console):
    data = {"OPENAI_API_KEY": "sk-test-key-12345"}
    (wizard_home / "credentials.json").write_text(json.dumps(data))
    w = OnboardWizard(mock_console)
    w._load_existing_creds()
    assert w.creds == data


def test_load_existing_creds_invalid_json_resets_empty(wizard_home, mock_console):
    (wizard_home / "credentials.json").write_text("{not valid json")
    w = OnboardWizard(mock_console)
    w._load_existing_creds()
    assert w.creds == {}


def test_load_existing_creds_missing_file_leaves_empty(wizard_home, mock_console):
    w = OnboardWizard(mock_console)
    w._load_existing_creds()
    assert w.creds == {}


# ── OnboardWizard._save_all ───────────────────────────────────────────────────


def test_save_all_writes_config_settings_and_encrypts_credentials(wizard_home, mock_console):
    """A7: credentials go to the encrypted BlindVault, NEVER to a
    plaintext ``credentials.json``. Settings and config stay plain
    JSON (they are not secret material)."""
    from security.vault import BlindVault, reset_vault

    reset_vault()

    w = OnboardWizard(mock_console)
    w.creds = {"OPENAI_API_KEY": "sk-secret"}
    w.config = {
        "provider": "openai",
        "model": "gpt-4.1",
        "base_url": "",
        "agent_name": "TestBot",
        "multi_agent": True,
        "vlm_provider": "",
        "vlm_model": "",
        "local_preset": "",
        "phone_bridge_url": "",
        "glasses_model": "",
    }
    w._save_all()

    creds_json = wizard_home / "credentials.json"
    creds_enc = wizard_home / "credentials.enc"
    assert not creds_json.exists(), (
        "A7 regression: setup wizard wrote a plaintext credentials.json. "
        f"Contents: {creds_json.read_text() if creds_json.exists() else '<none>'}"
    )
    assert creds_enc.exists(), "Encrypted vault file credentials.enc is missing"

    vault = BlindVault(vault_path=str(wizard_home / "credentials.json"))
    assert vault.retrieve("OPENAI_API_KEY") == "sk-secret"

    assert (wizard_home / "config.json").exists()
    settings = json.loads((wizard_home / "settings.json").read_text())
    assert settings["llm"]["provider"] == "openai"
    assert settings["llm"]["model"] == "gpt-4.1"
    assert settings["meta"]["setup_complete"] is True
    assert settings["features"]["multi_agent"] is True


def test_save_all_never_writes_plaintext_credentials_json(wizard_home, mock_console):
    """Explicit A7 regression: even if callers pass many keys (openai,
    tool keys, HA token), none of them end up on disk as cleartext."""
    from security.vault import reset_vault

    reset_vault()

    w = OnboardWizard(mock_console)
    w.creds = {
        "OPENAI_API_KEY": "sk-a",
        "EXA_API_KEY": "exa-b",
        "HA_TOKEN": "ha-c",
    }
    w.config = {"provider": "openai", "model": "gpt-4.1"}
    w._save_all()

    assert not (wizard_home / "credentials.json").exists()
    assert (wizard_home / "credentials.enc").exists()


def test_save_all_does_not_clobber_legacy_plaintext_until_vault_migrates(
    wizard_home, mock_console
):
    """Returning users with a  ``credentials.json`` must have
    their keys preserved via the BlindVault auto-migration. After
    ``_save_all`` the plaintext file is gone (moved to
    ``credentials.json.bak.legacy``) and the encrypted vault is the
    sole authoritative store."""
    from security.vault import BlindVault, reset_vault

    reset_vault()

    legacy = wizard_home / "credentials.json"
    legacy.write_text(json.dumps({"ANTHROPIC_API_KEY": "sk-ant-legacy"}))

    w = OnboardWizard(mock_console)
    w.creds = {"ANTHROPIC_API_KEY": "sk-ant-legacy", "OPENAI_API_KEY": "sk-new"}
    w.config = {"provider": "openai", "model": "gpt-4.1"}
    w._save_all()

    assert not legacy.exists(), "Plaintext credentials.json should be removed after migration"
    assert (wizard_home / "credentials.enc").exists()
    backup = wizard_home / "credentials.json.bak.legacy"
    assert backup.exists(), "Legacy backup is expected at credentials.json.bak.legacy"

    vault = BlindVault(vault_path=str(wizard_home / "credentials.json"))
    assert vault.retrieve("ANTHROPIC_API_KEY") == "sk-ant-legacy"
    assert vault.retrieve("OPENAI_API_KEY") == "sk-new"


# ── OnboardWizardPlain ────────────────────────────────────────────────────────


def test_onboard_wizard_plain_init():
    p = OnboardWizardPlain()
    assert p.config == {}
    assert p.creds == {}


# ── run_setup ─────────────────────────────────────────────────────────────────


def test_run_setup_delegates_to_new_package():
    """`cli.setup_wizard.run_setup` is a thin shim over the new
    `cli.setup.run_setup` modular wizard. The legacy OnboardWizard /
    OnboardWizardPlain entry-points are dead code paths kept only so
    older external imports resolve."""
    from cli import setup as new_setup

    with patch.object(new_setup, "run_setup") as mock_new_run:
        run_setup()
    mock_new_run.assert_called_once()


def test_run_setup_reimport_still_exposes_shim():
    """Protect against accidentally dropping the shim which breaks
    external installer scripts that `from cli.setup_wizard import
    run_setup`."""
    import importlib
    reloaded = importlib.reload(sw)
    assert callable(getattr(reloaded, "run_setup", None))


# ── _validate_key (httpx) ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider, url_suffix",
    [
        ("openai", "api.openai.com"),
        ("anthropic", "api.anthropic.com"),
        ("gemini", "generativelanguage.googleapis.com"),
        ("groq", "api.groq.com"),
        ("openrouter", "openrouter.ai"),
        ("deepseek", "api.deepseek.com"),
        ("kimi", "api.moonshot.cn"),
        ("qwen", "dashscope.aliyuncs.com"),
    ],
)
async def test_validate_key_success_per_provider(provider, url_suffix, mock_console):
    mock_response = MagicMock()
    mock_response.status_code = 200
    inner = _HttpxInnerStub(response=mock_response)

    with _patch_httpx_async_client(inner):
        w = OnboardWizard(mock_console)
        assert await w._validate_key(provider, "test-key") is True
    assert len(inner.calls) == 1
    called_url = inner.calls[0][0]
    assert url_suffix in called_url


@pytest.mark.asyncio
async def test_validate_key_non_200_returns_false(mock_console):
    mock_response = MagicMock()
    mock_response.status_code = 401
    inner = _HttpxInnerStub(response=mock_response)

    with _patch_httpx_async_client(inner):
        w = OnboardWizard(mock_console)
        assert await w._validate_key("openai", "bad") is False


@pytest.mark.asyncio
async def test_validate_key_exception_returns_false(mock_console):
    inner = _HttpxInnerStub(side_effect=RuntimeError("network down"))

    with _patch_httpx_async_client(inner):
        w = OnboardWizard(mock_console)
        assert await w._validate_key("openai", "x") is False


# ── _check_ollama / _list_ollama_models ───────────────────────────────────────


@pytest.mark.asyncio
async def test_check_ollama_running_with_models(mock_console):
    payload = {"models": [{"name": "llama3.1"}, {"name": "llava:7b"}]}
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = payload
    inner = _HttpxInnerStub(response=mock_response)
    with _patch_httpx_async_client(inner), patch(
        "cli.setup_wizard.ollama_base_url", return_value="http://127.0.0.1:11434"
    ):
        w = OnboardWizard(mock_console)
        await w._check_ollama()

    mock_console.print.assert_called()
    printed = " ".join(str(c) for c in mock_console.print.call_args_list)
    assert "Ollama running" in printed or "model" in printed.lower()


@pytest.mark.asyncio
async def test_check_ollama_not_running_message(mock_console):
    inner = _HttpxInnerStub(side_effect=ConnectionError("refused"))
    with _patch_httpx_async_client(inner), patch(
        "cli.setup_wizard.ollama_base_url", return_value="http://localhost:11434"
    ):
        w = OnboardWizard(mock_console)
        await w._check_ollama()

    printed = " ".join(str(c) for c in mock_console.print.call_args_list)
    assert "Ollama not running" in printed or "ollama serve" in printed


@pytest.mark.asyncio
async def test_list_ollama_models_success():
    payload = {"models": [{"name": "a"}, {"name": "b:latest"}]}
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = payload
    inner = _HttpxInnerStub(response=mock_response)
    with _patch_httpx_async_client(inner), patch(
        "cli.setup_wizard.ollama_base_url", return_value="http://localhost:11434"
    ):
        w = OnboardWizard(MagicMock())
        models = await w._list_ollama_models()

    assert models == ["a", "b:latest"]


@pytest.mark.asyncio
async def test_list_ollama_models_failure_returns_empty():
    bad = MagicMock()
    bad.status_code = 500
    inner = _HttpxInnerStub(response=bad)
    with _patch_httpx_async_client(inner), patch(
        "cli.setup_wizard.ollama_base_url", return_value="http://localhost:11434"
    ):
        w = OnboardWizard(MagicMock())
        assert await w._list_ollama_models() == []


@pytest.mark.asyncio
async def test_plain_list_ollama_models_uses_same_endpoint():
    payload = {"models": [{"name": "m1"}]}
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = payload
    inner = _HttpxInnerStub(response=mock_response)
    with _patch_httpx_async_client(inner), patch(
        "cli.setup_wizard.ollama_base_url", return_value="http://host:11434"
    ):
        p = OnboardWizardPlain()
        assert await p._list_ollama_models() == ["m1"]


# ── _step_finish ──────────────────────────────────────────────────────────────


def test_step_finish_summary_contains_provider_model_agent_and_paths(wizard_home):
    from rich.console import Console

    console = Console(record=True, width=100)
    w = OnboardWizard(console)
    w.config = {
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "agent_name": "MyAgent",
    }
    w._step_finish()

    text = console.export_text(clear=False)
    assert "Setup Complete" in text
    assert "Anthropic" in text
    assert "claude-sonnet-4-20250514" in text
    assert "MyAgent" in text
    assert "api_key" in text or "localhost:9090" in text


def test_step_finish_defaults_when_config_sparse(wizard_home):
    from rich.console import Console

    console = Console(record=True, width=100)
    w = OnboardWizard(console)
    w.config = {}
    w._step_finish()
    text = console.export_text(clear=False)
    assert "Setup Complete" in text
    assert "default" in text or "?" in text
