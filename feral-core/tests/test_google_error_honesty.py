"""Google integration error-reporting regressions.

D10  ``CalendarIntegration._fetch_ics_events`` caught every fetch
     exception and returned ``[]``. Callers treat ``[]`` as a valid
     empty calendar and return ``success: True``, so a 404, a DNS
     failure, or a timeout on the ICS feed rendered as a confident
     "you have no events today".

D12  Every Google handler collapsed to ``{"success": False, "error":
     str(e)}`` over an httpx ``raise_for_status()``. ``str()`` of an
     ``HTTPStatusError`` carries the status line and nothing else, so
     the response body — where Google puts
     ``ACCESS_TOKEN_SCOPE_INSUFFICIENT``, ``rateLimitExceeded``,
     ``invalid_grant`` — was discarded. Scope, quota, and
     revoked-consent failures were indistinguishable to the operator.

Out of scope but confirmed while writing these: ``_ics_dt`` slices
``raw[:len(fmt) + 4]``, which truncates a ``Z``-suffixed stamp
(``20990101T090000Z`` → ``+00:00`` makes it 21 chars against a 20-char
slice) and returns ``None``. Every event in a UTC ICS feed is therefore
filtered out. Left alone here so this change stays scoped to the fetch
path; the fixtures below use floating stamps to avoid coupling to it.
"""

from __future__ import annotations

import httpx
import pytest

from integrations._http_errors import http_error_detail, response_excerpt
from integrations.calendar import CalendarIntegration
from integrations.email import EmailIntegration
from integrations.google_contacts import GoogleContactsIntegration
from integrations.google_drive import GoogleDriveIntegration

SCOPE_ERROR_BODY = (
    '{"error":{"code":403,"message":"Request had insufficient '
    'authentication scopes.","status":"PERMISSION_DENIED","details":'
    '[{"reason":"ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}'
)


class StubOAuth:
    """OAuthManager stand-in that hands out a token for ``google``."""

    def __init__(self, connected: bool = True) -> None:
        self._connected = connected

    def is_connected(self, provider_id: str) -> bool:
        return self._connected

    async def get_token(self, provider_id: str) -> str:
        return "access-token"


def _status_error(status_code: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://www.googleapis.com/x")
    response = httpx.Response(status_code, text=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


# ── D12: the shared helper ───────────────────────────────────────────


def test_http_error_detail_carries_the_response_body():
    detail = http_error_detail(_status_error(403, SCOPE_ERROR_BODY))
    assert "HTTP 403" in detail
    assert "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in detail


def test_http_error_detail_truncates_and_falls_back():
    detail = http_error_detail(_status_error(429, "x" * 5000), limit=100)
    assert len(detail) < 200
    assert http_error_detail(httpx.ConnectTimeout("timed out")) == "timed out"


def test_response_excerpt_normalises_whitespace():
    request = httpx.Request("POST", "https://oauth2.googleapis.com/token")
    resp = httpx.Response(400, text="  invalid_grant\n\n  bad refresh  ",
                          request=request)
    assert response_excerpt(resp) == "invalid_grant bad refresh"


# ── D12: applied across the Google modules ───────────────────────────


async def test_calendar_surfaces_google_scope_error(monkeypatch):
    cal = CalendarIntegration(oauth_manager=StubOAuth())

    async def fake_get(*args, **kwargs):
        raise _status_error(403, SCOPE_ERROR_BODY)

    monkeypatch.setattr(cal._http, "get", fake_get)
    result = await cal.list_events()
    assert result["success"] is False
    assert "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in result["error"]


async def test_email_surfaces_google_scope_error(monkeypatch):
    mail = EmailIntegration(oauth_manager=StubOAuth())
    monkeypatch.setattr(EmailIntegration, "_use_imap", property(lambda self: False))

    async def fake_get(*args, **kwargs):
        raise _status_error(403, SCOPE_ERROR_BODY)

    monkeypatch.setattr(mail._http, "get", fake_get)
    result = await mail.list_inbox()
    assert result["success"] is False
    assert "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in result["error"]


async def test_drive_surfaces_google_quota_error(monkeypatch):
    drive = GoogleDriveIntegration(oauth_manager=StubOAuth())

    async def fake_get(*args, **kwargs):
        raise _status_error(429, '{"error":{"errors":[{"reason":'
                                 '"rateLimitExceeded"}]}}')

    monkeypatch.setattr(drive._http, "get", fake_get)
    result = await drive.list_files()
    assert result["success"] is False
    assert "rateLimitExceeded" in result["error"]


async def test_contacts_surfaces_google_scope_error(monkeypatch):
    contacts = GoogleContactsIntegration(oauth_manager=StubOAuth())

    async def fake_get(*args, **kwargs):
        raise _status_error(403, SCOPE_ERROR_BODY)

    monkeypatch.setattr(contacts._http, "get", fake_get)
    result = await contacts.list_contacts()
    assert result["success"] is False
    assert "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in result["error"]


# ── D10: a broken ICS feed is not an empty calendar ──────────────────


@pytest.fixture
def ics_calendar(monkeypatch):
    monkeypatch.setenv("FERAL_CALENDAR_ICS", "https://example.com/feed.ics")
    cal = CalendarIntegration(oauth_manager=StubOAuth(connected=False))
    assert cal._use_ics is True
    return cal


def _break_ics(monkeypatch, exc: Exception):
    class BrokenClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url):
            raise exc

    monkeypatch.setattr("integrations.calendar.httpx.AsyncClient",
                        lambda **kwargs: BrokenClient())


@pytest.mark.parametrize("endpoint", ["list_events", "get_today",
                                      "next_event", "search_events"])
async def test_ics_fetch_failure_is_not_an_empty_calendar(
    ics_calendar, monkeypatch, endpoint,
):
    _break_ics(monkeypatch, _status_error(404, "Not Found"))
    result = await getattr(ics_calendar, endpoint)()
    assert result["success"] is False
    assert "404" in result["error"]


async def test_ics_timeout_is_reported_as_a_failure(ics_calendar, monkeypatch):
    _break_ics(monkeypatch, httpx.ReadTimeout("feed timed out"))
    result = await ics_calendar.get_today()
    assert result["success"] is False
    assert "timed out" in result["error"]


async def test_ics_success_path_still_parses(ics_calendar, monkeypatch):
    feed = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:evt-1\r\n"
        "SUMMARY:Standup\r\n"
        # Floating (no trailing Z) — see the note in the module docstring
        # about ``_ics_dt`` truncating UTC-suffixed stamps.
        "DTSTART:20990101T090000\r\n"
        "DTEND:20990101T093000\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    class OkClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url):
            request = httpx.Request("GET", url)
            return httpx.Response(200, text=feed, request=request)

    monkeypatch.setattr("integrations.calendar.httpx.AsyncClient",
                        lambda **kwargs: OkClient())
    result = await ics_calendar.list_events(days_ahead=365 * 100)
    assert result["success"] is True
    assert [e["summary"] for e in result["data"]["events"]] == ["Standup"]
