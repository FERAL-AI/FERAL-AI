"""Two host conditions that silently ruin long-running work.

Both are lifted from omarchy's update pipeline, which refuses to start
below 10 GiB free and inhibits sleep for the duration. FERAL had neither,
and both failures happened during development of this module:

* the machine slowed to a crawl with a 99% full disk, and nothing in the
  brain noticed or said so, and
* a long agent task died mid-run with "your computer went to sleep",
  losing the work with no record of why.

Neither is a policy decision. A disk with no room cannot hold a memory
write, and a sleeping Mac cannot finish a build.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("feral.system.preflight")

# Omarchy refuses an update below 10 GiB. FERAL is not installing a
# distribution, so the floor is lower, but the memory store, embeddings,
# screen captures and background job output all grow without asking.
LOW_DISK_BYTES = 2 * 1024 * 1024 * 1024      # 2 GiB: warn
CRITICAL_DISK_BYTES = 512 * 1024 * 1024      # 512 MiB: refuse to grow


@dataclass(frozen=True)
class DiskStatus:
    """Free space on the volume holding a path, with a verdict."""

    path: Path
    total_bytes: int
    free_bytes: int
    level: str          # "ok" | "low" | "critical" | "unknown"
    detail: str

    @property
    def free_gib(self) -> float:
        return self.free_bytes / (1024 ** 3)

    @property
    def percent_used(self) -> float:
        if not self.total_bytes:
            return 0.0
        return 100.0 * (1 - self.free_bytes / self.total_bytes)


def check_disk(path: Path | str | None = None) -> DiskStatus:
    """Free space on the volume holding *path*, defaulting to FERAL_HOME.

    Never raises. A volume that cannot be measured returns ``unknown``
    rather than a guess, because a fabricated "fine" here is worse than
    saying nothing.
    """
    if path is None:
        try:
            from config.loader import feral_home
            path = feral_home()
        except Exception:
            path = Path.home()
    path = Path(path)

    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent

    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        return DiskStatus(path, 0, 0, "unknown", f"could not measure {probe}: {exc}")

    free = usage.free
    if free < CRITICAL_DISK_BYTES:
        level = "critical"
        detail = (
            f"{free / (1024**3):.2f} GiB free. Memory writes, embeddings and "
            "screen captures will start failing. Free space before running "
            "anything long."
        )
    elif free < LOW_DISK_BYTES:
        level = "low"
        detail = (
            f"{free / (1024**3):.2f} GiB free. The memory store and background "
            "job output grow without asking; this will become a failure."
        )
    else:
        level = "ok"
        detail = f"{free / (1024**3):.1f} GiB free of {usage.total / (1024**3):.0f} GiB"
    return DiskStatus(path, usage.total, free, level, detail)


class StayAwake:
    """Keep the machine awake while a long operation runs.

    Reference-counted, so nested or concurrent jobs share one assertion
    and the last one out releases it. Idempotent and never raises: a
    machine that will not hold an assertion should still run the work.

    macOS only for now, via ``caffeinate``. ``caffeinate -i`` inhibits
    idle sleep but deliberately NOT display sleep, so the screen still
    locks on schedule; keeping a display awake to run a background build
    would be a security regression dressed as a feature.
    """

    _lock = threading.Lock()
    _depth = 0
    _proc: subprocess.Popen | None = None

    @classmethod
    def supported(cls) -> bool:
        return sys.platform == "darwin" and shutil.which("caffeinate") is not None

    @classmethod
    def acquire(cls, reason: str = "") -> bool:
        """Take a reference. True when an assertion is actually held."""
        with cls._lock:
            cls._depth += 1
            if cls._proc is not None and cls._proc.poll() is None:
                return True
            if not cls.supported():
                return False
            try:
                cls._proc = subprocess.Popen(
                    ["caffeinate", "-i"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info("holding the machine awake%s", f" for {reason}" if reason else "")
                return True
            except Exception as exc:
                logger.debug("could not inhibit sleep: %s", exc)
                cls._proc = None
                return False

    @classmethod
    def release(cls) -> None:
        """Drop a reference. The last one out ends the assertion."""
        with cls._lock:
            cls._depth = max(0, cls._depth - 1)
            if cls._depth > 0 or cls._proc is None:
                return
            try:
                cls._proc.terminate()
                cls._proc.wait(timeout=2)
            except Exception:
                try:
                    cls._proc.kill()
                except Exception:
                    pass
            finally:
                cls._proc = None
                logger.info("released the sleep assertion")

    @classmethod
    def held(cls) -> bool:
        with cls._lock:
            return cls._proc is not None and cls._proc.poll() is None

    @classmethod
    def depth(cls) -> int:
        with cls._lock:
            return cls._depth

    def __init__(self, reason: str = ""):
        self._reason = reason

    def __enter__(self) -> "StayAwake":
        self.acquire(self._reason)
        return self

    def __exit__(self, *exc) -> None:
        self.release()
