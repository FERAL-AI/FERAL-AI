"""The runner, and the migration that retires the plaintext credential backup.

``~/.feral`` holds 93 entries and ``settings.json`` alone is read or
written from 22 call sites. Three releases changed shapes in there and
the record of how that was handled is four hand-made backups sitting in a
live install, one of them a plaintext credential file four months old.

The first migration is the second half of one that already exists:
``BlindVault._migrate_from_plaintext`` writes
``credentials.json.bak.legacy`` as a deliberate safety net for a
one-way encryption step, and nothing ever removes it.
"""

from __future__ import annotations

import json
import time

import pytest

from migrations import runner


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated FERAL_HOME. Never the operator's real one."""
    monkeypatch.setenv("FERAL_HOME", str(tmp_path))
    import config.loader as loader
    monkeypatch.setattr(loader, "feral_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(loader, "feral_data_home", lambda: tmp_path, raising=False)
    return tmp_path


def _write_migration(directory, name: str, body: str):
    (directory / f"{name}.py").write_text(body, encoding="utf-8")


class TestTheRunner:
    def test_it_discovers_only_timestamped_files(self, home, tmp_path, monkeypatch):
        d = tmp_path / "migs"
        d.mkdir()
        _write_migration(d, "1700000001_real", "def migrate():\n    return ('ok', True)\n")
        (d / "helpers.py").write_text("x = 1\n")          # not a migration
        (d / "README.md").write_text("notes\n")
        monkeypatch.setattr(runner, "migrations_dir", lambda: d)
        assert runner.pending_migrations() == ["1700000001_real"]

    def test_it_runs_oldest_first(self, home, tmp_path, monkeypatch):
        """Names sort chronologically because the prefix is a unix
        timestamp, which is the whole reason for that convention."""
        d = tmp_path / "migs"
        d.mkdir()
        for ts in ("1700000003", "1700000001", "1700000002"):
            _write_migration(d, f"{ts}_step", f"def migrate():\n    return ('{ts}', True)\n")
        monkeypatch.setattr(runner, "migrations_dir", lambda: d)
        assert [r.name.split("_")[0] for r in runner.run_pending()] == \
               ["1700000001", "1700000002", "1700000003"]

    def test_an_applied_migration_does_not_run_again(self, home, tmp_path, monkeypatch):
        d = tmp_path / "migs"
        d.mkdir()
        _write_migration(d, "1700000001_once",
                         "import pathlib\n"
                         "def migrate():\n"
                         "    p = pathlib.Path(__file__).parent / 'ran.txt'\n"
                         "    p.write_text(p.read_text() + 'x' if p.exists() else 'x')\n"
                         "    return ('ran', True)\n")
        monkeypatch.setattr(runner, "migrations_dir", lambda: d)
        runner.run_pending()
        runner.run_pending()
        assert (d / "ran.txt").read_text() == "x"
        assert runner.pending_migrations() == []

    def test_a_failure_leaves_no_marker_and_is_retried(self, home, tmp_path, monkeypatch):
        """A migration that cannot run today runs tomorrow. Marking a
        failed migration applied would strand the install forever."""
        d = tmp_path / "migs"
        d.mkdir()
        _write_migration(d, "1700000001_boom", "def migrate():\n    raise RuntimeError('too early')\n")
        monkeypatch.setattr(runner, "migrations_dir", lambda: d)
        results = runner.run_pending()
        assert results[0].ok is False
        assert "too early" in results[0].detail
        assert runner.pending_migrations() == ["1700000001_boom"]

    def test_one_failure_does_not_block_the_rest(self, home, tmp_path, monkeypatch):
        """One bad migration must not wedge an install or stop a boot."""
        d = tmp_path / "migs"
        d.mkdir()
        _write_migration(d, "1700000001_boom", "def migrate():\n    raise RuntimeError('nope')\n")
        _write_migration(d, "1700000002_fine", "def migrate():\n    return ('fine', True)\n")
        monkeypatch.setattr(runner, "migrations_dir", lambda: d)
        results = runner.run_pending()
        assert [r.ok for r in results] == [False, True]
        assert runner.pending_migrations() == ["1700000001_boom"]

    def test_a_module_without_migrate_is_reported_not_marked(self, home, tmp_path, monkeypatch):
        d = tmp_path / "migs"
        d.mkdir()
        _write_migration(d, "1700000001_empty", "value = 1\n")
        monkeypatch.setattr(runner, "migrations_dir", lambda: d)
        result = runner.run_pending()[0]
        assert result.ok is False and "migrate()" in result.detail
        assert runner.pending_migrations() == ["1700000001_empty"]

    def test_dry_run_changes_nothing(self, home, tmp_path, monkeypatch):
        d = tmp_path / "migs"
        d.mkdir()
        _write_migration(d, "1700000001_x", "def migrate():\n    return ('x', True)\n")
        monkeypatch.setattr(runner, "migrations_dir", lambda: d)
        assert all(r.skipped for r in runner.run_pending(dry_run=True))
        assert runner.pending_migrations() == ["1700000001_x"]

    def test_a_missing_state_dir_means_nothing_applied(self, home):
        assert runner.applied_migrations() == set()


class TestRetiringTheCredentialBackup:
    """The safety net must not be removed while it is still the net."""

    NAME = "1787220000_retire_legacy_credentials_backup"

    def _module(self):
        return runner._load(self.NAME, runner.migrations_dir() / f"{self.NAME}.py")

    def _backup(self, home, keys):
        p = home / "credentials.json.bak.legacy"
        p.write_text(json.dumps({k: f"secret-{k}" for k in keys}), encoding="utf-8")
        p.chmod(0o600)
        return p

    def test_no_backup_is_a_no_op(self, home):
        detail, changed = self._module().migrate()
        assert changed is False

    def test_it_refuses_when_the_vault_cannot_be_opened(self, home, monkeypatch):
        """The backup would then be the only copy of those credentials."""
        backup = self._backup(home, ["OPENAI_API_KEY"])
        import security.vault as vault_mod
        monkeypatch.setattr(vault_mod, "BlindVault",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no keychain")))
        with pytest.raises(RuntimeError, match="only copy"):
            self._module().migrate()
        assert backup.exists(), "the only copy of the credentials was deleted"

    def test_it_refuses_when_a_key_is_missing_from_the_vault(self, home, monkeypatch):
        """A partial migration is exactly when the backup earns its keep."""
        backup = self._backup(home, ["OPENAI_API_KEY", "BRAVE_API_KEY"])

        class PartialVault:
            def get(self, ns, key, requester=""):
                return "v" if key == "OPENAI_API_KEY" else None
            def retrieve(self, key, requester=""):
                return "v" if key == "OPENAI_API_KEY" else None

        import security.vault as vault_mod
        monkeypatch.setattr(vault_mod, "BlindVault", lambda *a, **k: PartialVault())
        with pytest.raises(RuntimeError, match="BRAVE_API_KEY"):
            self._module().migrate()
        assert backup.exists()

    def test_it_removes_the_plaintext_once_every_key_is_readable(self, home, monkeypatch):
        backup = self._backup(home, ["OPENAI_API_KEY", "BRAVE_API_KEY"])

        class FullVault:
            def get(self, ns, key, requester=""):
                return "value"
            def retrieve(self, key, requester=""):
                return "value"

        import security.vault as vault_mod
        monkeypatch.setattr(vault_mod, "BlindVault", lambda *a, **k: FullVault())
        detail, changed = self._module().migrate()
        assert changed is True
        assert not backup.exists()
        assert "2 keys" in detail

    def test_a_corrupt_backup_is_left_alone(self, home):
        backup = home / "credentials.json.bak.legacy"
        backup.write_text("{ not json", encoding="utf-8")
        with pytest.raises(RuntimeError, match="unreadable"):
            self._module().migrate()
        assert backup.exists()

    def test_rerunning_after_success_is_harmless(self, home, monkeypatch):
        """Markers can be lost, restored from backup, or copied between
        machines, so the marker is an optimisation and not the guarantee."""
        self._backup(home, ["OPENAI_API_KEY"])

        class FullVault:
            def get(self, ns, key, requester=""):
                return "value"
            def retrieve(self, key, requester=""):
                return "value"

        import security.vault as vault_mod
        monkeypatch.setattr(vault_mod, "BlindVault", lambda *a, **k: FullVault())
        self._module().migrate()
        detail, changed = self._module().migrate()
        assert changed is False


class TestTheShippedMigrationsAreWellFormed:
    def test_every_shipped_migration_has_a_migrate_function(self):
        for name, path in runner._discover():
            module = runner._load(name, path)
            assert callable(getattr(module, "migrate", None)), f"{name} has no migrate()"

    def test_every_shipped_migration_is_named_by_timestamp(self):
        """The convention that stops two contributors colliding."""
        names = [n for n, _ in runner._discover()]
        assert names, "no migrations discovered at all"
        for name in names:
            stamp = name.split("_")[0]
            assert stamp.isdigit() and len(stamp) == 10, name
            assert 1_600_000_000 < int(stamp) < 2_000_000_000, f"{name} is not a plausible epoch"


class TestMigrationsRunAtBoot:
    """A migration system nothing calls is a directory of dead files.

    The doctor row is ``_info`` rather than ``_warn`` precisely because
    the brain applies these itself, so if the boot call is ever removed
    the row becomes a lie and pending migrations become invisible.
    """

    def test_the_boot_path_applies_pending_migrations(self):
        import inspect

        import api.server as server

        source = inspect.getsource(server.startup)
        assert "run_pending" in source, (
            "startup() no longer applies migrations, so `feral doctor` "
            "claiming they will be applied on the next brain start is false"
        )

    def test_it_runs_before_state_init(self):
        """A migration exists to make the store safe to open. Applying it
        after the store is opened is too late."""
        import inspect

        import api.server as server

        source = inspect.getsource(server.startup)
        assert source.index("run_pending") < source.index("await state.init()"), (
            "migrations must be applied before anything reads ~/.feral"
        )

    def test_a_failing_migration_does_not_stop_the_boot(self):
        """A brain that refuses to start because of one migration is
        worse than the shape change the migration was fixing."""
        import inspect

        import api.server as server

        source = inspect.getsource(server.startup)
        block = source[source.index("run_pending") - 400:source.index("await state.init()")]
        assert "except Exception" in block, "the migration pass at boot is not guarded"
