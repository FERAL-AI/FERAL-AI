""" regression — v2 Settings "Save & switch" must NOT leak a
plaintext ``credentials.json`` to disk.

v2026.5.0 shipped a P0 where every credential save (triggered by the v2
Settings page's "Save & switch" button, wired to ``POST
/api/config/credentials``) produced *two* on-disk artefacts: the 
encrypted ``~/.feral/credentials.enc`` AND a plaintext
``~/.feral/credentials.json`` written by ``ConfigLoader.save_credentials``.
The maintainer's live log proved it:

    [feral.vault] Credential stored: credentials.OPENAI_API_KEY
    [feral.config] Credentials saved to /Users/…/.feral/credentials.json

 rewires ``ConfigLoader.save_credentials`` to route through the
BlindVault and never touch the plaintext file. This test boots the real
FastAPI app with a real ``ConfigLoader`` + real ``BlindVault`` inside an
isolated ``FERAL_HOME`` (no mocks on the write path), posts a credential,
and asserts:

    1. ``~/.feral/credentials.json`` does NOT exist after the POST.
    2. ``~/.feral/credentials.enc`` DOES exist.
    3. The route returns ``persisted_to_vault`` containing the key and
       ``persisted_to_credentials_json == True`` (the flag is a legacy
       name —  it means "routed to vault via ConfigLoader",
       which is exactly what we want: no disk-plaintext, success=True).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.no_auto_feral_home


def _feral_home(tmp_path):
    home = tmp_path / ".feral"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _fresh_state(feral_home):
    """Build an ``api.state.state`` stand-in with a REAL ConfigLoader +
    REAL BlindVault pointed at ``feral_home``. Everything else the routes
    touch is a cheap mock."""
    from config.loader import ConfigLoader
    from security.vault import BlindVault

    config = ConfigLoader()
    config.user_home = feral_home
    config.data_home = feral_home
    config.discover()

    vault = BlindVault(vault_path=str(feral_home / "credentials.json"))

    mock = MagicMock()
    mock.config = config
    mock.vault = vault
    mock.provider_catalog = None
    mock.orchestrator = None
    mock.channel_manager = None
    return mock


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = _feral_home(tmp_path)
    monkeypatch.setenv("FERAL_HOME", str(home))
    mock = _fresh_state(home)
    with patch("api.state.state", mock), \
         patch("api.routes.config.state", mock), \
         patch("api.routes.llm.state", mock):
        from api.server import app
        yield TestClient(app, raise_server_exceptions=False), home


def test_save_credentials_never_writes_plaintext_json(client):
    """POST /api/config/credentials — Settings v2 "Save & switch"."""
    c, home = client
    legacy_plaintext = home / "credentials.json"
    encrypted = home / "credentials.enc"

    assert not legacy_plaintext.exists()

    r = c.post("/api/config/credentials", json={"OPENAI_API_KEY": "sk-w24b-regress"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "OPENAI_API_KEY" in body["keys_saved"]
    assert "OPENAI_API_KEY" in body["persisted_to_vault"]

    assert not legacy_plaintext.exists(), (
        f"W24b regression: plaintext credentials.json was written to {legacy_plaintext}. "
        f"Contents: {legacy_plaintext.read_text() if legacy_plaintext.exists() else '<none>'}"
    )
    assert encrypted.exists(), (
        "Encrypted vault file credentials.enc is missing — the W9 vault "
        "did not persist the credential."
    )


def test_setup_complete_never_writes_plaintext_json(client):
    """POST /api/setup/complete — first-boot wizard submits a credential
    via a different route; that path also went through
    ``ConfigLoader.save_credentials`` and must stay clean."""
    c, home = client
    legacy_plaintext = home / "credentials.json"
    encrypted = home / "credentials.enc"

    r = c.post(
        "/api/setup/complete",
        json={
            "settings": {"llm": {"provider": "openai", "model": "gpt-4o-mini"}},
            "credentials": {"OPENAI_API_KEY": "sk-setup-regress"},
            "identity": {},
        },
    )
    assert r.status_code == 200, r.text

    assert not legacy_plaintext.exists(), (
        "W24b regression: /api/setup/complete wrote plaintext credentials.json"
    )
    assert encrypted.exists()


def test_configloader_direct_call_never_writes_plaintext(tmp_path, monkeypatch):
    """Belt-and-braces: even a direct ``ConfigLoader.save_credentials``
    call (used by CLI key commands, tests, legacy importers) must obey
    the invariant."""
    home = _feral_home(tmp_path)
    monkeypatch.setenv("FERAL_HOME", str(home))

    from config.loader import ConfigLoader

    loader = ConfigLoader()
    loader.user_home = home
    loader.discover()
    loader.save_credentials({"OPENAI_API_KEY": "sk-direct-regress"})

    assert not (home / "credentials.json").exists()
    assert (home / "credentials.enc").exists()
    assert loader.get_credential("OPENAI_API_KEY") == "sk-direct-regress"
