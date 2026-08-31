"""Checkpoint and revert for what the agent did while answering a turn.

What this is
------------
A checkpoint store keyed by ``turn_id`` (see ``skills/call_context.py``).
``revert_turn`` puts things back the way they were before that user
message was answered. It holds two shapes of record, and the difference
between them is the whole design:

**Reversal by state.** Every ``coding_tools__write_file`` /
``coding_tools__edit_file`` stashes the file's pre-write bytes in a
content-addressed blob store. Reverting is restoring those bytes. The
prior state is knowable and storable, so the undo is exact.

**Reversal by compensation.** A calendar event, a reminder or a routine
has no prior bytes to keep: the thing did not exist, and what "undo"
means is a second call that destroys it. So the store records the
*inverse call* instead of a snapshot, saga-style, and reverting means
making that call. The two live side by side and one turn can contain
both; ``revert_turn`` handles the lot and reports on each.

The compensation is derived from the tool's RESULT, never from its
arguments. The id only exists once the create has succeeded, and a call
that failed created nothing to compensate.

What is NOT covered, and why
----------------------------
Only three creations are reversible today, listed in
:data:`REVERSIBLE_ACTIONS`. Everything else that leaves the machine is
deliberately absent:

* **Email.** There is no unsend. A provider-side "undo send" is a delay
  before sending, not a reversal of one.
* **Slack, Telegram, WhatsApp, iMessage.** Retraction exists but is
  time-bounded and provider-specific, and it does not un-notify or
  un-read. That is a weaker guarantee than restoring bytes, and selling
  it as undo would be a lie in exactly the cases that matter.
* **Purchases.** ``buy_groceries`` moves money. No inverse call exists.
* **bash.** See below.
* **Lifecycle pairs.** ``browser_use__start_recording`` /
  ``stop_recording`` look like a pair and are not an undo: stopping a
  recording does not unmake the recording.

This list is load-bearing rather than decorative:
``security/trust_ledger.py`` grants earned autonomy only over tools this
module can revert, so adding a name here widens what runs without asking.

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

There is no drift check for compensations, and there cannot be one: the
store never held a copy of the created object, so it cannot tell an
untouched calendar event from one the user has since rewritten. What it
can tell is that the object is already gone, and that case is treated as
success rather than failure (see below).

Partial reverts, and what they report
-------------------------------------
A revert is not all-or-nothing once compensations are involved. One
compensating call can fail (offline, revoked token) while the file
restores succeed, and the caller has to be able to tell.

* ``success`` is true only when every item was handled.
* ``partial`` is true when some items were reverted and some were not.
  A caller that shows "reverted" off ``success`` alone will silently
  report a half-done revert as done, which is the failure this key
  exists to prevent.
* ``error_code`` is :data:`REVERT_INCOMPLETE` on any unfinished revert
  and :data:`REVERT_REFUSED_DRIFT` on a whole-turn drift refusal. Key a
  UI off these, never off parsing ``error``.
* Every entry carries its own ``status`` and ``detail``, so "what was
  not undone, and why" is answerable per item.

An object the user already deleted does NOT fail the revert. The
compensating call comes back 404/410, the entry reports
``already_reverted``, and the row is marked done so a second revert does
not call again. This is deliberately strict about what counts as gone:
only an explicit ``status_code`` of 404/410, or an error string in the
``HTTP 404: ...`` shape ``integrations/_http_errors`` produces. A
timeout, a 401 or a 500 is a failure, because treating an unreachable
provider as "already gone" would report a revert that never happened.

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
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional
from uuid import uuid4

logger = logging.getLogger("feral.skills.checkpoints")

__all__ = [
    "CheckpointStore",
    "ReversibleAction",
    "get_store",
    "checkpoint_root",
    "extract_target_id",
    "BASH_NOT_COVERED_NOTE",
    "CHECKPOINTED_FILE_TOOLS",
    "REVERSIBLE_ACTIONS",
    "REVERT_REFUSED_DRIFT",
    "REVERT_INCOMPLETE",
]

# Machine-readable reason on a refused revert. A UI should key off this and
# `refused`, never off `dry_run` or off parsing `error` prose.
REVERT_REFUSED_DRIFT = "revert_refused_drift"

# Machine-readable reason on a revert that ran but did not finish. Pair it
# with `partial` to tell "some of it came back" from "none of it did".
REVERT_INCOMPLETE = "revert_incomplete"

# Reversal by state: the tools whose prior bytes this module stashes.
CHECKPOINTED_FILE_TOOLS = frozenset({
    "coding_tools__write_file",
    "coding_tools__edit_file",
})


@dataclass(frozen=True)
class ReversibleAction:
    """How to undo one creation by calling its inverse.

    ``id_path``
        Where the created object's id sits in the RESULT envelope, as a
        key path walked from the top. It is the result and not the args
        on purpose: the id does not exist until the call has succeeded,
        and a failed call created nothing to compensate.
    ``inverse_arg``
        The single parameter the inverse endpoint takes the id in.
    ``label``
        Operator-facing noun for the thing, used in revert output.
    """

    tool_name: str
    inverse_tool: str
    inverse_arg: str
    id_path: tuple[str, ...]
    label: str


# The complete set of creations that can be compensated. Every entry was
# checked against the real implementation, not the manifest prose, because
# the manifest describes the promise and the implementation is the promise:
#
#   integrations/calendar.py:279    create_event -> {"data": {"id": ...}}
#   integrations/calendar.py:382    delete_event(event_id=...)
#   skills/impl/feral_reminders.py  create -> {"data": {"reminder": {"id": ...}}}
#                                   delete(id=...), 404 when already gone
#   skills/impl/feral_routines.py   create -> {"data": {"routine": {"id": ...}}}
#                                   delete(routine_id=...), 404 when gone
#
# Adding to this dict widens what `security/trust_ledger.py` will stop
# asking about. Read that module's boundary section before you do.
REVERSIBLE_ACTIONS: dict[str, ReversibleAction] = {
    "calendar_google__create_event": ReversibleAction(
        tool_name="calendar_google__create_event",
        inverse_tool="calendar_google__delete_event",
        inverse_arg="event_id",
        id_path=("data", "id"),
        label="calendar event",
    ),
    "feral_reminders__create": ReversibleAction(
        tool_name="feral_reminders__create",
        inverse_tool="feral_reminders__delete",
        inverse_arg="id",
        id_path=("data", "reminder", "id"),
        label="reminder",
    ),
    "feral_routines__create": ReversibleAction(
        tool_name="feral_routines__create",
        inverse_tool="feral_routines__delete",
        inverse_arg="routine_id",
        id_path=("data", "routine", "id"),
        label="routine",
    ),
}

BASH_NOT_COVERED_NOTE = (
    "Checkpoints cover file writes (coding_tools__write_file, "
    "coding_tools__edit_file), restored from stashed bytes, and three "
    "creations undone by their inverse call "
    "(calendar_google__create_event, feral_reminders__create, "
    "feral_routines__create). Anything coding_tools__bash changed in this "
    "turn (shell redirects, sed -i, formatters, package installs, git "
    "commands) is NOT reverted and is not tracked here. Neither is any "
    "other action: sent email cannot be unsent, chat messages cannot be "
    "un-notified, and purchases cannot be undone."
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

-- Reversal by compensation. A separate table rather than nullable columns
-- bolted onto `checkpoints`, because the two record shapes share nothing
-- but the turn they belong to, and because every already-installed
-- index.db keeps working with no migration step: the file logic reads the
-- table it always read.
CREATE TABLE IF NOT EXISTS reversals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    reversal_id    TEXT NOT NULL UNIQUE,
    turn_id        TEXT NOT NULL,
    session_id     TEXT NOT NULL DEFAULT '',
    surface        TEXT NOT NULL DEFAULT '',
    tool_name      TEXT NOT NULL DEFAULT '',
    call_id        TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL,
    target_id      TEXT NOT NULL,
    inverse_tool   TEXT NOT NULL,
    inverse_arg    TEXT NOT NULL,
    label          TEXT NOT NULL DEFAULT '',
    reverted_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_reversals_turn ON reversals(turn_id, id);
CREATE INDEX IF NOT EXISTS idx_reversals_session
    ON reversals(session_id, created_at);
"""

