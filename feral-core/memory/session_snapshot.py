"""Phase 3 (audit-r10 overhaul) — primary chat-thread snapshot store.

Operator complaint #15:
> "The chat on the app can't fetch stuff I did on the local brain chat."

The v2026.5.19 `primary_session_id` work unified the wire-level
`session_id` between web and phone. But the in-RAM thread still lives
under `Orchestrator.conversation_history[session_id]` and is wiped by
`on_session_disconnect` whenever ANY WebSocket on that id closes —
including a web tab refresh. So the operator sees the right design
("one brain, one memory") right up until a surface disconnects.

This module is one half of the Phase 3 fix: snapshot the primary
thread to disk so cold-boot rehydrates the last ~50 turns. The other
half is the `BrainState` session-refcount that skips cleanup while
any surface is still attached. Together they make the primary thread
durable across surface lifecycle AND brain restarts.

Wire format (JSON at `<feral_data_home>/primary_session_thread.json`):

    {
      "session_id": "primary-deadbeef",
      "saved_at": 1726342234.12,
      "conversation_history": [
        {"role": "user", "content": "...", "ts": ...},
        {"role": "assistant", "content": "...", "ts": ...},
        ...
      ],
      "working_memory": [
        {"role": "user", "text": "..."},
        {"role": "assistant", "text": "..."},
        ...
      ]
    }

Conservative caps: last 50 turns per surface to keep the file under
~256 KB on disk. Brain memory continues to hold the full deque in
RAM; the snapshot is the cold-boot baseline, not the truth source.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("feral.memory.session_snapshot")


# Default cap on history rows we persist. Tuned for "30 min of active
# back-and-forth" rather than full transcript export.
_DEFAULT_MAX_ENTRIES = 50


class SessionSnapshotStore:
    """JSON-file persistence for a single primary chat thread.

    Single-writer assumption: only the brain process writes. Reads are
    cheap (one file per brain install). If the file is missing or
    corrupt the loader returns `None` and the orchestrator boots from
    a clean primary thread — never raises.
    """

    def __init__(
        self,
        data_home: Path,
        *,
        filename: str = "primary_session_thread.json",
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._path = Path(data_home) / filename
        self._max_entries = max(1, int(max_entries))
        self._last_save_ts: float = 0.0
        self._min_save_interval_s: float = 2.5  # debounce hot loops
        # B9 — trailing-edge state. The debounce used to be leading-edge
        # ONLY: the first call in a window wrote, every later call
        # returned False, and nothing was scheduled to write the state
        # those calls carried. That is not a deferred write, it is a lost
        # one; five turns in rapid succession left one turn on disk while
        # RAM held five. ``_pending`` holds the newest suppressed
        # snapshot and ``_timer`` is the one-shot that lands it at the
        # end of the current window.
        self._lock = threading.RLock()
        self._pending: Optional[dict] = None
        self._timer: Optional[threading.Timer] = None
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Optional[dict]:
        """Return the persisted snapshot dict or None if absent/corrupt.

        Never raises — a brain that can't read its snapshot still
        boots; it just starts with an empty primary thread.
        """
        try:
            if not self._path.is_file():
                return None
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                logger.warning(
                    "primary_session_thread snapshot at %s is not a dict; ignoring",
                    self._path,
                )
                return None
            if "session_id" not in data:
                logger.warning(
                    "primary_session_thread snapshot missing session_id; ignoring",
                )
                return None
            return data
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "primary_session_thread snapshot read failed at %s: %s",
                self._path,
                exc,
            )
            return None

    def save(
        self,
        session_id: str,
        *,
        conversation_history: Optional[list[dict]] = None,
        working_memory: Optional[list[dict]] = None,
        force: bool = False,
    ) -> bool:
        """Atomically write the current primary thread snapshot.

        Returns True on success, False if skipped (debounced) or
        failed. Caller passes only the lists they have access to;
        either list may be omitted and the snapshot keeps whatever
        was there last (so the orchestrator side and the memory side
        can save independently).

        Atomicity: writes to a sibling temp file then renames so a
        crash mid-write never leaves a half-JSON snapshot.

        Debounce (B9): the return value still means "was written inline",
        so a hot chat loop is still not IO-bound and still gets False.
        What changed is that a suppressed call is now REMEMBERED and
        written on the trailing edge of the window instead of discarded.
        Before this, the debounce silently threw away every turn after
        the first in each 2.5s window, permanently.
        """
        if not session_id:
            return False

        now = time.time()
        if not force and (now - self._last_save_ts) < self._min_save_interval_s:
            self._defer(
                session_id,
                conversation_history=conversation_history,
                working_memory=working_memory,
                delay=self._min_save_interval_s - (now - self._last_save_ts),
            )
            return False

        with self._lock:
            # This write supersedes anything the debounce was holding.
            self._cancel_timer()
            self._pending = None

        existing = self.load() or {}
        merged: dict[str, Any] = {
            "session_id": session_id,
            "saved_at": now,
            "conversation_history": (
                _truncate(conversation_history, self._max_entries)
                if conversation_history is not None
                else existing.get("conversation_history", [])
            ),
            "working_memory": (
                _truncate(working_memory, self._max_entries)
                if working_memory is not None
                else existing.get("working_memory", [])
            ),
        }

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self._path.parent),
                delete=False,
                suffix=".tmp",
            ) as tmp:
                json.dump(merged, tmp, ensure_ascii=False)
                tmp_path = Path(tmp.name)
            os.replace(tmp_path, self._path)
            self._last_save_ts = now
            return True
        except OSError as exc:
            logger.warning(
                "primary_session_thread snapshot write failed at %s: %s",
                self._path,
                exc,
            )
            return False

    # ── B9: trailing edge ────────────────────────────────────────────

    def _defer(
        self,
        session_id: str,
        *,
        conversation_history: Optional[list[dict]],
        working_memory: Optional[list[dict]],
        delay: float,
    ) -> None:
        """Remember a debounced snapshot and arm the trailing write.

        The newest call wins: ``_pending`` is overwritten every time, so
        the write that eventually lands carries the LATEST state rather
        than whichever call happened to be first after the window opened.

        The timer is armed ONCE per window and never re-armed by a later
        call. Re-arming would be a reset-on-activity debounce, which
        under continuous traffic postpones the write forever -- the exact
        failure being fixed, wearing a different shape.
        """
        with self._lock:
            if self._closed:
                return
            self._pending = {
                "session_id": session_id,
                "conversation_history": conversation_history,
                "working_memory": working_memory,
            }
            if self._timer is not None:
                return
            timer = threading.Timer(max(0.0, delay), self._on_trailing_edge)
            # Daemon: a pending snapshot must never hold the interpreter
            # open. ``close()`` is the deterministic drain.
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _on_trailing_edge(self) -> None:
        """Timer callback. Runs off the brain's loop, on the timer thread."""
        with self._lock:
            self._timer = None
        try:
            self._flush_pending()
        except Exception:  # pragma: no cover - a timer thread must not die
            logger.warning(
                "primary_session_thread trailing snapshot failed", exc_info=True,
            )

    def _cancel_timer(self) -> None:
        """Caller must hold ``self._lock``."""
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _flush_pending(self) -> bool:
        """Write whatever the debounce is holding, if anything."""
        with self._lock:
            pending, self._pending = self._pending, None
        if not pending:
            return False
        return self.save(
            pending["session_id"],
            conversation_history=pending["conversation_history"],
            working_memory=pending["working_memory"],
            force=True,
        )

    def close(self) -> None:
        """Drain any deferred snapshot and stop the timer. Idempotent.

        The deterministic counterpart to the daemon timer: a process that
        exits inside a debounce window still lands its last turn.
        """
        with self._lock:
            self._cancel_timer()
            self._closed = True
        # Outside the timer, but the pending payload is still ours to
        # write; ``_flush_pending`` forces, so ``_closed`` cannot block it.
        self._flush_pending()

    def clear(self) -> None:
        """Remove the snapshot file (operator-initiated 'forget'
        action). Never raises."""
        try:
            self._path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning(
                "primary_session_thread snapshot clear failed at %s: %s",
                self._path,
                exc,
            )


