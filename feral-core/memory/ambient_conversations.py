"""Read side of the ambient conversation store.

The glasses are the microphone, the phone transcribes on device, and
``api/server.py`` owns every WRITE into ``ambient_transcripts.db``
(``_ambient_store``, ``_ambient_mark_processed``). This module is the
read side, and it exists because there was none: the transcripts, their
summaries, the people in them and the commitments lifted out of them
were all stored and none of it was reachable by the agent.

The concrete failure, reported 2026-08-22: asked "is there any ambient
conversation recorded?", the agent answered "I don't have any ambient
audio recording active" while the brain held four transcripts, digests
generated, and commitments already extracted from one of them. It did
not search and come up empty. It answered from its self-model, because
no tool existed for the question, so the only thing it could consult
was its own idea of what it is.

That is worse than an empty result. An empty result is a fact about the
world; this was a confident claim about a capability, and the ADHD use
case this feature exists for ("what did I say I'd do") is exactly the
case where the brain HAS the answer and denied having it.

Deliberately NOT importing from ``api.server``. Skills import this, and
api.server imports half the brain; the dependency would be a cycle and
a layering inversion. The cost is that the db path and the column names
are stated in two places, so the docstrings on both sides say so.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Optional

logger = logging.getLogger("feral.memory.ambient")

#: Columns this module reads. Kept explicit rather than ``SELECT *`` so
#: a column added by the writer cannot silently change the shape a tool
#: returns to the model.
_COLUMNS = (
    "transcript_id", "session_id", "node_id", "received_at",
    "processed_at", "episode_id", "digest_json",
)


def ambient_db_path():
    """Where received transcripts live. Mirrors api.server._ambient_db_path."""
    from config.loader import feral_home
    return feral_home() / "ambient_transcripts.db"


def _connect() -> Optional[sqlite3.Connection]:
    """Open the store read-only-ish, or None if it does not exist yet.

    None means "the brain has never received a transcript", which is a
    real and common state (no glasses paired, or nothing recorded yet).
    It is NOT an error and must not be reported as one: a tool that
    errors here would push the model straight back to guessing, which
    is the entire defect this module exists to close.
    """
    path = ambient_db_path()
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as exc:
        logger.warning("ambient: could not open %s: %s", path, exc)
        return None


def _has_table(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='ambient_transcripts'"
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _row_to_conversation(row: Any, *, include_detail: bool) -> dict:
    """One stored row as the shape a tool hands the model.

    ``status`` is the same vocabulary the phone already receives on
    ``ambient_digest``: ready, pending. It matters here for the same
    reason it matters there. A transcript that has arrived but is not
    yet summarized is not "no conversation"; saying so would reproduce
    the original bug one layer down.
    """
    digest: dict = {}
    raw = row["digest_json"] if "digest_json" in row.keys() else None
    if raw:
        try:
            digest = json.loads(raw) or {}
        except Exception:
            logger.debug("ambient: unreadable digest_json for %s", row["transcript_id"])
            digest = {}

    processed = row["processed_at"] if "processed_at" in row.keys() else None
    out = {
        "transcript_id": row["transcript_id"],
        "status": "ready" if processed else "pending",
        "recorded_at": row["received_at"] if "received_at" in row.keys() else None,
        "processed_at": processed,
        "episode_id": (row["episode_id"] if "episode_id" in row.keys() else "") or "",
        "summary": str(digest.get("summary") or ""),
        "people": [str(p) for p in (digest.get("people") or [])],
        "topics": [str(t) for t in (digest.get("topics") or [])],
        "commitments": [c for c in (digest.get("commitments") or []) if isinstance(c, dict)],
        "physiological_note": str(digest.get("physiological_note") or ""),
    }
    if include_detail:
        # The full transcript. Off by default: these run to 20k+
        # characters and a list of ten would bury the answer.
        out["detail"] = str(digest.get("detail") or "")
    return out


def list_conversations(
    limit: int = 20,
    *,
    include_detail: bool = False,
    with_commitments_only: bool = False,
) -> dict:
    """Recorded conversations, newest first.

    Answers "do I have any recordings", which is the question that had
    no tool. Returns a dict rather than a bare list so the count and
    the pending/ready split are stated explicitly: a model handed ``[]``
    has to infer what that means, and inferring is what went wrong.
    """
    conn = _connect()
    if conn is None:
        return {
            "total": 0, "returned": 0, "pending": 0, "conversations": [],
            "note": (
                "No ambient conversation store exists yet on this brain. "
                "Nothing has ever been recorded."
            ),
        }
    try:
        if not _has_table(conn):
            return {
                "total": 0, "returned": 0, "pending": 0, "conversations": [],
                "note": "No ambient conversations have been recorded yet.",
            }
        limit = max(1, min(int(limit or 20), 100))
        total = conn.execute(
            "SELECT COUNT(*) FROM ambient_transcripts"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM ambient_transcripts WHERE processed_at IS NULL"
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM ambient_transcripts "
            "ORDER BY received_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception as exc:
        logger.warning("ambient: list failed: %s", exc)
        return {
            "total": 0, "returned": 0, "pending": 0, "conversations": [],
            "note": f"Could not read the ambient conversation store: {exc}",
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass

    items = [_row_to_conversation(r, include_detail=include_detail) for r in rows]
    if with_commitments_only:
        items = [c for c in items if c["commitments"]]
    return {
        "total": total,
        "returned": len(items),
        "pending": pending,
        "conversations": items,
    }


def search_conversations(
    query: str,
    limit: int = 20,
    *,
    include_detail: bool = False,
) -> dict:
    """Conversations matching ``query`` across summary, people, topics,
    commitments and the transcript itself.

    A plain substring scan over the stored digest, deliberately. The
    corpus is one row per recorded conversation, which is tens to low
    thousands for a single operator, and the alternative (a second FTS
    index over a table api/server.py owns) buys ranking on a set small
    enough to read whole. If this ever needs ranking, the episodes
    table already has FTS over the same summaries.
    """
    q = str(query or "").strip().lower()
    if not q:
        return list_conversations(limit=limit, include_detail=include_detail)

    everything = list_conversations(limit=100, include_detail=True)
    if not everything["conversations"]:
        return {
            "query": query, "total_searched": everything["total"],
            "matched": 0, "conversations": [],
            "note": everything.get("note")
            or "No ambient conversations have been recorded yet.",
        }

    matched = []
    for conv in everything["conversations"]:
        haystack = " ".join([
            conv["summary"],
            " ".join(conv["people"]),
            " ".join(conv["topics"]),
            " ".join(str(c.get("text") or "") for c in conv["commitments"]),
            conv.get("detail") or "",
        ]).lower()
        if q in haystack:
            if not include_detail:
                conv = {k: v for k, v in conv.items() if k != "detail"}
            matched.append(conv)
        if len(matched) >= max(1, min(int(limit or 20), 100)):
            break

    return {
        "query": query,
        "total_searched": everything["total"],
        "matched": len(matched),
        "conversations": matched,
    }


def commitments_from_conversations(limit: int = 50) -> dict:
    """Every commitment lifted out of a recorded conversation.

    The ADHD case this whole feature exists for is "what did I say I'd
    do". That question was answerable from stored data and had no path
    to it, so it gets a direct one rather than requiring the model to
    list conversations and then read each digest.
    """
    listing = list_conversations(limit=100)
    out = []
    for conv in listing["conversations"]:
        for commitment in conv["commitments"]:
            out.append({
                "text": str(commitment.get("text") or ""),
                "due_iso": str(commitment.get("due_iso") or "") or None,
                "transcript_id": conv["transcript_id"],
                "recorded_at": conv["recorded_at"],
                "from_conversation": conv["summary"][:200],
                "people": conv["people"],
            })
        if len(out) >= max(1, min(int(limit or 50), 200)):
            break
    return {
        "total_conversations": listing["total"],
        "commitments": out,
        "count": len(out),
        "note": listing.get("note"),
    }
