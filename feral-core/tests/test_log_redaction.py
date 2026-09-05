"""Credentials must not reach the brain's log files.

``api/server.py`` set the root logger to INFO with a bare ``basicConfig``
and ``httpx`` logs every request URL at INFO. The Telegram Bot API path
(``/bot<token>/...``, channels/base.py, integrations/messaging.py,
security/probe.py) and the Gemini REST API (``?key=<api_key>``,
providers/gemini_provider.py, security/probe.py) carry the credential in
the URL. The operator's ``~/.feral/logs/brain.err`` had the bot token on
5,983 lines and the Gemini key on 1,857 lines, in a 0644 file.

The fake credentials below are shaped like the real ones and are not real.
"""

from __future__ import annotations

import io
import logging

import pytest

from observability.log_redaction import (
    NOISY_HTTP_LOGGERS,
    REDACTED,
    SecretRedactingFilter,
    configure_brain_logging,
    install_log_redaction,
    quiet_noisy_http_loggers,
    redact,
)

FAKE_BOT_TOKEN = "123456789:AAFakeTokenForTests_abcDEF-ghiJKL0123"
FAKE_API_KEY = "AIzaFakeKeyForTests0123456789abcdefghijk"
FAKE_BEARER = "sk-fake-bearer-token-for-tests-0123456789"


@pytest.fixture
def capture():
    """A private logger with one StreamHandler carrying the filter, so the
    assertions do not depend on pytest's own capture handlers."""
    logger = logging.getLogger("test.log_redaction")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(SecretRedactingFilter())
    logger.addHandler(handler)
    yield logger, stream
    logger.handlers.clear()


def test_redact_bot_token_in_url():
    out = redact(f"HTTP Request: POST https://api.telegram.org/bot{FAKE_BOT_TOKEN}/getUpdates")
    assert FAKE_BOT_TOKEN not in out
    assert f"/bot{REDACTED}/getUpdates" in out


def test_redact_query_key():
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={FAKE_API_KEY}&alt=json"
    out = redact(url)
    assert FAKE_API_KEY not in out
    assert f"?key={REDACTED}&alt=json" in out


@pytest.mark.parametrize("param", ["token", "access_token", "api_key", "KEY"])
def test_redact_other_query_secret_names(param):
    out = redact(f"GET https://x.test/a?b=1&{param}={FAKE_API_KEY} done")
    assert FAKE_API_KEY not in out
    assert f"{param}={REDACTED}" in out


def test_redact_bearer_header():
    out = redact(f"headers: {{'Authorization': 'Bearer {FAKE_BEARER}', 'Accept': '*/*'}}")
    assert FAKE_BEARER not in out
    assert REDACTED in out
    out2 = redact(f"Authorization: Bearer {FAKE_BEARER}")
    assert out2 == f"Authorization: Bearer {REDACTED}"


def test_filter_redacts_secret_arriving_through_args(capture):
    logger, stream = capture
    logger.info("HTTP Request: GET %s", f"https://api.telegram.org/bot{FAKE_BOT_TOKEN}/getMe")
    text = stream.getvalue()
    assert FAKE_BOT_TOKEN not in text
    assert REDACTED in text


def test_filter_redacts_secret_in_format_string(capture):
    logger, stream = capture
    logger.warning(f"probe failed for ?key={FAKE_API_KEY}")
    text = stream.getvalue()
    assert FAKE_API_KEY not in text
    assert f"?key={REDACTED}" in text


def test_filter_leaves_non_string_args_intact(capture):
    logger, stream = capture
    logger.info("synced %d ops in %.1fs for %s", 42, 1.5, {"peer": "p1"})
    assert "synced 42 ops in 1.5s for {'peer': 'p1'}" in stream.getvalue()


def test_filter_survives_mismatched_args(capture):
    logger, stream = capture
    # logging reports this on stderr through handleError; the filter must
    # not raise and must not swallow the record.
    record = logging.LogRecord("test.log_redaction", logging.INFO, __file__, 1, "%s %s", ("only-one",), None)
    assert SecretRedactingFilter().filter(record) is True


def test_filter_redacts_exception_text(capture):
    logger, stream = capture
    try:
        raise RuntimeError(f"connect failed: https://api.telegram.org/bot{FAKE_BOT_TOKEN}/getMe")
    except RuntimeError:
        logger.exception("request blew up")
    text = stream.getvalue()
    assert FAKE_BOT_TOKEN not in text
    assert "RuntimeError" in text
    assert REDACTED in text


def test_install_is_idempotent_on_root_handlers():
    root = logging.getLogger()
    before = list(root.handlers)
    probe = logging.NullHandler()
    root.addHandler(probe)
    try:
        install_log_redaction()
        install_log_redaction()
        assert sum(isinstance(f, SecretRedactingFilter) for f in probe.filters) == 1
    finally:
        root.removeHandler(probe)
        assert list(root.handlers) == before


def test_noisy_http_loggers_are_quieted():
    for name in NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)
    quiet_noisy_http_loggers()
    for name in ("httpx", "httpcore", "websockets.client", "websockets.server"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name


def test_configure_brain_logging_sets_httpx_to_warning_and_installs_filter():
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    configure_brain_logging(level=logging.INFO)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    root = logging.getLogger()
    assert root.handlers, "basicConfig installed no handler"
    assert all(
        any(isinstance(f, SecretRedactingFilter) for f in h.filters) for h in root.handlers
    )


def test_server_module_logging_setup_quiets_httpx():
    """The autouse conftest fixture imports ``api.server``; the level it
    leaves behind is what a running brain has."""
    import api.server  # noqa: F401, imported for its module-level logging setup

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
