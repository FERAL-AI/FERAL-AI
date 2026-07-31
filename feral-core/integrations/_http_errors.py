"""Shared HTTP error rendering for the integration clients.

``str(httpx.HTTPStatusError)`` carries the status line and the request
URL — never the response body. Google puts the only actionable part of
a failure *in* the body: ``ACCESS_TOKEN_SCOPE_INSUFFICIENT`` (the
consent screen granted fewer scopes than the integration calls),
``rateLimitExceeded`` (quota), ``invalid_grant`` (refresh token revoked
or the clock skewed). Handlers that returned ``str(e)`` made those three
indistinguishable from each other and from a generic 403.

``integrations/oauth_manager.py`` already captured
``exc.response.text[:400]`` on the token-exchange path; these helpers are
that behaviour lifted out so Gmail, Calendar, Drive, and Contacts report
the same way.
"""

from __future__ import annotations

import httpx

# Long enough for a Google JSON error envelope (code + message + the
# ``reason`` detail), short enough that a stray HTML error page doesn't
# flood the operator's UI or the logs.
DETAIL_LIMIT = 400


def response_excerpt(response: httpx.Response, *, limit: int = DETAIL_LIMIT) -> str:
    """Whitespace-normalised head of a response body, ``limit`` chars."""
    return " ".join((response.text or "").split())[:limit]


def http_error_detail(exc: BaseException, *, limit: int = DETAIL_LIMIT) -> str:
    """Render ``exc`` for an operator-facing ``error`` field.

    For an httpx status error this is ``"HTTP <code>: <body excerpt>"``;
    for anything else (timeouts, DNS, connection resets) it is the
    exception's own message, which already names the cause.
    """
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        body = response_excerpt(exc.response, limit=limit)
        status = f"HTTP {exc.response.status_code}"
        return f"{status}: {body}" if body else status
    return str(exc)
