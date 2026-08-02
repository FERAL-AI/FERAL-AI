"""REST surface for FERAL's file-write checkpoints.

Read the turns the agent has written files in, inspect what a revert
would do, and run one. Backed by ``skills/checkpoints.py``, which stores
content-addressed blobs plus a SQLite index under
``$FERAL_HOME/checkpoints`` and never invokes git.

``feral checkpoints`` in the CLI deliberately does NOT go through these
routes: it reads the same SQLite directly, because the case you most
need a revert in is the one where the brain is wedged and its HTTP
surface is not answering.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from skills.checkpoints import BASH_NOT_COVERED_NOTE, get_store

router = APIRouter(tags=["checkpoints"])


def _store():
    try:
        return get_store()
    except Exception as exc:  # noqa: BLE001 - surfaced as 503, not a 500 trace
        raise HTTPException(
            status_code=503, detail=f"Checkpoint store unavailable: {exc}",
        ) from exc


@router.get("/api/checkpoints/turns")
async def list_turns(session_id: str = "", limit: int = 20):
    """Turns that wrote at least one file, most recent first."""
    store = _store()
    rows = await asyncio.to_thread(
        store.list_turns,
        session_id=session_id.strip() or None,
        limit=max(1, min(int(limit), 200)),
    )
    return {"count": len(rows), "turns": rows, "note": BASH_NOT_COVERED_NOTE}


@router.get("/api/checkpoints/turns/{turn_id}")
async def turn_detail(turn_id: str):
    """Per-write rows for one turn, plus the revert plan for it."""
    store = _store()
    entries = await asyncio.to_thread(store.entries_for_turn, turn_id)
    if not entries:
        raise HTTPException(
            status_code=404, detail=f"No checkpoints recorded for turn '{turn_id}'",
        )
    return {
        "turn_id": turn_id,
        "writes": entries,
        "plan": await asyncio.to_thread(store.plan_revert, turn_id),
    }


@router.post("/api/checkpoints/revert")
async def revert(body: dict):
    """Revert a turn.

    ``force`` defaults to false, and without it a turn containing ANY
    file whose current content no longer matches what the agent left is
    refused WHOLE: nothing is restored, not even the files that did not
    drift. That refusal is the point of the endpoint, and the
    all-or-nothing shape is deliberate, since a turn half-reverted is a
    tree in a state neither the user nor the agent ever saw. The drifted
    paths come back under ``drifted``; re-send with ``force`` to revert
    anyway and lose their newer content.

    Two shapes of the response to know about, both verified against a
    live brain and both surprising enough to check for before you build
    a UI on them:

    * A refused revert reports ``refused: true`` with
      ``error_code: "revert_refused_drift"`` and ``dry_run: false``. Key a
      UI off ``refused`` and ``error_code``, never off ``dry_run`` and
      never by parsing ``error`` prose. Both fields are present on every
      response, so they can be read unconditionally.
      (Previously a refusal and a preview were byte-identical: drift was
      checked before ``dry_run``, so previewing a drifted turn returned
      the refusal envelope. A dry run applies nothing, so it is now
      answered first and reports the drift as data.)
    * ``skipped`` is not where drifted files go. It only ever holds
      entries whose ``action`` is ``skip``; drift lands in ``drifted``
      while keeping ``action: "restore"``.
    """
    store = _store()
    turn_id = str((body or {}).get("turn_id") or "").strip()
    session_id = str((body or {}).get("session_id") or "").strip()
    if not turn_id:
        turn_id = await asyncio.to_thread(store.latest_turn, session_id or None) or ""
    if not turn_id:
        raise HTTPException(status_code=404, detail="No checkpointed turn to revert")

    # SQLite plus a restore per file. Blocking, so it goes to a thread
    # rather than stalling every other request on this loop.
    return await asyncio.to_thread(
        store.revert_turn,
        turn_id,
        force=bool((body or {}).get("force", False)),
        dry_run=bool((body or {}).get("dry_run", False)),
    )
