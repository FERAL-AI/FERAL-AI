"""Cold snapshots and restore boundaries, using disposable profiles only."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import zipfile
from uuid import uuid4

import pytest

from config import profile_archive as module
from config.profile_archive import ArchiveLimits, ProfileArchiveError, create_archive, restore_archive

pytestmark = pytest.mark.no_auto_feral_home


@pytest.fixture
def archive_patch(restore_process_env):
    # Close this local patch context before the process-environment guard reads it.
    with pytest.MonkeyPatch.context() as patch:
        yield patch


def _xdg_profile(tmp_path, archive_patch):
    """Actual loader defaults and legacy readers, with no personal-home access."""
    from config.loader import feral_data_home, feral_home
    home = tmp_path / "synthetic-user"
    home.mkdir()
    archive_patch.setattr(Path, "home", classmethod(lambda cls: home))
    archive_patch.delenv("FERAL_HOME", raising=False)
    archive_patch.delenv("FERAL_DATA_HOME", raising=False)
    archive_patch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    archive_patch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    config, data = feral_home(), feral_data_home()
    config.mkdir(parents=True)
    data.mkdir(parents=True)
    (config / "settings.json").write_text('{"llm":{"max_tokens":111}}')
    return config, data, home / ".feral"


@pytest.mark.parametrize("partial_default", [False, True])
def test_default_refuses_omitted_actual_capability_denial_before_copy(
    tmp_path, archive_patch, partial_default,
):
    from security.capability_grants import CapabilityGrantStore
    config, _, legacy = _xdg_profile(tmp_path, archive_patch)
    grants = CapabilityGrantStore()
    grants.set_grant("synthetic-device", "camera", False)
    assert grants.is_granted("synthetic-device", "camera") is False
    database = legacy / "capability_grants.db"
    original = database.read_bytes()
    archive_patch.setattr(module, "_snapshot", lambda *args: pytest.fail("Must refuse before copy"))
    destination = tmp_path / "refused.zip"
    options = {"config_root": config} if partial_default else {}
    with pytest.raises(ProfileArchiveError, match="omit legacy runtime storage"):
        create_archive(destination, offline=True, **options)
    assert not destination.exists()
    assert not list(tmp_path.glob(".feral-archive-*"))
    assert database.read_bytes() == original
    assert CapabilityGrantStore().is_granted("synthetic-device", "camera") is False


@pytest.mark.parametrize("legacy_state", ["absent", "empty"])
def test_xdg_defaults_without_legacy_data_round_trip_settings(tmp_path, archive_patch, legacy_state):
    from config.loader import ConfigLoader
    config, data, legacy = _xdg_profile(tmp_path, archive_patch)
    if legacy_state == "empty":
        legacy.mkdir()
    archive = tmp_path / "xdg.zip"
    receipt = create_archive(archive, offline=True)
    assert receipt["coverage"] == "selected_roots_only"
    assert receipt["native_preferences_included"] is False
    target_config, target_data = tmp_path / "new-config" / "feral", tmp_path / "new-data" / "feral"
    target_config.parent.mkdir()
    target_data.parent.mkdir()
    restore_archive(archive, config_root=target_config, data_root=target_data, offline=True)
    # The restore selects explicit roots; the reader uses the corresponding XDG layout.
    archive_patch.setenv("XDG_CONFIG_HOME", str(target_config.parent))
    archive_patch.setenv("XDG_DATA_HOME", str(target_data.parent))
    # Read exactly the restored settings through the existing loader, without a vault.
    reader = ConfigLoader(project_dir=tmp_path / "no-project")
    assert reader.user_home == target_config and reader.data_home == target_data
    settings = reader.discover(load_credentials=False)
    assert settings["llm"]["max_tokens"] == 111
    assert (config / "settings.json").exists() and data.is_dir()


@pytest.mark.parametrize("kind", ["symlink", "dangling-symlink", "file", "inaccessible"])
def test_uninspectable_uncovered_legacy_default_fails_closed(tmp_path, archive_patch, kind):
    _, _, legacy = _xdg_profile(tmp_path, archive_patch)
    if kind == "file":
        legacy.write_text("synthetic-only")
    elif kind in {"symlink", "dangling-symlink"}:
        target = tmp_path / "link-target"
        if kind == "symlink":
            target.mkdir()
        legacy.symlink_to(target, target_is_directory=True)
    else:
        legacy.mkdir()
        original_open = os.open

        def inaccessible(path, *args, **kwargs):
            if Path(path) == legacy:
                raise PermissionError("synthetic private exception")
            return original_open(path, *args, **kwargs)

        archive_patch.setattr(os, "open", inaccessible)
    with pytest.raises(ProfileArchiveError, match="Legacy runtime storage") as error:
        create_archive(tmp_path / "refused.zip", offline=True)
    assert "synthetic private exception" not in str(error.value)
    assert not (tmp_path / "refused.zip").exists()
    assert not list(tmp_path.glob(".feral-archive-*"))


def test_new_uncovered_legacy_entry_during_copy_prevents_publication(tmp_path, archive_patch):
    _, _, legacy = _xdg_profile(tmp_path, archive_patch)
    legacy.mkdir()
    original = module._snapshot
    calls = 0

    def snapshot_then_add(roots, limits):
        nonlocal calls
        result = original(roots, limits)
        calls += 1
        if calls == 2:
            (legacy / "unknown-runtime-artifact").write_text("synthetic-only")
        return result

    archive_patch.setattr(module, "_snapshot", snapshot_then_add)
    with pytest.raises(ProfileArchiveError, match="omit legacy runtime storage"):
        create_archive(tmp_path / "changed.zip", offline=True)
    assert not (tmp_path / "changed.zip").exists()
    assert not list(tmp_path.glob(".feral-archive-*"))


def test_explicit_roots_remain_a_partial_snapshot_without_legacy_inspection(tmp_path, archive_patch):
    config, data, legacy = _xdg_profile(tmp_path, archive_patch)
    legacy.mkdir()
    (legacy / "not-selected").write_text("synthetic-unselected")
    archive_patch.setattr(module, "_check_default_layout", lambda *args: pytest.fail("Explicit selection"))
    receipt = create_archive(tmp_path / "partial.zip", config_root=config, data_root=data, offline=True)
    assert receipt["coverage"] == "selected_roots_only"
    assert receipt["native_preferences_included"] is False
    with zipfile.ZipFile(tmp_path / "partial.zip") as archive:
        assert not any("not-selected" in name for name in archive.namelist())


def test_cli_default_omission_is_typed_redacted_and_unpublished(tmp_path, archive_patch):
    from security.capability_grants import CapabilityGrantStore
    _, _, legacy = _xdg_profile(tmp_path, archive_patch)
    CapabilityGrantStore().set_grant("synthetic-device-private", "camera", False)
    original = (legacy / "capability_grants.db").read_bytes()
    command = (
        "import sys; from pathlib import Path; "
        "synthetic_home=Path(sys.argv.pop(1)); Path.home=classmethod(lambda cls: synthetic_home); "
        "from config.profile_archive import main; raise SystemExit(main())"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", command, str(legacy.parent),
         "backup", str(tmp_path / "cli-refused.zip"), "--offline"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert "omit legacy runtime storage" in result.stderr
    assert "Traceback" not in result.stderr
    assert "synthetic-device-private" not in result.stdout + result.stderr
    assert not result.stdout and not (tmp_path / "cli-refused.zip").exists()
    assert (legacy / "capability_grants.db").read_bytes() == original


def test_empty_feral_home_does_not_conceal_two_runtime_defaults(tmp_path, archive_patch):
    _xdg_profile(tmp_path, archive_patch)
    archive_patch.setenv("FERAL_HOME", "")
    archive_patch.setattr(module, "_snapshot", lambda *args: pytest.fail("Must refuse before copy"))
    with pytest.raises(ProfileArchiveError, match="ambiguous with empty FERAL_HOME"):
        create_archive(tmp_path / "empty-override.zip", offline=True)
    assert not (tmp_path / "empty-override.zip").exists()


def test_omitted_legacy_refusal_preserves_an_existing_archive(tmp_path, archive_patch):
    _, _, legacy = _xdg_profile(tmp_path, archive_patch)
    legacy.mkdir()
    (legacy / "unrecognized-artifact").write_text("synthetic-only")
    destination = tmp_path / "historical.zip"
    destination.write_bytes(b"synthetic-historical-archive")
    with pytest.raises(ProfileArchiveError, match="omit legacy runtime storage"):
        create_archive(destination, offline=True)
    assert destination.read_bytes() == b"synthetic-historical-archive"
    assert not list(tmp_path.glob(".feral-archive-*"))


def test_legacy_root_contained_by_explicit_config_is_not_omitted(tmp_path, archive_patch):
    from security.capability_grants import CapabilityGrantStore
    _, data, legacy = _xdg_profile(tmp_path, archive_patch)
    CapabilityGrantStore().set_grant("synthetic-device", "camera", False)
    archive = tmp_path / "contained.zip"
    create_archive(archive, config_root=legacy.parent, offline=True)
    with zipfile.ZipFile(archive) as snapshot:
        assert "config/.feral/capability_grants.db" in snapshot.namelist()
    assert data.is_dir()


def test_unresolvable_legacy_root_is_a_typed_refusal(tmp_path, archive_patch):
    _, _, legacy = _xdg_profile(tmp_path, archive_patch)
    legacy.mkdir()
    original = Path.resolve

    def unresolvable(path, *args, **kwargs):
        if path == legacy:
            raise RuntimeError("synthetic private symlink-loop detail")
        return original(path, *args, **kwargs)

    archive_patch.setattr(Path, "resolve", unresolvable)
    with pytest.raises(ProfileArchiveError, match="coverage cannot be verified") as error:
        create_archive(tmp_path / "unresolvable.zip", offline=True)
    assert "synthetic private" not in str(error.value)
    assert not (tmp_path / "unresolvable.zip").exists()


def test_native_feral_home_round_trip_actual_readers_and_fences(tmp_path, archive_patch):
    from config.loader import ConfigLoader, feral_data_home, feral_home
    from memory.runtime_session_checkpoint import CheckpointStatus, encode_context
    from memory.store import MemoryStore
    from security.capability_grants import CapabilityGrantStore
    from security.sandbox_policy import SandboxPolicy

    native = tmp_path / "native-home"
    native.mkdir()
    archive_patch.setenv("FERAL_HOME", str(native))
    archive_patch.setenv("FERAL_DATA_HOME", str(native / "data"))
    archive_patch.setenv("FERAL_EMBED_PROVIDER", "hash")
    archive_patch.setenv("FERAL_NATIVE_DEFER_VAULT", "1")
    assert feral_home() == feral_data_home() == native  # Actual loader precedence.
    (native / "settings.json").write_text('{"llm":{"max_tokens":111}}')
    (native / "data").mkdir()
    (native / "data" / "synthetic-artifact").write_text("included-regular-file")
    grants = CapabilityGrantStore()
    grants.set_grant("synthetic-device", "camera", False)
    workspace = tmp_path / "synthetic-workspace"
    workspace.mkdir()
    assert SandboxPolicy.load_default().grant_folder(str(workspace), "read")["ok"] is True
    sid = "thread-" + str(uuid4())
    history = [{"role": "user", "content": "Synthetic fact: amber 47"},
               {"role": "assistant", "content": "Recorded synthetic fact."}]

    async def seed():
        store = MemoryStore()
        try:
            assert Path(store.db_path) == native / "memory.db"
            initialized = await store.runtime_checkpoint_initialize_empty(sid)
            assert initialized.record is not None
            pending = await store.runtime_checkpoint_begin(
                sid, attempt_id=str(uuid4()), expected=initialized.record.fence,
            )
            assert pending.record is not None
            committed = await store.runtime_checkpoint_commit(
                pending.record.fence, encode_context(history, []),
            )
            assert committed.record is not None
            await store.conversation_save(sid, history, title="Synthetic archive thread")
            await store.conversation_rename(sid, "Synthetic custom title")
            await store.conversation_set_pinned(sid, True)
            note = await store.save("Synthetic amber note", tags=["synthetic"])
            await store.knowledge_store("Synthetic subject", "color", "amber")
            await store.wiki_upsert_page(
                page_id="synthetic-page", title="Synthetic wiki", kind="topic",
                body_markdown="Synthetic amber wiki", source_refs=[],
            )
            return committed.record.fence, note["id"]
        finally:
            await store.aclose()

    fence, note_id = asyncio.run(seed())
    original = hashlib.sha256((native / "memory.db").read_bytes()).digest()
    archive = tmp_path / "native.zip"
    receipt = create_archive(archive, offline=True)
    target = tmp_path / "restored-native"
    restored = restore_archive(archive, config_root=target, data_root=target, offline=True)
    assert receipt["coverage"] == restored["coverage"] == "selected_roots_only"
    assert restored["runtime_started"] is False
    assert hashlib.sha256((native / "memory.db").read_bytes()).digest() == original
    assert (target / "data" / "synthetic-artifact").read_text() == "included-regular-file"
    archive_patch.setenv("FERAL_HOME", str(target))
    archive_patch.setenv("FERAL_DATA_HOME", str(target / "data"))
    assert CapabilityGrantStore().is_granted("synthetic-device", "camera") is False
    assert SandboxPolicy.load_default().list_grants()[0]["mode"] == "read"
    settings = ConfigLoader(project_dir=tmp_path / "no-project").discover(load_credentials=False)
    assert settings["llm"]["max_tokens"] == 111

    async def read_restored():
        store = MemoryStore()
        try:
            assert Path(store.db_path) == target / "memory.db"
            notes = await store.list_recent()
            assert any(note["id"] == note_id and note["content"] == "Synthetic amber note" for note in notes)
            conversation = await store.conversation_get(sid)
            assert conversation["messages"] == history
            assert conversation["pinned"] is True and conversation["title_custom"] is True
            assert conversation["title"] == "Synthetic custom title"
            assert (await store.knowledge_query("Synthetic subject", "color"))[0]["object"] == "amber"
            assert (await store.wiki_get_page("synthetic-page"))["body_markdown"] == "Synthetic amber wiki"
            checkpoint = await store.runtime_checkpoint_read(sid)
            assert checkpoint.status == CheckpointStatus.READY
            assert checkpoint.record.fence == fence
            assert checkpoint.record.context.history() == history
        finally:
            await store.aclose()

    asyncio.run(read_restored())


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


def test_actual_default_helpers_are_used_without_invented_data_home(tmp_path, profile, archive_patch):
    from config import loader
    archive_patch.setenv("FERAL_HOME", str(profile[0]))
    archive_patch.setattr(loader, "feral_home", lambda: profile[0])
    archive_patch.setattr(loader, "feral_data_home", lambda: profile[1])
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


def _native_attachment(tmp_path, profile):
    primary, avatar = "fixture-session", b"disposable-avatar"
    (profile[0] / "avatars").mkdir()
    (profile[0] / "avatars" / "pet.png").write_bytes(avatar)
    (profile[0] / "native-context-recovery.json").write_text(
        '{"fixture":"recovery-journal"}'
    )
    body = {
        "format_version": 1,
        "primary_session_id": primary,
        "preferences": {
            "displayName": "Féral 👓",
            "avatarChoice": "imported",
            "onboarded": True,
            "native.savedContextModes.v1": {primary: ["fixture-chat"]},
            "feral.native.selectedConversation."
            + primary.encode().hex(): "fixture-chat",
        },
        "avatar": {
            "relative_path": "avatars/pet.png",
            "bytes": len(avatar),
            "sha256": hashlib.sha256(avatar).hexdigest(),
        },
    }
    payload = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    path = tmp_path / "explicit-native-snapshot.json"
    path.write_bytes(payload)
    return module.NativePreferenceAttachment(
        path, profile[0], profile[1], primary, hashlib.sha256(payload).hexdigest()
    ), payload


def test_native_attachment_round_trip_inventory_and_inspectable_snapshot(
    tmp_path, profile
):
    attachment, payload = _native_attachment(tmp_path, profile)
    archive = tmp_path / "native-attached.zip"
    created = create_archive(
        archive,
        offline=True,
        config_root=profile[0],
        data_root=profile[1],
        native_preferences=attachment,
    )
    assert (
        created["native_preferences_included"] is True
        and created["preferences_applied"] is False
    )
    with zipfile.ZipFile(archive) as container:
        manifest = json.loads(container.read("manifest.json"))
        assert manifest["format_version"] == 1
        assert (
            manifest["files"][module.RESERVED_ENTRY]["sha256"]
            == hashlib.sha256(payload).hexdigest()
        )
        assert container.read(module.RESERVED_ENTRY) == payload
        assert str(tmp_path) not in json.dumps(manifest)
    config, data = tmp_path / "restored-c", tmp_path / "restored-d"
    restored = restore_archive(
        archive,
        offline=True,
        config_root=config,
        data_root=data,
        native_primary_session_id="fixture-session",
    )
    assert (
        restored["native_preferences_included"] is True
        and restored["preferences_applied"] is False
    )
    assert (
        restored["runtime_started"] is False and restored["os_keys_included"] is False
    )
    assert Path(restored["native_preferences_path"]).read_bytes() == payload
    assert (
        config / "native-context-recovery.json"
    ).read_text() == '{"fixture":"recovery-journal"}'
    assert (data / "primary_session_id").read_text() == "fixture-session"
    assert attachment.snapshot_path.read_bytes() == payload
    assert not (profile[0] / ".feral-native-preferences.v1.json").exists()


@pytest.mark.parametrize("primary", [None, "foreign-primary"])
def test_native_archive_requires_reviewed_primary_before_creating_roots(
    tmp_path, profile, primary
):
    attachment, _ = _native_attachment(tmp_path, profile)
    archive = _backup(tmp_path, profile, native_preferences=attachment)
    config, data = tmp_path / "refused-c", tmp_path / "refused-d"
    with pytest.raises(ProfileArchiveError, match="reviewed primary"):
        restore_archive(
            archive,
            offline=True,
            config_root=config,
            data_root=data,
            native_primary_session_id=primary,
        )
    assert not config.exists() and not data.exists()


@pytest.mark.parametrize(
    "binding",
    [
        "config",
        "data",
        "primary",
        "sha",
        "avatar",
        "actual_primary",
        "missing_primary",
        "collision",
        "source_inside",
    ],
)
def test_native_bad_binding_never_publishes(tmp_path, profile, binding):
    from dataclasses import replace

    attachment, payload = _native_attachment(tmp_path, profile)
    if binding == "config":
        attachment = replace(attachment, config_root=tmp_path)
    elif binding == "data":
        attachment = replace(attachment, data_root=profile[0])
    elif binding == "primary":
        attachment = replace(attachment, primary_session_id="foreign-primary")
    elif binding == "sha":
        attachment = replace(attachment, snapshot_sha256="0" * 64)
    elif binding == "avatar":
        (profile[0] / "avatars" / "pet.png").write_bytes(b"changed avatar")
    elif binding == "actual_primary":
        (profile[1] / "primary_session_id").write_text("foreign-actual-primary")
    elif binding == "missing_primary":
        (profile[1] / "primary_session_id").unlink()
    elif binding == "collision":
        (profile[0] / ".feral-native-preferences.v1.json").write_text(
            "preserve existing"
        )
    else:
        path = profile[0] / "exported.json"
        path.write_bytes(payload)
        attachment = replace(attachment, snapshot_path=path)
    destination = tmp_path / "refused.zip"
    with pytest.raises(ProfileArchiveError):
        create_archive(
            destination,
            offline=True,
            config_root=profile[0],
            data_root=profile[1],
            native_preferences=attachment,
        )
    assert not destination.exists() and not list(tmp_path.glob(".feral-archive-*"))
    assert attachment.snapshot_path.read_bytes() == payload


@pytest.mark.parametrize("changed", ["snapshot", "primary", "avatar"])
def test_native_input_drift_refuses_publication(
    tmp_path, profile, monkeypatch, changed
):
    attachment, _ = _native_attachment(tmp_path, profile)
    original, calls = module._snapshot, 0

    def drifting(roots, limits):
        nonlocal calls
        result = original(roots, limits)
        calls += 1
        if calls == 2:
            if changed == "snapshot":
                attachment.snapshot_path.write_bytes(b"changed private fixture")
            elif changed == "primary":
                (profile[1] / "primary_session_id").write_text("changed-primary")
            else:
                (profile[0] / "avatars" / "pet.png").write_bytes(b"changed image")
        return result

    monkeypatch.setattr(module, "_snapshot", drifting)
    destination = tmp_path / "drift.zip"
    with pytest.raises(ProfileArchiveError):
        create_archive(
            destination,
            offline=True,
            config_root=profile[0],
            data_root=profile[1],
            native_preferences=attachment,
        )
    assert not destination.exists() and not list(tmp_path.glob(".feral-archive-*"))


@pytest.mark.parametrize(
    "malformed", ["credential", "avatar", "actual_primary", "metadata", "missing_entry"]
)
def test_native_archive_semantic_refusal_precedes_root_creation(
    tmp_path, profile, malformed
):
    attachment, _ = _native_attachment(tmp_path, profile)
    archive = _backup(tmp_path, profile, native_preferences=attachment)
    invalid = tmp_path / "invalid-native.zip"

    def mutate(manifest, files):
        if malformed == "metadata":
            manifest["native_preferences"]["format_version"] = 2
            return
        if malformed == "missing_entry":
            files.pop(module.RESERVED_ENTRY)
            manifest["files"].pop(module.RESERVED_ENTRY)
            return
        name = module.RESERVED_ENTRY
        body = json.loads(files[name])
        if malformed == "credential":
            body["preferences"]["api_key"] = "fixture-private-sentinel"
        elif malformed == "avatar":
            body["avatar"]["sha256"] = "0" * 64
        else:
            name = "data/primary_session_id"
            files[name] = b"foreign-actual-primary"
        if malformed != "actual_primary":
            files[name] = json.dumps(
                body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        manifest["files"][name]["sha256"] = hashlib.sha256(files[name]).hexdigest()
        manifest["files"][name]["size"] = len(files[name])

    _rewrite(archive, invalid, mutate)
    config, data = tmp_path / "invalid-c", tmp_path / "invalid-d"
    with pytest.raises(ProfileArchiveError) as error:
        restore_archive(
            invalid,
            offline=True,
            config_root=config,
            data_root=data,
            native_primary_session_id="fixture-session",
        )
    assert not config.exists() and not data.exists()
    assert "fixture-private-sentinel" not in str(error.value)


def test_native_cli_explicit_round_trip_and_missing_binding(tmp_path, profile):
    attachment, _ = _native_attachment(tmp_path, profile)
    archive = tmp_path / "native-cli.zip"
    command = [
        sys.executable,
        "-m",
        "config.profile_archive",
        "backup",
        str(archive),
        "--offline",
        "--config-root",
        str(profile[0]),
        "--data-root",
        str(profile[1]),
        "--native-preferences",
        str(attachment.snapshot_path),
        "--primary-session-id",
        "fixture-session",
        "--native-preferences-sha256",
        attachment.snapshot_sha256,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["preferences_applied"] is False
    restore = [
        sys.executable,
        "-m",
        "config.profile_archive",
        "restore",
        str(archive),
        "--offline",
        "--config-root",
        str(tmp_path / "cli-restored-c"),
        "--data-root",
        str(tmp_path / "cli-restored-d"),
    ]
    refused = subprocess.run(restore, capture_output=True, text=True, timeout=30)
    assert refused.returncode == 2 and "Traceback" not in refused.stderr
    completed = subprocess.run(
        restore + ["--primary-session-id", "fixture-session"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["runtime_started"] is False
    incomplete = subprocess.run(
        command[:-2], capture_output=True, text=True, timeout=30
    )
    assert incomplete.returncode == 2 and "Traceback" not in incomplete.stderr
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


def test_inverse_nested_roots_and_in_profile_destination_are_rejected(tmp_path, profile):
    nested = profile[0] / "nested"
    nested.mkdir()
    with pytest.raises(ProfileArchiveError, match="overlap"):
        _backup(tmp_path, (nested, profile[0]))
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


def _native_default_layout(tmp_path):
    """BrainRuntime sets FERAL_DATA_HOME to FERAL_HOME/data by default."""
    config = tmp_path / "native-home"
    data = config / "data"
    data.mkdir(parents=True)
    (config / "settings.json").write_text('{"llm":{"max_tokens":111}}')
    (data / "primary_session_id").write_text("fixture-session")
    (data / "runtime.bin").write_bytes(b"synthetic-runtime-data")
    (data / "empty").mkdir()
    # An unrelated config-level file must never substitute for actual data identity.
    (config / "primary_session_id").write_text("foreign-config-primary")
    return config, data


def test_native_default_nested_layout_archives_once_and_restores_existing_loader(
    tmp_path,
    archive_patch,
):
    from config.loader import ConfigLoader, feral_data_home, feral_home

    config, data = _native_default_layout(tmp_path)
    archive_patch.setenv("FERAL_HOME", str(config))
    archive_patch.setenv("FERAL_DATA_HOME", str(data))
    # The native launcher sets both variables, but the current loader prioritizes
    # FERAL_HOME and does not consume FERAL_DATA_HOME. Explicit archive bindings
    # must preserve the reviewed nested directory without changing that reader.
    assert feral_home() == config and feral_data_home() == config
    archive = tmp_path / "nested-default.zip"
    receipt = create_archive(archive, offline=True, config_root=config, data_root=data)
    with zipfile.ZipFile(archive) as container:
        manifest = json.loads(container.read("manifest.json"))
        assert manifest["roots"] == {"config": "config", "data": "config"}
        assert manifest["nested_roots"] == {
            "data": {"parent": "config", "relative_path": "data"},
        }
        assert "config/data/empty" in manifest["directories"]
        assert set(manifest["files"]) == {
            "config/settings.json",
            "config/primary_session_id",
            "config/data/primary_session_id",
            "config/data/runtime.bin",
        }
        assert receipt["bytes"] == sum(
            len(container.read(name)) for name in manifest["files"]
        )
        assert receipt["files"] == len(manifest["files"]) == 4
    restored = tmp_path / "restored-native-home"
    result = restore_archive(
        archive,
        offline=True,
        config_root=restored,
        data_root=restored / "data",
    )
    assert result["runtime_started"] is False
    assert (restored / "data" / "runtime.bin").read_bytes() == b"synthetic-runtime-data"
    assert (restored / "data" / "empty").is_dir()
    archive_patch.setenv("FERAL_HOME", str(restored))
    archive_patch.setenv("FERAL_DATA_HOME", str(restored / "data"))
    reader = ConfigLoader(project_dir=tmp_path / "no-project")
    assert reader.user_home == restored and reader.data_home == restored
    assert reader.discover(load_credentials=False)["llm"]["max_tokens"] == 111
    assert (data / "runtime.bin").read_bytes() == b"synthetic-runtime-data"


def test_native_attachment_nested_layout_binds_actual_data_primary(tmp_path):
    profile = _native_default_layout(tmp_path)
    attachment, payload = _native_attachment(tmp_path, profile)
    archive = _backup(tmp_path, profile, native_preferences=attachment)
    restored = tmp_path / "nested-attached-restore"
    receipt = restore_archive(
        archive,
        offline=True,
        config_root=restored,
        data_root=restored / "data",
        native_primary_session_id="fixture-session",
    )
    assert Path(receipt["native_preferences_path"]).read_bytes() == payload
    assert receipt["native_primary_session_id"] == "fixture-session"
    assert (
        receipt["preferences_applied"] is False and receipt["runtime_started"] is False
    )
    assert (restored / "data" / "primary_session_id").read_text() == "fixture-session"
    assert (restored / "primary_session_id").read_text() == "foreign-config-primary"
    assert (restored / "avatars" / "pet.png").read_bytes() == b"disposable-avatar"


@pytest.mark.parametrize("target", ["same", "split", "different_nested"])
def test_nested_archive_refuses_layout_change_before_root_creation(tmp_path, target):
    profile = _native_default_layout(tmp_path)
    archive = _backup(tmp_path, profile)
    config = tmp_path / "refused-layout"
    data = (
        config
        if target == "same"
        else tmp_path / "split-data"
        if target == "split"
        else config / "other-data"
    )
    with pytest.raises(ProfileArchiveError, match="layout"):
        restore_archive(archive, offline=True, config_root=config, data_root=data)
    assert not config.exists() and not data.exists()


def test_nested_archive_preserves_deeper_data_offset_and_counts_once(tmp_path):
    config, original_data = _native_default_layout(tmp_path)
    data = config / "runtime" / "data"
    data.parent.mkdir()
    original_data.rename(data)
    archive = _backup(tmp_path, (config, data), limits=ArchiveLimits(max_entries=8))
    with zipfile.ZipFile(archive) as container:
        manifest = json.loads(container.read("manifest.json"))
        assert manifest["nested_roots"]["data"]["relative_path"] == "runtime/data"
        assert "config/runtime/data/primary_session_id" in manifest["files"]
        assert "config/runtime" in manifest["directories"]
        assert "config/runtime/data" in manifest["directories"]
    restored = tmp_path / "deep-restore"
    restore_archive(
        archive,
        offline=True,
        config_root=restored,
        data_root=restored / "runtime" / "data",
    )
    assert (restored / "runtime" / "data" / "empty").is_dir()
    with pytest.raises(ProfileArchiveError, match="bound"):
        create_archive(
            tmp_path / "small.zip",
            config_root=config,
            data_root=data,
            offline=True,
            limits=ArchiveLimits(max_entries=7),
        )
    assert not (tmp_path / "small.zip").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign_review",
        "actual_primary",
        "missing_primary",
        "avatar",
        "data_redirect",
    ],
)
def test_nested_native_attachment_refuses_bad_binding_without_publication(
    tmp_path, mutation
):
    from dataclasses import replace

    profile = _native_default_layout(tmp_path)
    attachment, _ = _native_attachment(tmp_path, profile)
    if mutation == "foreign_review":
        attachment = replace(attachment, data_root=profile[0])
    elif mutation == "actual_primary":
        (profile[0] / "primary_session_id").write_text("fixture-session")
        (profile[1] / "primary_session_id").write_text("foreign-data-primary")
    elif mutation == "missing_primary":
        (profile[0] / "primary_session_id").write_text("fixture-session")
        (profile[1] / "primary_session_id").unlink()
    elif mutation == "avatar":
        (profile[0] / "avatars" / "pet.png").write_bytes(b"changed-avatar")
    else:
        moved = tmp_path / "moved-data"
        profile[1].rename(moved)
        profile[1].symlink_to(moved, target_is_directory=True)
    with pytest.raises(ProfileArchiveError):
        _backup(tmp_path, profile, native_preferences=attachment)
    assert not (tmp_path / "profile.zip").exists()
    assert not list(tmp_path.glob(".feral-archive-*"))


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        [],
        {},
        {"data": "data"},
        {"data": {"parent": "data", "relative_path": "data"}},
        {"data": {"parent": "config", "relative_path": "data", "extra": True}},
        {"data": {"parent": "config", "relative_path": "../data"}},
        {"data": {"parent": "config", "relative_path": "/data"}},
        {"data": {"parent": "config", "relative_path": "data/"}},
        {"data": {"parent": "config", "relative_path": "a" * 513}},
        {"data": {"parent": "config", "relative_path": "/".join(["a"] * 17)}},
        {"data": {"parent": "config", "relative_path": "missing-directory"}},
        {"data": {"parent": "config", "relative_path": "settings.json"}},
    ],
)
def test_malformed_nested_descriptor_refuses_before_new_roots(tmp_path, malformed):
    profile = _native_default_layout(tmp_path)
    archive = _backup(tmp_path, profile)
    invalid = tmp_path / "malformed-nested.zip"
    _rewrite(
        archive,
        invalid,
        lambda manifest, files: manifest.update(nested_roots=malformed),
    )
    config = tmp_path / "malformed-restore"
    with pytest.raises(ProfileArchiveError):
        restore_archive(
            invalid, offline=True, config_root=config, data_root=config / "data"
        )
    assert not config.exists()


def test_duplicate_nested_descriptor_refuses_before_new_roots(tmp_path):
    profile = _native_default_layout(tmp_path)
    archive = _backup(tmp_path, profile)
    invalid = tmp_path / "duplicate-nested.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(invalid, "w") as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename == "manifest.json":
                manifest = json.loads(payload)
                payload = json.dumps(manifest, separators=(",", ":")).encode()
                payload = payload.replace(
                    b'"parent":"config"', b'"parent":"config","parent":"config"', 1
                )
            target.writestr(item, payload)
    config = tmp_path / "duplicate-restore"
    with pytest.raises(ProfileArchiveError, match="Duplicate"):
        restore_archive(
            invalid, offline=True, config_root=config, data_root=config / "data"
        )
    assert not config.exists()


def test_missing_nested_data_directory_never_publishes(tmp_path):
    config = tmp_path / "no-data-home"
    config.mkdir()
    (config / "settings.json").write_text("{}")
    with pytest.raises(ProfileArchiveError, match="missing"):
        _backup(tmp_path, (config, config / "data"))
    assert not (tmp_path / "profile.zip").exists()


def test_nested_archive_rejects_source_and_restore_ancestor_redirects(tmp_path):
    config, data = _native_default_layout(tmp_path)
    archive = _backup(tmp_path, (config, data))
    alias = tmp_path / "ancestor-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ProfileArchiveError, match="redirect"):
        create_archive(
            tmp_path / "alias.zip",
            offline=True,
            config_root=alias / config.name,
            data_root=alias / config.name / "data",
        )
    assert not (tmp_path / "alias.zip").exists()
    with pytest.raises(ProfileArchiveError, match="redirect"):
        restore_archive(
            archive,
            offline=True,
            config_root=alias / "refused-root",
            data_root=alias / "refused-root" / "data",
        )
    assert not (tmp_path / "refused-root").exists()


def test_nested_native_archive_cannot_redirect_primary_to_config_level(tmp_path):
    profile = _native_default_layout(tmp_path)
    attachment, _ = _native_attachment(tmp_path, profile)
    archive = _backup(tmp_path, profile, native_preferences=attachment)
    invalid = tmp_path / "wrong-nested-primary.zip"

    def mutate(manifest, files):
        name = "config/data/primary_session_id"
        files[name] = b"foreign-data-primary"
        manifest["files"][name]["size"] = len(files[name])
        manifest["files"][name]["sha256"] = hashlib.sha256(files[name]).hexdigest()
        # A matching config-level identity still must not override data identity.
        name = "config/primary_session_id"
        files[name] = b"fixture-session"
        manifest["files"][name]["size"] = len(files[name])
        manifest["files"][name]["sha256"] = hashlib.sha256(files[name]).hexdigest()

    _rewrite(archive, invalid, mutate)
    restored = tmp_path / "foreign-data-restore"
    with pytest.raises(ProfileArchiveError, match="actual profile primary"):
        restore_archive(
            invalid,
            offline=True,
            config_root=restored,
            data_root=restored / "data",
            native_primary_session_id="fixture-session",
        )
    assert not restored.exists()


def test_nested_native_cli_roundtrip_preserves_reviewed_data_directory(tmp_path):
    profile = _native_default_layout(tmp_path)
    attachment, payload = _native_attachment(tmp_path, profile)
    archive = tmp_path / "nested-cli.zip"
    command = [
        sys.executable,
        "-m",
        "config.profile_archive",
        "backup",
        str(archive),
        "--offline",
        "--config-root",
        str(profile[0]),
        "--data-root",
        str(profile[1]),
        "--native-preferences",
        str(attachment.snapshot_path),
        "--native-preferences-sha256",
        attachment.snapshot_sha256,
        "--primary-session-id",
        "fixture-session",
    ]
    backed_up = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert backed_up.returncode == 0, backed_up.stderr
    assert json.loads(backed_up.stdout)["native_preferences_included"] is True
    restored = tmp_path / "cli-nested-restore"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "config.profile_archive",
            "restore",
            str(archive),
            "--offline",
            "--config-root",
            str(restored),
            "--data-root",
            str(restored / "data"),
            "--primary-session-id",
            "fixture-session",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    assert Path(receipt["native_preferences_path"]).read_bytes() == payload
    assert (restored / "data" / "primary_session_id").read_text() == "fixture-session"
    assert (
        receipt["preferences_applied"] is False and receipt["runtime_started"] is False
    )


@pytest.mark.parametrize("mutation", ["missing_descriptor", "split_alias"])
def test_nested_layout_metadata_cannot_collapse_or_duplicate_inventory(
    tmp_path, mutation
):
    profile = _native_default_layout(tmp_path)
    archive = _backup(tmp_path, profile)
    invalid = tmp_path / "collapsed-layout.zip"

    def mutate(manifest, files):
        if mutation == "missing_descriptor":
            manifest.pop("nested_roots")
        else:
            manifest["roots"]["data"] = "data"

    _rewrite(archive, invalid, mutate)
    restored = tmp_path / "collapsed-restore"
    with pytest.raises(ProfileArchiveError, match="layout|single parent"):
        restore_archive(
            invalid, offline=True, config_root=restored, data_root=restored / "data"
        )
    assert not restored.exists()
