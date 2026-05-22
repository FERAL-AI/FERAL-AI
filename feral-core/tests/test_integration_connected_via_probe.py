"""
Lane 10 — every integration's ``connected`` property reflects probe
results when available (probe.ok overrides token-presence).

Pre-Lane-10 the integrations reported ``connected=True`` whenever a
token or fallback host env var was present. Finding 19 calls this out
as a "lying" surface — the LLM and the WebUI ended up calling APIs
that 401'd on every request because the cached token was actually
revoked. The contract this lane lands:

* When a probe has run within ``STATUS_TTL_SECONDS`` the integration
  reflects the live ``ok`` value.
* No probe yet → fall back to existing token-presence behaviour so
  we don't downgrade healthy connections.
* ``probe_connected()`` async method on every integration forces a
  live round-trip and updates the shared cache.
* ICS / IMAP fallbacks no longer short-circuit ``connected=True`` —
  they're advertised separately via ``ics_configured`` /
  ``imap_configured``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _reset_probe_status():
    from integrations import _probe_status

    _probe_status.clear()
    yield
    _probe_status.clear()


class _StubOAuth:
    """Mimics OAuthManager just enough for ``connected`` properties."""

    def __init__(self, connected_providers: set[str] | None = None):
        self._connected = set(connected_providers or set())
        self._vault = None

    def is_connected(self, provider_id: str) -> bool:
        return provider_id in self._connected


# ──────────────────────────────────────────────────────────────────────
# Probe overrides token-presence
# ──────────────────────────────────────────────────────────────────────


def test_failed_probe_overrides_token_presence_spotify():
    from integrations import _probe_status
    from integrations.spotify import SpotifyIntegration

    spotify = SpotifyIntegration(oauth_manager=_StubOAuth({"spotify"}))
    assert spotify.connected is True  # baseline: token present

    _probe_status.mark_probe_result(
        "spotify",
        ok=False,
        reason="unauthorized",
        detail="401",
    )
    assert spotify.connected is False, (
        "A failed probe must override token-presence — the LLM should "
        "not be advertised an integration that returns 401"
    )


def test_successful_probe_marks_connected_when_no_token_present():
    from integrations import _probe_status
    from integrations.notion import NotionIntegration

    notion = NotionIntegration(oauth_manager=_StubOAuth())
    notion._token = ""
    assert notion.connected is False

    _probe_status.mark_probe_result("notion", ok=True, reason="ok")
    assert notion.connected is True


# ──────────────────────────────────────────────────────────────────────
# ICS / IMAP no longer lie about Google connectivity
# ──────────────────────────────────────────────────────────────────────


def test_calendar_with_only_ics_url_is_not_connected(monkeypatch):
    monkeypatch.setenv("FERAL_CALENDAR_ICS",
                       "https://calendar.google.com/calendar/ical/abc/basic.ics")
    from integrations.calendar import CalendarIntegration

    cal = CalendarIntegration(oauth_manager=_StubOAuth())
    assert cal.ics_configured is True
    assert cal.connected is False, (
        "ICS feed availability must not advertise Google Calendar as "
        "connected — finding 19 says 'connected' should reflect a live "
        "Google probe, not a sidecar feed."
    )


def test_email_with_only_imap_host_is_not_connected(monkeypatch):
    monkeypatch.setenv("FERAL_EMAIL_IMAP_HOST", "imap.gmail.com")
    from integrations.email import EmailIntegration

    email = EmailIntegration(oauth_manager=_StubOAuth())
    assert email.imap_configured is True
    assert email.connected is False


# ──────────────────────────────────────────────────────────────────────
# probe_connected() round-trip
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_connected_calls_registered_probe(monkeypatch):
    """``probe_connected`` should call the registered probe with
    ``force=True`` and propagate the resulting ``ok`` value."""
    import time

    import security.probe as probe_mod
    from integrations import _probe_status
    from integrations.spotify import SpotifyIntegration

    calls: list[tuple[str, bool]] = []

    async def fake_probe(provider_id, *, vault=None, force=False):
        calls.append((provider_id, force))
        return probe_mod.ProbeResult(
            provider=provider_id,
            ok=True,
            status_code=200,
            reason="ok",
            detail="",
            probed_at=time.time(),
            latency_ms=12.0,
        )

    monkeypatch.setattr("security.probe.probe", fake_probe)

    spotify = SpotifyIntegration(oauth_manager=_StubOAuth({"spotify"}))
    ok = await spotify.probe_connected()
    assert ok is True
    assert calls == [("spotify", True)]
    # And the cache was updated:
    assert _probe_status.latest("spotify") is True


# ──────────────────────────────────────────────────────────────────────
# Probe registry coverage
# ──────────────────────────────────────────────────────────────────────


def test_probe_registry_covers_all_lane10_providers():
    """Lane 10 must register probes for every integration whose
    ``connected`` flag now consults the cache. Without these registrations
    ``probe_connected`` would silently fall through to the in-memory
    fallback and ``finding 19`` would still be open."""
    from security.probe import registered_probe_ids

    ids = set(registered_probe_ids())
    required = {
        "google", "notion", "spotify", "microsoft", "whoop", "oura",
        "home_assistant", "telegram", "slack", "discord",
    }
    missing = required - ids
    assert not missing, f"Missing probes: {missing}"
