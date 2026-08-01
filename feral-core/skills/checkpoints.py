"""Checkpoint and revert for agent file writes.

What this is
------------
Every ``coding_tools__write_file`` / ``coding_tools__edit_file`` stashes
the file's pre-write bytes in a content-addressed blob store and records
a row in SQLite keyed by ``turn_id`` (see ``skills/call_context.py``).
``revert_turn`` puts the files back the way they were before that user
message was answered.

Why not git
-----------
``git stash`` and ``git add`` mutate the user's index and working tree.
An agent that quietly stages or stashes the user's in-progress work is a
worse problem than the one being solved, and it is not recoverable from
inside the agent once it has happened. FERAL also writes routinely
outside any repository (scratch dirs, config under the user's home, files
on a mounted volume), where git has nothing to say at all. Content
addressing behaves identically with or without a repository, so the
no-repo case needs no special handling and there is no second code path
to keep correct.

Refuse on drift
---------------
This is the safety property that matters. If a file's current bytes no
longer match what the agent left there, something or somebody else has
edited it since, and restoring the pre-agent bytes would destroy that
work. Those paths are listed and skipped, and the revert reports partial
unless the caller passes ``force``.

bash is not covered
-------------------
Shell commands are not checkpointed. There is no sound way to know what
a shell command touched, so pretending otherwise would produce a revert
that claims completeness it does not have. Every response from this
module says so out loud rather than leaving the caller to infer it.

Layout::

    $FERAL_HOME/checkpoints/index.db
    $FERAL_HOME/checkpoints/blobs/<first-2-hex>/<sha256>
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from uuid import uuid4

logger = logging.getLogger("feral.skills.checkpoints")

__all__ = [
    "CheckpointStore",
    "get_store",
    "checkpoint_root",
    "BASH_NOT_COVERED_NOTE",
]

BASH_NOT_COVERED_NOTE = (
    "Checkpoints cover coding_tools__write_file and coding_tools__edit_file "
    "only. Anything coding_tools__bash changed in this turn (shell "
    "redirects, sed -i, formatters, package installs, git commands) is NOT "
    "reverted and is not tracked here."
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_id  TEXT NOT NULL UNIQUE,
    turn_id        TEXT NOT NULL,
    session_id     TEXT NOT NULL DEFAULT '',
    surface        TEXT NOT NULL DEFAULT '',
    tool_name      TEXT NOT NULL DEFAULT '',
    call_id        TEXT NOT NULL DEFAULT '',
    path           TEXT NOT NULL,
    created_at     REAL NOT NULL,
    existed        INTEGER NOT NULL,
    before_hash    TEXT,
    before_size    INTEGER,
    after_hash     TEXT,
    after_size     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_turn ON checkpoints(turn_id, id);
CREATE INDEX IF NOT EXISTS idx_checkpoints_session
    ON checkpoints(session_id, created_at);
"""

# Files above this size are recorded but not blobbed: the row still shows
# the write happened, and the revert reports the path as unrecoverable
# instead of pretending.
def max_blob_bytes() -> int:
    try:
        return int(os.environ.get("FERAL_CHECKPOINT_MAX_BLOB_BYTES", "8388608"))
    except ValueError:
        return 8 * 1024 * 1024


def retention_days() -> int:
    try:
        return int(os.environ.get("FERAL_CHECKPOINT_RETENTION_DAYS", "14"))
    except ValueError:
        return 14


def checkpoint_root() -> Path:
    """``$FERAL_HOME/checkpoints``.

    ``FERAL_CHECKPOINT_DIR`` overrides it outright, which is what tests
    and the isolated-home safety rule use.
    """
    override = os.environ.get("FERAL_CHECKPOINT_DIR")
    if override:
        return Path(override).expanduser()
    from config.loader import feral_home

    return feral_home() / "checkpoints"


@dataclass(frozen=True)
class RevertEntry:
    path: str
    status: str          # restorable | drifted | already_reverted | unrecoverable
    action: str          # restore | delete | skip
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "action": self.action,
            "detail": self.detail,
        }


class CheckpointStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = Path(root) if root is not None else checkpoint_root()
        self._blobs = self._root / "blobs"
        self._db_path = self._root / "index.db"
        self._last_prune = 0.0

    # ── plumbing ──────────────────────────────────────────────────

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def root(self) -> Path:
        return self._root

    @contextmanager
    def _connect(self):
        """A short-lived connection, committed and closed on exit.

        Per-operation rather than pooled: the store is touched from the
        event loop, from the REST route and from ``feral checkpoints`` in
        a separate process, and SQLite's own file locking is the only
        coordination that works across all three.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(_SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _blob_path(self, digest: str) -> Path:
        return self._blobs / digest[:2] / digest

    def _put_blob(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        dest = self._blob_path(digest)
        if dest.exists():
            return digest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{uuid4().hex[:8]}.tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return digest

    def _read_blob(self, digest: str) -> Optional[bytes]:
        path = self._blob_path(digest)
        try:
            return path.read_bytes()
        except OSError:
            return None

    @staticmethod
    def _hash_file(path: Path) -> Optional[tuple[str, int]]:
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return hashlib.sha256(data).hexdigest(), len(data)

    # ── capture ───────────────────────────────────────────────────

    def capture(
        self,
        path,
        *,
        turn_id: str,
        session_id: str = "",
        surface: str = "",
        tool_name: str = "",
        call_id: str = "",
    ) -> Optional[str]:
        """Stash the pre-write bytes of ``path``. Returns a checkpoint id.

        Called between the write check and the write itself. Returns
        ``None`` on any failure; the caller treats that as "no checkpoint"
        and proceeds with the write regardless. Checkpointing must never
        be the reason a write fails.
        """
        target = Path(path).expanduser()
        try:
            resolved = str(target.resolve())
        except OSError:
            resolved = str(target)

        existed = target.is_file()
        before_hash: Optional[str] = None
        before_size: Optional[int] = None
        if existed:
            try:
                size = target.stat().st_size
            except OSError:
                size = 0
            before_size = size
            if size <= max_blob_bytes():
                try:
                    before_hash = self._put_blob(target.read_bytes())
                except OSError as exc:
                    logger.debug("checkpoint blob write failed for %s: %s", resolved, exc)

        checkpoint_id = f"cp_{uuid4().hex[:16]}"
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints (checkpoint_id, turn_id, session_id, "
                "surface, tool_name, call_id, path, created_at, existed, "
                "before_hash, before_size) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    checkpoint_id, turn_id or "", session_id, surface,
                    tool_name, call_id, resolved, time.time(),
                    1 if existed else 0, before_hash, before_size,
                ),
            )
        self._maybe_prune()
        return checkpoint_id

    def record_after(self, checkpoint_id: str, path) -> None:
        """Record the post-write fingerprint so drift can be detected later."""
        if not checkpoint_id:
            return
        fp = self._hash_file(Path(path).expanduser())
        if fp is None:
            return
        digest, size = fp
        with self._connect() as conn:
            conn.execute(
                "UPDATE checkpoints SET after_hash = ?, after_size = ? "
                "WHERE checkpoint_id = ?",
                (digest, size, checkpoint_id),
            )

    # ── queries ───────────────────────────────────────────────────

    def list_turns(
        self, *, session_id: Optional[str] = None, limit: int = 20,
    ) -> list[dict]:
        sql = (
            "SELECT turn_id, session_id, COUNT(*) AS writes, "
            "COUNT(DISTINCT path) AS files, MIN(created_at) AS started_at, "
            "MAX(created_at) AS ended_at "
            "FROM checkpoints "
        )
        params: list = []
        if session_id:
            sql += "WHERE session_id = ? "
            params.append(session_id)
        sql += "GROUP BY turn_id, session_id ORDER BY ended_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def entries_for_turn(self, turn_id: str) -> list[dict]:
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM checkpoints WHERE turn_id = ? ORDER BY id",
                    (turn_id,),
                )
            ]

    def latest_turn(self, session_id: Optional[str] = None) -> Optional[str]:
        turns = self.list_turns(session_id=session_id, limit=1)
        return turns[0]["turn_id"] if turns else None

    # ── revert ────────────────────────────────────────────────────

    def _plan(self, turn_id: str) -> list[RevertEntry]:
        rows = self.entries_for_turn(turn_id)
        by_path: dict[str, list[dict]] = {}
        for row in rows:
            by_path.setdefault(row["path"], []).append(row)

        plan: list[RevertEntry] = []
        for path, group in by_path.items():
            # The earliest row holds the true pre-turn state; the latest
            # holds what the agent left behind, which is what drift is
            # measured against.
            first, last = group[0], group[-1]
            target = Path(path)
            current = self._hash_file(target) if target.is_file() else None
            current_hash = current[0] if current else None

            restore_hash = first["before_hash"]
            existed = bool(first["existed"])

            if existed and not restore_hash:
                plan.append(RevertEntry(
                    path, "unrecoverable", "skip",
                    "pre-write content was not stored (file exceeded "
                    "FERAL_CHECKPOINT_MAX_BLOB_BYTES or could not be read).",
                ))
                continue

            if existed and current_hash == restore_hash:
                plan.append(RevertEntry(
                    path, "already_reverted", "skip",
                    "file already matches its pre-turn content.",
                ))
                continue
            if not existed and current_hash is None:
                plan.append(RevertEntry(
                    path, "already_reverted", "skip",
                    "file did not exist before the turn and does not exist now.",
                ))
                continue

            expected = last["after_hash"]
            if expected is None:
                plan.append(RevertEntry(
                    path, "drifted", "restore" if existed else "delete",
                    "post-write fingerprint was never recorded, so the "
                    "current content cannot be confirmed as the agent's.",
                ))
                continue
            if current_hash != expected:
                plan.append(RevertEntry(
                    path, "drifted", "restore" if existed else "delete",
                    "file changed after the agent wrote it; reverting would "
                    "discard that change.",
                ))
                continue

            plan.append(RevertEntry(
                path, "restorable", "restore" if existed else "delete",
                "",
            ))
        return plan

    def plan_revert(self, turn_id: str) -> dict:
        plan = self._plan(turn_id)
        return self._envelope(turn_id, plan, applied=[], dry_run=True, forced=False)

    def revert_turn(
        self, turn_id: str, *, force: bool = False, dry_run: bool = False,
    ) -> dict:
        plan = self._plan(turn_id)
        if not plan:
            return {
                "success": False,
                "turn_id": turn_id,
                "error": f"No checkpoints recorded for turn '{turn_id}'.",
                "bash_not_covered": True,
                "note": BASH_NOT_COVERED_NOTE,
            }

        drifted = [e for e in plan if e.status == "drifted"]
        if drifted and not force:
            env = self._envelope(turn_id, plan, applied=[], dry_run=True, forced=False)
            env["success"] = False
            env["error"] = (
                f"{len(drifted)} file(s) changed after the agent wrote them. "
                f"Refusing to overwrite them. Re-run with force to revert "
                f"anyway (their newer content will be lost)."
            )
            return env
        if dry_run:
            return self._envelope(turn_id, plan, applied=[], dry_run=True, forced=force)

        first_rows: dict[str, dict] = {}
        for row in self.entries_for_turn(turn_id):
            first_rows.setdefault(row["path"], row)

        applied: list[str] = []
        failures: list[RevertEntry] = []
        for entry in plan:
            if entry.action == "skip":
                continue
            if entry.status == "drifted" and not force:
                continue
            row = first_rows.get(entry.path)
            if row is None:
                continue
            target = Path(entry.path)
            try:
                if entry.action == "delete":
                    if target.exists():
                        target.unlink()
                else:
                    data = self._read_blob(row["before_hash"] or "")
                    if data is None:
                        failures.append(RevertEntry(
                            entry.path, "unrecoverable", "skip",
                            "checkpoint blob is missing from the store.",
                        ))
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                applied.append(entry.path)
            except OSError as exc:
                failures.append(RevertEntry(
                    entry.path, "failed", "skip", str(exc),
                ))

        env = self._envelope(
            turn_id, plan + failures, applied=applied, dry_run=False, forced=force,
        )
        env["success"] = not failures
        if failures:
            env["error"] = f"{len(failures)} file(s) could not be reverted."
        return env

    @staticmethod
    def _envelope(
        turn_id: str,
        plan: Iterable[RevertEntry],
        *,
        applied: list[str],
        dry_run: bool,
        forced: bool,
    ) -> dict:
        entries = [e.as_dict() for e in plan]
        return {
            "success": True,
            "turn_id": turn_id,
            "dry_run": dry_run,
            "forced": forced,
            "reverted": applied,
            "reverted_count": len(applied),
            "skipped": [e for e in entries if e["action"] == "skip"],
            "drifted": [e for e in entries if e["status"] == "drifted"],
            "files": entries,
            # Said on every response, not only when something went wrong:
            # a partial revert that reads as complete is worse than none.
            "bash_not_covered": True,
            "note": BASH_NOT_COVERED_NOTE,
        }

    # ── retention ─────────────────────────────────────────────────

    def _maybe_prune(self) -> None:
        now = time.time()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        try:
            self.prune()
        except Exception as exc:  # noqa: BLE001 - retention is best-effort
            logger.debug("checkpoint prune failed: %s", exc)

    def prune(self) -> int:
        """Drop rows older than the retention window and any blob no row
        references any more. Returns the number of rows removed."""
        cutoff = time.time() - retention_days() * 86400
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM checkpoints WHERE created_at < ?", (cutoff,),
            )
            removed = cur.rowcount or 0
            live = {
                row[0]
                for row in conn.execute(
                    "SELECT before_hash FROM checkpoints WHERE before_hash IS NOT NULL"
                )
            }
        if not self._blobs.is_dir():
            return removed
        for shard in self._blobs.iterdir():
            if not shard.is_dir():
                continue
            for blob in shard.iterdir():
                if blob.name not in live:
                    try:
                        blob.unlink()
                    except OSError:
                        pass
        return removed


_store: Optional[CheckpointStore] = None
_store_root: Optional[Path] = None


def get_store() -> CheckpointStore:
    """Process-wide store. Rebuilt when ``FERAL_HOME`` moves under it, so
    a test that repoints the home directory does not keep writing to the
    previous one."""
    global _store, _store_root
    root = checkpoint_root()
    if _store is None or _store_root != root:
        _store = CheckpointStore(root)
        _store_root = root
    return _store
