"""audit-r14 / lane-07 () — `feral integrations` connect / list /
disconnect.

Covers the three connect flows mandated by parent ack:

* Gmail App Password (R-PROD-001) — 16-char client-side validation
  (parent ack reminder #2), IMAP+SMTP probe, vault persistence.
* OAuth self-serve (R-PROD-002) — ``--self-hosted`` flag prompts for
  client_id/secret, persists to vault, reuses ``OAuthManager``.
* Home Assistant long-lived token — REST probe, vault persistence.

Plus the table renderer + disconnect cleanup.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def stub_vault(monkeypatch):
    """In-memory vault matching the BlindVault.set_credential /
    get_credential / remove surface used by integration_commands."""
    class _StubVault:
        def __init__(self):
            self.creds: dict[str, str] = {}

        def set_credential(self, key, value):
            self.creds[key] = value

        def get_credential(self, key):
            return self.creds.get(key)

        def remove(self, key, *, removed_by="user"):
            return self.creds.pop(key, None) is not None

        # OAuthManager calls these too — implement the bare minimum.
        def retrieve(self, key, *, requester="oauth_manager"):
            return self.creds.get(key)

        def store(self, key, value, *, requester="oauth_manager"):
            self.creds[key] = value

        def list_keys(self):
            return list(self.creds.keys())

    stub = _StubVault()
    monkeypatch.setattr("security.vault.get_vault", lambda: stub)
    return stub


# ----------------------------------------------------------------------
# Gmail App Password (R-PROD-001)
# ----------------------------------------------------------------------


class TestGmailAppPassword:
    def test_app_password_format_validated_16_chars(
        self, stub_vault, monkeypatch, capsys,
    ):
        """Client-side App Password format check (parent ack #2): 16
        lowercase letters, with optional 4-char-group spaces. Reject
        on first try, accept on second."""
        from cli import integration_commands as ic

        # Simulate user inputs in order: yes 2FA, gmail addr, bad pwd, good pwd.
        monkeypatch.setattr(ic.ui_kit, "confirm", lambda *a, **kw: True)
        monkeypatch.setattr(ic.ui_kit, "text", lambda *a, **kw: "alice@gmail.com")

        passwords = iter(["short", "abcd efgh ijkl mnop"])
        monkeypatch.setattr(ic.ui_kit, "password", lambda *a, **kw: next(passwords))

        # Mock IMAP + SMTP probes to succeed.
        monkeypatch.setattr(ic, "_probe_imap", lambda *a, **kw: (True, "ok"))
        monkeypatch.setattr(ic, "_probe_smtp", lambda *a, **kw: (True, "ok"))

        rc = ic._connect_gmail_app_password()
        out = capsys.readouterr().out

        assert rc == 0
        # First attempt rejected with format error.
        assert "doesn't match the App Password format" in out
        # Final success message.
        assert "Gmail connected" in out
        # Spaces in the input MUST be stripped before storage.
        assert stub_vault.creds["FERAL_EMAIL_IMAP_PASS"] == "abcdefghijklmnop"
        assert stub_vault.creds["FERAL_EMAIL_IMAP_USER"] == "alice@gmail.com"
        assert stub_vault.creds["FERAL_EMAIL_IMAP_HOST"] == "imap.gmail.com"

    def test_gmail_address_must_be_valid_domain(
        self, stub_vault, monkeypatch, capsys,
    ):
        """The address is constrained to gmail.com / googlemail.com so
        the operator can't accidentally pour Outlook credentials into
        the IMAP probe."""
        from cli import integration_commands as ic

        monkeypatch.setattr(ic.ui_kit, "confirm", lambda *a, **kw: True)
        addresses = iter(["alice@example.com", "alice@gmail.com"])
        monkeypatch.setattr(ic.ui_kit, "text", lambda *a, **kw: next(addresses))
        monkeypatch.setattr(ic.ui_kit, "password", lambda *a, **kw: "abcdefghijklmnop")
        monkeypatch.setattr(ic, "_probe_imap", lambda *a, **kw: (True, "ok"))
        monkeypatch.setattr(ic, "_probe_smtp", lambda *a, **kw: (True, "ok"))

        rc = ic._connect_gmail_app_password()
        out = capsys.readouterr().out
        assert rc == 0
        assert "doesn't look like a gmail" in out
        assert stub_vault.creds["FERAL_EMAIL_IMAP_USER"] == "alice@gmail.com"

    def test_imap_probe_failure_does_not_persist(
        self, stub_vault, monkeypatch, capsys,
    ):
        """If the IMAP login fails, the vault MUST stay empty. Operators
        rely on this: a half-saved credential set is worse than nothing."""
        from cli import integration_commands as ic

        monkeypatch.setattr(ic.ui_kit, "confirm", lambda *a, **kw: True)
        monkeypatch.setattr(ic.ui_kit, "text", lambda *a, **kw: "alice@gmail.com")
        monkeypatch.setattr(ic.ui_kit, "password", lambda *a, **kw: "abcdefghijklmnop")
        monkeypatch.setattr(
            ic, "_probe_imap",
            lambda *a, **kw: (False, "[AUTHENTICATIONFAILED] Invalid credentials"),
        )

        rc = ic._connect_gmail_app_password()
        out = capsys.readouterr().out

        assert rc == 1
        assert "IMAP probe failed" in out
        assert "credentials are NOT saved" in out
        assert stub_vault.creds == {}

    def test_2fa_not_confirmed_aborts(
        self, stub_vault, monkeypatch, capsys,
    ):
        from cli import integration_commands as ic

        monkeypatch.setattr(ic.ui_kit, "confirm", lambda *a, **kw: False)
        rc = ic._connect_gmail_app_password()
        assert rc == 1
        assert stub_vault.creds == {}
        out = capsys.readouterr().out
        assert "Enable 2FA first" in out


# ----------------------------------------------------------------------
# Home Assistant token
# ----------------------------------------------------------------------


class TestHomeAssistantToken:
    def test_ha_probe_ok_persists_url_and_token(
        self, stub_vault, monkeypatch, capsys,
    ):
        from cli import integration_commands as ic

        monkeypatch.setattr(ic.ui_kit, "text", lambda *a, **kw: "http://ha.local:8123")
        monkeypatch.setattr(ic.ui_kit, "password", lambda *a, **kw: "ha-llt-token-xyz")
        monkeypatch.setattr(ic, "_probe_home_assistant", lambda *a, **kw: (True, "HTTP 200"))

        rc = ic._connect_home_assistant_token()
        out = capsys.readouterr().out

        assert rc == 0
        assert "Home Assistant connected" in out
        assert stub_vault.creds["HOMEASSISTANT_URL"] == "http://ha.local:8123"
        assert stub_vault.creds["HOMEASSISTANT_TOKEN"] == "ha-llt-token-xyz"

    def test_ha_probe_failure_does_not_persist(
        self, stub_vault, monkeypatch, capsys,
    ):
        from cli import integration_commands as ic

        monkeypatch.setattr(ic.ui_kit, "text", lambda *a, **kw: "http://nope")
        monkeypatch.setattr(ic.ui_kit, "password", lambda *a, **kw: "bad-token")
        monkeypatch.setattr(ic, "_probe_home_assistant", lambda *a, **kw: (False, "401 unauthorized"))

        rc = ic._connect_home_assistant_token()
        out = capsys.readouterr().out

        assert rc == 1
        assert "Probe failed" in out
        assert stub_vault.creds == {}


# ----------------------------------------------------------------------
# OAuth self-serve (R-PROD-002)
# ----------------------------------------------------------------------


class TestOAuthSelfHosted:
    def test_self_hosted_persists_client_id_and_secret(
        self, stub_vault, monkeypatch, capsys,
    ):
        """``--self-hosted`` MUST write the user-supplied client_id +
        client_secret to the vault keys ``OAuthManager`` reads at
        construction. We don't assert the full OAuth round-trip
        (would require the brain running) — just the persistence."""
        from cli import integration_commands as ic

        monkeypatch.setattr(ic.ui_kit, "text", lambda *a, **kw: "test-client-id")
        monkeypatch.setattr(ic.ui_kit, "password", lambda *a, **kw: "test-client-secret")

        # Stub OAuthManager so we don't actually open a browser or
        # spin the polling loop.
        from integrations import oauth_manager as oauth_mod

        class _FakeMgr:
            def __init__(self, *_, **__):
                pass

            def build_authorize_response(self, pid):
                return {
                    "success": True, "provider": pid,
                    "url": f"https://auth.example/{pid}", "state": "x" * 32,
                    "scopes": [],
                }

            def list_providers(self):
                # Pretend the OAuth dance completed instantly so the
                # poll loop returns success on first iteration.
                return [
                    {"id": "google", "connected": True, "name": "Google",
                     "auth_type": "oauth2", "has_client_id": True,
                     "setup_status": "ready", "setup_doc_url": "",
                     "setup_doc_summary": "", "scopes": []},
                ]

        monkeypatch.setattr(oauth_mod, "OAuthManager", _FakeMgr)
        monkeypatch.setattr("webbrowser.open", lambda *a, **kw: None)

        rc = ic._connect_oauth("google", self_hosted=True, no_browser=True)
        out = capsys.readouterr().out

        assert rc == 0
        # Client id + secret persisted to the vault under the keys
        # OAuthManager._VAULT_CREDENTIAL_KEYS reads.
        assert stub_vault.creds["GOOGLE_OAUTH_CLIENT_ID"] == "test-client-id"
        assert stub_vault.creds["GOOGLE_OAUTH_CLIENT_SECRET"] == "test-client-secret"
        assert "google connected" in out

    def test_no_self_hosted_steers_to_self_hosted_when_no_client_id(
        self, stub_vault, monkeypatch, capsys,
    ):
        """Without ``--self-hosted`` and no first-party client baked in,
        we MUST refuse and tell the operator to add ``--self-hosted``."""
        from cli import integration_commands as ic
        from integrations import oauth_manager as oauth_mod

        class _StubMgr:
            def __init__(self, *_, **__):
                pass

            def setup_status(self, pid):
                return "provider_setup_required"

        monkeypatch.setattr(oauth_mod, "OAuthManager", _StubMgr)

        rc = ic.cmd_integrations_connect(
            integration_id="notion", self_hosted=False, no_browser=True,
        )
        out = capsys.readouterr().out

        assert rc == 2
        assert "--self-hosted" in out


# ----------------------------------------------------------------------
# list + disconnect
# ----------------------------------------------------------------------


class TestIntegrationsListDisconnect:
    def test_list_renders_without_brain(self, stub_vault, capsys):
        """``feral integrations list`` MUST work pure-local — no brain
        round-trip. Reads OAuthManager.list_providers (vault) +
        synthesises a Gmail row."""
        from cli.integration_commands import cmd_integrations_list

        rc = cmd_integrations_list()
        out = capsys.readouterr().out
        assert rc == 0
        assert "Integrations" in out
        # Gmail synthetic row appears.
        assert "Gmail" in out

    def test_disconnect_gmail_clears_imap_and_smtp_keys(
        self, stub_vault, monkeypatch, capsys,
    ):
        from cli.integration_commands import cmd_integrations_disconnect

        # Plant gmail creds first.
        for k, v in {
            "FERAL_EMAIL_IMAP_HOST": "imap.gmail.com",
            "FERAL_EMAIL_IMAP_USER": "a@gmail.com",
            "FERAL_EMAIL_IMAP_PASS": "secret",
            "FERAL_EMAIL_SMTP_USER": "a@gmail.com",
            "FERAL_EMAIL_SMTP_PASS": "secret",
        }.items():
            stub_vault.set_credential(k, v)

        rc = cmd_integrations_disconnect(integration_id="gmail", skip_confirm=True)
        out = capsys.readouterr().out
        assert rc == 0
        assert "FERAL_EMAIL_IMAP_USER" not in stub_vault.creds
        assert "FERAL_EMAIL_IMAP_PASS" not in stub_vault.creds
        assert "Removed" in out

    def test_integrations_subcommand_classified_pure_local(self):
        from cli import main as cli_main

        assert "integrations" in cli_main.PURE_LOCAL_SUBCOMMANDS
