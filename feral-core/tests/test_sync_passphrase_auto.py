"""audit-r12 A2 (v2026.5.38) — ``ensure_sync_passphrase`` auto-generates
the federated-sync shared secret on first boot, persists it to the
vault, and refuses the handshake when no value is configured.

Pre-fix:

* ``FERAL_SYNC_PASSPHRASE`` defaulted to ``""``.
* The ``/sync`` handshake only checked the value *when non-empty*, so
  the default install accepted any federation peer with zero auth.
* No path generated or persisted a value for the operator.

Post-fix:

* :func:`ensure_sync_passphrase` is invoked from
  :py:meth:`BrainState.init`. It resolves in order:
  env → vault → freshly-generated + persisted + printed.
* The ``/sync`` handshake rejects when the local passphrase is
  unset (a separate test in ``test_sync.py`` covers the mismatch
  path that already existed).
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest


@pytest.fixture
def fresh_sync_module(tmp_path, monkeypatch):
    """Reload ``memory.sync`` so module-level constants pick up
    the per-test env + home overrides."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    monkeypatch.delenv("FERAL_SYNC_PASSPHRASE", raising=False)
    sys.modules.pop("memory.sync", None)
    import memory.sync as sync_mod  # noqa: WPS433

    return sync_mod


class _FakeVault:
    """In-memory stand-in for BlindVault that records every read/write."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.reads: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str, str, str]] = []

    def get(self, namespace: str, key: str) -> str | None:
        self.reads.append((namespace, key))
        return self.store.get((namespace, key))

    def put(self, namespace: str, key: str, value: str, *, stored_by: str = "user") -> None:
        self.writes.append((namespace, key, value, stored_by))
        self.store[(namespace, key)] = value


@pytest.fixture
def fake_vault(monkeypatch):
    from security import vault as vault_mod
    v = _FakeVault()
    monkeypatch.setattr(vault_mod, "get_vault", lambda *a, **kw: v)
    return v


class TestEnsureSyncPassphrase:
    def test_env_var_wins(self, fresh_sync_module, fake_vault, monkeypatch):
        monkeypatch.setenv("FERAL_SYNC_PASSPHRASE", "from-env-123")
        result = fresh_sync_module.ensure_sync_passphrase()
        assert result == "from-env-123"
        assert fresh_sync_module.SYNC_PASSPHRASE == "from-env-123"
        # Env-supplied value is authoritative; vault is not touched.
        assert fake_vault.reads == []
        assert fake_vault.writes == []

    def test_vault_value_used_when_env_unset(self, fresh_sync_module, fake_vault):
        fake_vault.store[("sync", "passphrase")] = "from-vault-xyz"
        result = fresh_sync_module.ensure_sync_passphrase()
        assert result == "from-vault-xyz"
        assert fresh_sync_module.SYNC_PASSPHRASE == "from-vault-xyz"
        # The vault read happened; no write because a value already
        # existed.
        assert fake_vault.reads == [("sync", "passphrase")]
        assert fake_vault.writes == []
        assert os.environ.get("FERAL_SYNC_PASSPHRASE") == "from-vault-xyz"

    def test_first_boot_generates_persists_prints(self, fresh_sync_module, fake_vault, capsys, tmp_path):
        result = fresh_sync_module.ensure_sync_passphrase()
        assert len(result) >= 32, f"generated passphrase too short: {result!r}"
        # Was persisted to the vault.
        assert ("sync", "passphrase", result, "boot.auto-generate") in fake_vault.writes
        # Module-level constant + env var updated for downstream readers.
        assert fresh_sync_module.SYNC_PASSPHRASE == result
        assert os.environ["FERAL_SYNC_PASSPHRASE"] == result
        # Operator banner printed on stderr (NOT logged at INFO).
        captured = capsys.readouterr()
        assert result in captured.err
        assert "federated sync passphrase" in captured.err.lower()
        # First-boot marker file written to FERAL_HOME chmod 0600 so a
        # headless operator can recover it.
        marker = tmp_path / "sync_passphrase.first_boot"
        assert marker.exists()
        assert marker.read_text().strip() == result
        # Permissions: 0600 on platforms that honour chmod.
        if os.name != "nt":
            assert (marker.stat().st_mode & 0o777) == 0o600

    def test_vault_write_failure_falls_back_to_process_lifetime(
        self, fresh_sync_module, monkeypatch, capsys
    ):
        from security import vault as vault_mod
        from security.audit_log import AuditFailure

        class _ExplodingVault:
            def get(self, *_a, **_kw):
                return None

            def put(self, *_a, **_kw):
                raise AuditFailure("disk on fire")

        monkeypatch.setattr(vault_mod, "get_vault", lambda *a, **kw: _ExplodingVault())
        result = fresh_sync_module.ensure_sync_passphrase()
        assert len(result) >= 32
        captured = capsys.readouterr()
        # Banner labels the secret as process-lifetime so the operator
        # knows to set FERAL_SYNC_PASSPHRASE explicitly.
        assert "process-lifetime" in captured.err.lower()

    def test_subsequent_call_returns_same_value(self, fresh_sync_module, fake_vault, capsys):
        first = fresh_sync_module.ensure_sync_passphrase()
        # Clear what stderr saw from the first banner so we can prove
        # the second call doesn't re-mint.
        capsys.readouterr()
        second = fresh_sync_module.ensure_sync_passphrase()
        assert first == second
        # The second call read the value back out of the vault — the
        # banner is silent.
        captured = capsys.readouterr()
        assert "federated sync passphrase" not in captured.err.lower()


class TestEnvOverridesAfterPersist:
    """Once an operator pins ``FERAL_SYNC_PASSPHRASE`` the env value
    must win even if a different value is persisted in the vault
    (e.g. previously auto-generated)."""

    def test_env_wins_over_vault(self, fresh_sync_module, fake_vault, monkeypatch):
        fake_vault.store[("sync", "passphrase")] = "old-vault-value"
        monkeypatch.setenv("FERAL_SYNC_PASSPHRASE", "pinned-by-ops")
        result = fresh_sync_module.ensure_sync_passphrase()
        assert result == "pinned-by-ops"
