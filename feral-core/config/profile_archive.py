"""Bounded cold-profile archives. Callers must stop every profile writer first.

This module never starts the brain, unlocks a vault, exports an OS key, applies a
migration, or replaces an existing profile. A sync bundle is a different format.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from dataclasses import dataclass
import zipfile
from collections.abc import Collection

from config.native_preference_snapshot import (
    MAX_SNAPSHOT_BYTES, RESERVED_ENTRY, NativePreferenceSnapshot,
    NativePreferenceSnapshotError, valid_digest, valid_id, validate_snapshot,
)


class ProfileArchiveError(ValueError):
    """The archive or explicit cold-profile precondition could not be verified."""


@dataclass(frozen=True)
class ArchiveLimits:
    max_entries: int = 20_000
    max_total_bytes: int = 1_073_741_824
    max_file_bytes: int = 268_435_456
    max_manifest_bytes: int = 4_194_304

    def validate(self) -> None:
        if any(type(value) is not int or value <= 0 for value in (
            self.max_entries, self.max_total_bytes, self.max_file_bytes,
            self.max_manifest_bytes,
        )):
            raise ProfileArchiveError("Archive limits must be positive integers")


@dataclass(frozen=True)
class _File:
    size: int
    digest: str
    mode: int
    identity: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class NativePreferenceAttachment:
    """Reviewed explicit bindings; no identity/root is inferred from the payload."""
    snapshot_path: Path
    config_root: Path
    data_root: Path
    primary_session_id: str
    snapshot_sha256: str


_MANIFEST = "manifest.json"
_CHUNK = 65_536


def _offline(offline: bool) -> None:
    if offline is not True:
        raise ProfileArchiveError("Stop all profile writers and explicitly select offline mode")


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _name(value: object) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
        raise ProfileArchiveError("Invalid bounded archive path")
    path = PurePosixPath(value)
    if (str(path) != value or len(path.parts) < 2 or len(path.parts) > 32
            or path.parts[0] not in {"config", "data"}
            or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
            or "\\" in value or "\x00" in value):
        raise ProfileArchiveError("Invalid archive path")
    return value


def _nested_layout(value: object) -> dict[str, dict[str, str]]:
    if (not isinstance(value, dict) or set(value) != {"data"}
            or not isinstance(value["data"], dict)
            or set(value["data"]) != {"parent", "relative_path"}
            or value["data"]["parent"] != "config"):
        raise ProfileArchiveError("Invalid nested root layout")
    relative = value["data"]["relative_path"]
    if (not isinstance(relative, str) or not relative
            or len(relative.encode("utf-8")) > 512
            or len(PurePosixPath(relative).parts) > 16):
        raise ProfileArchiveError("Invalid bounded nested root layout")
    _name("config/" + relative)
    return {"data": {"parent": "config", "relative_path": relative}}


def _roots(config_root: Path, data_root: Path
           ) -> tuple[dict[str, Path], dict[str, str], dict[str, dict[str, str]] | None]:
    raw = {"config": Path(config_root).absolute(), "data": Path(data_root).absolute()}
    if any(path.is_symlink() for path in raw.values()):
        raise ProfileArchiveError("Profile roots cannot be symlinks")
    paths = {key: path.resolve() for key, path in raw.items()}
    if any(raw[key] != path for key, path in paths.items()):
        raise ProfileArchiveError("Profile root ancestors cannot redirect through symlinks")
    if paths["config"] == paths["data"]:
        return {"config": paths["config"]}, {"config": "config", "data": "config"}, None
    if paths["config"] in paths["data"].parents:
        nested = _nested_layout({"data": {"parent": "config",
            "relative_path": paths["data"].relative_to(paths["config"]).as_posix()}})
        # Inventory the parent exactly once; data is an explicitly bound offset.
        return {"config": paths["config"]}, {"config": "config", "data": "config"}, nested
    if paths["data"] in paths["config"].parents:
        raise ProfileArchiveError("Profile roots cannot overlap")
    return paths, {"config": "config", "data": "data"}, None


def _selected_root(roots: dict[str, Path], aliases: dict[str, str],
                   nested: dict[str, dict[str, str]] | None, label: str) -> Path:
    selected = roots[aliases[label]]
    if label == "data" and nested is not None:
        selected /= nested["data"]["relative_path"]
    return selected


def _primary_entry(aliases: dict[str, str], nested: dict[str, dict[str, str]] | None) -> str:
    prefix = aliases["data"]
    if nested is not None:
        prefix += "/" + nested["data"]["relative_path"]
    return prefix + "/primary_session_id"


def _require_nested_directory(nested: dict[str, dict[str, str]] | None,
                              directories: Collection[str]) -> None:
    if nested is not None and "config/" + nested["data"]["relative_path"] not in directories:
        raise ProfileArchiveError("Nested data root is missing from directory inventory")


def _read_file(path: Path, limit: int) -> _File:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ProfileArchiveError("Unsupported or oversized profile file")
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ProfileArchiveError("Profile changed during snapshot")
        while chunk := stream.read(_CHUNK):
            size += len(chunk)
            if size > limit:
                raise ProfileArchiveError("Profile file exceeds bound")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if size != before.st_size or _identity(after) != _identity(before):
        raise ProfileArchiveError("Profile changed during snapshot")
    mode = 0o700 if before.st_mode & 0o111 else 0o600
    return _File(size, digest.hexdigest(), mode, _identity(before))


def _inventory(roots: dict[str, Path], limits: ArchiveLimits
               ) -> tuple[dict[str, _File], dict[str, tuple[int, int, int, int, int]]]:
    files: dict[str, _File] = {}
    directories: dict[str, tuple[int, int, int, int, int]] = {}
    total = 0
    for label, root in roots.items():
        root_info = root.lstat()
        if not stat.S_ISDIR(root_info.st_mode):
            raise ProfileArchiveError("Profile root must be a directory")
        directories[label] = _identity(root_info)
        pending = [root]
        while pending:
            directory = pending.pop()
            for path in sorted(directory.iterdir()):
                name = _name(f"{label}/{path.relative_to(root).as_posix()}")
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    directories[name] = _identity(info)
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    item = _read_file(path, limits.max_file_bytes)
                    total += item.size
                    files[name] = item
                else:
                    raise ProfileArchiveError("Profile contains a symlink or nonregular entry")
                if len(files) + len(directories) > limits.max_entries or total > limits.max_total_bytes:
                    raise ProfileArchiveError("Profile exceeds archive bounds")
    return files, directories


def _snapshot(roots: dict[str, Path], limits: ArchiveLimits
              ) -> tuple[dict[str, _File], dict[str, tuple[int, int, int, int, int]]]:
    try:
        return _inventory(roots, limits)
    except OSError as error:
        raise ProfileArchiveError("Profile is unavailable or changed during snapshot") from error


def _read_native_attachment(attachment: NativePreferenceAttachment, roots: dict[str, Path],
                            aliases: dict[str, str], nested: dict[str, dict[str, str]] | None
                            ) -> tuple[NativePreferenceSnapshot, _File]:
    try:
        if (not isinstance(attachment, NativePreferenceAttachment)
                or not valid_digest(attachment.snapshot_sha256) or not valid_id(attachment.primary_session_id)):
            raise ProfileArchiveError("Invalid reviewed native preference attachment")
        for label, reviewed in (("config", attachment.config_root), ("data", attachment.data_root)):
            path = Path(reviewed).absolute()
            if path != path.resolve() or path != _selected_root(roots, aliases, nested, label):
                raise ProfileArchiveError("Native preference reviewed roots do not match")
        path = Path(attachment.snapshot_path).absolute()
        if path != path.resolve():
            raise ProfileArchiveError("Native preference snapshot cannot be redirected")
        if any(path == root or root in path.parents for root in roots.values()):
            raise ProfileArchiveError("Native preference input must be outside selected profile roots")
        item = _read_file(path, MAX_SNAPSHOT_BYTES)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if _identity(os.fstat(stream.fileno())) != item.identity:
                raise ProfileArchiveError("Native preference snapshot changed")
            payload = stream.read(MAX_SNAPSHOT_BYTES + 1)
            if _identity(os.fstat(stream.fileno())) != item.identity:
                raise ProfileArchiveError("Native preference snapshot changed")
        if (_identity(path.lstat()) != item.identity or item.digest != attachment.snapshot_sha256
                or hashlib.sha256(payload).hexdigest() != item.digest):
            raise ProfileArchiveError("Native preference snapshot changed or differs from review")
        return validate_snapshot(payload, attachment.primary_session_id), item
    except (OSError, NativePreferenceSnapshotError) as error:
        raise ProfileArchiveError("Native preference attachment could not be verified") from error


def _native_primary_bytes(payload: bytes, snapshot: NativePreferenceSnapshot) -> None:
    try:
        primary = payload.decode("utf-8").strip()
    except UnicodeError as error:
        raise ProfileArchiveError("Native preference profile primary cannot be verified") from error
    if len(payload) > 1024 or not valid_id(primary) or primary != snapshot.primary_session_id:
        raise ProfileArchiveError("Native preference snapshot differs from actual profile primary")


def _native_source_primary(snapshot: NativePreferenceSnapshot, roots: dict[str, Path],
                           aliases: dict[str, str], nested: dict[str, dict[str, str]] | None,
                           files: dict[str, _File]) -> None:
    name = _primary_entry(aliases, nested)
    item = files.get(name)
    if item is None or item.size > 1024:
        raise ProfileArchiveError("Native preferences require a persisted primary installation")
    path = _selected_root(roots, aliases, nested, "data") / "primary_session_id"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if _identity(os.fstat(stream.fileno())) != item.identity:
                raise ProfileArchiveError("Profile primary changed during snapshot")
            payload = stream.read(1025)
            if _identity(os.fstat(stream.fileno())) != item.identity:
                raise ProfileArchiveError("Profile primary changed during snapshot")
        if hashlib.sha256(payload).hexdigest() != item.digest:
            raise ProfileArchiveError("Profile primary changed during snapshot")
        _native_primary_bytes(payload, snapshot)
    except OSError as error:
        raise ProfileArchiveError("Native preference profile primary cannot be verified") from error


def _native_avatar_inventory(snapshot: NativePreferenceSnapshot, files: dict) -> None:
    if snapshot.avatar is None:
        return
    item = files.get("config/" + snapshot.avatar.relative_path)
    size = item.size if isinstance(item, _File) else item.get("size") if isinstance(item, dict) else None
    digest = item.digest if isinstance(item, _File) else item.get("sha256") if isinstance(item, dict) else None
    if size != snapshot.avatar.size or digest != snapshot.avatar.sha256:
        raise ProfileArchiveError("Native avatar does not match selected profile inventory")


def _native_source_avatar(snapshot: NativePreferenceSnapshot, roots: dict[str, Path], files: dict[str, _File]) -> None:
    if snapshot.avatar is not None:
        try:
            observed = _read_file(roots["config"] / snapshot.avatar.relative_path, snapshot.avatar.size)
        except OSError as error:
            raise ProfileArchiveError("Native avatar changed before publication") from error
        if observed != files["config/" + snapshot.avatar.relative_path]:
            raise ProfileArchiveError("Native avatar changed before publication")


def _native_metadata(archive: zipfile.ZipFile, manifest: dict, primary: str | None) -> NativePreferenceSnapshot | None:
    if "native_preferences" not in manifest:
        return None
    metadata = manifest["native_preferences"]
    if (not isinstance(metadata, dict) or set(metadata) != {"format_version", "entry", "primary_session_id"}
            or type(metadata["format_version"]) is not int or metadata["format_version"] != 1
            or metadata["entry"] != RESERVED_ENTRY or not valid_id(metadata["primary_session_id"])
            or primary != metadata["primary_session_id"]):
        raise ProfileArchiveError("Native preference archive requires its exact reviewed primary installation")
    item = manifest["files"].get(RESERVED_ENTRY)
    if not isinstance(item, dict) or item["size"] > MAX_SNAPSHOT_BYTES or item["mode"] != 0o600:
        raise ProfileArchiveError("Invalid native preference archive entry")
    try:
        snapshot = validate_snapshot(archive.read(RESERVED_ENTRY), metadata["primary_session_id"])
    except NativePreferenceSnapshotError as error:
        raise ProfileArchiveError("Native preference archive payload could not be verified") from error
    _native_avatar_inventory(snapshot, manifest["files"])
    primary_entry = _primary_entry(manifest["roots"], manifest.get("nested_roots"))
    primary_item = manifest["files"].get(primary_entry)
    if not isinstance(primary_item, dict) or primary_item["size"] > 1024:
        raise ProfileArchiveError("Native preferences require an archived primary installation")
    _native_primary_bytes(archive.read(primary_entry), snapshot)
    return snapshot


def _check_default_layout(roots: dict[str, Path]) -> None:
    """Refuse omitted legacy storage; never expand a caller's chosen roots.

    Capability grants, pairing and other runtime stores still use FERAL_HOME
    or ~/.feral independently of the loader's XDG config/data selection.
    An uncovered nonempty root cannot be assumed irrelevant to a cold backup.
    This checks absence/emptiness only, without reading any omitted payload.
    """
    override = os.environ.get("FERAL_HOME")
    if override == "":
        # Some legacy stores treat '' as cwd; the loader and other stores
        # fall back to ~/.feral. Neither root alone proves complete coverage.
        raise ProfileArchiveError("Default storage layout is ambiguous with empty FERAL_HOME")
    legacy = Path(override if override is not None else Path.home() / ".feral").absolute()
    try:
        info = legacy.lstat()
    except FileNotFoundError:
        return
    except (OSError, RuntimeError) as error:
        raise ProfileArchiveError("Legacy runtime storage coverage cannot be verified") from error
    if not stat.S_ISDIR(info.st_mode):
        raise ProfileArchiveError("Legacy runtime storage must be a nonsymlink directory")
    try:
        resolved = legacy.resolve()
    except (OSError, RuntimeError) as error:
        raise ProfileArchiveError("Legacy runtime storage coverage cannot be verified") from error
    if any(resolved == root or root in resolved.parents for root in roots.values()):
        return
    try:
        descriptor = os.open(legacy, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if _identity(os.fstat(descriptor)) != _identity(info):
                raise ProfileArchiveError("Legacy runtime storage changed during coverage check")
            if _identity(legacy.lstat()) != _identity(info):
                raise ProfileArchiveError("Legacy runtime storage changed during coverage check")
            with os.scandir(descriptor) as entries:
                if next(entries, None) is not None:
                    raise ProfileArchiveError(
                        "Default profile roots omit legacy runtime storage; "
                        "explicit roots select only a partial cold snapshot"
                    )
            if _identity(os.fstat(descriptor)) != _identity(info):
                raise ProfileArchiveError("Legacy runtime storage changed during coverage check")
            if _identity(legacy.lstat()) != _identity(info):
                raise ProfileArchiveError("Legacy runtime storage changed during coverage check")
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ProfileArchiveError("Legacy runtime storage coverage cannot be verified") from error


def create_archive(destination: Path, *, offline: bool = False,
                   config_root: Path | None = None, data_root: Path | None = None,
                   limits: ArchiveLimits = ArchiveLimits(),
                   native_preferences: NativePreferenceAttachment | None = None) -> dict:
    """Archive selected roots without mutating source files or exporting keys.

    `offline=True` is a caller precondition, not inferred process quiescence.
    The full regular-file set includes SQLite WAL/SHM and encrypted artifacts.
    Hashes and file/directory identities detect changes during the cold copy.
    Default selection refuses uncovered legacy runtime storage. Explicit roots
    are a caller-selected snapshot, not proof of full deployment coverage.
    OS defaults are never read. A separately reviewed native snapshot may be
    attached explicitly; OS keys and credential portability remain excluded.
    """
    _offline(offline)
    limits.validate()
    default_selection = config_root is None or data_root is None
    if config_root is None or data_root is None:
        from config.loader import feral_data_home, feral_home
        config_root = feral_home() if config_root is None else config_root
        data_root = feral_data_home() if data_root is None else data_root
    roots, aliases, nested = _roots(config_root, data_root)
    native = None
    native_item = None
    if native_preferences is not None:
        if default_selection:
            raise ProfileArchiveError("Native preferences require explicit reviewed config and data roots")
        native, native_item = _read_native_attachment(native_preferences, roots, aliases, nested)
    if default_selection:
        _check_default_layout(roots)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ProfileArchiveError("Archive destination already exists")
    parent = destination.parent.resolve()
    if any(parent == root or root in parent.parents for root in roots.values()):
        raise ProfileArchiveError("Archive destination must be outside profile roots")
    source_files, directories = _snapshot(roots, limits)
    _require_nested_directory(nested, directories)
    files = dict(source_files)
    if native is not None and native_item is not None:
        if RESERVED_ENTRY in files or RESERVED_ENTRY in directories:
            raise ProfileArchiveError("Reserved native preference entry already exists")
        _native_avatar_inventory(native, files)
        _native_source_primary(native, roots, aliases, nested, source_files)
        files[RESERVED_ENTRY] = _File(len(native.payload), native.sha256, 0o600, native_item.identity)
        if (len(files) + len(directories) > limits.max_entries
                or sum(item.size for item in files.values()) > limits.max_total_bytes
                or len(native.payload) > limits.max_file_bytes):
            raise ProfileArchiveError("Native preference attachment exceeds archive bounds")
    manifest = {
        "format_version": 1, "offline_required": True, "roots": aliases,
        "os_keys_included": False, "credentials_portable": False,
        "files": {name: {"size": item.size, "sha256": item.digest, "mode": item.mode}
                  for name, item in files.items()},
        "directories": sorted(name for name in directories if name not in roots),
    }
    if nested is not None:
        manifest["nested_roots"] = nested
    if native is not None:
        manifest["native_preferences"] = {"format_version": 1, "entry": RESERVED_ENTRY,
                                          "primary_session_id": native.primary_session_id}
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > limits.max_manifest_bytes:
        raise ProfileArchiveError("Archive manifest exceeds bound")
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".feral-archive-", dir=parent)
    except OSError as error:
        raise ProfileArchiveError("Archive destination is unavailable") from error
    staging = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(_MANIFEST, encoded)
                for name, item in files.items():
                    label, relative = name.split("/", 1)
                    source = roots[label] / relative
                    info = zipfile.ZipInfo(name)
                    info.external_attr = (stat.S_IFREG | item.mode) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    if native is not None and name == RESERVED_ENTRY:
                        archive.writestr(info, native.payload)
                        continue
                    digest = hashlib.sha256()
                    size = 0
                    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, "rb") as stream, archive.open(info, "w") as target:
                        if _identity(os.fstat(stream.fileno())) != item.identity:
                            raise ProfileArchiveError("Profile changed during snapshot")
                        while chunk := stream.read(_CHUNK):
                            size += len(chunk)
                            if size > item.size:
                                raise ProfileArchiveError("Profile changed during snapshot")
                            digest.update(chunk)
                            target.write(chunk)
                        if size != item.size or digest.hexdigest() != item.digest:
                            raise ProfileArchiveError("Profile changed during snapshot")
            output.flush()
            os.fsync(output.fileno())
        if _snapshot(roots, limits) != (source_files, directories):
            raise ProfileArchiveError("Profile changed during snapshot")
        if native is not None and native_preferences is not None:
            if _read_native_attachment(native_preferences, roots, aliases, nested) != (native, native_item):
                raise ProfileArchiveError("Native preference attachment changed before publication")
            _native_source_primary(native, roots, aliases, nested, source_files)
            _native_source_avatar(native, roots, source_files)
        if default_selection:
            _check_default_layout(roots)
        # Hard-link publication is exclusive, unlike replace/rename over a file.
        os.link(staging, destination)
        receipt = {"status": "completed", "files": len(files),
                "bytes": sum(item.size for item in files.values()),
                "coverage": "selected_roots_only", "native_preferences_included": native is not None,
                "os_keys_included": False, "credentials_portable": False}
        if native is not None:
            receipt.update(native_preferences_entry=RESERVED_ENTRY, native_preferences_sha256=native.sha256,
                           native_primary_session_id=native.primary_session_id, preferences_applied=False)
        return receipt
    except (OSError, zipfile.BadZipFile) as error:
        raise ProfileArchiveError("Archive creation could not be completed") from error
    finally:
        staging.unlink(missing_ok=True)


def _json_unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProfileArchiveError("Duplicate manifest key")
        result[key] = value
    return result


def _validate(archive: zipfile.ZipFile, limits: ArchiveLimits) -> dict:
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    if len(names) != len(set(names)) or len(names) > limits.max_entries + 1:
        raise ProfileArchiveError("Duplicate or excessive archive entries")
    if any(stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)
           or entry.flag_bits & 1 for entry in entries):
        raise ProfileArchiveError("Unsupported archive entry")
    if _MANIFEST not in names or archive.getinfo(_MANIFEST).file_size > limits.max_manifest_bytes:
        raise ProfileArchiveError("Missing or oversized manifest")
    manifest = json.loads(archive.read(_MANIFEST), object_pairs_hook=_json_unique)
    if (not isinstance(manifest, dict) or type(manifest.get("format_version")) is not int
            or manifest["format_version"] != 1 or manifest.get("offline_required") is not True
            or manifest.get("os_keys_included") is not False
            or manifest.get("credentials_portable") is not False):
        raise ProfileArchiveError("Unsupported archive contract")
    roots = manifest.get("roots")
    if roots not in ({"config": "config", "data": "config"}, {"config": "config", "data": "data"}):
        raise ProfileArchiveError("Invalid root mapping")
    nested = _nested_layout(manifest["nested_roots"]) if "nested_roots" in manifest else None
    if nested is not None and roots != {"config": "config", "data": "config"}:
        raise ProfileArchiveError("Nested roots require a single parent inventory")
    files, directories = manifest.get("files"), manifest.get("directories")
    if not isinstance(files, dict) or not isinstance(directories, list):
        raise ProfileArchiveError("Invalid archive inventory")
    if len(files) + len(directories) + len(set(roots.values())) > limits.max_entries:
        raise ProfileArchiveError("Inventory exceeds entry bound")
    if set(names) != {_MANIFEST, *files}:
        raise ProfileArchiveError("Archive entries differ from manifest")
    seen: set[str] = set()
    for name in directories:
        valid = _name(name)
        if valid in seen or valid in files or valid.split("/", 1)[0] not in roots.values():
            raise ProfileArchiveError("Invalid directory inventory")
        seen.add(valid)
    for name in {*files, *seen}:
        _name(name)
        if any(parent.as_posix() in files for parent in PurePosixPath(name).parents):
            raise ProfileArchiveError("Archive file conflicts with a parent directory")
    _require_nested_directory(nested, seen)
    total = 0
    for name, item in files.items():
        _name(name)
        if name.split("/", 1)[0] not in roots.values() or not isinstance(item, dict):
            raise ProfileArchiveError("Invalid file inventory")
        size, digest, mode = item.get("size"), item.get("sha256"), item.get("mode")
        if (type(size) is not int or size < 0 or size > limits.max_file_bytes
                or not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or type(mode) is not int or mode not in (0o600, 0o700)):
            raise ProfileArchiveError("Invalid file metadata")
        total += size
        info = archive.getinfo(name)
        kind = stat.S_IFMT(info.external_attr >> 16)
        if (info.file_size != size or kind not in (0, stat.S_IFREG)
                or info.flag_bits & 1 or total > limits.max_total_bytes):
            raise ProfileArchiveError("Unsupported or oversized archive file")
        calculated = hashlib.sha256()
        actual = 0
        with archive.open(info) as source:
            while chunk := source.read(_CHUNK):
                actual += len(chunk)
                if actual > size:
                    raise ProfileArchiveError("Archive file exceeds advertised bound")
                calculated.update(chunk)
        if actual != size or calculated.hexdigest() != digest:
            raise ProfileArchiveError("Archive content integrity failed")
    return manifest


def restore_archive(archive_path: Path, *, config_root: Path, data_root: Path,
                    offline: bool = False, limits: ArchiveLimits = ArchiveLimits(),
                    native_primary_session_id: str | None = None) -> dict:
    """Restore verified bytes to new roots only; do not start or activate them.

    Same-root/split-root/nested-data layout is preserved. This is not cross-machine key
    recovery or a migration that activates existing pairing/grant identities.
    """
    _offline(offline)
    limits.validate()
    roots, aliases, nested = _roots(config_root, data_root)
    if any(root.exists() or root.is_symlink() for root in roots.values()):
        raise ProfileArchiveError("Restore destinations must not exist")
    owned: dict[Path, tuple[int, int]] = {}
    try:
        archive_path = Path(archive_path)
        info = archive_path.lstat()
        archive_identity = _identity(info)
        maximum_archive = limits.max_total_bytes + limits.max_manifest_bytes + limits.max_entries * 8192
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum_archive:
            raise ProfileArchiveError("Unsupported or oversized archive container")
        descriptor = os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source_archive, zipfile.ZipFile(source_archive) as archive:
            if _identity(os.fstat(source_archive.fileno())) != archive_identity:
                raise ProfileArchiveError("Archive changed before restore")
            manifest = _validate(archive, limits)
            native = _native_metadata(archive, manifest, native_primary_session_id)
            if aliases != manifest["roots"] or nested != manifest.get("nested_roots"):
                raise ProfileArchiveError("Restore must preserve root layout")
            for root in roots.values():
                root.mkdir(mode=0o700)
                info = root.stat()
                owned[root] = info.st_dev, info.st_ino
            for name in sorted(manifest["directories"], key=lambda item: len(PurePosixPath(item).parts)):
                label, relative = name.split("/", 1)
                (roots[label] / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
            for name, item in manifest["files"].items():
                label, relative = name.split("/", 1)
                destination = roots[label] / relative
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with archive.open(name) as source, destination.open("xb") as target:
                    os.chmod(destination, item["mode"])
                    while chunk := source.read(_CHUNK):
                        target.write(chunk)
                        digest.update(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if digest.hexdigest() != item["sha256"]:
                    raise ProfileArchiveError("Archive changed during restore")
            if _identity(os.fstat(source_archive.fileno())) != archive_identity:
                raise ProfileArchiveError("Archive changed during restore")
        receipt = {"status": "completed", "files": len(manifest["files"]),
                "bytes": sum(item["size"] for item in manifest["files"].values()),
                "runtime_started": False, "os_keys_included": False,
                "coverage": "selected_roots_only", "native_preferences_included": native is not None,
                "credentials_portable": False}
        if native is not None:
            receipt.update(native_preferences_path=str(roots["config"] / RESERVED_ENTRY.split("/", 1)[1]),
                           native_preferences_sha256=native.sha256, native_primary_session_id=native.primary_session_id,
                           preferences_applied=False)
        return receipt
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
        for root, identity in reversed(list(owned.items())):
            if root.exists() and not root.is_symlink():
                info = root.stat()
                if (info.st_dev, info.st_ino) == identity:
                    shutil.rmtree(root)
        if isinstance(error, ProfileArchiveError):
            raise
        raise ProfileArchiveError("Archive restore could not be completed") from error


def main() -> int:
    """Explicit standalone CLI; never selects a restore target automatically."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Back up a stopped FERAL profile or restore to new roots")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("destination", type=Path)
    backup.add_argument("--config-root", type=Path)
    backup.add_argument("--data-root", type=Path)
    backup.add_argument("--native-preferences", type=Path)
    backup.add_argument("--native-preferences-sha256")
    backup.add_argument("--primary-session-id")
    backup.add_argument("--offline", action="store_true", required=True,
                        help="Confirm that every writer for both profile roots is stopped")
    restore = commands.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--config-root", type=Path, required=True)
    restore.add_argument("--data-root", type=Path, required=True)
    restore.add_argument("--offline", action="store_true", required=True)
    restore.add_argument("--primary-session-id")
    args = parser.parse_args()
    try:
        if args.command == "backup":
            native = None
            supplied = (args.native_preferences, args.native_preferences_sha256, args.primary_session_id)
            if any(item is not None for item in supplied):
                if (any(item is None for item in supplied) or args.config_root is None
                        or args.data_root is None):
                    raise ProfileArchiveError("Native preferences require snapshot, SHA256, primary and both explicit roots")
                native = NativePreferenceAttachment(args.native_preferences, args.config_root, args.data_root,
                                                    args.primary_session_id, args.native_preferences_sha256)
            result = create_archive(args.destination, offline=args.offline,
                                    config_root=args.config_root, data_root=args.data_root,
                                    native_preferences=native)
        else:
            result = restore_archive(args.archive, offline=args.offline,
                                     config_root=args.config_root, data_root=args.data_root,
                                     native_primary_session_id=args.primary_session_id)
    except ProfileArchiveError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
