"""Per-session todo list for the agent's own working memory.

Why this is not a new skill
---------------------------
FERAL already ships four overlapping list concepts plus a dormant fifth:

* ``feral_reminders``: user-facing, time-anchored, the user's list.
* ``feral_routines``: recurring schedule, cron-backed.
* ``feral_workflows``: ordered steps the brain EXECUTES as a TaskFlow.
* ``background_task`` (``task.json``): long-horizon autonomous runs.
* ``agents/coding_run.py``: a plan-then-apply loop, unwired.

A sixth top-level skill would cost two tools (the endpoint plus a
catalog entry the router has to rank) on a chat path that applies no
tool cap at all and already carries ~59 always-include tools. So the
todo list is one extra endpoint on ``feral_workflows``, the concept it
is most confused with, where the two descriptions can disambiguate each
other in the same block of the prompt.

Semantics
---------
Full-list replacement, never incremental patches, so the model's view
and the store cannot drift. At most one ``in_progress`` item, enforced
here rather than suggested in prose, which is what makes it a focus
mechanism rather than a wish list. In-memory per session with a durable
JSON mirror so the UI panel survives a brain restart. No SQLite: nothing
here needs cross-session queries, and the list is scratch state that
should not accumulate.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("feral.skills.todo_store")

__all__ = [
    "MAX_TODO_ITEMS",
    "MAX_CONTENT_CHARS",
    "TODO_STATUSES",
    "TodoStore",
    "TodoValidationError",
    "get_store",
]

# Hard cap on list length. Chosen BELOW the `feed` result-budget tier's
# `max_list_len` of 50 (see skills/result_budget.py) so the echo this
# endpoint returns can never be truncated by the sanitizer. That matters
# more than it looks: the model rewrites the whole list from what it last
# saw, so a truncated echo makes it silently drop the tail on the next
# write. The cap has to stay strictly under the tier, and the tier has to
# stay declared on the endpoint; the pair is tested together.
MAX_TODO_ITEMS = 40
MAX_CONTENT_CHARS = 500

TODO_STATUSES = ("pending", "in_progress", "completed")

_MIRROR_FILENAME = "agent_todos.json"


class TodoValidationError(ValueError):
    """Rejected write. Carries a machine-readable code for the tool envelope."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def _default_mirror_path() -> Path:
    """Resolved lazily, per call, so tests can redirect via ``FERAL_HOME``
    without the import order deciding where writes land."""
    try:
        from config.loader import feral_data_home
        return Path(feral_data_home()) / _MIRROR_FILENAME
    except Exception:
        return Path(os.path.expanduser("~/.feral")) / _MIRROR_FILENAME


class TodoStore:
    """In-memory per-session todo lists with a durable JSON mirror."""

    def __init__(self, mirror_path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._mirror_path = Path(mirror_path) if mirror_path else None
        self._sessions: Dict[str, List[dict]] = {}
        self._load_mirror()

    # -- persistence ---------------------------------------------------

    @property
    def mirror_path(self) -> Path:
        return self._mirror_path or _default_mirror_path()

    def _load_mirror(self) -> None:
        path = self.mirror_path
        try:
            if not path.exists():
                return
            payload = json.loads(path.read_text())
            sessions = payload.get("sessions")
            if isinstance(sessions, dict):
                self._sessions = {
                    str(k): [dict(t) for t in v if isinstance(t, dict)]
                    for k, v in sessions.items()
                    if isinstance(v, list)
                }
        except Exception as exc:
            # A corrupt mirror must never stop the brain booting. The list
            # is scratch state; losing it costs one rewrite by the model.
            logger.warning("todo mirror unreadable at %s (%s), starting empty", path, exc)
            self._sessions = {}

    def _write_mirror(self) -> None:
        path = self.mirror_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps({
                "version": 1,
                "updated_at": time.time(),
                "sessions": self._sessions,
            }, indent=2, default=str))
            tmp.replace(path)
        except Exception as exc:
            logger.warning("todo mirror write failed at %s: %s", path, exc)

    # -- validation ----------------------------------------------------

    @staticmethod
    def _normalize(raw: Any) -> List[dict]:
        if not isinstance(raw, list):
            raise TodoValidationError(
                "invalid_args",
                "'todos' must be an array of {id, content, status} objects.",
            )
        if len(raw) > MAX_TODO_ITEMS:
            raise TodoValidationError(
                "too_many_todos",
                f"At most {MAX_TODO_ITEMS} todos, got {len(raw)}. Keep the list "
                "to the work actually in flight and drop what is done.",
            )

        rows: List[dict] = []
        seen_ids: set[str] = set()
        in_progress = 0
        for position, item in enumerate(raw):
            if isinstance(item, str):
                item = {"content": item}
            if not isinstance(item, dict):
                raise TodoValidationError(
                    "invalid_args",
                    f"Todo at position {position} must be an object or a string.",
                )
            content = str(item.get("content") or item.get("title") or "").strip()
            if not content:
                raise TodoValidationError(
                    "invalid_args",
                    f"Todo at position {position} has no 'content'.",
                )
            status = str(item.get("status") or "pending").strip().lower()
            if status not in TODO_STATUSES:
                raise TodoValidationError(
                    "invalid_status",
                    f"Todo at position {position} has status {status!r}; "
                    f"valid statuses are {', '.join(TODO_STATUSES)}.",
                )
            if status == "in_progress":
                in_progress += 1
            todo_id = str(item.get("id") or "").strip() or uuid.uuid4().hex[:8]
            if todo_id in seen_ids:
                todo_id = uuid.uuid4().hex[:8]
            seen_ids.add(todo_id)
            rows.append({
                "id": todo_id,
                "content": content[:MAX_CONTENT_CHARS],
                "status": status,
            })

        if in_progress > 1:
            raise TodoValidationError(
                "multiple_in_progress",
                f"At most one todo may be 'in_progress', got {in_progress}. "
                "Pick the single item you are actually working on now and set "
                "the rest to 'pending'.",
            )
        return rows

    # -- api -----------------------------------------------------------

    def replace(self, session_id: str, todos: Any) -> List[dict]:
        """Wholesale replacement of one session's list. Validated first,
        so a rejected write leaves the previous list untouched rather than
        half-applied."""
        rows = self._normalize(todos)
        with self._lock:
            if rows:
                self._sessions[str(session_id)] = rows
            else:
                self._sessions.pop(str(session_id), None)
            self._write_mirror()
        return [dict(r) for r in rows]

    def get(self, session_id: str) -> List[dict]:
        with self._lock:
            return [dict(r) for r in self._sessions.get(str(session_id), [])]

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            if self._sessions.pop(str(session_id), None) is not None:
                self._write_mirror()

    @staticmethod
    def counts(rows: List[dict]) -> Dict[str, int]:
        out = {status: 0 for status in TODO_STATUSES}
        for row in rows:
            out[row["status"]] = out.get(row["status"], 0) + 1
        out["total"] = len(rows)
        return out


_STORE: Optional[TodoStore] = None
_STORE_LOCK = threading.Lock()


def get_store() -> TodoStore:
    """Process-wide store. Built on first use so ``FERAL_HOME`` set after
    import still decides where the mirror lands."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = TodoStore()
        return _STORE
