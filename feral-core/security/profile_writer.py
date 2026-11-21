"""Cooperative runtime writer exclusion, before state/store boot side effects.

Locks contain no credentials or PID takeover metadata. Older/uncooperative
software and offline utilities are outside this runtime-startup contract.
"""
from __future__ import annotations

import atexit
import errno
import fcntl
import os
from pathlib import Path
import stat
import threading


class RuntimeProfileError(RuntimeError):
    code = "runtime_profile_unavailable"


class RuntimeProfileBusy(RuntimeProfileError):
    code = "runtime_profile_in_use"

    def __init__(self):
        super().__init__("Runtime profile is already in use; no backend startup was admitted.")


class RuntimeProfileUnsafe(RuntimeProfileError):
    code = "runtime_profile_unsafe"

    def __init__(self):
        super().__init__("Runtime profile ownership or lock identity is unsafe; no backend startup was admitted.")


class RuntimeProfileOwnerChanged(RuntimeProfileError):
    code = "runtime_profile_owner_changed"

    def __init__(self):
        super().__init__("Runtime profile owner changed; inherited state cannot start a backend.")


_LOCK_NAME = ".runtime-writer.lock"
_mutex = threading.RLock()
_held: dict[str, "_RootLock"] = {}


class _RootLock:
    def __init__(self, path: Path, directory: int, fd: int):
        self.path, self.directory, self.fd = path, directory, fd
        self.owner_pid, self.references = os.getpid(), 1
        info = os.fstat(directory)
        self.directory_identity = (info.st_dev, info.st_ino)

    def validate(self) -> None:
        if self.owner_pid != os.getpid():
            raise RuntimeProfileOwnerChanged()
        directory = os.stat(self.path, follow_symlinks=False)
        if (directory.st_uid != os.geteuid() or directory.st_mode & 0o022
                or (directory.st_dev, directory.st_ino) != self.directory_identity):
            raise RuntimeProfileUnsafe()
        current = os.stat(_LOCK_NAME, dir_fd=self.directory, follow_symlinks=False)
        opened = os.fstat(self.fd)
        _validate_file(opened)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeProfileUnsafe()


def _validate_file(info) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise RuntimeProfileUnsafe()


def _open_root(path: Path) -> _RootLock:
    directory, fd = -1, -1
    try:
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise RuntimeProfileUnsafe()
        fd = os.open(_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=directory)
        os.set_inheritable(directory, False)
        os.set_inheritable(fd, False)
        _validate_file(os.fstat(fd))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise RuntimeProfileBusy() from None
            raise
        held = _RootLock(path, directory, fd)
        held.validate()
        return held
    except BaseException:
        if fd >= 0:
            os.close(fd)
        if directory >= 0:
            os.close(directory)
        raise


class RuntimeProfileLease:
    """Exact same-process admission token; runtime callers retain until exit."""
    def __init__(self, roots: tuple[_RootLock, ...]):
        self._roots, self._owner_pid, self._released = roots, os.getpid(), False

    def assert_owner(self) -> None:
        with _mutex:
            if self._released or self._owner_pid != os.getpid():
                raise RuntimeProfileOwnerChanged()
            for root in self._roots:
                if _held.get(str(root.path)) is not root:
                    raise RuntimeProfileOwnerChanged()
                root.validate()

    def release(self) -> None:
        with _mutex:
            if self._owner_pid != os.getpid():
                raise RuntimeProfileOwnerChanged()
            if self._released:
                return
            self.assert_owner()
            self._released = True
            for root in reversed(self._roots):
                _drop(root)


def _drop(root: _RootLock) -> None:
    root.references -= 1
    if root.references == 0:
        _held.pop(str(root.path), None)
        # Never unlink or replace the stable lock inode.
        os.close(root.fd)
        os.close(root.directory)


def acquire_runtime_profile(config_root: Path | str | None = None,
                            data_root: Path | str | None = None) -> RuntimeProfileLease:
    """Acquire config/data and the legacy API-key root before runtime boot."""
    from config.loader import feral_home, feral_data_home
    chosen: tuple[Path, ...] = (Path(config_root) if config_root is not None else feral_home(),
              Path(data_root) if data_root is not None else feral_data_home())
    if config_root is None and data_root is None:
        # api.keys.get_api_key_path intentionally retains this legacy rule.
        # Do not import api.keys before admission or silently relocate its key.
        key_root = Path(os.environ.get("FERAL_HOME", str(Path.home() / ".feral")))
        chosen += (key_root,)
    acquired = []
    try:
        with _mutex:
            paths = []
            for path in chosen:
                path = path.expanduser()
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                canonical = path.resolve(strict=True)
                if canonical not in paths:
                    paths.append(canonical)
            for path in sorted(paths, key=str):
                root = _held.get(str(path))
                if root is not None:
                    root.validate()
                    root.references += 1
                else:
                    root = _open_root(path)
                    _held[str(path)] = root
                acquired.append(root)
            return RuntimeProfileLease(tuple(acquired))
    except BaseException as exc:
        with _mutex:
            for root in reversed(acquired):
                _drop(root)
        if isinstance(exc, OSError):
            raise RuntimeProfileUnsafe() from None
        raise


def _after_fork() -> None:
    global _mutex
    # Close, never LOCK_UN: fork shares the parent's open file description.
    for root in _held.values():
        for fd in (root.fd, root.directory):
            try:
                os.close(fd)
            except OSError:
                pass
    _held.clear()
    _mutex = threading.RLock()


def _at_exit() -> None:
    for root in tuple(_held.values()):
        if root.owner_pid == os.getpid():
            for fd in (root.fd, root.directory):
                try:
                    os.close(fd)
                except OSError:
                    pass
    _held.clear()


os.register_at_fork(after_in_child=_after_fork)
atexit.register(_at_exit)
