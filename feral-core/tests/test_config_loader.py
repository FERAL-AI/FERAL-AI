"""Tests for ``config/loader.py`` credential-loading behaviour.

Broader ConfigLoader coverage lives in ``test_config.py``. This
module focuses narrowly on the credential surface the boot-time
hydration path in ``api/state.py`` relies on:

  * plaintext ``credentials.json`` is parsed into ``_credentials``
  * a missing file is a no-op (fresh install)
  * a corrupt file is survivable — the loader logs and continues so
    the vault-fallback path in ``BrainState._load_stored_credentials``
    still has somewhere to land
  * env-var credentials round-trip through ``_credentials`` into
    ``export_as_env`` so the brain can re-hydrate ``os.environ`` on
    restart
"""

from __future__ import annotations

import json
import os

import pytest

from config.loader import ConfigLoader

pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture(autouse=True)
def _clean_feral_env(monkeypatch):
    """Strip ambient provider keys and FERAL_* env vars so each test
    asserts on a predictable baseline."""
    for key in list(os.environ):
        if key.startswith("FERAL_") or key in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "GROQ_API_KEY",
            "GEMINI_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)


class TestCredentialFileLoading:
    def test_missing_credentials_file_leaves_empty_dict(self, tmp_path):
        loader = ConfigLoader(project_dir=str(tmp_path))
        loader.user_home = tmp_path / ".feral"
        loader.discover()
        assert loader.get_credential("OPENAI_API_KEY") == ""
        assert loader.credentials.get("OPENAI_API_KEY") in (None, "")

    def test_partial_credentials_file_is_parsed(self, tmp_path):
        user_home = tmp_path / ".feral"
        user_home.mkdir()
        (user_home / "credentials.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-partial"})
        )

        loader = ConfigLoader(project_dir=str(tmp_path))
        loader.user_home = user_home
        loader.discover()

        assert loader.get_credential("OPENAI_API_KEY") == "sk-partial"
        assert loader.get_credential("ANTHROPIC_API_KEY") == ""

    def test_corrupt_credentials_file_does_not_raise(self, tmp_path, caplog):
        user_home = tmp_path / ".feral"
        user_home.mkdir()
        (user_home / "credentials.json").write_text("{not json,,")

        loader = ConfigLoader(project_dir=str(tmp_path))
        loader.user_home = user_home
        # v2026.5.37 (Lane 03): ConfigLoader is now vault-first; legacy plaintext
        # credentials.json is no longer read directly. The corrupt file is detected
        # by vault auto-migration (logged under feral.vault) AND ConfigLoader emits
        # a deprecation warning under feral.config. Either signal is acceptable —
        # what matters is that the loader does not raise and credentials remain
        # empty.
        with caplog.at_level("WARNING"):
            loader.discover()

        assert loader.get_credential("OPENAI_API_KEY") == ""
        assert any(
            (
                "Deprecated plaintext credentials.json" in rec.getMessage()
                or "vault.legacy_corrupt" in rec.getMessage()
                or "Failed to load credentials" in rec.getMessage()
            )
            for rec in caplog.records
        ), "expected a corrupt-credentials warning (deprecation or vault quarantine or load failure)"

    def test_env_api_key_is_captured_in_credentials(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")

        loader = ConfigLoader(project_dir=str(tmp_path))
        loader.user_home = tmp_path / ".feral"
        loader.discover()

        assert loader.get_credential("OPENAI_API_KEY") == "sk-from-env"


class TestExportAsEnv:
    def test_credentials_round_trip_through_export(self, tmp_path):
        user_home = tmp_path / ".feral"
        user_home.mkdir()
        (user_home / "credentials.json").write_text(
            json.dumps(
                {
                    "OPENAI_API_KEY": "sk-round-trip",
                    "ANTHROPIC_API_KEY": "sk-ant-round",
                }
            )
        )

        loader = ConfigLoader(project_dir=str(tmp_path))
        loader.user_home = user_home
        loader.discover()
        env = loader.export_as_env()

        assert env["OPENAI_API_KEY"] == "sk-round-trip"
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-round"

    def test_missing_credential_is_absent_from_export(self, tmp_path):
        loader = ConfigLoader(project_dir=str(tmp_path))
        loader.user_home = tmp_path / ".feral"
        loader.discover()
        env = loader.export_as_env()

        assert "OPENAI_API_KEY" not in env
        assert "ANTHROPIC_API_KEY" not in env
