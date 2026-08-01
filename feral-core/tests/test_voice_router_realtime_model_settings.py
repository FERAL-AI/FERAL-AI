"""Lane U2 — voice router honours ``audio.realtime_model`` setting.

Pre-Lane-U2 the router hardcoded ``"gpt-realtime"`` at three call
sites (``router.py:199, 252-256, 703``), which meant the operator
could pick a realtime model in Settings → Voice or via the CLI
preflight and the runtime would silently ignore it on every session
open. This test pins the new contract: when ``opts`` does NOT supply
a model the router reads ``load_settings()["audio"]["realtime_model"]``
and passes that to ``RealtimeProxy.start_session``.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from voice.realtime_proxy import DEFAULT_MODEL
from voice.router import VoiceRouter


@pytest.fixture()
def feral_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    return tmp_path


def _write_settings(home, **audio_overrides):
    settings_path = home / "settings.json"
    settings_path.write_text(json.dumps({"audio": audio_overrides}))


async def test_router_honors_audio_realtime_model_setting(feral_home):
    """When ``audio.realtime_model`` is set, ``open_voice_session``
    must propagate that value into ``RealtimeProxy.start_session`` —
    the proxy URL is constructed against that model id so an operator
    pick of ``gpt-realtime-mini`` MUST NOT degrade silently to the
    hardcoded ``gpt-realtime`` default."""
    _write_settings(feral_home, realtime_model="gpt-realtime-mini")

    mock_realtime = MagicMock()
    mock_realtime.available = True
    mock_realtime.start_session = AsyncMock(return_value=MagicMock(connected=True))

    router = VoiceRouter(realtime_proxy=mock_realtime)
    await router.open_session("sess-abc", "openai_realtime", provider_opts={})

    mock_realtime.start_session.assert_awaited_once()
    kwargs = mock_realtime.start_session.await_args.kwargs
    assert kwargs["model"] == "gpt-realtime-mini"


async def test_router_falls_back_to_proxy_default_when_setting_blank(feral_home):
    """A blank or missing ``audio.realtime_model`` must yield the proxy's
    own default, not a crash and not a literal empty string. This is the
    path existing installs that never touched the setting will hit.

    Asserted against ``DEFAULT_MODEL`` rather than a hardcoded name: the
    default is a cost decision (the mini tier runs roughly a third of the
    full model per audio token) and may move again. What this test exists
    to pin is that the fallback resolves to the proxy default at all.
    """
    _write_settings(feral_home, realtime_model="")

    mock_realtime = MagicMock()
    mock_realtime.available = True
    mock_realtime.start_session = AsyncMock(return_value=MagicMock(connected=True))

    router = VoiceRouter(realtime_proxy=mock_realtime)
    await router.open_session("sess-xyz", "openai_realtime", provider_opts={})

    kwargs = mock_realtime.start_session.await_args.kwargs
    assert kwargs["model"] == DEFAULT_MODEL


async def test_router_remaps_retired_preview_model_to_ga(feral_home):
    """A stale settings.json pinning a retired OpenAI preview snapshot
    (e.g. gpt-4o-realtime-preview-2025-06-03, which OpenAI rejects with
    4004 model_not_found) must be auto-corrected to the GA rolling alias
    `gpt-realtime` — so voice works without the operator hand-editing
    settings."""
    _write_settings(feral_home, realtime_model="gpt-4o-realtime-preview-2025-06-03")

    mock_realtime = MagicMock()
    mock_realtime.available = True
    mock_realtime.start_session = AsyncMock(return_value=MagicMock(connected=True))

    router = VoiceRouter(realtime_proxy=mock_realtime)
    await router.open_session("sess-dep", "openai_realtime", provider_opts={})

    kwargs = mock_realtime.start_session.await_args.kwargs
    # Deliberately the GA alias, not DEFAULT_MODEL. The operator pinned a
    # full-tier preview model, so the closest correct replacement is the
    # full-tier GA alias. Remapping them onto the cheaper mini default
    # would quietly change quality on a path they never asked to change.
    assert kwargs["model"] == "gpt-realtime"


async def test_handle_audio_from_client_uses_settings_model(feral_home):
    """The web-client audio path (``handle_audio_from_client``) used
    to omit ``model`` entirely → ``RealtimeProxy`` invented its own
    default. Lane U2 requires this path to also surface the operator's
    setting so the WebUI dropdown actually changes runtime behaviour."""
    _write_settings(feral_home, realtime_model="gpt-realtime-mini")

    mock_realtime = MagicMock()
    mock_realtime.available = True
    mock_realtime.get_session = MagicMock(return_value=None)
    mock_realtime.evict_dead_session = AsyncMock(return_value=False)
    mock_realtime.start_session = AsyncMock(return_value=MagicMock(connected=True, send_audio=AsyncMock()))

    router = VoiceRouter(realtime_proxy=mock_realtime)
    router.set_session_voice_mode("sess-web", "realtime")

    await router.handle_audio_from_client("sess-web", "AAAA==")

    kwargs = mock_realtime.start_session.await_args.kwargs
    assert kwargs["model"] == "gpt-realtime-mini"