# What counts as "the thing is already gone" on a compensating call.
# Deliberately narrow. Anything looser (matching "not found" anywhere in
# an error string, or treating any failure as gone) would let an
# unreachable provider be reported as a completed revert.
_GONE_STATUS = frozenset({404, 410})
_GONE_TEXT = re.compile(r"^\s*HTTP (?:404|410)\b")


def extract_target_id(spec: ReversibleAction, result) -> str:
    """Pull the created object's id out of a tool result, or "".

    Returns "" for anything that is not a successful envelope carrying an
    id at ``spec.id_path``. The caller treats "" as "no compensation was
    recorded" and must not claim the action is undoable.
    """
    if not isinstance(result, dict) or result.get("success") is not True:
        return ""
    node = result
    for key in spec.id_path:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    if node is None or isinstance(node, (dict, list, bool)):
        return ""
    return str(node).strip()


def _already_gone(outcome) -> bool:
    """Did the inverse call fail because the object no longer exists?"""
    if not isinstance(outcome, dict):
        return False
    code = outcome.get("status_code")
    if isinstance(code, int) and not isinstance(code, bool) and code in _GONE_STATUS:
        return True
    return bool(_GONE_TEXT.match(str(outcome.get("error") or "")))

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
    """One thing a revert would do, or tried to do.

    ``path`` stays the first field and keeps its meaning for files so the
    existing envelope and every reader of it are unchanged. Compensation
    entries carry an empty ``path`` and describe themselves through
    ``kind``, ``target``, ``label`` and ``inverse_tool`` instead.
    """

    path: str
    # files:   restorable | drifted | already_reverted | unrecoverable | failed
    # actions: reversible | already_reverted | unrecoverable | failed
    status: str
    action: str          # restore | delete | compensate | skip
    detail: str = ""
    kind: str = "file"   # file | action
    target: str = ""     # the created object's id, for actions
    tool_name: str = ""  # the tool that created it
    inverse_tool: str = ""
    label: str = ""

    def as_dict(self) -> dict:
        entry = {
            "path": self.path,
            "status": self.status,
            "action": self.action,
            "detail": self.detail,
            "kind": self.kind,
        }
        if self.kind == "action":
            entry.update({
                "target": self.target,
                "tool_name": self.tool_name,
                "inverse_tool": self.inverse_tool,
                "label": self.label,
            })
        return entry

    @property
    def describe(self) -> str:
        """One line naming what this entry is about."""
        if self.kind == "action":
            return f"{self.label or 'action'} {self.target}".strip()
        return self.path


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

    # ── capture: reversal by compensation ─────────────────────────

    def capture_action(
        self,
        *,
        tool_name: str,
        result,
        turn_id: str,
        session_id: str = "",
        surface: str = "",
        call_id: str = "",
    ) -> Optional[str]:
        """Record how to undo one successful creation. Returns a row id.

        Called AFTER the tool ran, with the tool's result, because that is
        the only place the created object's id exists. Returns ``None``
        without recording anything when the tool is not one of
        :data:`REVERSIBLE_ACTIONS`, when the call did not succeed, or when
        the result carries no usable id. ``None`` means "this action has
        no undo", and the caller must not pretend otherwise.
        """
        spec = REVERSIBLE_ACTIONS.get(tool_name)
        if spec is None:
            return None
        target = extract_target_id(spec, result)
        if not target:
            return None

        reversal_id = f"rv_{uuid4().hex[:16]}"
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO reversals (reversal_id, turn_id, session_id, "
                "surface, tool_name, call_id, created_at, target_id, "
                "inverse_tool, inverse_arg, label) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    reversal_id, turn_id or "", session_id, surface,
                    tool_name, call_id, time.time(), target,
                    spec.inverse_tool, spec.inverse_arg, spec.label,
                ),
            )
        self._maybe_prune()
        return reversal_id

    def _mark_action_reverted(self, reversal_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE reversals SET reverted_at = ? WHERE reversal_id = ?",
                (time.time(), reversal_id),
            )

    # ── queries ───────────────────────────────────────────────────

    def list_turns(
        self, *, session_id: Optional[str] = None, limit: int = 20,
    ) -> list[dict]:
        """Turns that changed something, most recent first.

        Unions both record kinds so a turn that only created a calendar
        event is listed too. ``writes`` and ``files`` keep counting file
        rows only, which is what they always meant; ``actions`` is the
        new column.
        """
        sql = (
            "SELECT turn_id, session_id, "
            "SUM(is_file) AS writes, "
            "COUNT(DISTINCT CASE WHEN is_file = 1 THEN path END) AS files, "
            "SUM(1 - is_file) AS actions, "
            "MIN(created_at) AS started_at, MAX(created_at) AS ended_at "
            "FROM ("
            "  SELECT turn_id, session_id, path, created_at, 1 AS is_file"
            "  FROM checkpoints"
            "  UNION ALL"
            "  SELECT turn_id, session_id, '' AS path, created_at, 0 AS is_file"
            "  FROM reversals"
            ") "
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
        """The file-write rows for one turn."""
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM checkpoints WHERE turn_id = ? ORDER BY id",
                    (turn_id,),
                )
            ]

    def reversals_for_turn(self, turn_id: str) -> list[dict]:
        """The compensation rows for one turn."""
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM reversals WHERE turn_id = ? ORDER BY id",
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

    def _plan_actions(self, turn_id: str) -> list[tuple[dict, RevertEntry]]:
        """Compensation entries for one turn, paired with their rows.

        Nothing is probed remotely here. A dry run must not call a
        provider, and there is no cheap read that would tell us more than
        the compensating call itself does.
        """
        planned: list[tuple[dict, RevertEntry]] = []
        for row in self.reversals_for_turn(turn_id):
            base = dict(
                kind="action",
                target=row["target_id"],
                tool_name=row["tool_name"],
                inverse_tool=row["inverse_tool"],
                label=row["label"],
            )
            if row["reverted_at"] is not None:
                planned.append((row, RevertEntry(
                    "", "already_reverted", "skip",
                    "this action was already undone by an earlier revert.",
                    **base,
                )))
                continue
            planned.append((row, RevertEntry(
                "", "reversible", "compensate",
                f"calls {row['inverse_tool']} to undo it.",
                **base,
            )))
        return planned

    def plan_revert(self, turn_id: str) -> dict:
        plan = self._plan(turn_id)
        actions = [entry for _, entry in self._plan_actions(turn_id)]
        return self._envelope(
            turn_id, plan + actions, applied=[], applied_actions=[],
            dry_run=True, forced=False,
        )

    def revert_turn(
        self,
        turn_id: str,
        *,
        force: bool = False,
        dry_run: bool = False,
        compensate: Optional[Callable[[str, dict], dict]] = None,
    ) -> dict:
        """Undo one turn: restore its files, compensate its actions.

        ``compensate`` is how the caller lends this module the ability to
        make a tool call. It takes ``(inverse_tool, args)`` and returns a
        result envelope. It is a parameter rather than an import because
        this module must stay dispatch-agnostic: it is read by the REST
        route, by ``coding_tools__revert_turn``, and by ``feral
        checkpoints`` in a process with no brain in it at all.

        Without it, compensations are reported as ``unrecoverable`` and
        the revert reports incomplete. It never silently drops them.
        """
        plan = self._plan(turn_id)
        planned_actions = self._plan_actions(turn_id)
        action_plan = [entry for _, entry in planned_actions]
        if not plan and not action_plan:
            return {
                "success": False,
                "turn_id": turn_id,
                "refused": False,
                "error_code": "no_checkpoints",
                "error": f"No checkpoints recorded for turn '{turn_id}'.",
                "bash_not_covered": True,
                "note": BASH_NOT_COVERED_NOTE,
            }

        # A preview is answered before the drift check, never after it. A dry
        # run writes nothing, so there is nothing to refuse: the drifted paths
        # are exactly what the caller asked to be shown, and they come back
        # under `drifted` with the plan. Refusing it made a preview and a
        # refusal byte-identical, so no caller could tell which it had.
        #
        # A dry run also makes no compensating call, so a preview never
        # deletes anybody's calendar event.
        if dry_run:
            return self._envelope(
                turn_id, plan + action_plan, applied=[], applied_actions=[],
                dry_run=True, forced=force,
            )

        drifted = [e for e in plan if e.status == "drifted"]
        if drifted and not force:
            # The refusal is whole-turn, so the compensations are not run
            # either. A turn half-undone is a state neither the user nor
            # the agent ever saw, and that is worse than not starting.
            env = self._envelope(
                turn_id, plan + action_plan, applied=[], applied_actions=[],
                dry_run=False, forced=False,
            )
            env["success"] = False
            env["refused"] = True
            env["error_code"] = REVERT_REFUSED_DRIFT
            env["error"] = (
                f"{len(drifted)} file(s) changed after the agent wrote them. "
                f"Refusing to overwrite them. Re-run with force to revert "
                f"anyway (their newer content will be lost)."
            )
            return env

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

        action_entries, applied_actions, action_failures = self._compensate_all(
            planned_actions, compensate,
        )

        env = self._envelope(
            turn_id, plan + failures + action_entries,
            applied=applied, applied_actions=applied_actions,
            dry_run=False, forced=force,
        )
        env["success"] = not failures and not action_failures
        # Reverted some of it and not the rest. Stated as its own key
        # because `success: false` alone does not say whether anything
        # came back, and a UI that renders it as "revert failed" when
        # half the turn was undone is telling the operator the opposite
        # of what happened.
        env["partial"] = bool(env["reverted_count"]) and not env["success"]
        if not env["success"]:
            env["error_code"] = REVERT_INCOMPLETE
            parts = []
            if failures:
                parts.append(f"{len(failures)} file(s)")
            if action_failures:
                parts.append(f"{len(action_failures)} action(s)")
            env["error"] = f"{' and '.join(parts)} could not be reverted."
        return env

    def _compensate_all(
        self,
        planned: list[tuple[dict, RevertEntry]],
        compensate: Optional[Callable[[str, dict], dict]],
    ) -> tuple[list[RevertEntry], list[dict], list[RevertEntry]]:
        """Run every outstanding compensating call.

        Returns ``(entries, applied, failures)``. ``entries`` replaces the
        planned action entries with what actually happened, so the
        envelope never shows a plan where an outcome exists.

        One failing call does not stop the others. Compensations are
        independent, and abandoning the rest because the calendar was
        offline would leave more behind than it saved.
        """
        entries: list[RevertEntry] = []
        applied: list[dict] = []
        failures: list[RevertEntry] = []

        for row, planned_entry in planned:
            if planned_entry.action == "skip":
                entries.append(planned_entry)
                continue
            if compensate is None:
                entries.append(self._action_entry(
                    row, "unrecoverable", "skip",
                    f"no way to call {row['inverse_tool']} from here. Revert "
                    f"from the brain (the REST route or "
                    f"coding_tools__revert_turn); `feral checkpoints revert` "
                    f"restores files but cannot undo actions.",
                ))
                failures.append(entries[-1])
                continue

            try:
                outcome = compensate(
                    row["inverse_tool"], {row["inverse_arg"]: row["target_id"]},
                )
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                logger.warning(
                    "compensating call %s failed for %s: %s",
                    row["inverse_tool"], row["target_id"], exc,
                )
                entries.append(self._action_entry(row, "failed", "skip", str(exc)))
                failures.append(entries[-1])
                continue

            if isinstance(outcome, dict) and outcome.get("success") is True:
                self._mark_action_reverted(row["reversal_id"])
                entries.append(self._action_entry(row, "reverted", "compensate", ""))
                applied.append(entries[-1].as_dict())
                continue

            if _already_gone(outcome):
                # Idempotency. The user got there first, which is the
                # outcome the revert wanted. Mark it done so a second
                # revert does not call again, and do not count it as
                # something this revert undid.
                self._mark_action_reverted(row["reversal_id"])
                entries.append(self._action_entry(
                    row, "already_reverted", "skip",
                    "it no longer exists; nothing to undo.",
                ))
                continue

            detail = ""
            if isinstance(outcome, dict):
                detail = str(outcome.get("error") or outcome.get("status") or "")
            entries.append(self._action_entry(
                row, "failed", "skip",
                detail or f"{row['inverse_tool']} did not report success.",
            ))
            failures.append(entries[-1])

        return entries, applied, failures

    @staticmethod
    def _action_entry(row: dict, status: str, action: str, detail: str) -> RevertEntry:
        return RevertEntry(
            "", status, action, detail,
            kind="action",
            target=row["target_id"],
            tool_name=row["tool_name"],
            inverse_tool=row["inverse_tool"],
            label=row["label"],
        )

    @staticmethod
    def _envelope(
        turn_id: str,
        plan: Iterable[RevertEntry],
        *,
        applied: list[str],
        applied_actions: list[dict],
        dry_run: bool,
        forced: bool,
    ) -> dict:
        entries = [e.as_dict() for e in plan]
        files = [e for e in entries if e["kind"] == "file"]
        actions = [e for e in entries if e["kind"] == "action"]
        return {
            "success": True,
            "turn_id": turn_id,
            "dry_run": dry_run,
            # Always present so a caller can read it unconditionally rather
            # than inferring a refusal from the absence of a key.
            "refused": False,
            "error_code": "",
            "forced": forced,
            "reverted": applied,
            "reverted_actions": applied_actions,
            "reverted_count": len(applied) + len(applied_actions),
            # True when the revert did some of its job and not the rest.
            # A caller that reads `success` alone will call that done.
            "partial": False,
            "skipped": [e for e in entries if e["action"] == "skip"],
            "drifted": [e for e in entries if e["status"] == "drifted"],
            # `files` keeps its exact old meaning. `actions` and `entries`
            # are the new views; nothing that read `files` starts seeing
            # compensations in it.
            "files": files,
            "actions": actions,
            "entries": entries,
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
        references any more. Returns the number of rows removed.

        Compensation rows expire on the same clock as file checkpoints.
        They have no blobs, so there is nothing to sweep behind them.
        """
        cutoff = time.time() - retention_days() * 86400
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM checkpoints WHERE created_at < ?", (cutoff,),
            )
            removed = cur.rowcount or 0
            cur = conn.execute(
                "DELETE FROM reversals WHERE created_at < ?", (cutoff,),
            )
            removed += cur.rowcount or 0
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
