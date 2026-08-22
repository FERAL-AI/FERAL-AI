"""Discover, order, and apply pending migrations."""

from __future__ import annotations

import importlib.util
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("feral.migrations")

# 1787215824_sweep_legacy_credentials.py
_NAME_RE = re.compile(r"^(\d{10})_([a-z0-9_]+)\.py$")


@dataclass(frozen=True)
class MigrationResult:
    """What happened to one migration."""

    name: str
    ok: bool
    detail: str
    changed: bool = False
    skipped: bool = False


def migrations_dir() -> Path:
    """Where the migration files live. Ships with the package."""
    return Path(__file__).resolve().parent


def state_dir() -> Path:
    """Where the applied-markers live, under the operator's data home."""
    from config.loader import feral_data_home
    return feral_data_home() / "state" / "migrations"


def _discover() -> list[tuple[str, Path]]:
    """Every migration file, oldest first. Names sort chronologically
    because the prefix is a unix timestamp."""
    out: list[tuple[str, Path]] = []
    for path in sorted(migrations_dir().glob("*.py")):
        match = _NAME_RE.match(path.name)
        if match:
            out.append((path.stem, path))
    return out


def applied_migrations() -> set[str]:
    """Names with a marker on disk. Missing directory means none."""
    directory = state_dir()
    if not directory.is_dir():
        return set()
    return {p.name for p in directory.iterdir() if p.is_file()}


def pending_migrations() -> list[str]:
    """Names not yet applied, oldest first.

    A RECURRING migration is excluded. It is a sweep that runs on every
    boot by design, so counting it as outstanding work would leave
    `feral migrate --pending` permanently non-empty and doctor
    permanently yellow, which trains people to ignore both.
    """
    done = applied_migrations()
    out = []
    for name, path in _discover():
        if name in done:
            continue
        try:
            if getattr(_load(name, path), "RECURRING", False):
                continue
        except Exception:
            # Unloadable is genuinely outstanding: report it.
            pass
        out.append(name)
    return out


def _mark_applied(name: str) -> None:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(f"{time.time()}\n", encoding="utf-8")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"feral_migration_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load migration {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_pending(dry_run: bool = False) -> list[MigrationResult]:
    """Apply everything outstanding, oldest first.

    Never raises. A migration that fails leaves no marker and is retried
    on the next run; the ones behind it still get their turn, because one
    bad migration must not wedge an install or stop the brain booting.
    """
    results: list[MigrationResult] = []
    done = applied_migrations()

    for name, path in _discover():
        if name in done:
            continue
        if dry_run:
            results.append(MigrationResult(name, True, "pending", skipped=True))
            continue

        try:
            module = _load(name, path)
            migrate = getattr(module, "migrate", None)
            if not callable(migrate):
                results.append(MigrationResult(name, False, "no migrate() function"))
                continue
            outcome = migrate()
            detail, changed = _normalise(outcome)
            # A RECURRING migration is a standing sweep, not a one-time
            # step, and must never be marked applied.
            #
            # The credential sweep is the case that forced this. It runs
            # before state.init(), and the file it removes is created
            # LATER IN THE SAME BOOT, lazily, when the vault first
            # unlocks. So on a fresh install it found nothing, was marked
            # applied, and could never run again. Measured across four
            # boots: the plaintext credentials stayed on disk and doctor
            # reported "Migrations up to date" over them.
            if not getattr(module, "RECURRING", False):
                _mark_applied(name)
            results.append(MigrationResult(name, True, detail, changed=changed))
            if changed:
                logger.info("migration %s: %s", name, detail)
            else:
                logger.debug("migration %s: %s", name, detail)
        except Exception as exc:
            # Deliberately not re-raised. Deferring is the correct
            # behaviour for a step that may simply be too early.
            logger.warning("migration %s failed, will retry next run: %s", name, exc)
            results.append(MigrationResult(name, False, str(exc)[:300]))

    return results


def _normalise(outcome) -> tuple[str, bool]:
    """A migration may return nothing, a string, or (detail, changed)."""
    if outcome is None:
        return "applied", False
    if isinstance(outcome, tuple) and len(outcome) == 2:
        return str(outcome[0]), bool(outcome[1])
    return str(outcome), True
