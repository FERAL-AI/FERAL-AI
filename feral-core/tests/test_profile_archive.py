"""Cold snapshots and restore boundaries, using disposable profiles only."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import zipfile

import pytest

from config import profile_archive as module
from config.profile_archive import ArchiveLimits, ProfileArchiveError, create_archive, restore_archive

pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def profile(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    data.mkdir()
    (config / "settings.json").write_text('{"fixture":true}')
    (config / "credentials.enc").write_bytes(b"ciphertext-fixture-not-a-key")
    (config / "extensions" / "empty").mkdir(parents=True)
    (config / "extensions" / "skill.py").write_text("fixture = True\n")
    (data / "memory.db.enc").write_bytes(b"encrypted-memory-fixture")
    (data / "primary_session_id").write_text("fixture-session")
    return config, data


def _backup(tmp_path, profile, **options):
    archive = tmp_path / "profile.zip"
    create_archive(archive, config_root=profile[0], data_root=profile[1], offline=True, **options)
    return archive


def _rewrite(source, destination, mutate):
    with zipfile.ZipFile(source) as original:
        files = {name: original.read(name) for name in original.namelist()}
    manifest = json.loads(files["manifest.json"])
    mutate(manifest, files)
    files["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(destination, "w") as changed:
        for name, content in files.items():
            changed.writestr(name, content)


def test_restore_preserves_split_roots_ciphertext_extensions_and_private_modes(tmp_path, profile):
    archive = _backup(tmp_path, profile)
    target_config, target_data = tmp_path / "restored-config", tmp_path / "restored-data"
    receipt = restore_archive(archive, config_root=target_config, data_root=target_data, offline=True)
    assert receipt["status"] == "completed"
    assert receipt["runtime_started"] is False
    assert receipt["os_keys_included"] is False
    assert receipt["credentials_portable"] is False
    assert (target_config / "credentials.enc").read_bytes() == b"ciphertext-fixture-not-a-key"
    assert (target_data / "memory.db.enc").read_bytes() == b"encrypted-memory-fixture"
    assert (target_config / "extensions" / "empty").is_dir()
    assert (target_config / "extensions" / "skill.py").read_text() == "fixture = True\n"
    assert stat.S_IMODE(target_config.stat().st_mode) == 0o700
    assert stat.S_IMODE((target_config / "credentials.enc").stat().st_mode) == 0o600
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert (profile[0] / "credentials.enc").read_bytes() == b"ciphertext-fixture-not-a-key"


def test_same_root_is_archived_once_and_layout_cannot_silently_change(tmp_path, profile):
    root = profile[0]
    archive = _backup(tmp_path, (root, root))
    with zipfile.ZipFile(archive) as snapshot:
        manifest = json.loads(snapshot.read("manifest.json"))
        assert manifest["roots"] == {"config": "config", "data": "config"}
        assert all(name == "manifest.json" or name.startswith("config/") for name in snapshot.namelist())
    target = tmp_path / "same-restored"
    restore_archive(archive, config_root=target, data_root=target, offline=True)
    assert (target / "settings.json").exists()
    with pytest.raises(ProfileArchiveError, match="layout"):
        restore_archive(archive, config_root=tmp_path / "split-a", data_root=tmp_path / "split-b", offline=True)
    assert not (tmp_path / "split-a").exists()


def test_committed_sqlite_wal_is_preserved_not_just_main_database(tmp_path, profile):
    connection = sqlite3.connect(profile[1] / "ledger.db")
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE receipts (value TEXT)")
        connection.execute("INSERT INTO receipts VALUES ('fixture-committed')")
        connection.commit()
        # Writes are stopped, while this fixture keeps committed WAL bytes present.
        assert (profile[1] / "ledger.db-wal").stat().st_size > 0
        archive = _backup(tmp_path, profile)
        target_config, target_data = tmp_path / "restored-c", tmp_path / "restored-d"
        restore_archive(archive, config_root=target_config, data_root=target_data, offline=True)
        with sqlite3.connect(target_data / "ledger.db") as restored:
            assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert restored.execute("SELECT value FROM receipts").fetchall() == [("fixture-committed",)]
    finally:
        connection.close()


def test_actual_default_helpers_are_used_without_invented_data_home(tmp_path, profile, monkeypatch):
    from config import loader
    monkeypatch.setattr(loader, "feral_home", lambda: profile[0])
    monkeypatch.setattr(loader, "feral_data_home", lambda: profile[1])
    archive = tmp_path / "default.zip"
    create_archive(archive, offline=True)
    with zipfile.ZipFile(archive) as snapshot:
        assert "data/primary_session_id" in snapshot.namelist()
    assert not (tmp_path / "not-a-source").exists()


def test_missing_source_is_a_typed_refusal(tmp_path, profile):
    with pytest.raises(ProfileArchiveError, match="unavailable"):
        create_archive(tmp_path / "missing.zip", config_root=profile[0],
                       data_root=tmp_path / "absent", offline=True)
    assert not (tmp_path / "missing.zip").exists()


def test_restore_requires_explicit_offline_precondition(tmp_path, profile):
    archive = _backup(tmp_path, profile)
    with pytest.raises(ProfileArchiveError, match="offline"):
        restore_archive(archive, config_root=tmp_path / "new-c", data_root=tmp_path / "new-d")
    assert not (tmp_path / "new-c").exists()


def test_symlink_archive_container_is_refused(tmp_path, profile):
    archive = _backup(tmp_path, profile)
    link = tmp_path / "alias.zip"
    link.symlink_to(archive)
    with pytest.raises(ProfileArchiveError, match="container"):
        restore_archive(link, config_root=tmp_path / "new-c", data_root=tmp_path / "new-d", offline=True)


@pytest.mark.parametrize("offline", [False, None, 1, "true"])
def test_explicit_offline_precondition_cannot_be_truthy_coercion(tmp_path, profile, offline):
    with pytest.raises(ProfileArchiveError, match="offline"):
        create_archive(tmp_path / "no.zip", config_root=profile[0], data_root=profile[1], offline=offline)
    assert not (tmp_path / "no.zip").exists()


def test_existing_archive_and_profile_are_never_overwritten(tmp_path, profile):
    archive = _backup(tmp_path, profile)
    original = archive.read_bytes()
    with pytest.raises(ProfileArchiveError, match="already exists"):
        create_archive(archive, config_root=profile[0], data_root=profile[1], offline=True)
    assert archive.read_bytes() == original
    with pytest.raises(ProfileArchiveError, match="must not exist"):
        restore_archive(archive, config_root=profile[0], data_root=profile[1], offline=True)
    assert (profile[0] / "settings.json").read_text() == '{"fixture":true}'


@pytest.mark.parametrize("kind", ["symlink-file", "symlink-directory", "fifo"])
def test_source_nonregular_entries_fail_without_archive(tmp_path, profile, kind):
    entry = profile[0] / "unsupported"
    if kind == "fifo":
        os.mkfifo(entry)
    elif kind == "symlink-directory":
        entry.symlink_to(profile[1], target_is_directory=True)
    else:
        entry.symlink_to(profile[0] / "settings.json")
    with pytest.raises(ProfileArchiveError, match="nonregular"):
        _backup(tmp_path, profile)
    assert not (tmp_path / "profile.zip").exists()


def test_nested_roots_and_in_profile_destination_are_rejected(tmp_path, profile):
    nested = profile[0] / "nested"
    nested.mkdir()
    with pytest.raises(ProfileArchiveError, match="overlap"):
        _backup(tmp_path, (profile[0], nested))
    with pytest.raises(ProfileArchiveError, match="outside"):
        create_archive(profile[0] / "backup.zip", config_root=profile[0], data_root=profile[1], offline=True)


@pytest.mark.parametrize("limits", [ArchiveLimits(max_entries=1), ArchiveLimits(max_total_bytes=1),
                                   ArchiveLimits(max_file_bytes=1), ArchiveLimits(max_manifest_bytes=1)])
def test_creation_size_bounds_leave_no_completed_archive(tmp_path, profile, limits):
    with pytest.raises(ProfileArchiveError, match="bound|oversized"):
        _backup(tmp_path, profile, limits=limits)
    assert not (tmp_path / "profile.zip").exists()


def test_source_change_during_copy_prevents_publication(tmp_path, profile, monkeypatch):
    original = module._read_file
    changed = False

    def read_and_change(path, limit):
        nonlocal changed
        item = original(path, limit)
        if not changed and path.name == "settings.json":
            changed = True
            path.write_text('{"changed":true}')
        return item

    monkeypatch.setattr(module, "_read_file", read_and_change)
    with pytest.raises(ProfileArchiveError, match="changed"):
        _backup(tmp_path, profile)
    assert not (tmp_path / "profile.zip").exists()
    assert not list(tmp_path.glob(".feral-archive-*"))


@pytest.mark.parametrize("malformation", ["traversal", "hash", "extra", "directory-duplicate", "oversized", "parent-file"])
def test_malformed_archives_never_create_restore_roots(tmp_path, profile, malformation):
    original = _backup(tmp_path, profile)
    bad = tmp_path / "bad.zip"

    def mutate(manifest, files):
        key = next(iter(manifest["files"]))
        if malformation == "traversal":
            invalid = "config/../escape"
            manifest["files"][invalid] = manifest["files"].pop(key)
            files[invalid] = files.pop(key)
        elif malformation == "hash":
            manifest["files"][key]["sha256"] = "0" * 64
        elif malformation == "extra":
            files["config/unlisted"] = b"unlisted"
        elif malformation == "directory-duplicate":
            manifest["directories"] *= 2
        elif malformation == "parent-file":
            parent = "config/extensions"
            manifest["files"][parent] = manifest["files"].pop(key)
            files[parent] = files.pop(key)
            manifest["directories"].remove(parent)
        else:
            manifest["files"][key]["size"] = 10**20

    _rewrite(original, bad, mutate)
    targets = tmp_path / "new-c", tmp_path / "new-d"
    with pytest.raises(ProfileArchiveError):
        restore_archive(bad, config_root=targets[0], data_root=targets[1], offline=True)
    assert not any(target.exists() for target in targets)
    assert not (tmp_path / "escape").exists()


def test_archive_changed_after_validation_rolls_back_new_roots(tmp_path, profile, monkeypatch):
    archive = _backup(tmp_path, profile)
    original = module._validate

    def validate_then_change(source, limits):
        manifest = original(source, limits)
        with archive.open("ab") as changed:
            changed.write(b"fixture-concurrent-change")
        return manifest

    monkeypatch.setattr(module, "_validate", validate_then_change)
    targets = tmp_path / "new-c", tmp_path / "new-d"
    with pytest.raises(ProfileArchiveError, match="changed during restore"):
        restore_archive(archive, config_root=targets[0], data_root=targets[1], offline=True)
    assert not any(target.exists() for target in targets)
    assert (profile[0] / "settings.json").exists()


def test_duplicate_zip_member_and_manifest_key_are_rejected(tmp_path, profile):
    original = _backup(tmp_path, profile)
    duplicate = tmp_path / "duplicate.zip"
    duplicate.write_bytes(original.read_bytes())
    with zipfile.ZipFile(duplicate, "a") as archive, pytest.warns(UserWarning):
        archive.writestr("manifest.json", "{}")
    with pytest.raises(ProfileArchiveError, match="Duplicate"):
        restore_archive(duplicate, config_root=tmp_path / "c1", data_root=tmp_path / "d1", offline=True)
    invalid = tmp_path / "duplicate-key.zip"
    with zipfile.ZipFile(invalid, "w") as archive:
        archive.writestr("manifest.json", '{"format_version":1,"format_version":1}')
    with pytest.raises(ProfileArchiveError, match="Duplicate"):
        restore_archive(invalid, config_root=tmp_path / "c2", data_root=tmp_path / "d2", offline=True)


def test_archived_symlink_mode_is_not_restored(tmp_path, profile):
    original = _backup(tmp_path, profile)
    bad = tmp_path / "link.zip"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(bad, "w") as target:
        for info in source.infolist():
            if info.filename == "config/settings.json":
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            target.writestr(info, source.read(info.filename))
    with pytest.raises(ProfileArchiveError, match="Unsupported"):
        restore_archive(bad, config_root=tmp_path / "c", data_root=tmp_path / "d", offline=True)


def test_partial_restore_failure_removes_only_owned_new_roots(tmp_path, profile, monkeypatch):
    archive = _backup(tmp_path, profile)
    first, second = tmp_path / "restore-first", tmp_path / "restore-second"
    original = Path.mkdir

    def fail_second(path, *args, **kwargs):
        if path == second:
            raise OSError("fixture destination unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_second)
    with pytest.raises(ProfileArchiveError, match="could not be completed"):
        restore_archive(archive, config_root=first, data_root=second, offline=True)
    assert not first.exists() and not second.exists()
    assert (profile[0] / "credentials.enc").exists()


def test_restore_bounds_and_executable_mode(tmp_path, profile):
    script = profile[0] / "extensions" / "worker"
    script.write_bytes(b"fixture executable")
    script.chmod(0o755)
    archive = _backup(tmp_path, profile)
    with pytest.raises(ProfileArchiveError):
        restore_archive(archive, config_root=tmp_path / "bounded-c", data_root=tmp_path / "bounded-d",
                        offline=True, limits=ArchiveLimits(max_total_bytes=1))
    target = tmp_path / "exec-c"
    restore_archive(archive, config_root=target, data_root=tmp_path / "exec-d", offline=True)
    assert stat.S_IMODE((target / "extensions" / "worker").stat().st_mode) == 0o700
    assert hashlib.sha256(script.read_bytes()).digest() == hashlib.sha256((target / "extensions" / "worker").read_bytes()).digest()


def test_standalone_cli_round_trip_prints_receipts_without_profile_content(tmp_path, profile):
    archive = tmp_path / "cli.zip"
    sentinel = "fixture-private-content-do-not-log"
    (profile[0] / "private.txt").write_text(sentinel)
    result = subprocess.run([sys.executable, "-m", "config.profile_archive", "backup", str(archive),
                             "--offline", "--config-root", str(profile[0]), "--data-root", str(profile[1])],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "completed"
    assert sentinel not in result.stdout + result.stderr
    restored = subprocess.run([sys.executable, "-m", "config.profile_archive", "restore", str(archive),
                               "--offline", "--config-root", str(tmp_path / "cli-c"),
                               "--data-root", str(tmp_path / "cli-d")],
                              capture_output=True, text=True, timeout=30)
    assert restored.returncode == 0, restored.stderr
    assert json.loads(restored.stdout)["runtime_started"] is False
    assert (tmp_path / "cli-c" / "private.txt").read_text() == sentinel
    assert sentinel not in restored.stdout + restored.stderr


def test_backup_missing_destination_parent_is_typed_refusal(tmp_path, profile):
    destination = tmp_path / "missing-parent" / "profile.zip"
    with pytest.raises(ProfileArchiveError, match="Archive destination is unavailable"):
        create_archive(destination, config_root=profile[0], data_root=profile[1], offline=True)
    assert not destination.parent.exists()
    assert not list(tmp_path.glob(".feral-archive-*"))


def test_standalone_cli_missing_destination_parent_has_no_traceback(tmp_path, profile):
    destination = tmp_path / "missing-parent" / "profile.zip"
    result = subprocess.run([sys.executable, "-m", "config.profile_archive", "backup", str(destination),
                             "--offline", "--config-root", str(profile[0]), "--data-root", str(profile[1])],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert "Archive destination is unavailable" in result.stderr
    assert "Traceback" not in result.stderr
    assert not destination.parent.exists()


def test_standalone_cli_requires_offline_option(tmp_path, profile):
    result = subprocess.run([sys.executable, "-m", "config.profile_archive", "backup", str(tmp_path / "no.zip"),
                             "--config-root", str(profile[0]), "--data-root", str(profile[1])],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert "--offline" in result.stderr
    assert not (tmp_path / "no.zip").exists()
