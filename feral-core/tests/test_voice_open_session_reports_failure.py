"""``VoiceRouter.open_session`` must say when it did not open a session.

Every failure inside ``open_session`` was reported by returning
``None``: an unavailable OpenAI Realtime proxy and an unavailable
Gemini proxy each logged a warning, an unrecognised mode logged at
DEBUG, and a chained pipeline that could not build its providers
returned None from ``open_chained_session``. Measured before the fix
against a real ``VoiceRouter``, every one of those paths sent the
client zero frames, so nothing on any surface could tell an open voice
session apart from one that never existed.

The one case that is NOT a failure: a realtime ``start_session`` that
returns ``None`` after ``handle_realtime_failure`` morphed the session
onto the chained pipeline. Voice is live there, on another provider,
and reporting it as a failure would be the same lie in the other
direction. That is pinned here too.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from voice.router import VoiceRouter


def _statuses(send_to_session: AsyncMock) -> list[dict]:
    out = []
    for call in send_to_session.await_args_list:
        msg = call.args[1]
        if getattr(msg, "type", "") == "voice_status":
            out.append(msg.payload)
    return out


def _router(**kwargs) -> tuple[VoiceRouter, AsyncMock]:
    send = AsyncMock()
    return VoiceRouter(send_to_session=send, **kwargs), send


@pytest.mark.asyncio
async def test_realtime_proxy_without_key_reports_unavailable():
    proxy = MagicMock(available=False)
    router, send = _router(realtime_proxy=proxy)

    result = await router.open_session("sess-a", "openai_realtime")

    assert result is None
    frames = _statuses(send)
    assert frames, "a refused open told the client nothing"
    assert frames[-1]["state"] == "unavailable"
    assert frames[-1]["reason"] == "openai_realtime_unavailable"
    assert frames[-1]["provider"] == "openai"
    assert frames[-1]["cause"] == "no_api_key"
    assert frames[-1]["summary"]
    assert frames[-1]["recommendation"]


@pytest.mark.asyncio
async def test_gemini_proxy_without_key_reports_unavailable():
    router, send = _router()
    router._gemini = MagicMock(available=False)

    result = await router.open_session("sess-b", "gemini_live")

    assert result is None
    frames = _statuses(send)
    assert frames, "a refused open told the client nothing"
    assert frames[-1]["state"] == "unavailable"
    assert frames[-1]["reason"] == "gemini_live_unavailable"
    assert frames[-1]["cause"] == "no_api_key"


@pytest.mark.asyncio
async def test_unknown_mode_reports_unavailable():
    router, send = _router()

    result = await router.open_session("sess-c", "telepathy")

    assert result is None
    frames = _statuses(send)
    assert frames, "an unrecognised mode was refused in silence"
    assert frames[-1]["state"] == "unavailable"
    assert frames[-1]["reason"] == "unknown_voice_mode"
    assert "telepathy" in frames[-1]["summary"]


@pytest.mark.asyncio
async def test_realtime_start_failure_reports_unavailable():
    proxy = MagicMock(available=True)
    proxy.start_session = AsyncMock(return_value=None)
    router, send = _router(realtime_proxy=proxy)

    result = await router.open_session("sess-d", "openai_realtime")

    assert result is None
    frames = _statuses(send)
    assert frames[-1]["state"] == "unavailable"
    assert frames[-1]["reason"] == "openai_realtime_start_failed"


@pytest.mark.asyncio
async def test_realtime_start_failure_that_morphed_returns_the_live_session():
    """A recovered session must NOT be reported as a failed open."""
    proxy = MagicMock(available=True)
    chained_handle = MagicMock(name="chained-session")

    async def _start(*_a, **_k):
        # What handle_realtime_failure's chained morph leaves behind.
        router._session_voice_mode["sess-e"] = "chained"
        return None

    proxy.start_session = AsyncMock(side_effect=_start)
    router, send = _router(realtime_proxy=proxy)
    router._chained = MagicMock()
    router._chained.get_session = MagicMock(return_value=chained_handle)

    result = await router.open_session("sess-e", "openai_realtime")

    assert result is chained_handle
    assert not [
        f for f in _statuses(send) if f["state"] == "unavailable"
    ], "a session that recovered onto chained was reported as unavailable"


@pytest.mark.asyncio
async def test_open_failure_does_not_overwrite_a_more_specific_status():
    """``_construct_provider`` names the engine; keep that, not a generic tag."""
    router, send = _router()
    router._chained = MagicMock()
    router._chained.get_session = MagicMock(return_value=None)
    router.open_chained_session = AsyncMock(return_value=None)
    router._session_degraded["sess-f"] = {
        "state": "unavailable",
        "reason": "local_stt_unavailable",
        "provider": "",
        "fallback_provider": "",
        "detail": "faster_whisper: weights missing",
    }

    result = await router.open_session("sess-f", "chained")

    assert result is None
    assert router._session_degraded["sess-f"]["reason"] == "local_stt_unavailable"
