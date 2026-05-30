"""A7 — tests for ``cli.setup.state.WizardState``.

Focuses on the credential persistence contract: every save MUST route
credentials through the W9 encrypted ``BlindVault`` and MUST NOT leave
a plaintext ``credentials.json`` on disk, even when the wizard is run
repeatedly or when a legacy pre-W9 plaintext file is present.
"""

from __future__ import annotations

import json

import pytest

from cli.setup.state import WizardState


@pytest.fixture
def wizard_home(tmp_path):
    home = tmp_path / ".feral"
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture(autouse=True)
def _reset_vault_singleton():
    from security.vault import reset_vault

    reset_vault()
    yield
    reset_vault()


# ── save() routes credentials through the encrypted vault ──────────────────


def test_save_writes_settings_and_routes_credentials_to_vault(wizard_home):
    state = WizardState(home=wizard_home)
    state.settings = {"llm": {"provider": "openai", "model": "gpt-4.1"}}
    state.credentials = {"OPENAI_API_KEY": "sk-state-test"}
    state.save()

    settings = json.loads((wizard_home / "settings.json").read_text())
    assert settings["llm"]["provider"] == "openai"
    # W7 (lane-07): save() persists settings + credentials but must NOT
    # mark setup complete — only the finish step's mark_complete() does
    # (closes finding 09's "quit-marks-complete" defect). This test used
    # to assert the opposite, contradicting that intentional design.
    assert settings.get("meta", {}).get("setup_complete") is not True

    assert not (wizard_home / "credentials.json").exists(), (
        "A7 regression: WizardState.save wrote a plaintext credentials.json"
    )
    assert (wizard_home / "credentials.enc").exists()

    from security.vault import BlindVault

    vault = BlindVault(vault_path=str(wizard_home / "credentials.json"))
    assert vault.retrieve("OPENAI_API_KEY") == "sk-state-test"


def test_save_never_writes_plaintext_for_any_key(wizard_home):
    """Belt-and-braces check: multiple credentials + identity still
    produce exactly zero plaintext credential artefacts."""
    state = WizardState(home=wizard_home)
    state.credentials = {
        "OPENAI_API_KEY": "sk-1",
        "ANTHROPIC_API_KEY": "sk-ant-2",
        "TAVILY_API_KEY": "tvly-3",
    }
    state.identity = {"name": "Tester"}
    state.save()

    assert not (wizard_home / "credentials.json").exists()
    assert (wizard_home / "credentials.enc").exists()
    assert (wizard_home / "identity.json").exists()


def test_save_skips_empty_values_without_writing_plaintext(wizard_home):
    """Blank fields (user hit Enter at the prompt) must not trigger any
    disk write — and critically, must not create a plaintext file."""
    state = WizardState(home=wizard_home)
    state.credentials = {"OPENAI_API_KEY": "", "EMPTY_KEY": ""}
    state.save()

    assert not (wizard_home / "credentials.json").exists()


def test_save_idempotent_no_plaintext_leak_across_runs(wizard_home):
    """Running save() twice (e.g. user re-runs `feral setup`) must keep
    the plaintext file absent throughout."""
    state = WizardState(home=wizard_home)
    state.credentials = {"OPENAI_API_KEY": "sk-a"}
    state.save()
    assert not (wizard_home / "credentials.json").exists()

    state.credentials["OPENAI_API_KEY"] = "sk-b"
    state.save()
    assert not (wizard_home / "credentials.json").exists()

    from security.vault import BlindVault, reset_vault

    reset_vault()
    vault = BlindVault(vault_path=str(wizard_home / "credentials.json"))
    assert vault.retrieve("OPENAI_API_KEY") == "sk-b"


# ── load() migrates legacy plaintext and keeps returning users whole ───────


def test_load_migrates_legacy_plaintext_credentials(wizard_home):
    """A pre-W9 install has ``credentials.json`` on disk. load() must
    surface those keys AND the subsequent save() must have removed the
    plaintext file (migrated to the encrypted vault + backup)."""
    legacy = wizard_home / "credentials.json"
    legacy.write_text(json.dumps({"OPENAI_API_KEY": "sk-legacy"}))

    state = WizardState.load(wizard_home)
    assert state.credentials["OPENAI_API_KEY"] == "sk-legacy"

    state.save()
    assert not legacy.exists(), "Legacy plaintext should have been removed by vault migration"
    assert (wizard_home / "credentials.enc").exists()
    assert (wizard_home / "credentials.json.bak.legacy").exists()


def test_load_returns_empty_on_fresh_install(wizard_home):
    state = WizardState.load(wizard_home)
    assert state.credentials == {}
    assert state.settings == {}
    assert state.identity == {}


def test_load_survives_corrupt_legacy_plaintext(wizard_home):
    """If the legacy file is malformed, the wizard must still boot with
    an empty credential map rather than crashing — matching the vault's
    `.corrupt` quarantine behaviour."""
    (wizard_home / "credentials.json").write_text("{not json")

    state = WizardState.load(wizard_home)
    assert state.credentials == {}


# ── Step helpers mutate credentials correctly ──────────────────────────────


def test_set_credential_is_persisted_via_vault(wizard_home):
    state = WizardState(home=wizard_home)
    state.set_credential("OPENAI_API_KEY", "sk-step")
    state.set_credential("BLANK_KEY", "")
    assert state.credentials == {"OPENAI_API_KEY": "sk-step"}

    state.save()

    from security.vault import BlindVault

    vault = BlindVault(vault_path=str(wizard_home / "credentials.json"))
    assert vault.retrieve("OPENAI_API_KEY") == "sk-step"
    assert vault.retrieve("BLANK_KEY") is None
    assert not (wizard_home / "credentials.json").exists()


def test_has_credential_reflects_in_memory_state(wizard_home):
    state = WizardState(home=wizard_home)
    assert state.has_credential("OPENAI_API_KEY") is False
    state.set_credential("OPENAI_API_KEY", "sk-1")
    assert state.has_credential("OPENAI_API_KEY") is True


def test_set_setting_nests_under_section(wizard_home):
    state = WizardState(home=wizard_home)
    state.set_setting("llm", "provider", "anthropic")
    state.set_setting("llm", "model", "claude-opus")
    assert state.get_setting("llm", "provider") == "anthropic"
    assert state.get_setting("llm", "model") == "claude-opus"
    assert state.get_setting("llm", "missing", "fallback") == "fallback"
