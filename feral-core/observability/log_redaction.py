"""Keep credentials out of the brain's log files.

Why this exists. ``api/server.py`` configures the ROOT logger at INFO, and
``httpx`` logs one line per request at INFO with the full URL. Two of the
integrations put credentials in the URL itself: the Telegram Bot API path
is ``https://api.telegram.org/bot<token>/...`` (channels/base.py,
integrations/messaging.py, security/probe.py) and the Gemini REST API is
addressed as ``...?key=<api_key>`` (providers/gemini_provider.py,
security/probe.py). The operator's ``~/.feral/logs/brain.err`` was found
to carry the Telegram bot token on 5,983 lines and the Gemini key on
1,857 lines, in a file created mode 0644.

Two layers, because either one alone leaves a hole:

* :func:`quiet_noisy_http_loggers` drops ``httpx`` / ``httpcore`` /
  ``websockets`` to WARNING. That removes the per-request INFO line, the
  bulk of the leak, but a WARNING or an exception traceback from those
  same libraries still carries the URL.
* :class:`SecretRedactingFilter` rewrites every record that passes a
  root handler, whatever logger produced it and whatever its level. It
  formats the message first (so a secret arriving through ``%s`` args is
  caught), redacts, and then clears ``record.args`` so the formatter does
  not re-interpolate the raw values. Exception text and stack text are
  redacted the same way.

The filter goes on the handlers rather than on a logger: a filter on the
root logger is not consulted for records that propagate up from child
loggers, a filter on the root handlers sees all of them.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

REDACTED = "<redacted>"

# Telegram Bot API path segment: ``/bot123456:AAH...``. The token is
# ``<numeric id>:<35 url-safe chars>`` and the ``/bot`` prefix is fixed by
# Telegram, so the shape is unambiguous.
_BOT_TOKEN_RE = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+")

# Credentials passed as a query parameter. ``key`` is what Google's REST
# APIs (Gemini) use; the others are common enough that catching them costs
# nothing. The group keeps the ``?key=`` so the log line stays readable.
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:key|token|access_token|api_key)=)[^&\s\"']+",
    re.IGNORECASE,
)

# ``Authorization: Bearer <token>`` as it appears in a dumped header set
# (``httpx`` request/response dumps, debug prints of ``headers=``).
_BEARER_RE = re.compile(
    r"(Authorization[\"']?\s*[:=]\s*[\"']?Bearer\s+)[^\s\"',}]+",
    re.IGNORECASE,
)

# Loggers whose INFO output is one line per request with the full URL.
NOISY_HTTP_LOGGERS: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "websockets.client",
    "websockets.server",
)


def redact(text: str) -> str:
    """Return ``text`` with every recognised credential replaced."""
    if not text:
        return text
    out = _BOT_TOKEN_RE.sub("/bot" + REDACTED, text)
    out = _QUERY_SECRET_RE.sub(r"\g<1>" + REDACTED, out)
    out = _BEARER_RE.sub(r"\g<1>" + REDACTED, out)
    return out


class SecretRedactingFilter(logging.Filter):
    """Redact credentials from a record before any handler formats it.

    Formats the message first so a secret arriving through ``record.args``
    is caught, then clears ``args`` so the formatter does not put the raw
    values back. A record whose ``args`` do not match its format string
    (a caller bug that ``logging`` would otherwise report on stderr) is
    left untouched rather than turned into a second error here.
    """

    _exc_formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003, stdlib name
        try:
            message = record.getMessage()
        except Exception:
            # Mismatched args. Leave the record for logging's own
            # handleError path; there is nothing safe to redact against.
            return True
        redacted = redact(message)
        if redacted != message or record.args:
            record.msg = redacted
            record.args = ()
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = self._exc_formatter.formatException(record.exc_info)
            except Exception:
                record.exc_text = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def _has_redaction(handler: logging.Handler) -> bool:
    return any(isinstance(f, SecretRedactingFilter) for f in handler.filters)


def install_log_redaction(
    logger_names: Iterable[str] = ("", "uvicorn", "uvicorn.error", "uvicorn.access"),
) -> int:
    """Attach :class:`SecretRedactingFilter` to every handler of the named
    loggers (root by default, plus uvicorn's, which do not propagate).

    Idempotent. Returns the number of handlers that gained the filter.
    Safe to call twice: once at import, when only the root handler from
    ``basicConfig`` exists, and again from the FastAPI startup hook, after
    ``uvicorn.run`` has installed its own non-propagating handlers.
    """
    added = 0
    for name in logger_names:
        for handler in logging.getLogger(name).handlers:
            if not _has_redaction(handler):
                handler.addFilter(SecretRedactingFilter())
                added += 1
    return added


def quiet_noisy_http_loggers(level: int = logging.WARNING) -> None:
    """Raise the per-request HTTP client loggers to ``level``.

    ``httpx`` logs ``HTTP Request: GET <full url> "HTTP/1.1 200 OK"`` at
    INFO for every call, and the Telegram and Gemini URLs carry the
    credential. WARNING keeps connection errors and drops the request log.
    """
    for name in NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(level)


def configure_brain_logging(
    level: int = logging.INFO,
    fmt: str = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    *,
    force: Optional[bool] = None,
) -> None:
    """The brain's logging setup: ``basicConfig`` plus both redaction layers.

    Replaces the bare ``logging.basicConfig(...)`` call that used to sit at
    the top of ``api/server.py``. Kept as a function so a test can run it
    against a fresh logging tree without importing the FastAPI app.
    """
    kwargs = {"level": level, "format": fmt}
    if force is not None:
        kwargs["force"] = force
    logging.basicConfig(**kwargs)
    quiet_noisy_http_loggers()
    install_log_redaction()