def _truncate(items: Optional[list[dict]], cap: int) -> list[dict]:
    """v2026.5.29 — tool-aware tail that never persists orphan
    ``function_call_output`` rows.

    Two passes:

    1. Tail to the most recent ``cap`` rows, expanding backwards
       through any ``role:"tool"`` rows so the cut never lands inside
       an assistant ``tool_calls`` round-trip.
    2. Drop any leading orphan ``tool`` rows whose announcing assistant
       turn is absent from the tail (covers stale snapshots written
       by older brain builds).
    """
    if not items:
        return []
    cleaned = [dict(x) for x in items if isinstance(x, dict)]
    if len(cleaned) > cap:
        start = len(cleaned) - cap
        while start > 0:
            row = cleaned[start]
            if row.get("role") != "tool":
                break
            prev = cleaned[start - 1]
            prev_role = prev.get("role")
            if prev_role == "tool":
                start -= 1
                continue
            if prev_role == "assistant" and prev.get("tool_calls"):
                start -= 1
                continue
            break
        cleaned = cleaned[start:]
    announced: set[str] = set()
    for row in cleaned:
        if row.get("role") == "assistant" and row.get("tool_calls"):
            for tc in row["tool_calls"]:
                if isinstance(tc, dict):
                    cid = tc.get("id")
                    if isinstance(cid, str) and cid:
                        announced.add(cid)
    drop_until = 0
    for i, row in enumerate(cleaned):
        if row.get("role") != "tool":
            break
        cid = row.get("tool_call_id") or row.get("call_id") or ""
        if cid and cid in announced:
            break
        drop_until = i + 1
    if drop_until:
        cleaned = cleaned[drop_until:]
    return cleaned


__all__ = ["SessionSnapshotStore"]
