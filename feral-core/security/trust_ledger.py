"""Earned autonomy: latitude that widens with a track record, bounded by undo.

The problem
===========
Approval is binary everywhere in this market. An operator picks a static
posture up front -- ask about everything, ask about risky things, ask
about nothing -- and it never moves. So the layer is either noise or
absent, and the well-documented outcome is that people start approving
without reading:

    "users start rubber-stamping everything within a week, and then the
    whole layer is theater"

An agent that has written the same file correctly forty times should not
ask a forty-first time in the same voice it used the first time. That is
what this module is for.

Why it lives here and not in ``learner.py``
==========================================
``agents/learner.py:247`` already computes per-skill reliability from the
execution log -- success rate, recent failures, an ``avoid`` /
``caution`` / ``degraded`` verdict. Its only consumer is
``get_routing_penalties``, which decides *which skill to call*. The
approval gate never sees it.

That signal cannot be reused directly here, because
``ToolRunner.enforce_safety`` is **synchronous** (``tool_runner.py:436``)
and ``get_skill_reliability`` is a coroutine that queries the execution
log. Awaiting a database read inside the approval hot path would be the
wrong trade even if it were possible. So trust is maintained
incrementally as executions complete and read synchronously at the gate.

It is also a deliberately different question. Routing asks "is this skill
working?" at skill granularity. Trust asks "has this exact tool behaved,
recently, without me having to intervene?" -- and a single bad outcome
must revoke it immediately, which a success *rate* smooths away. A skill
at 0.92 over 100 calls looks healthy to routing while its last three
calls failed.

The boundary: trust never exceeds undo
======================================
Latitude is granted only for actions FERAL can take back. You cannot
honestly stop asking about something you cannot reverse.

Today that is exactly the checkpoint-covered file writes
(``skills/checkpoints.py``): ``coding_tools__write_file`` and
``coding_tools__edit_file`` stash their prior contents and are restorable
by ``revert_turn``. Sent email, calendar mutations, smart-home actions
and purchases have no undo, so they are never trusted here regardless of
track record.

That is a feature boundary rather than a caveat. When checkpoints extend
to another domain, that domain becomes eligible automatically by being
added to ``UNDOABLE_TOOLS`` -- and until it does, the honest answer to
"why does it still ask about sending email?" is "because it cannot
unsend it."

What this does not do
=====================
* It never overrides ``strict``. An operator who chose strict asked to be
  consulted, and a track record is not consent.
* It never overrides ``loose``. Loose already approves everything.
* It never touches ``DENY``. A denied tool is denied by policy, and no
  amount of good behaviour promotes it.
* It never grants a tool that has not run cleanly here. There is no
  priming, no inheritance between tools, no cross-session credit for a
  tool the ledger has not itself observed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("feral.security.trust_ledger")

__all__ = [
    "TrustLedger",
    "UNDOABLE_TOOLS",
    "DEFAULT_PROMOTE_AFTER",
    "get_ledger",
]

# Exactly the tools ``skills/checkpoints.py`` can restore. Adding a name
# here without extending checkpoints would grant latitude over an action
# nobody can take back, which is the one thing this module exists to
# prevent -- ``test_trust_ledger.py`` asserts the two stay in step.
UNDOABLE_TOOLS = frozenset({
    "coding_tools__write_file",
    "coding_tools__edit_file",
})

# Consecutive clean executions before a tool stops asking. Five is a
# deliberate compromise: low enough that a real coding session reaches it
# within one task, high enough that a tool cannot be promoted by a single
# lucky call. It is not tuned against data, because there is no data yet
# -- it is the number an operator can override once there is.
DEFAULT_PROMOTE_AFTER = 5

_ENV_PROMOTE_AFTER = "FERAL_TRUST_PROMOTE_AFTER"
_ENV_DISABLE = "FERAL_TRUST_DISABLED"


def _state_path() -> Path:
    from config.loader import feral_home

    return Path(feral_home()) / "trust_ledger.json"


class TrustLedger:
    """Per-tool consecutive-success counters, read synchronously.

    Thread-safe: the gate is called from the request path while
    ``record`` is called from the orchestrator's execution path, and on
    some surfaces those are different threads.
    """

    def __init__(
        self,
        *,
        promote_after: Optional[int] = None,
        path: Optional[Path] = None,
        persist: bool = True,
    ):
        self._lock = threading.RLock()
        self._streaks: dict[str, int] = {}
        self._last_outcome: dict[str, float] = {}
        self._persist = persist
        self._path = Path(path) if path is not None else None

        if promote_after is not None:
            self.promote_after = max(1, int(promote_after))
        else:
            raw = os.environ.get(_ENV_PROMOTE_AFTER, "").strip()
            try:
                self.promote_after = max(1, int(raw)) if raw else DEFAULT_PROMOTE_AFTER
            except ValueError:
                logger.warning(
                    "%s=%r is not an integer; using %d",
                    _ENV_PROMOTE_AFTER, raw, DEFAULT_PROMOTE_AFTER,
                )
                self.promote_after = DEFAULT_PROMOTE_AFTER

        if self._persist:
            self._load()

    # ── the gate's question ──────────────────────────────────────────

    def is_trusted(self, tool_name: str) -> bool:
        """Has this tool earned the right to skip the approval prompt?

        Synchronous and allocation-free on the hot path. Returns False
        for anything outside :data:`UNDOABLE_TOOLS` no matter how long
        its streak, so a bug that let an unreversible tool accumulate a
        streak still cannot promote it.
        """
        if os.environ.get(_ENV_DISABLE, "").strip().lower() in ("1", "true", "yes"):
            return False
        if tool_name not in UNDOABLE_TOOLS:
            return False
        with self._lock:
            return self._streaks.get(tool_name, 0) >= self.promote_after

    # ── outcomes ─────────────────────────────────────────────────────

    def record(self, tool_name: str, *, success: bool) -> None:
        """Fold one execution outcome into the tool's streak.

        A failure resets to zero rather than decrementing. Trust should
        be slow to gain and instant to lose: an operator who sees a tool
        misbehave and is then asked about it once, twice, and then not
        again has learned that the gate does not mean anything.
        """
        if tool_name not in UNDOABLE_TOOLS:
            # Cheap guard, but also keeps the state file from growing a
            # row for every tool the brain has ever called.
            return
        with self._lock:
            if success:
                self._streaks[tool_name] = self._streaks.get(tool_name, 0) + 1
            else:
                had = self._streaks.get(tool_name, 0)
                self._streaks[tool_name] = 0
                if had >= self.promote_after:
                    logger.info(
                        "trust revoked for %s after a failed execution "
                        "(was at %d clean runs)", tool_name, had,
                    )
            self._last_outcome[tool_name] = time.time()
            self._save_locked()

    def revoke(self, tool_name: str, *, reason: str = "") -> None:
        """Drop a tool back to asking, immediately.

        Called when the operator reverts a turn. A revert is the
        strongest possible signal that an auto-approved action was not
        wanted, and it is stronger than a failed execution: the tool
        reported success and the human disagreed.
        """
        with self._lock:
            had = self._streaks.get(tool_name, 0)
            self._streaks[tool_name] = 0
            self._save_locked()
        if had:
            logger.info(
                "trust revoked for %s (%s); was at %d clean runs",
                tool_name, reason or "operator action", had,
            )

    def revoke_all(self, *, reason: str = "") -> None:
        """Reset every tool. Used when a whole turn is reverted."""
        with self._lock:
            if not any(self._streaks.values()):
                return
            self._streaks = {k: 0 for k in self._streaks}
            self._save_locked()
        logger.info("trust revoked for all tools (%s)", reason or "operator action")

    # ── introspection, for the receipts UI ───────────────────────────

    def state(self, tool_name: str) -> dict:
        """What the operator would need to see to trust this feature."""
        with self._lock:
            streak = self._streaks.get(tool_name, 0)
            return {
                "tool_name": tool_name,
                "undoable": tool_name in UNDOABLE_TOOLS,
                "clean_runs": streak,
                "promote_after": self.promote_after,
                "trusted": self.is_trusted(tool_name),
                "last_outcome_at": self._last_outcome.get(tool_name),
            }

    def snapshot(self) -> list[dict]:
        """Every tool eligible for trust, and where it stands."""
        return [self.state(name) for name in sorted(UNDOABLE_TOOLS)]

    # ── persistence ──────────────────────────────────────────────────

    def _resolve_path(self) -> Optional[Path]:
        if self._path is not None:
            return self._path
        try:
            return _state_path()
        except Exception:
            return None

    def _load(self) -> None:
        path = self._resolve_path()
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            logger.warning("trust ledger unreadable (%s); starting fresh", exc)
            return
        streaks = data.get("streaks")
        if not isinstance(streaks, dict):
            return
        with self._lock:
            # Only names that are still undoable. A tool that loses its
            # checkpoint coverage between releases must not keep a
            # streak that would promote it under the new rules.
            self._streaks = {
                k: int(v)
                for k, v in streaks.items()
                if k in UNDOABLE_TOOLS and isinstance(v, (int, float))
            }
            last = data.get("last_outcome")
            if isinstance(last, dict):
                self._last_outcome = {
                    k: float(v) for k, v in last.items()
                    if k in UNDOABLE_TOOLS and isinstance(v, (int, float))
                }

    def _save_locked(self) -> None:
        if not self._persist:
            return
        path = self._resolve_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "version": 1,
                "streaks": self._streaks,
                "last_outcome": self._last_outcome,
            }, indent=2))
            tmp.replace(path)
        except OSError as exc:
            # A ledger that cannot persist still works for this process.
            # Losing it means more prompts, never fewer, so this is the
            # safe direction to fail in.
            logger.debug("trust ledger not persisted: %s", exc)


_ledger: Optional[TrustLedger] = None
_ledger_lock = threading.Lock()


def get_ledger() -> TrustLedger:
    """Process-wide ledger."""
    global _ledger
    with _ledger_lock:
        if _ledger is None:
            _ledger = TrustLedger()
        return _ledger
