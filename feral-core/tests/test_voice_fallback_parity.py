"""Lane 05  — Gemini + Realtime fallback parity (THESIS_SCENARIOS S4).

Closes AUDIT-r14 finding 15 fixes #2 + #3:

* Fix #2: OpenAI Realtime *connect-time* failure (no key, bad key,
  network) now emits ``voice_status: degraded`` via the attached
  fallback router. Pre-fix it just returned None and the phone
  saw dead air.

* Fix #3: GeminiRealtimeProxy gained ``attach_fallback_router``
  + an ``_handle_error`` classifier that mirrors the OpenAI one
  (auth → ``gemini_live_auth``, quota → ``gemini_live_quota``,
  overload → ``gemini_live_overload``, other → ``gemini_live_error``).
  Connect-time failure ditto.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.gemini_realtime import GeminiRealtimeProxy  # noqa: E402
from voice.realtime_proxy import RealtimeProxy  # noqa: E402


def _make_capturing_router():
    """Build a fake VoiceRouter that records handle_realtime_failure
    calls so we can assert (session_id, reason, detail) without
    standing up the full router."""
    router = MagicMock()
    router.handle_realtime_failure = AsyncMock()
    return router


# ── Gemini parity ─────────────────────────────────────────────────


def test_gemini_proxy_supports_attach_fallback_router():
    proxy = GeminiRealtimeProxy()
    router = _make_capturing_router()
    proxy.attach_fallback_router(router)
    assert proxy._fallback_router is router


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected_reason",
    [
        ("API_KEY_INVALID: please regenerate", "gemini_live_auth"),
        ("HTTP 401 Unauthorized", "gemini_live_auth"),
        ("Permission denied (403)", "gemini_live_auth"),
        ("RESOURCE_EXHAUSTED: quota for your project exceeded", "gemini_live_quota"),
        ("HTTP 429 too many requests", "gemini_live_quota"),
        ("Service overloaded — try again", "gemini_live_overload"),
        ("HTTP 503 Service Unavailable", "gemini_live_overload"),
        ("Some other transient blip", "gemini_live_error"),
    ],
)
async def test_gemini_handle_error_classification(error, expected_reason):
    proxy = GeminiRealtimeProxy()
    router = _make_capturing_router()
    proxy.attach_fallback_router(router)

    await proxy._handle_error("session-123", error)

    router.handle_realtime_failure.assert_awaited_once()
    call = router.handle_realtime_failure.await_args
    assert call.kwargs["session_id"] == "session-123"
    assert call.kwargs["reason"] == expected_reason
    assert error[:200] in call.kwargs["detail"]


@pytest.mark.asyncio
async def test_gemini_handle_error_without_router_does_not_raise():
    """Defensive: if no fallback router was ever attached (early
    boot, unit test) the classifier still logs without raising."""
    proxy = GeminiRealtimeProxy()
    await proxy._handle_error("session-123", "anything")  # must not raise


# ── OpenAI Realtime connect-time failure ──────────────────────────


@pytest.mark.asyncio
async def test_realtime_no_key_emits_degraded_voice_status(monkeypatch):
    """When no OpenAI key is configured, ``start_session`` emits
    ``openai_realtime_no_key`` through the fallback router instead
    of returning None silently.

    "Not configured" means both sources. ``_resolve_openai_key`` reads
    the vault first and only falls back to the env var, so clearing the
    env alone does not express it: under a randomised test order this
    picked up an ``sk-test`` another test had left in the shared vault,
    reached a real handshake, and failed with "Incorrect API key".
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "security.vault_keys.get_active_key", lambda *a, **k: "",
    )
    proxy = RealtimeProxy()
    router = _make_capturing_router()
    proxy.attach_fallback_router(router)

    rs = await proxy.start_session("session-abc", "node-xyz")

    assert rs is None
    router.handle_realtime_failure.assert_awaited_once()
    call = router.handle_realtime_failure.await_args
    assert call.kwargs["reason"] == "openai_realtime_no_key"
    assert "OPENAI_API_KEY" in call.kwargs["detail"]


@pytest.mark.asyncio
async def test_gemini_no_key_emits_degraded_voice_status(monkeypatch):
    """Same connect-failure contract on the Gemini side."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    proxy = GeminiRealtimeProxy()
    router = _make_capturing_router()
    proxy.attach_fallback_router(router)

    gs = await proxy.start_session("session-abc", "node-xyz")

    assert gs is None
    router.handle_realtime_failure.assert_awaited_once()
    call = router.handle_realtime_failure.await_args
    assert call.kwargs["reason"] == "gemini_live_no_key"
