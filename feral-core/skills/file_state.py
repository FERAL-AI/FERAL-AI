"""Per-session read-before-edit and staleness tracking.

Two failure modes this closes:

* The model writes a file it never read, from memory or from a guess,
  and silently reverts somebody else's work.
* The model reads a file, thinks for four tool calls, and then edits
  against the version it remembers while the file has since changed
  underneath it (the user saved in their editor, a formatter ran, a
  parallel subagent wrote it).

Why not mtime alone
-------------------
Editors restore mtime on save-in-place, some sync tools preserve it
outright, and FERAL's own writes routinely land inside the same clock
second as the read that preceded them. The observation is therefore the
triple ``(mtime_ns, size, content_hash)``; the hash is authoritative and
the cheap fields short-circuit it.

Verdicts
--------
``ok`` / ``never_read`` / ``stale`` / ``gone``.

There is deliberately **no ``partial`` verdict**. The model legitimately
reads a large file through an ``offset``/``limit`` window and then edits
a line outside that window, which is correct behaviour; refusing it is a
false positive that trains the model to re-read whole files defensively
and burns the context window doing it. The flag is recorded on the
observation so a later lane can measure how often it would have fired,
but it never changes the verdict.

Enforcement
-----------
``FERAL_READ_BEFORE_EDIT`` selects ``off`` / ``warn`` / ``enforce``,
defaulting to **warn**: the write proceeds and the result carries a
warning field. That gives telemetry on how often the guard would fire
before it starts failing real work. (Environment rather than
``settings.json`` because ``config/loader.py`` belongs to another lane
this wave; the settings mirror lands after merge.)

Concurrency
-----------
``ToolRunner.spawn_subagents`` runs up to six workers concurrently, all
with full ``coding_tools`` access. Check-then-write is therefore a real
TOCTOU window, and :meth:`FileStateTracker.lock_for` hands out a per-path
``asyncio.Lock`` that the caller holds across the whole check-capture-write
sequence.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("feral.skills.file_state")

__all__ = [
    "FileObservation",
    "WriteCheck",
    "FileStateTracker",
    "get_tracker",
    "enforcement_mode",
    "bash_is_read_only",
    "VERDICT_OK",
    "VERDICT_NEVER_READ",
    "VERDICT_STALE",
    "VERDICT_GONE",
]

VERDICT_OK = "ok"
VERDICT_NEVER_READ = "never_read"
VERDICT_STALE = "stale"
VERDICT_GONE = "gone"

MODE_OFF = "off"
MODE_WARN = "warn"
MODE_ENFORCE = "enforce"

# Files above this size are fingerprinted by (mtime_ns, size) only. A
# 2 MB read cap already exists in `coding_tools._read_file`, so this is a
# safety valve rather than a normal path.
MAX_HASH_BYTES = 8 * 1024 * 1024
_HASH_CHUNK = 1 << 20

# Bound the lock table on a long-lived brain.
_MAX_LOCKS = 4096


def enforcement_mode() -> str:
    """``off`` | ``warn`` | ``enforce``. Defaults to ``warn``."""
    raw = os.environ.get("FERAL_READ_BEFORE_EDIT", MODE_WARN).strip().lower()
    return raw if raw in (MODE_OFF, MODE_WARN, MODE_ENFORCE) else MODE_WARN


@dataclass(frozen=True)
class FileObservation:
    path: str
    mtime_ns: int
    size: int
    content_hash: str
    observed_at: float
    #: The read that produced this observation covered only part of the
    #: file. Recorded for telemetry; see the module docstring for why it
    #: does not gate anything.
    partial: bool = False
    window: Optional[tuple[int, int]] = None


@dataclass(frozen=True)
class WriteCheck:
    verdict: str
    path: str
    mode: str
    allowed: bool
    message: str = ""
    partial_read: bool = False

    def as_dict(self) -> dict:
        out = {
            "verdict": self.verdict,
            "mode": self.mode,
            "message": self.message,
        }
        if self.partial_read:
            out["read_was_partial"] = True
        return out


def _fingerprint(path: Path) -> Optional[tuple[int, int, str]]:
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    digest = ""
    if st.st_size <= MAX_HASH_BYTES:
        h = hashlib.sha256()
        try:
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(_HASH_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
        except OSError:
            return None
        digest = h.hexdigest()
    return st.st_mtime_ns, st.st_size, digest


class FileStateTracker:
    """Per-session record of what the agent has actually looked at."""

    def __init__(self) -> None:
        self._observations: dict[str, dict[str, FileObservation]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ── keys ──────────────────────────────────────────────────────

    @staticmethod
    def _key(path) -> str:
        try:
            return str(Path(path).expanduser().resolve())
        except OSError:
            return str(path)

    # ── locks ─────────────────────────────────────────────────────

    def lock_for(self, path) -> asyncio.Lock:
        """The lock guarding check-then-write for one path.

        Held across the whole sequence by the caller, so two concurrent
        subagents editing the same file serialise instead of both passing
        the staleness check against the same pre-write fingerprint.
        """
        key = self._key(path)
        lock = self._locks.get(key)
        if lock is None:
            if len(self._locks) >= _MAX_LOCKS:
                for k, existing in list(self._locks.items()):
                    if not existing.locked():
                        self._locks.pop(k, None)
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    # ── observations ──────────────────────────────────────────────

    def observe(
        self,
        path,
        *,
        partial: bool = False,
        window: Optional[tuple[int, int]] = None,
    ) -> Optional[FileObservation]:
        """Fingerprint ``path``. **Blocking**: stats and hashes the file.

        Split out from :meth:`record_read` so a caller that is already
        inside an ``asyncio.to_thread`` hop can take the fingerprint in the
        same hop as the read it belongs to, then store it on the event loop
        with :meth:`remember`. Taking it in a second hop would be worse
        than pointless: the file could change in between, and the
        observation would then record a state the agent never actually
        read, which makes a later staleness check pass when it should fail.
        """
        key = self._key(path)
        fp = _fingerprint(Path(key))
        if fp is None:
            return None
        mtime_ns, size, digest = fp
        return FileObservation(
            path=key,
            mtime_ns=mtime_ns,
            size=size,
            content_hash=digest,
            observed_at=_now(),
            partial=partial,
            window=window,
        )

    def remember(self, session_id: str, obs: Optional[FileObservation]) -> None:
        """Store an observation. Pure in-memory, safe on the event loop."""
        if not session_id or obs is None:
            return
        self._observations.setdefault(session_id, {})[obs.path] = obs

    def record_read(
        self,
        session_id: str,
        path,
        *,
        partial: bool = False,
        window: Optional[tuple[int, int]] = None,
    ) -> Optional[FileObservation]:
        """Blocking convenience wrapper around observe + remember."""
        if not session_id:
            return None
        obs = self.observe(path, partial=partial, window=window)
        self.remember(session_id, obs)
        return obs

    def note_write(self, session_id: str, path) -> Optional[FileObservation]:
        """Refresh the observation after the agent's own write, so the
        next edit in the same turn is not reported stale against the value
        the agent itself just produced."""
        return self.record_read(session_id, path, partial=False)

    def get(self, session_id: str, path) -> Optional[FileObservation]:
        return self._observations.get(session_id, {}).get(self._key(path))

    def invalidate_session(self, session_id: str, reason: str = "") -> int:
        """Drop every observation for a session.

        Called after any shell command that is not provably read-only. We
        do not try to work out which paths a shell command touched: shell
        is a full programming language, path extraction is unsound, and a
        guard that is wrong in the unsafe direction is worse than one that
        is merely conservative.
        """
        dropped = len(self._observations.pop(session_id, {}) or {})
        if dropped:
            logger.debug(
                "invalidated %d file observation(s) for session %s (%s)",
                dropped, session_id, reason or "unspecified",
            )
        return dropped

    def forget_session(self, session_id: str) -> None:
        self._observations.pop(session_id, None)

    # ── the check ─────────────────────────────────────────────────

    def check_write(self, session_id: str, path) -> WriteCheck:
        mode = enforcement_mode()
        key = self._key(path)
        target = Path(key)

        if mode == MODE_OFF or not session_id:
            return WriteCheck(VERDICT_OK, key, mode, allowed=True)

        obs = self._observations.get(session_id, {}).get(key)
        exists = target.is_file()

        if obs is None:
            if not exists:
                # Creating a new file. Nothing to have read.
                return WriteCheck(VERDICT_OK, key, mode, allowed=True)
            return self._verdict(
                VERDICT_NEVER_READ, key, mode,
                f"{key} was not read in this session before being written. "
                f"Call coding_tools__read_file first so the edit is made "
                f"against what the file actually contains.",
            )

        if not exists:
            return self._verdict(
                VERDICT_GONE, key, mode,
                f"{key} was read in this session but no longer exists.",
                partial_read=obs.partial,
            )

        fp = _fingerprint(target)
        if fp is None:
            return WriteCheck(VERDICT_OK, key, mode, allowed=True)
        mtime_ns, size, digest = fp
        unchanged = (
            size == obs.size
            and (
                (digest and obs.content_hash and digest == obs.content_hash)
                or (not digest and mtime_ns == obs.mtime_ns)
            )
        )
        if unchanged:
            return WriteCheck(
                VERDICT_OK, key, mode, allowed=True, partial_read=obs.partial,
            )
        return self._verdict(
            VERDICT_STALE, key, mode,
            f"{key} changed since this session read it (size "
            f"{obs.size} -> {size}). Re-read it before editing so the edit "
            f"does not discard the newer content.",
            partial_read=obs.partial,
        )

    @staticmethod
    def _verdict(
        verdict: str, path: str, mode: str, message: str, *,
        partial_read: bool = False,
    ) -> WriteCheck:
        return WriteCheck(
            verdict=verdict,
            path=path,
            mode=mode,
            allowed=mode != MODE_ENFORCE,
            message=message,
            partial_read=partial_read,
        )


def _now() -> float:
    return time.time()


# ── shell read-only classification ────────────────────────────────────

# Conservative: a command only counts as read-only when every segment's
# argv[0] is on this list. Anything else invalidates the session's
# observations. Being wrong here in the permissive direction means an
# edit passes a staleness check it should have failed, so the list stays
# short on purpose.
READ_ONLY_ARGV0 = frozenset({
    "awk", "basename", "cat", "cksum", "column", "cut", "date", "df",
    "dirname", "du", "echo", "env", "file", "find", "fgrep", "grep",
    "head", "hostname", "id", "jq", "less", "ls", "md5", "md5sum", "more",
    "nl", "od", "printenv", "ps", "pwd", "readlink", "realpath", "rg",
    "sha1sum", "sha256sum", "sort", "stat", "tail", "top", "tr", "tree",
    "type", "uname", "uniq", "uptime", "wc", "whereis", "which", "who",
    "whoami", "xxd",
})

GIT_READ_ONLY_SUBCOMMANDS = frozenset({
    "blame", "branch", "cat-file", "describe", "diff", "grep", "log",
    "ls-files", "ls-remote", "remote", "rev-parse", "shortlog", "show",
    "status", "tag",
})

_SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\||\n")
_REDIRECT = re.compile(r"(?<!\d)>|>>|\btee\b")


def bash_is_read_only(command: str) -> bool:
    """True when every segment of ``command`` is a known read-only tool.

    Deliberately syntactic and shallow. The alternative, extracting the
    paths a shell command writes, is not decidable for a shell, and a
    wrong answer silently disarms the staleness guard.
    """
    if not command or not command.strip():
        return True
    if _REDIRECT.search(command):
        return False
    for segment in _SEGMENT_SPLIT.split(command):
        tokens = segment.strip().split()
        if not tokens:
            continue
        argv0 = Path(tokens[0]).name
        # Strip a leading `VAR=value` assignment prefix.
        while "=" in argv0 and not argv0.startswith("="):
            tokens = tokens[1:]
            if not tokens:
                return False
            argv0 = Path(tokens[0]).name
        if argv0 == "git":
            sub = next(
                (t for t in tokens[1:] if not t.startswith("-")), "",
            )
            if sub not in GIT_READ_ONLY_SUBCOMMANDS:
                return False
            continue
        if argv0 not in READ_ONLY_ARGV0:
            return False
    return True


# ── singleton ─────────────────────────────────────────────────────────

_tracker: Optional[FileStateTracker] = None


def get_tracker() -> FileStateTracker:
    global _tracker
    if _tracker is None:
        _tracker = FileStateTracker()
    return _tracker
