"""Which agent session a FERAL session handle belongs to, across restarts.

The problem
===========
``bridges/sessions.py`` holds live sessions in a process-wide dict. That
is correct while FERAL is up, and useless the moment it is not. Three
things end a subprocess without ending the user's train of thought: the
agent crashes, the idle sweeper reaps it after fifteen minutes, or FERAL
itself restarts. In all three the user's next message is a follow-up
("now do the same for the other module") and the handle the LLM is
holding no longer resolves.

So the mapping from a handle to an agent-side session id is written to
disk the moment a session opens, and read back when a handle misses in
the live registry. The handle is stable across the gap: an LLM that was
told ``ext-ab12cd34`` an hour ago can still use it.

What is deliberately NOT here
=============================
No conversation transcript and no event stream. This file is a pointer,
a few hundred bytes per session. The record of what the agent actually
did lives in episodic memory (``memory/agent_activity.py``), because
that is a memory concern and this is a plumbing concern, and keeping
them apart is what stops this from becoming the fifth overlapping
task-tracking store in this codebase.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("feral.bridges.continuity")

# Entries older than this are dropped on load. An agent-side session that
# has not been touched in a fortnight is not something a user is still
# following up on, and both reference agents garbage-collect their own
# session storage on their own schedule anyway.
DEFAULT_MAX_AGE_SECONDS = 14 * 24 * 3600

# Hard cap on the file, newest first. Stops an install that opens a
# session per commit from growing an unbounded index.
MAX_ENTRIES = 200

INDEX_FILENAME = "external_agent_sessions.json"


def index_path() -> Path:
    """Where the index lives.

    Rooted at ``FERAL_HOME`` so a test can point it somewhere temporary
    and never touch the user's real ``~/.feral``. ``config.loader`` owns
    the XDG resolution, so it is asked rather than reimplemented; the
    fallback exists only for a bridges-only import with no configured
    brain, which is how the protocol tests run.
    """
    override = os.environ.get("FERAL_EXTERNAL_AGENT_INDEX")
    if override:
        return Path(override)
    try:
        from config.loader import feral_home

        root = Path(feral_home())
    except Exception:
        env_home = os.environ.get("FERAL_HOME")
        root = Path(env_home) if env_home else Path.home() / ".feral"
    return root / INDEX_FILENAME


@dataclass
class SessionRecord:
    """Enough to reattach, and nothing more."""

    handle: str
    agent_id: str
    cwd: str
    acp_session_id: str
    conversation_id: str = ""
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    turns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Optional["SessionRecord"]:
        try:
            return cls(
                handle=str(raw["handle"]),
                agent_id=str(raw["agent_id"]),
                cwd=str(raw["cwd"]),
                acp_session_id=str(raw.get("acp_session_id") or ""),
                conversation_id=str(raw.get("conversation_id") or ""),
                created_at=float(raw.get("created_at") or 0.0),
                last_active=float(raw.get("last_active") or 0.0),
                turns=int(raw.get("turns") or 0),
            )
        except (KeyError, TypeError, ValueError):
            return None


class SessionIndex:
    """A tiny JSON file mapping handles to agent-side sessions.

    Read and written whole. A handful of entries of a few hundred bytes
    does not justify a database, and a single atomic replace is easier to
    reason about than a second SQLite file competing for the same lock as
    the memory store.
    """

    def __init__(self, path: Optional[Path] = None, *, max_age: float = DEFAULT_MAX_AGE_SECONDS):
        self._path = Path(path) if path is not None else None
        self.max_age = max_age

    @property
    def path(self) -> Path:
        # Resolved late so a test that sets FERAL_HOME after construction
        # still gets the temporary location.
        return self._path if self._path is not None else index_path()

    def _read(self) -> list[SessionRecord]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            # A corrupt index is a lost convenience, not a lost repo.
            logger.warning("external agent session index unreadable: %s", exc)
            return []
        entries = raw.get("sessions") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return []
        cutoff = time.time() - self.max_age
        records = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            record = SessionRecord.from_dict(item)
            if record is None or record.last_active < cutoff:
                continue
            records.append(record)
        return records

    def _write(self, records: list[SessionRecord]) -> None:
        records = sorted(records, key=lambda r: r.last_active, reverse=True)[:MAX_ENTRIES]
        payload = {"version": 1, "sessions": [r.to_dict() for r in records]}
        target = self.path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            # Atomic on POSIX, so a crash mid-write cannot leave a
            # half-file that the next read discards wholesale.
            os.replace(tmp, target)
        except OSError as exc:
            logger.warning("could not persist external agent session index: %s", exc)

    # ------------------------------------------------------------------

    def remember(self, record: SessionRecord) -> None:
        records = [r for r in self._read() if r.handle != record.handle]
        records.append(record)
        self._write(records)

    def touch(self, handle: str, *, turns: Optional[int] = None) -> None:
        records = self._read()
        for record in records:
            if record.handle == handle:
                record.last_active = time.time()
                if turns is not None:
                    record.turns = turns
                self._write(records)
                return

    def get(self, handle: str) -> Optional[SessionRecord]:
        for record in self._read():
            if record.handle == handle:
                return record
        return None

    def find(
        self, *, conversation_id: str = "", agent_id: str = "", cwd: str = ""
    ) -> Optional[SessionRecord]:
        """The newest record matching every filter that was supplied.

        With a ``conversation_id`` this is the FERAL-conversation to
        agent-session mapping. Without one it degrades to "the last
        session this agent had in this repo", which is the right answer
        for a chat surface that does not hand skills a conversation id.
        """
        want_cwd = os.path.abspath(os.path.expanduser(cwd)) if cwd else ""
        best: Optional[SessionRecord] = None
        for record in self._read():
            if conversation_id and record.conversation_id != conversation_id:
                continue
            if agent_id and record.agent_id != agent_id:
                continue
            if want_cwd and os.path.abspath(record.cwd) != want_cwd:
                continue
            if best is None or record.last_active > best.last_active:
                best = record
        return best

    def forget(self, handle: str) -> bool:
        records = self._read()
        remaining = [r for r in records if r.handle != handle]
        if len(remaining) == len(records):
            return False
        self._write(remaining)
        return True

    def list(self) -> list[SessionRecord]:
        return sorted(self._read(), key=lambda r: r.last_active, reverse=True)


_INDEX: Optional[SessionIndex] = None


def index() -> SessionIndex:
    """The process-wide index. Lazy so importing bridges stays cheap."""
    global _INDEX
    if _INDEX is None:
        _INDEX = SessionIndex()
    return _INDEX
