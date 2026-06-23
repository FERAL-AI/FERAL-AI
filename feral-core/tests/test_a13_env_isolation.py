"""-A13 regression tests — env-mutation blast-radius reduction.

These tests prove that:

* ``/api/config/credentials`` no longer mutates ``os.environ`` for
  channel bot tokens, skill keys, or any non-LLM-SDK credential. Only
  the narrow legacy SDK set (``_LEGACY_ENV_EXPORT_KEYS``) is exported.
* The route still hands the freshly-saved channel token to
  ``ChannelManager.start_channel`` so reconnect behaviour is preserved
  *without* depending on the global env side channel.
* ``BrainState._start_channels`` resolves channel credentials from the
  in-process ``ConfigLoader._credentials`` dict rather than mutating
  ``os.environ``, so config refresh / hot-update across sessions does
  not leak tokens into the parent process.

Together these guard against the historical pattern where one test
case's "save Telegram token" silently became the next test case's
``os.environ['FERAL_TELEGRAM_BOT_TOKEN']``.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.no_auto_feral_home


CHANNEL_TOKEN_KEYS = (
    "FERAL_TELEGRAM_BOT_TOKEN",
    "FERAL_DISCORD_BOT_TOKEN",
    "FERAL_SLACK_BOT_TOKEN",
    "FERAL_SLACK_APP_TOKEN",
    "FERAL_WHATSAPP_ACCESS_TOKEN",
    "FERAL_WHATSAPP_PHONE_NUMBER_ID",
    "FERAL_WHATSAPP_APP_SECRET",
    "FERAL_WHATSAPP_VERIFY_TOKEN",
)


def _build_state_mock(tmp_path):
    mock = MagicMock()
    mock.config = MagicMock()
    mock.config.save_credentials = MagicMock(return_value=True)
    mock.vault = None
    mock.provider_catalog = None
    mock.orchestrator = None
    mock.channel_manager = MagicMock()
    mock.channel_manager._channels = {}
    mock.channel_manager.start_channel = AsyncMock(return_value=None)
    return mock


@pytest.fixture
def client(tmp_path, monkeypatch):
    for key in CHANNEL_TOKEN_KEYS:
        monkeypatch.delenv(key, raising=False)
    mock = _build_state_mock(tmp_path)
    with (
        patch("api.state.state", mock),
        patch("api.routes.config.state", mock),
        patch("api.routes.channels.state", mock),
    ):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), mock


def test_save_credentials_does_not_export_channel_tokens_to_env(client, monkeypatch):
    c, _mock = client
    monkeypatch.delenv("FERAL_TELEGRAM_BOT_TOKEN", raising=False)

    r = c.post(
        "/api/config/credentials",
        json={"FERAL_TELEGRAM_BOT_TOKEN": "tg-secret-do-not-leak"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "FERAL_TELEGRAM_BOT_TOKEN" in body["keys_saved"]
    assert os.environ.get("FERAL_TELEGRAM_BOT_TOKEN") is None, (
        "channel bot tokens must not be exported to os.environ"
    )


def test_save_credentials_does_not_export_skill_keys_to_env(client, monkeypatch):
    c, _mock = client
    monkeypatch.delenv("FERAL_KEY_weather", raising=False)
    monkeypatch.delenv("FERAL_KEY_search", raising=False)

    r = c.post(
        "/api/config/credentials",
        json={"skill_keys": {"weather": "w-secret", "search": "s-secret"}},
    )
    assert r.status_code == 200
    assert os.environ.get("FERAL_KEY_weather") is None
    assert os.environ.get("FERAL_KEY_search") is None


@pytest.mark.parametrize("env_var", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"])
def test_save_credentials_still_exports_legacy_sdk_keys(client, env_var, monkeypatch):
    c, _mock = client
    monkeypatch.delenv(env_var, raising=False)

    r = c.post("/api/config/credentials", json={env_var: "sk-legacy"})
    assert r.status_code == 200
    assert os.environ.get(env_var) == "sk-legacy", (
        "legacy LLM SDK keys must still be exported so openai/anthropic "
        "client libraries that read from os.environ keep working"
    )


def test_save_credentials_starts_channel_using_request_payload(client, monkeypatch):
    c, mock = client
    monkeypatch.delenv("FERAL_TELEGRAM_BOT_TOKEN", raising=False)

    r = c.post(
        "/api/config/credentials",
        json={"FERAL_TELEGRAM_BOT_TOKEN": "tg-direct-pass"},
    )
    assert r.status_code == 200

    async def _drain():
        for _ in range(10):
            await asyncio.sleep(0)

    asyncio.run(_drain())

    mock.channel_manager.start_channel.assert_called()
    args, kwargs = mock.channel_manager.start_channel.call_args
    ch_type, ch_config = args[0], args[1]
    assert ch_type == "telegram"
    assert ch_config["bot_token"] == "tg-direct-pass"
    assert ch_config["enabled"] is True
    assert os.environ.get("FERAL_TELEGRAM_BOT_TOKEN") is None


def test_two_sequential_writes_do_not_leak_first_value_via_env(client, monkeypatch):
    """Refresh / hot-update must not depend on env-mutation persistence."""
    c, mock = client
    for key in CHANNEL_TOKEN_KEYS:
        monkeypatch.delenv(key, raising=False)

    r1 = c.post("/api/config/credentials", json={"FERAL_TELEGRAM_BOT_TOKEN": "first"})
    assert r1.status_code == 200
    assert os.environ.get("FERAL_TELEGRAM_BOT_TOKEN") is None

    r2 = c.post("/api/config/credentials", json={"FERAL_TELEGRAM_BOT_TOKEN": "second"})
    assert r2.status_code == 200
    assert os.environ.get("FERAL_TELEGRAM_BOT_TOKEN") is None

    last_call = mock.channel_manager.start_channel.call_args_list[-1]
    _ch_type, ch_config = last_call.args[0], last_call.args[1]
    assert ch_config["bot_token"] == "second"


def test_save_credentials_starts_whatsapp_using_request_payload(client, monkeypatch):
    c, mock = client
    monkeypatch.delenv("FERAL_WHATSAPP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("FERAL_WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("FERAL_WHATSAPP_APP_SECRET", raising=False)

    r = c.post(
        "/api/config/credentials",
        json={
            "FERAL_WHATSAPP_ACCESS_TOKEN": "wa-access",
            "FERAL_WHATSAPP_PHONE_NUMBER_ID": "wa-phone",
            "FERAL_WHATSAPP_APP_SECRET": "wa-secret",
        },
    )
    assert r.status_code == 200

    async def _drain():
        for _ in range(10):
            await asyncio.sleep(0)

    asyncio.run(_drain())

    calls = [call for call in mock.channel_manager.start_channel.call_args_list if call.args[0] == "whatsapp"]
    assert calls, "whatsapp channel should restart after credential save"
    wa_cfg = calls[-1].args[1]
    assert wa_cfg["access_token"] == "wa-access"
    assert wa_cfg["phone_number_id"] == "wa-phone"
    assert wa_cfg["app_secret"] == "wa-secret"
    assert wa_cfg["enabled"] is True
    assert os.environ.get("FERAL_WHATSAPP_ACCESS_TOKEN") is None
    assert os.environ.get("FERAL_WHATSAPP_PHONE_NUMBER_ID") is None
    assert os.environ.get("FERAL_WHATSAPP_APP_SECRET") is None


def test_boot_loader_does_not_export_channel_secrets_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FERAL_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("FERAL_WHATSAPP_VERIFY_TOKEN", raising=False)

    (tmp_path / "credentials.json").write_text(
        '{"OPENAI_API_KEY":"sk-openai","FERAL_TELEGRAM_BOT_TOKEN":"tg-secret","FERAL_WHATSAPP_VERIFY_TOKEN":"wa-verify"}',
        encoding="utf-8",
    )

    from api.state import BrainState

    BrainState._load_stored_credentials()

    assert os.environ.get("OPENAI_API_KEY") == "sk-openai"
    assert os.environ.get("FERAL_TELEGRAM_BOT_TOKEN") is None
    assert os.environ.get("FERAL_WHATSAPP_VERIFY_TOKEN") is None


# ── BrainState._start_channels uses config-resident credentials ──


def test_start_channels_reads_from_config_credentials_without_env(monkeypatch):
    """``_cred`` must resolve channel tokens through ``state.config._credentials``
    (i.e. the same path used by config refresh) without exporting them
    into ``os.environ``.
    """
    for key in CHANNEL_TOKEN_KEYS:
        monkeypatch.delenv(key, raising=False)

    from api.state import BrainState

    fake_state = SimpleNamespace()
    fake_state.config = SimpleNamespace(
        _credentials={
            "FERAL_TELEGRAM_BOT_TOKEN": "from-config-only",
        },
    )
    fake_state.config.get_credential = lambda key, default="": (
        fake_state.config._credentials.get(key, default)
    )

    fake_state.channel_manager = MagicMock()
    fake_state.channel_manager.start_channel = AsyncMock(return_value=None)
    fake_state.channel_manager.set_handler = MagicMock()
    fake_state.orchestrator = MagicMock()
    fake_state.session_handoff = None
    fake_state.memory = None
    fake_state.sessions = {}
    fake_state._channel_collectors = {}

    asyncio.run(BrainState._start_channels(fake_state))

    fake_state.channel_manager.start_channel.assert_called()
    started = {
        call.args[0]: call.args[1] for call in fake_state.channel_manager.start_channel.call_args_list
    }
    assert "telegram" in started
    assert started["telegram"]["bot_token"] == "from-config-only"
    assert os.environ.get("FERAL_TELEGRAM_BOT_TOKEN") is None, (
        "_start_channels must not export channel tokens into os.environ"
    )


def test_start_channels_passes_whatsapp_app_secret_without_env(monkeypatch):
    for key in CHANNEL_TOKEN_KEYS:
        monkeypatch.delenv(key, raising=False)

    from api.state import BrainState

    fake_state = SimpleNamespace()
    fake_state.config = SimpleNamespace(
        _credentials={
            "FERAL_WHATSAPP_ACCESS_TOKEN": "wa-access-from-config",
            "FERAL_WHATSAPP_PHONE_NUMBER_ID": "wa-phone-from-config",
            "FERAL_WHATSAPP_APP_SECRET": "wa-secret-from-config",
        },
    )
    fake_state.config.get_credential = lambda key, default="": (
        fake_state.config._credentials.get(key, default)
    )
    fake_state.vault = None
    fake_state.channel_manager = MagicMock()
    fake_state.channel_manager.start_channel = AsyncMock(return_value=None)
    fake_state.channel_manager.set_handler = MagicMock()
    fake_state.orchestrator = MagicMock()
    fake_state.session_handoff = None
    fake_state.memory = None
    fake_state.sessions = {}
    fake_state._channel_collectors = {}

    asyncio.run(BrainState._start_channels(fake_state))

    started = {
        call.args[0]: call.args[1] for call in fake_state.channel_manager.start_channel.call_args_list
    }
    assert "whatsapp" in started
    assert started["whatsapp"]["access_token"] == "wa-access-from-config"
    assert started["whatsapp"]["phone_number_id"] == "wa-phone-from-config"
    assert started["whatsapp"]["app_secret"] == "wa-secret-from-config"
    assert os.environ.get("FERAL_WHATSAPP_ACCESS_TOKEN") is None
    assert os.environ.get("FERAL_WHATSAPP_APP_SECRET") is None


def test_whatsapp_webhook_verify_prefers_config_credential(client, monkeypatch):
    c, mock = client
    monkeypatch.delenv("FERAL_WHATSAPP_VERIFY_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_VERIFY_TOKEN", raising=False)
    mock.config.get_credential = MagicMock(return_value="wa-config-verify")

    r = c.get(
        "/api/channels/whatsapp/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wa-config-verify",
            "hub.challenge": "abc123",
        },
    )
    assert r.status_code == 200
    assert r.text == "abc123"
