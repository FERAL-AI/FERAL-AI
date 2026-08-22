"""Migrations that move an existing ``~/.feral`` forward.

Modelled on omarchy's runner, with the pieces that matter kept and the
shell-isms dropped.

**Named by unix timestamp.** ``1787215824_sweep_legacy_credentials.py``.
Two people adding a migration on the same day never collide, and there
is no sequence number to coordinate.

**A marker file per migration**, under ``<data home>/state/migrations``.
Presence means done. That is the whole state model: no table, no schema
version, nothing to keep in sync with the files on disk.

**Each migration owns its own idempotency.** The marker is an
optimisation, not a guarantee: a migration that is re-run must be
harmless, because a marker directory can be lost, restored from a
backup, or copied between machines.

**A failure defers rather than aborts.** ``run_pending`` records the
error, leaves the marker absent, and moves on. A migration that cannot
run today runs tomorrow, and one bad migration never blocks the ones
behind it or stops the brain from booting.

Why this exists: ``~/.feral`` holds 93 entries and ``settings.json``
alone is read or written from 22 call sites. Three releases have changed
shapes in there, and the evidence of how that was handled is four
hand-made backups sitting in a live install, one of which is a plaintext
credential file that was never cleaned up.
"""

from migrations.runner import (
    MigrationResult,
    applied_migrations,
    migrations_dir,
    pending_migrations,
    run_pending,
    state_dir,
)

__all__ = [
    "MigrationResult",
    "applied_migrations",
    "migrations_dir",
    "pending_migrations",
    "run_pending",
    "state_dir",
]
