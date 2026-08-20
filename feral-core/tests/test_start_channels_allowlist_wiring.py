"""``BrainState._start_channels`` must hand every channel its allowlist.

The gate lives in ``channels.base.Channel``, but it is only as good as
the config it receives. This covers the wiring: the allowlist is
resolved through the same ``_cred`` ladder the bot tokens use
(config credentials -> os.environ -> credentials.json -> vault), unioned
with the ``channels`` section of settings.json, and the pairing window
defaults to OFF.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from channels.base import _coerce_id_set


CHANNEL_KEYS = (
    "FERAL_TELEGRAM_BOT_TOKEN",
    "FERAL_DISCORD_BOT_TOKEN",
    "FERAL_SLACK_BOT_TOKEN",
    "FERAL_SLACK_APP_TOKEN",
    "FERAL_WHATSAPP_ACCESS_TOKEN",
    "FERAL_WHATSAPP_PHONE_NUMBER_ID",
    "FERAL_WHATSAPP_APP_SECRET",
    "FERAL_TELEGRAM_ALLOWED_SENDERS",
    "FERAL_TELEGRAM_ALLOWED_CHATS",
    "FERAL_TELEGRAM_PAIRING_WINDOW_SEC",
    "FERAL_DISCORD_ALLOWED_SENDERS",
    "FERAL_SLACK_ALLOWED_SENDERS",
)


def _fake_state(credentials: dict, merged: dict | None = None) -> SimpleNamespace:
    st = SimpleNamespace()
    st.config = SimpleNamespace(_credentials=dict(credentials))
    st.config._merged = merged or {}
    st.config.get_credential = lambda key, default="": (
        st.config._credentials.get(key, default)
    )
    st.config.update_settings = MagicMock()
    st.vault = None
    st.channel_manager = MagicMock()
    st.channel_manager.start_channel = AsyncMock(return_value=None)
    st.channel_manager.set_handler = MagicMock()
    st.orchestrator = MagicMock()
    st.session_handoff = None
    st.memory = None
    st.sessions = {}
    st._channel_collectors = {}
    return st


def _run(st) -> dict:
    from api.state import BrainState

    asyncio.run(BrainState._start_channels(st))
    return {
        call.args[0]: call.args[1]
        for call in st.channel_manager.start_channel.call_args_list
    }


@pytest.fixture(autouse=True)
def _clear_channel_env(monkeypatch):
    for key in CHANNEL_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_allowlist_resolves_through_the_credential_ladder():
    st = _fake_state({
        "FERAL_TELEGRAM_BOT_TOKEN": "tok",
        "FERAL_TELEGRAM_ALLOWED_SENDERS": "123456789",
    })
    started = _run(st)
    assert _coerce_id_set(started["telegram"]["allowed_senders"]) == {"123456789"}


def test_allowlist_also_reads_the_settings_channels_section():
    st = _fake_state(
        {"FERAL_TELEGRAM_BOT_TOKEN": "tok"},
        merged={"channels": {
            "telegram_allowed_senders": ["111"],
            "telegram_allowed_chats": ["-100222"],
        }},
    )
    started = _run(st)
    assert _coerce_id_set(started["telegram"]["allowed_senders"]) == {"111"}
    assert _coerce_id_set(started["telegram"]["allowed_chats"]) == {"-100222"}


def test_credential_and_settings_sources_are_unioned():
    st = _fake_state(
        {
            "FERAL_TELEGRAM_BOT_TOKEN": "tok",
            "FERAL_TELEGRAM_ALLOWED_SENDERS": "111",
        },
        merged={"channels": {"telegram_allowed_senders": ["222", "333"]}},
    )
    started = _run(st)
    assert _coerce_id_set(started["telegram"]["allowed_senders"]) == {"111", "222", "333"}


def test_env_var_is_read_when_no_config_credential_exists(monkeypatch):
    monkeypatch.setenv("FERAL_DISCORD_ALLOWED_SENDERS", "snowflake-1")
    st = _fake_state({"FERAL_DISCORD_BOT_TOKEN": "tok"})
    started = _run(st)
    assert _coerce_id_set(started["discord"]["allowed_senders"]) == {"snowflake-1"}


def test_every_channel_gets_an_access_policy():
    st = _fake_state({
        "FERAL_TELEGRAM_BOT_TOKEN": "t",
        "FERAL_DISCORD_BOT_TOKEN": "d",
        "FERAL_SLACK_BOT_TOKEN": "s",
        "FERAL_WHATSAPP_ACCESS_TOKEN": "w",
        "FERAL_WHATSAPP_PHONE_NUMBER_ID": "p",
    })
    started = _run(st)
    assert set(started) == {"telegram", "discord", "slack", "whatsapp"}
    for name, cfg in started.items():
        assert "allowed_senders" in cfg, f"{name} got no allowlist"
        assert "allowed_chats" in cfg, f"{name} got no chat allowlist"
        assert cfg["pairing_window_sec"] == 0, f"{name} pairing is not off by default"
        assert callable(cfg["on_pair"])


def test_unconfigured_channel_is_handed_an_empty_allowlist_not_a_missing_one():
    """Empty means deny-all in the channel. A MISSING key would too, but
    passing it explicitly keeps the fail-closed contract visible."""
    st = _fake_state({"FERAL_TELEGRAM_BOT_TOKEN": "tok"})
    started = _run(st)
    assert _coerce_id_set(started["telegram"]["allowed_senders"]) == set()
    assert _coerce_id_set(started["telegram"]["allowed_chats"]) == set()


def test_pairing_window_is_opt_in_per_channel_or_globally():
    st = _fake_state(
        {"FERAL_TELEGRAM_BOT_TOKEN": "t", "FERAL_SLACK_BOT_TOKEN": "s"},
        merged={"channels": {
            "telegram_pairing_window_sec": 300,
            "pairing_window_sec": 60,
        }},
    )
    started = _run(st)
    assert started["telegram"]["pairing_window_sec"] == 300  # per-channel wins
    assert started["slack"]["pairing_window_sec"] == 60      # global fallback


def test_garbage_pairing_window_falls_back_to_off():
    st = _fake_state(
        {"FERAL_TELEGRAM_BOT_TOKEN": "t"},
        merged={"channels": {"telegram_pairing_window_sec": "forever"}},
    )
    started = _run(st)
    assert started["telegram"]["pairing_window_sec"] == 0


def test_on_pair_hook_persists_into_the_channels_settings_section():
    st = _fake_state({"FERAL_TELEGRAM_BOT_TOKEN": "t"})
    started = _run(st)
    started["telegram"]["on_pair"]("telegram", "555", "-100999")

    writes = {
        call.args[1]: call.args[2]
        for call in st.config.update_settings.call_args_list
        if call.args[0] == "channels"
    }
    assert writes["telegram_allowed_senders"] == ["555"]
    assert writes["telegram_allowed_chats"] == ["-100999"]


def test_on_pair_hook_appends_without_dropping_existing_entries():
    st = _fake_state(
        {"FERAL_TELEGRAM_BOT_TOKEN": "t"},
        merged={"channels": {"telegram_allowed_senders": ["111"]}},
    )
    started = _run(st)
    started["telegram"]["on_pair"]("telegram", "222", "")

    writes = {
        call.args[1]: call.args[2]
        for call in st.config.update_settings.call_args_list
        if call.args[0] == "channels"
    }
    assert writes["telegram_allowed_senders"] == ["111", "222"]
