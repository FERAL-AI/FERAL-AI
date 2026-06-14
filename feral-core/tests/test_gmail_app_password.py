"""Gmail App Password (IMAP/SMTP) integration wiring.

Covers the path that was previously dead: saving a Gmail address +
16-char App Password in Settings must persist to the vault and make the
EmailIntegration report connected / route reads through IMAP — with no
brain restart and no Google OAuth.
"""

from __future__ import annotations

import json

import pytest

from integrations import _probe_status
from integrations.email import EMAIL_CRED_VAULT_KEY, EmailIntegration


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """`EmailIntegration.connected` falls back to the module-global probe
    cache for "google"; clear it so these assertions are independent of
    any earlier test that primed the cache."""
    _probe_status.clear()
    yield
    _probe_status.clear()


class _FakeVault:
    def __init__(self):
        self.store_calls: dict[str, str] = {}

    def store(self, key_name: str, value: str, stored_by: str = "user") -> None:
        self.store_calls[key_name] = value

    def retrieve(self, key_name: str, requester: str = "executor"):
        return self.store_calls.get(key_name)

    def remove(self, key_name: str, removed_by: str = "user") -> bool:
        return self.store_calls.pop(key_name, None) is not None


class _FakeOAuth:
    def __init__(self, vault):
        self._vault = vault

    def is_connected(self, provider_id: str) -> bool:
        return False


def _make_email():
    vault = _FakeVault()
    oauth = _FakeOAuth(vault)
    return EmailIntegration(oauth_manager=oauth), vault


def test_unconfigured_is_not_connected():
    email, _ = _make_email()
    assert email.connected is False
    assert email.imap_configured is False
    assert email._use_imap is False


def test_store_app_password_persists_and_connects():
    email, vault = _make_email()
    email.store_app_password("me@gmail.com", "abcd efgh ijkl mnop")

    # Persisted to the vault under the dedicated key, spaces stripped.
    raw = vault.retrieve(EMAIL_CRED_VAULT_KEY)
    assert raw is not None
    data = json.loads(raw)
    assert data["address"] == "me@gmail.com"
    assert data["app_password"] == "abcdefghijklmnop"

    # Now reports connected and routes through the IMAP/SMTP path.
    assert email.connected is True
    assert email.imap_configured is True
    assert email._use_imap is True

    host, port, user, password = email._resolve_imap()
    assert (host, port, user) == ("imap.gmail.com", 993, "me@gmail.com")
    assert password == "abcdefghijklmnop"


def test_reload_after_save_without_restart():
    """A second EmailIntegration on the same vault sees saved creds."""
    email1, vault = _make_email()
    email1.store_app_password("me@gmail.com", "abcdefghijklmnop")

    oauth2 = _FakeOAuth(vault)
    email2 = EmailIntegration(oauth_manager=oauth2)
    assert email2.connected is True
    assert email2._resolve_imap()[2] == "me@gmail.com"


def test_clear_app_password_disconnects():
    email, _ = _make_email()
    email.store_app_password("me@gmail.com", "abcdefghijklmnop")
    assert email.connected is True
    email.clear_app_password()
    assert email.connected is False
    assert email._resolve_imap() is None


def test_oauth_takes_priority_over_app_password():
    """When Google OAuth is live, use the full Gmail API, not IMAP."""
    vault = _FakeVault()

    class _ConnectedOAuth(_FakeOAuth):
        def is_connected(self, provider_id: str) -> bool:
            return provider_id == "google"

    email = EmailIntegration(oauth_manager=_ConnectedOAuth(vault))
    email.store_app_password("me@gmail.com", "abcdefghijklmnop")
    assert email._use_imap is False
