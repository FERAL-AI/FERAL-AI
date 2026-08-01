"""Durable memory of what an external coding agent did in a workspace.

Why this is episodes and not a new table
========================================
FERAL already has episodic memory, notes, a knowledge graph and a fused
timeline. This codebase has repeatedly grown a parallel subsystem next to
the one that already fit, so this module deliberately adds no storage of
its own. One external-agent turn becomes exactly one row in ``episodes``
with ``event_type`` :data:`EVENT_TYPE`, which buys, for free:

* FTS5 and vector recall through ``episode_search_hybrid``,
* the "what did I do yesterday" card, because
  ``skills/impl/timeline_fusion.py`` already fans out over
  ``episode_recent`` and window-filters it,
* decay, access tracking, at-rest encryption and cross-device sync,
  because every episode gets those.

The existing episode columns are used for what they already mean rather
than being repurposed: ``location`` is the workspace directory (where it
happened), ``participants`` is ``[agent_id]`` (who did it),
``session_id`` is the FERAL session handle (which run it belonged to).
Nothing new is added to the schema, so nothing has to be migrated.

What is kept and what is thrown away
====================================
A real opencode run produced 1026 ACP events for one turn. Storing that
stream would be useless twice over: too big to put in a prompt, and
mostly restatements of the same few facts. The digest keeps what is still
true after the turn ends and drops what was only true while it ran.

Dropped:

* ``agent_thought_chunk``. The agent's reasoning is the largest single
  contributor to the stream and it is not a fact about the workspace. The
  count is kept so the record is honest about what was discarded.
* Intermediate ``tool_call_update`` frames. ACP emits pending, then
  in_progress, then completed for one ``toolCallId``. Only the terminal
  state is a fact; the rest is progress reporting.
* Per-chunk message deltas, which are reassembled into one string.

Kept:

* One entry per ``toolCallId``, never fewer. This is the lesson from
  qm's ``src/harness/context-compaction.ts``, which refuses to drop a
  ``tool_call`` whose ``tool_result`` did not arrive and writes an
  explicit ``INTERRUPTED_TOOL_RESULT`` marker instead. The failure mode
  it avoids is a summary that reads as if an action never happened when
  in fact it happened and its outcome is unknown. Our analogue is
  :data:`INTERRUPTED_TOOL_CALL`, applied to any call that never reached a
  terminal status because the turn was cancelled, rejected or crashed.
* Every file path the turn touched, from three sources: the client's own
  ``fs/write_text_file`` handler (authoritative, FERAL performed the
  write), the ACP ``locations[]`` array on the tool call, and ``diff``
  content blocks.
* Every permission question and how it was answered, including the ones
  that were refused. "The agent asked to run rm and I said no" is the
  single most useful thing to be able to recall later.
* The assembled agent text, head and tail, so a long explanation keeps
  both its opening and its conclusion.
* The stop reason or the error, so "finished" and "gave up" are
  distinguishable a day later.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger("feral.memory.agent_activity")

# The episode ``event_type`` every external-agent turn is filed under.
# Grep for this string to find every consumer.
EVENT_TYPE = "external_agent_session"

# Written in place of a terminal status for a tool call that never
# reported one. Borrowed in spirit from qm's INTERRUPTED_TOOL_RESULT: the
# call must stay visible, because "we do not know if this happened" is a
# different fact from "this did not happen".
INTERRUPTED_TOOL_CALL = "interrupted (no terminal status was reported)"

# Terminal ACP tool-call statuses. Anything else at the end of a turn is
# an interrupted call.
TERMINAL_STATUSES = frozenset({"completed", "failed"})

# Caps. Every one of these is a bound on what reaches a prompt later, so
# they are deliberately small. The detail cap is the only one an operator
# is likely to want to move, so it is the only one with an env var.
DEFAULT_DETAIL_CHARS = 4000
MAX_TOOL_CALLS_KEPT = 40
MAX_FILES_KEPT = 40
MAX_PERMISSIONS_KEPT = 20
TEXT_HEAD_CHARS = 900
TEXT_TAIL_CHARS = 600

# How many recent episodes to pull before filtering down to external
# agent ones. Mirrors the sizing already used by timeline_fusion.
RECALL_PULL_LIMIT = 600


def memory_enabled() -> bool:
    """Whether turns are recorded at all.

    Env var, not a settings key: ``config/loader.py`` owns
    ``DEFAULT_SETTINGS`` and a key added there without a reader fails
    ``tests/test_settings_keys_have_readers.py``. Default is on, because
    a feature whose whole point is recall is useless off by default.
    Set ``FERAL_EXTERNAL_AGENT_MEMORY=0`` to disable.
    """
    raw = os.environ.get("FERAL_EXTERNAL_AGENT_MEMORY", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def detail_char_cap() -> int:
    """Cap on the stored ``detail`` body.

    ``FERAL_EXTERNAL_AGENT_DETAIL_CHARS``, default
    :data:`DEFAULT_DETAIL_CHARS`. Floored at 500: a cap small enough to
    cut the header off would store a record that cannot be read back.
    """
    try:
        value = int(os.environ.get("FERAL_EXTERNAL_AGENT_DETAIL_CHARS", ""))
    except (TypeError, ValueError):
        return DEFAULT_DETAIL_CHARS
    return max(500, value)


# ----------------------------------------------------------------------
# Digest
# ----------------------------------------------------------------------

@dataclass
class ToolCallDigest:
    """One ``toolCallId``, collapsed to its final state."""

    tool_call_id: str
    tool_name: str
    title: str
    status: str
    updates: int = 1

    @property
    def interrupted(self) -> bool:
        return self.status not in TERMINAL_STATUSES

    def line(self) -> str:
        state = self.status if not self.interrupted else INTERRUPTED_TOOL_CALL
        label = self.title or self.tool_name or self.tool_call_id
        if self.title and self.tool_name and self.tool_name not in self.title:
            label = f"{self.tool_name}: {self.title}"
        return f"- {label} [{state}]"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "title": self.title,
            "status": self.status,
            "interrupted": self.interrupted,
            "updates": self.updates,
        }


@dataclass
class TurnDigest:
    """Everything worth remembering about one external-agent turn."""

    agent_id: str
    workspace_dir: str
    session_handle: str
    acp_session_id: str = ""
    prompt: str = ""
    status: str = ""
    stop_reason: str = ""
    error: str = ""
    text: str = ""
    tool_calls: list[ToolCallDigest] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    permissions: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    started_at: float = 0.0
    ended_at: float = field(default_factory=time.time)

    @property
    def finished(self) -> bool:
        return self.status == "completed" and not self.error

    @property
    def refused(self) -> bool:
        return any(not p.get("allowed", True) for p in self.permissions)

    def headline(self) -> str:
        """The one line that lands in ``episodes.summary``.

        Written so it reads on its own in a timeline card, because that
        is where it will usually be seen: agent, repository, outcome,
        and the ask.
        """
        repo = os.path.basename(self.workspace_dir.rstrip(os.sep)) or self.workspace_dir
        if self.error:
            outcome = "failed"
        elif self.status == "completed":
            outcome = "finished"
        else:
            outcome = self.status or "unknown"
        touched = len(self.files)
        suffix = f", {touched} file{'s' if touched != 1 else ''} changed" if touched else ""
        if self.refused:
            suffix += ", a permission was refused"
        ask = " ".join(self.prompt.split())[:120]
        return f"{self.agent_id} {outcome} in {repo}{suffix}: {ask}"

    def detail(self, cap: Optional[int] = None) -> str:
        """The structured body that lands in ``episodes.detail``."""
        limit = cap if cap is not None else detail_char_cap()
        lines: list[str] = [
            f"agent: {self.agent_id}",
            f"workspace: {self.workspace_dir}",
            f"session: {self.session_handle}"
            + (f" (acp {self.acp_session_id})" if self.acp_session_id else ""),
            f"status: {self.status}"
            + (f" / {self.stop_reason}" if self.stop_reason else ""),
        ]
        if self.error:
            lines.append(f"error: {self.error}")
        if self.prompt:
            lines.append("")
            lines.append(f"asked: {' '.join(self.prompt.split())}")
        if self.files:
            lines.append("")
            lines.append("files touched:")
            lines.extend(f"- {p}" for p in self.files)
        if self.tool_calls:
            lines.append("")
            lines.append("tool calls:")
            lines.extend(tc.line() for tc in self.tool_calls)
        if self.permissions:
            lines.append("")
            lines.append("permissions:")
            for entry in self.permissions:
                verdict = entry.get("decision") or (
                    "allowed" if entry.get("allowed") else "refused"
                )
                lines.append(
                    f"- {entry.get('tool_name') or 'unknown'}: "
                    f"{entry.get('title') or ''} [{verdict}]".rstrip()
                )
        if self.text:
            lines.append("")
            lines.append("said:")
            lines.append(self.text)
        body = "\n".join(lines)
        if len(body) <= limit:
            return body
        return body[: limit - 20].rstrip() + "\n[truncated]"

    def importance(self) -> float:
        """How hard this record should resist decay.

        A turn that changed files or was refused a permission is worth
        more later than one that only read and answered. Nothing here is
        ever 1.0: an external-agent turn is not more important than the
        user's own memories.
        """
        score = 0.45
        if self.files:
            score += 0.2
        if self.refused or self.error:
            score += 0.15
        if any(tc.interrupted for tc in self.tool_calls):
            score += 0.05
        return min(0.9, score)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "workspace_dir": self.workspace_dir,
            "session_handle": self.session_handle,
            "acp_session_id": self.acp_session_id,
            "prompt": self.prompt,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "error": self.error,
            "text": self.text,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "files": list(self.files),
            "permissions": list(self.permissions),
            "counts": dict(self.counts),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "headline": self.headline(),
        }


def _trim(text: str) -> str:
    """Keep the head and the tail of a long agent message."""
    text = text.strip()
    if len(text) <= TEXT_HEAD_CHARS + TEXT_TAIL_CHARS:
        return text
    return (
        text[:TEXT_HEAD_CHARS].rstrip()
        + "\n[...]\n"
        + text[-TEXT_TAIL_CHARS:].lstrip()
    )


def _paths_from_event(raw: dict[str, Any]) -> list[str]:
    """File paths an ACP tool-call frame claims to have touched.

    ``locations[]`` is the protocol's own answer and both reference
    agents populate it (opencode ``src/acp/tool.ts``, hermes
    ``acp_adapter/tools.py``). ``diff`` content blocks carry a ``path``
    too, and an agent that reports one but not the other should not cost
    us the record.
    """
    found: list[str] = []
    locations = raw.get("locations")
    if isinstance(locations, list):
        for item in locations:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                found.append(item["path"])
    contents = raw.get("content")
    if isinstance(contents, list):
        for item in contents:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                found.append(item["path"])
    return found


def _relative(path: str, workspace_dir: str) -> str:
    """Workspace-relative when it sits inside, absolute otherwise."""
    if not workspace_dir:
        return path
    try:
        if os.path.commonpath([os.path.abspath(path), os.path.abspath(workspace_dir)]) == (
            os.path.abspath(workspace_dir)
        ):
            return os.path.relpath(path, workspace_dir)
    except (ValueError, OSError):
        pass
    return path


def digest_turn(
    *,
    agent_id: str,
    workspace_dir: str,
    session_handle: str,
    events: Iterable[Any],
    prompt: str = "",
    status: str = "",
    stop_reason: str = "",
    error: str = "",
    acp_session_id: str = "",
    permissions: Optional[list[dict[str, Any]]] = None,
    written_paths: Optional[Iterable[str]] = None,
    started_at: float = 0.0,
) -> TurnDigest:
    """Collapse a turn's ACP event stream into a bounded record.

    ``events`` are :class:`bridges.acp.AcpEvent` instances, but only
    their attributes are touched, so any object with the same shape works
    and this module stays importable without the bridges package.
    """
    calls: dict[str, ToolCallDigest] = {}
    message_parts: list[str] = []
    counts = {
        "events": 0,
        "thoughts_dropped": 0,
        "tool_call_frames": 0,
        "message_chunks": 0,
        "other_dropped": 0,
    }
    paths: list[str] = []
    seen_paths: set[str] = set()

    def _add_path(candidate: str) -> None:
        if not candidate:
            return
        shown = _relative(candidate, workspace_dir)
        if shown in seen_paths:
            return
        seen_paths.add(shown)
        paths.append(shown)

    for path in written_paths or ():
        _add_path(str(path))

    for event in events:
        counts["events"] += 1
        kind = getattr(event, "kind", "")
        if kind == "agent_thought_chunk":
            counts["thoughts_dropped"] += 1
            continue
        if kind == "agent_message_chunk":
            counts["message_chunks"] += 1
            message_parts.append(getattr(event, "text", "") or "")
            continue
        if kind in ("tool_call", "tool_call_update"):
            counts["tool_call_frames"] += 1
            call_id = getattr(event, "tool_call_id", "") or ""
            if not call_id:
                # A frame with no id cannot be collapsed against anything,
                # so it is counted and dropped rather than invented into a
                # synthetic call that would look like a real action.
                counts["other_dropped"] += 1
                continue
            existing = calls.get(call_id)
            title = getattr(event, "title", "") or ""
            tool_name = getattr(event, "tool_name", "") or ""
            new_status = getattr(event, "status", "") or ""
            if existing is None:
                calls[call_id] = ToolCallDigest(
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    title=title,
                    status=new_status,
                )
            else:
                existing.updates += 1
                # Later frames often omit the title and repeat the id, so
                # a field is only overwritten when the new frame has one.
                if title:
                    existing.title = title
                if tool_name:
                    existing.tool_name = tool_name
                if new_status:
                    existing.status = new_status
            for path in _paths_from_event(getattr(event, "raw", {}) or {}):
                _add_path(path)
            continue
        counts["other_dropped"] += 1

    ordered = list(calls.values())
    if len(ordered) > MAX_TOOL_CALLS_KEPT:
        # Keep the interrupted ones first: an unknown outcome is the
        # thing a reader most needs to see, and dropping it silently is
        # exactly the failure qm's marker exists to prevent.
        interrupted = [c for c in ordered if c.interrupted]
        rest = [c for c in ordered if not c.interrupted]
        ordered = (interrupted + rest)[:MAX_TOOL_CALLS_KEPT]

    return TurnDigest(
        agent_id=agent_id,
        workspace_dir=workspace_dir,
        session_handle=session_handle,
        acp_session_id=acp_session_id,
        prompt=prompt,
        status=status,
        stop_reason=stop_reason,
        error=error,
        text=_trim("".join(message_parts)),
        tool_calls=ordered,
        files=paths[:MAX_FILES_KEPT],
        permissions=list(permissions or ())[:MAX_PERMISSIONS_KEPT],
        counts=counts,
        started_at=started_at,
        ended_at=time.time(),
    )


# ----------------------------------------------------------------------
# Persistence, through the episode store that already exists
# ----------------------------------------------------------------------

async def record_turn(memory: Any, digest: TurnDigest) -> Optional[dict[str, Any]]:
    """Write one digest into episodic memory.

    Returns the store's episode row, or ``None`` when there is no memory
    store or recording is switched off. Never raises: losing the record
    of a coding turn must not turn a successful turn into a failed tool
    call.
    """
    if memory is None or not memory_enabled():
        return None
    saver = getattr(memory, "episode_save", None)
    if saver is None:
        return None
    try:
        return await saver(
            session_id=digest.session_handle,
            event_type=EVENT_TYPE,
            summary=digest.headline(),
            detail=digest.detail(),
            location=digest.workspace_dir,
            participants=[digest.agent_id],
            importance=digest.importance(),
        )
    except Exception as exc:
        logger.warning("could not record external agent turn: %s", exc)
        return None


# ----------------------------------------------------------------------
# Recall, across every agent at once
# ----------------------------------------------------------------------

def _is_agent_episode(row: dict[str, Any]) -> bool:
    return (row or {}).get("event_type") == EVENT_TYPE


def _agent_of(row: dict[str, Any]) -> str:
    participants = row.get("participants") or []
    if isinstance(participants, list) and participants:
        return str(participants[0])
    return "unknown"


def _same_workspace(row: dict[str, Any], workspace_dir: str) -> bool:
    if not workspace_dir:
        return True
    location = str(row.get("location") or "")
    want = os.path.abspath(os.path.expanduser(workspace_dir))
    have = os.path.abspath(location) if location else ""
    return have == want


async def recall(
    memory: Any,
    *,
    query: str = "",
    workspace_dir: str = "",
    agent_id: str = "",
    from_ts: Optional[float] = None,
    to_ts: Optional[float] = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Answer "what did the coding agents do" across every agent.

    This is the one-answer-for-four-agents surface. It reads back the
    same episodes anything else can read: a user who instead asks the
    fused timeline for yesterday sees these turns in the same card,
    because ``timeline_fusion`` already pulls ``episode_recent``.

    ``query`` routes through hybrid search when given, so a semantic ask
    ("the refactor that broke the tests") works; without one it is a
    straight recency walk, which is what "what happened yesterday" wants.
    """
    if memory is None:
        return {
            "sessions": [],
            "by_agent": {},
            "agents": [],
            "degraded": "no_memory",
        }

    rows: list[dict[str, Any]] = []
    degraded = ""
    try:
        if query:
            searcher = (
                getattr(memory, "episode_search_hybrid", None)
                or getattr(memory, "episode_search", None)
            )
            if searcher is None:
                degraded = "no_episode_search"
            else:
                rows = await searcher(query, limit=RECALL_PULL_LIMIT)
        else:
            recent = getattr(memory, "episode_recent", None)
            if recent is None:
                degraded = "no_episode_recent"
            else:
                rows = await recent(limit=RECALL_PULL_LIMIT)
    except Exception as exc:
        logger.warning("external agent recall failed: %s", exc)
        degraded = f"query_failed: {type(exc).__name__}"
        rows = []

    sessions: list[dict[str, Any]] = []
    for row in rows or []:
        if not _is_agent_episode(row):
            continue
        created = float(row.get("created_at") or 0)
        if from_ts is not None and created < from_ts:
            continue
        if to_ts is not None and created > to_ts:
            continue
        if not _same_workspace(row, workspace_dir):
            continue
        who = _agent_of(row)
        if agent_id and who != agent_id:
            continue
        sessions.append(
            {
                "episode_id": row.get("id"),
                "agent_id": who,
                "workspace_dir": row.get("location") or "",
                "session_handle": row.get("session_id") or "",
                "at": created,
                "summary": row.get("summary") or "",
                "detail": row.get("detail") or "",
            }
        )

    sessions.sort(key=lambda item: item["at"], reverse=True)
    sessions = sessions[: max(1, int(limit or 20))]

    by_agent: dict[str, list[dict[str, Any]]] = {}
    for entry in sessions:
        by_agent.setdefault(entry["agent_id"], []).append(entry)

    return {
        "sessions": sessions,
        "by_agent": by_agent,
        "agents": sorted(by_agent),
        "workspaces": sorted({s["workspace_dir"] for s in sessions if s["workspace_dir"]}),
        "degraded": degraded,
    }


async def last_turn_summary(
    memory: Any, session_handle: str
) -> str:
    """The headline of the most recent recorded turn for one session.

    Used to re-brief an agent that had to be restarted cold, so the
    replacement process is not blind to what its predecessor did.
    """
    if memory is None or not session_handle:
        return ""
    recent = getattr(memory, "episode_recent", None)
    if recent is None:
        return ""
    try:
        rows = await recent(limit=5, session_id=session_handle)
    except Exception as exc:
        logger.debug("could not read back the last turn: %s", exc)
        return ""
    for row in rows or []:
        if _is_agent_episode(row):
            return str(row.get("detail") or row.get("summary") or "")
    return ""
