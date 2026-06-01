"""v2026.5.28 — ``feral start`` honors the values ``feral setup`` writes.

The audit synthesized in v2026.5.28 found two priority-inverted paths:

* ``config.runtime.brain_port`` was env-only, so a port persisted in
  ``~/.feral/settings.json`` by the setup wizard was ignored at boot.
* ``config.runtime.brain_tls_enabled`` was env-only, with the same gap.

This module locks in the fixed behaviour: env still wins (so ops with
the brain inside docker / systemd can pin the port without touching
the wizard), but ``settings.json`` is the second source of truth
instead of the hard-coded default.

It also confirms the rest of the parity rules that landed in
v2026.5.28:

* The ``persist_port`` / ``persist_tls`` helpers in
  ``cli/setup/network.py`` write to the expected JSON shape.
* The new ``cli/ui_kit.print_start_banner`` and ``print_ready_panel``
  helpers exist + render with the brand chrome.
* ``cmd_doctor`` reads vault credentials, not the legacy
  ``credentials.json`` plaintext.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_settings(home: Path, data: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(data, indent=2, sort_keys=True))


def _fresh_runtime(monkeypatch, home: Path):
    """Reload ``config.runtime`` with ``FERAL_HOME`` pointed at ``home``.

    Re-importing pulls in the new env value so ``feral_home()``
    resolves to the temp directory and our settings.json gets read.
    """
    monkeypatch.setenv("FERAL_HOME", str(home))
    for k in (
        "FERAL_PORT",
        "FERAL_BRAIN_PORT",
        "FERAL_TLS",
        "FERAL_HOST",
        "FERAL_BIND_HOST",
    ):
        monkeypatch.delenv(k, raising=False)
    from config import runtime as _runtime

    return importlib.reload(_runtime)


# ---------------------------------------------------------------------------
# brain_port
# ---------------------------------------------------------------------------


def test_brain_port_reads_settings_when_env_unset(monkeypatch, tmp_path):
    home = tmp_path / ".feral"
    _write_settings(home, {"network": {"port": 9999}})
    runtime = _fresh_runtime(monkeypatch, home)
    assert runtime.brain_port() == 9999


def test_brain_port_env_wins_over_settings(monkeypatch, tmp_path):
    home = tmp_path / ".feral"
    _write_settings(home, {"network": {"port": 9999}})
    runtime = _fresh_runtime(monkeypatch, home)
    monkeypatch.setenv("FERAL_PORT", "8123")
    assert runtime.brain_port() == 8123


def test_brain_port_default_when_neither(monkeypatch, tmp_path):
    home = tmp_path / ".feral"
    home.mkdir()
    runtime = _fresh_runtime(monkeypatch, home)
    assert runtime.brain_port() == 9090


def test_brain_port_rejects_garbage_in_settings(monkeypatch, tmp_path):
    home = tmp_path / ".feral"
    _write_settings(home, {"network": {"port": "not a number"}})
    runtime = _fresh_runtime(monkeypatch, home)
    # Falls through to the default rather than crashing the CLI.
    assert runtime.brain_port() == 9090


# ---------------------------------------------------------------------------
# brain_tls_enabled
# ---------------------------------------------------------------------------


def test_brain_tls_enabled_reads_settings_when_env_unset(monkeypatch, tmp_path):
    home = tmp_path / ".feral"
    _write_settings(home, {"network": {"tls": True}})
    runtime = _fresh_runtime(monkeypatch, home)
    assert runtime.brain_tls_enabled() is True


def test_brain_tls_enabled_env_wins_over_settings(monkeypatch, tmp_path):
    home = tmp_path / ".feral"
    _write_settings(home, {"network": {"tls": True}})
    runtime = _fresh_runtime(monkeypatch, home)
    monkeypatch.setenv("FERAL_TLS", "0")
    assert runtime.brain_tls_enabled() is False


def test_brain_tls_enabled_default_false(monkeypatch, tmp_path):
    home = tmp_path / ".feral"
    home.mkdir()
    runtime = _fresh_runtime(monkeypatch, home)
    assert runtime.brain_tls_enabled() is False


def test_hydrate_brain_runtime_env_from_settings(monkeypatch, tmp_path):
    home = tmp_path / ".feral"
    _write_settings(
        home,
        {"network": {"bind_host": "0.0.0.0", "port": 8123, "tls": True}},
    )
    runtime = _fresh_runtime(monkeypatch, home)
    monkeypatch.delenv("FERAL_BIND_HOST", raising=False)
    monkeypatch.delenv("FERAL_PORT", raising=False)
    monkeypatch.delenv("FERAL_TLS", raising=False)
    runtime.hydrate_brain_runtime_env()
    assert os.environ.get("FERAL_BIND_HOST") == "0.0.0.0"
    assert os.environ.get("FERAL_PORT") == "8123"
    assert os.environ.get("FERAL_TLS") == "1"


def test_hydrate_brain_runtime_env_does_not_override_explicit_env(monkeypatch, tmp_path):
    home = tmp_path / ".feral"
    _write_settings(home, {"network": {"bind_host": "0.0.0.0", "port": 8123}})
    runtime = _fresh_runtime(monkeypatch, home)
    monkeypatch.setenv("FERAL_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("FERAL_PORT", "9090")
    runtime.hydrate_brain_runtime_env()
    assert os.environ.get("FERAL_BIND_HOST") == "127.0.0.1"
    assert os.environ.get("FERAL_PORT") == "9090"


# ---------------------------------------------------------------------------
# persist_port / persist_tls helpers
# ---------------------------------------------------------------------------


def test_persist_port_writes_int_in_network_block(monkeypatch, tmp_path):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / ".feral"))
    from cli.setup import network as _nw

    importlib.reload(_nw)
    _nw.persist_port(8123)
    data = json.loads((tmp_path / ".feral" / "settings.json").read_text())
    assert data["network"]["port"] == 8123


def test_persist_port_rejects_out_of_range(monkeypatch, tmp_path):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / ".feral"))
    from cli.setup import network as _nw

    importlib.reload(_nw)
    with pytest.raises(ValueError):
        _nw.persist_port(0)
    with pytest.raises(ValueError):
        _nw.persist_port(99999)


def test_persist_tls_writes_bool(monkeypatch, tmp_path):
    monkeypatch.setenv("FERAL_HOME", str(tmp_path / ".feral"))
    from cli.setup import network as _nw

    importlib.reload(_nw)
    _nw.persist_tls(True)
    data = json.loads((tmp_path / ".feral" / "settings.json").read_text())
    assert data["network"]["tls"] is True
    _nw.persist_tls(False)
    data = json.loads((tmp_path / ".feral" / "settings.json").read_text())
    assert data["network"]["tls"] is False


# ---------------------------------------------------------------------------
# ui_kit chrome surface — print_start_banner + print_ready_panel exist
# ---------------------------------------------------------------------------


def test_ui_kit_exports_start_banner_helpers():
    from cli import ui_kit as _kit

    assert hasattr(_kit, "print_start_banner")
    assert hasattr(_kit, "print_ready_panel")
    assert "print_start_banner" in _kit.__all__
    assert "print_ready_panel" in _kit.__all__


def test_print_start_banner_smoke(capsys):
    from cli.ui_kit import print_start_banner

    print_start_banner(port=9090, tls=False, bind_host="127.0.0.1")
    out = capsys.readouterr().out
    assert "F E R A L" in out
    assert "http://127.0.0.1:9090" in out


def test_print_start_banner_tls_uses_https(capsys):
    from cli.ui_kit import print_start_banner

    print_start_banner(port=9443, tls=True, bind_host="0.0.0.0")
    out = capsys.readouterr().out
    assert "https://0.0.0.0:9443" in out


def test_print_ready_panel_renders_summary(capsys):
    from cli.ui_kit import print_ready_panel

    print_ready_panel(
        port=9090,
        llm_ok=True,
        skills_count=42,
        memory_notes=7,
        tls=False,
    )
    out = capsys.readouterr().out
    assert "Brain ready" in out
    assert "42" in out
    assert "7 notes" in out
    assert "http://localhost:9090" in out


# ---------------------------------------------------------------------------
# cmd_doctor consults the vault, not credentials.json
# ---------------------------------------------------------------------------


def test_cmd_doctor_no_longer_reads_credentials_json_for_keys():
    """The vault-only fix must NOT regress to plaintext credentials.json.

    Source-level guard: the LLM-credentials block must reach into the
    `BlindVault` import path, not `credentials.json`. Catches future
    refactors that re-add the legacy file probe.
    """
    src = (
        Path(__file__).resolve().parent.parent / "cli" / "main.py"
    ).read_text()

    doctor_start = src.find("def cmd_doctor()")
    assert doctor_start != -1
    # Bracket the audit window with the next top-level def so we read
    # only cmd_doctor's body, not later helpers.
    next_def = src.find("\ndef ", doctor_start + 1)
    doctor_body = src[doctor_start:next_def] if next_def != -1 else src[doctor_start:]

    # Must consult the vault for LLM keys.
    assert "BlindVault" in doctor_body, (
        "cmd_doctor must query BlindVault for vaulted credentials"
    )
    assert "get_credential" in doctor_body, (
        "cmd_doctor must call BlindVault.get_credential"
    )

    # Must NOT load credentials.json for the LLM-keys probe.
    # (`creds_path = home / "credentials.json"` was the pre-fix pattern.)
    assert 'credentials.json"' not in doctor_body, (
        "cmd_doctor regressed to reading credentials.json plaintext"
    )
