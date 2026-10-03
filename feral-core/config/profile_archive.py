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


def _roots(config_root: Path, data_root: Path) -> tuple[dict[str, Path], dict[str, str]]:
    raw = {"config": Path(config_root).absolute(), "data": Path(data_root).absolute()}
    if any(path.is_symlink() for path in raw.values()):
        raise ProfileArchiveError("Profile roots cannot be symlinks")
    paths = {key: path.resolve() for key, path in raw.items()}
    if paths["config"] == paths["data"]:
        return {"config": paths["config"]}, {"config": "config", "data": "config"}
    if (paths["config"] in paths["data"].parents
            or paths["data"] in paths["config"].parents):
        raise ProfileArchiveError("Profile roots cannot overlap")
    return paths, {"config": "config", "data": "data"}


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


def create_archive(destination: Path, *, offline: bool = False,
                   config_root: Path | None = None, data_root: Path | None = None,
                   limits: ArchiveLimits = ArchiveLimits()) -> dict:
    """Archive both actual roots without mutating source files or exporting keys.

    `offline=True` is a caller precondition, not inferred process quiescence.
    The full regular-file set includes SQLite WAL/SHM and encrypted artifacts.
    Hashes and file/directory identities detect changes during the cold copy.
    """
    _offline(offline)
    limits.validate()
    if config_root is None or data_root is None:
        from config.loader import feral_data_home, feral_home
        config_root = feral_home() if config_root is None else config_root
        data_root = feral_data_home() if data_root is None else data_root
    roots, aliases = _roots(config_root, data_root)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ProfileArchiveError("Archive destination already exists")
    parent = destination.parent.resolve()
    if any(parent == root or root in parent.parents for root in roots.values()):
        raise ProfileArchiveError("Archive destination must be outside profile roots")
    files, directories = _snapshot(roots, limits)
    manifest = {
        "format_version": 1, "offline_required": True, "roots": aliases,
        "os_keys_included": False, "credentials_portable": False,
        "files": {name: {"size": item.size, "sha256": item.digest, "mode": item.mode}
                  for name, item in files.items()},
        "directories": sorted(name for name in directories if name not in roots),
    }
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
        if _snapshot(roots, limits) != (files, directories):
            raise ProfileArchiveError("Profile changed during snapshot")
        # Hard-link publication is exclusive, unlike replace/rename over a file.
        os.link(staging, destination)
        return {"status": "completed", "files": len(files),
                "bytes": sum(item.size for item in files.values()),
                "os_keys_included": False, "credentials_portable": False}
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
                    offline: bool = False, limits: ArchiveLimits = ArchiveLimits()) -> dict:
    """Restore verified bytes to new roots only; do not start or activate them.

    Same-root/split-root layout is preserved. This is not cross-machine key
    recovery or a migration that activates existing pairing/grant identities.
    """
    _offline(offline)
    limits.validate()
    roots, aliases = _roots(config_root, data_root)
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
            if aliases != manifest["roots"]:
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
        return {"status": "completed", "files": len(manifest["files"]),
                "bytes": sum(item["size"] for item in manifest["files"].values()),
                "runtime_started": False, "os_keys_included": False,
                "credentials_portable": False}
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
    backup.add_argument("--offline", action="store_true", required=True,
                        help="Confirm that every writer for both profile roots is stopped")
    restore = commands.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--config-root", type=Path, required=True)
    restore.add_argument("--data-root", type=Path, required=True)
    restore.add_argument("--offline", action="store_true", required=True)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            result = create_archive(args.destination, offline=args.offline,
                                    config_root=args.config_root, data_root=args.data_root)
        else:
            result = restore_archive(args.archive, offline=args.offline,
                                     config_root=args.config_root, data_root=args.data_root)
    except ProfileArchiveError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
