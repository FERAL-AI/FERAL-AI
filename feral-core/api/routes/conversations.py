"""Conversation threads and session snapshot/branch/restore endpoints."""

import time
from uuid import uuid4
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException

from api.state import state
from agents.runtime_context_checkpoint import RuntimeContextError, legacy_context_mutation

router = APIRouter()


@asynccontextmanager
async def _legacy_context_guard(identities: tuple[str, ...], operation: str):
    try:
        async with legacy_context_mutation(state.orchestrator, state.memory, identities, operation):
            yield
    except RuntimeContextError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": "This operation cannot modify managed runtime context."}) from None


# ── Conversation Threads ──

@router.get("/api/conversations")
async def list_conversations(limit: int = 25, offset: int = 0, q: str = ""):
    """Page of conversation metadata, newest first, pinned on top.

    ``total`` is the count matching ``q`` across the whole table, not
    the length of ``conversations``. The v2 thread pane used to request
    this route with no parameters, render whatever came back, and offer
    no way forward, so thread 51 was unreachable.
    """
    if not state.memory:
        return {"conversations": [], "total": 0, "limit": limit, "offset": offset, "has_more": False}
    page = await state.memory.conversation_page(limit=limit, offset=offset, query=q)
    return {
        "conversations": page["items"],
        "total": page["total"],
        "limit": page["limit"],
        "offset": page["offset"],
        "has_more": page["has_more"],
        "query": q,
    }


@router.post("/api/conversations/new")
async def create_conversation(body: dict | None = None):
    payload = body or {}
    if "create_if_missing" in payload and type(payload["create_if_missing"]) is not bool:
        raise HTTPException(status_code=422, detail="create_if_missing must be a Boolean")
    if payload.get("create_if_missing") is True:
        # Opt-in atomic contract. Never fall back to the legacy upsert.
        identifier = payload.get("id")
        title = payload.get("title", "New conversation")
        if (not isinstance(identifier, str) or not identifier or identifier.strip() != identifier
                or len(identifier.encode("utf-8")) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in identifier)
                or not isinstance(title, str) or len(title.encode("utf-8")) > 800
                or set(payload) - {"id", "title", "create_if_missing"}):
            raise HTTPException(status_code=422, detail="Atomic creation requires a bounded exact ID/title and no message payload")
        if not state.memory:
            raise HTTPException(status_code=503, detail="Memory not initialized")
        operation = getattr(state.memory, "conversation_create_if_missing", None)
        if not callable(operation):
            raise HTTPException(status_code=501, detail="Atomic conversation creation is unavailable")
        try:
            result = await operation(identifier, title=title)
            document = result.get("conversation") if isinstance(result, dict) else None
            if (not isinstance(result, dict) or type(result.get("created")) is not bool or not isinstance(document, dict)
                    or document.get("id") != identifier or not isinstance(document.get("messages"), list)):
                raise ValueError("Unverified atomic creation result")
        except Exception:
            raise HTTPException(status_code=502, detail="Atomic conversation creation was not confirmed; inspect the exact thread before retrying") from None
        return {"ok": True, "id": identifier, "create_if_missing": True,
                "created": result["created"], "conversation": document}
    if not state.memory:
        return {"error": "Memory not initialized"}
    conversation_id = payload.get("id") or f"thread-{str(uuid4())[:10]}"
    title = payload.get("title", "New conversation")
    created = await state.memory.conversation_save(conversation_id, [], title=title)
    return {"ok": True, **created}


@router.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    if not state.memory:
        return {"error": "Memory not initialized"}
    conv = await state.memory.conversation_get(conversation_id)
    if not conv:
        return {"error": "Not found"}
    return conv


@router.get("/api/conversations/active/thread")
async def get_active_conversation(conversation_id: str = ""):
    """Resolve the active conversation for UI rehydration.

    Resolution order:
      1) explicit ``conversation_id`` query param when it exists,
      2) most recently updated thread,
      3) create-and-return a brand new empty thread.
    """
    if not state.memory:
        return {"error": "Memory not initialized"}

    conv = None
    if conversation_id:
        conv = await state.memory.conversation_get(conversation_id)

    if not conv:
        recent = await state.memory.conversation_list(limit=1)
        if recent:
            conv = await state.memory.conversation_get(recent[0]["id"])

    if conv:
        return conv

    created_id = f"thread-{str(uuid4())[:10]}"
    created = await state.memory.conversation_save(created_id, [], title="New conversation")
    return {
        "id": created.get("id", created_id),
        "title": created.get("title", "New conversation"),
        "messages": [],
        "message_count": created.get("message_count", 0),
        "updated_at": created.get("updated_at", time.time()),
    }


@router.post("/api/conversations/save")
async def save_conversation(body: dict):
    if not state.memory:
        return {"error": "Memory not initialized"}
    cid = body.get("id", "")
    messages = body.get("messages", [])
    title = body.get("title", "")
    if not cid:
        return {"error": "id is required"}
    return await state.memory.conversation_save(cid, messages, title)


@router.post("/api/conversations/{conversation_id}/rename")
async def rename_conversation(conversation_id: str, body: dict):
    """Set a user-chosen title that autosave will not overwrite.

    Distinct from ``/save`` on purpose: only this route marks the title
    as user-chosen. ``/save`` carries a title derived from the first
    user message on every autosave, and cannot be told apart from a
    rename by inspecting the string.
    """
    if not state.memory:
        return {"error": "Memory not initialized"}
    title = str((body or {}).get("title", "")).strip()
    if not title:
        return {"error": "title is required"}
    conv = await state.memory.conversation_rename(conversation_id, title)
    if not conv:
        return {"error": "Not found"}
    return {"ok": True, "id": conv["id"], "title": conv["title"], "title_custom": conv["title_custom"]}


@router.post("/api/conversations/{conversation_id}/pin")
async def pin_conversation(conversation_id: str, body: dict | None = None):
    """Pin or unpin a thread. Pinned threads sort above the rest."""
    if not state.memory:
        return {"error": "Memory not initialized"}
    pinned = bool((body or {}).get("pinned", True))
    conv = await state.memory.conversation_set_pinned(conversation_id, pinned)
    if not conv:
        return {"error": "Not found"}
    return {"ok": True, "id": conv["id"], "pinned": conv["pinned"]}


@router.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    if not state.memory:
        return {"error": "Memory not initialized"}
    async with _legacy_context_guard((conversation_id,), "delete"):
        await state.memory.conversation_delete(conversation_id)
    return {"ok": True}


# ── Session Snapshots ──

@router.post("/api/session/snapshot")
async def create_session_snapshot(body: dict):
    session_id = body.get("session_id", "")
    if not session_id:
        return {"error": "session_id is required"}
    async with _legacy_context_guard((session_id,), "snapshot"):
        return await _create_session_snapshot(body)


async def _create_session_snapshot(body: dict):
    if not state.memory:
        return {"error": "Memory store not initialized"}
    session_id = body.get("session_id", "")
    if not session_id:
        return {"error": "session_id is required"}
    history = state.orchestrator.conversation_history.get(session_id, []) if state.orchestrator else []
    return await state.memory.snapshot_session(
        session_id=session_id,
        history=history,
        label=body.get("label", ""),
        branch_name=body.get("branch_name", "main"),
    )


@router.get("/api/session/snapshots")
async def list_session_snapshots(session_id: str = "", branch_name: str = "", limit: int = 50):
    if not state.memory:
        return {"snapshots": []}
    snapshots = await state.memory.list_snapshots(
        session_id=session_id,
        branch_name=branch_name,
        limit=limit,
    )
    return {"snapshots": snapshots}


@router.get("/api/session/snapshots/{snapshot_id}")
async def get_session_snapshot(snapshot_id: str):
    if not state.memory:
        return {"error": "Memory store not initialized"}
    snap = await state.memory.get_snapshot(snapshot_id)
    if not snap:
        return {"error": f"Snapshot not found: {snapshot_id}"}
    return snap


@router.post("/api/session/branch")
async def branch_session(body: dict):
    if not state.memory:
        return {"error": "Memory store not initialized"}
    source_snapshot_id = body.get("snapshot_id", "")
    if source_snapshot_id:
        source = await state.memory.get_snapshot(source_snapshot_id)
    else:
        session_id = body.get("session_id", "")
        if not session_id:
            return {"error": "session_id is required when snapshot_id is omitted"}
        identities = (session_id, body["target_session_id"]) if body.get("target_session_id") else (session_id,)
        async with _legacy_context_guard(identities, "branch"):
            history = state.orchestrator.conversation_history.get(session_id, []) if state.orchestrator else []
            auto = await state.memory.snapshot_session(
                session_id=session_id, history=history, label="auto-branch-source", branch_name="main",
            )
        source_snapshot_id = auto["snapshot_id"]
        source = await state.memory.get_snapshot(source_snapshot_id)

    if not source:
        return {"error": f"Snapshot not found: {source_snapshot_id}"}

    branch_name = body.get("branch_name", f"branch-{int(time.time())}")
    branch_session_id = body.get("target_session_id", f"{source['session_id']}:{branch_name}:{str(uuid4())[:6]}")
    async with _legacy_context_guard((source["session_id"], branch_session_id), "branch"):
        state.memory.working_replace(branch_session_id, source.get("working", []))
        if state.orchestrator:
            state.orchestrator.conversation_history[branch_session_id] = source.get("history", [])
        branched_snapshot = await state.memory.snapshot_session(
            session_id=branch_session_id,
            history=source.get("history", []),
            label=body.get("label", f"branch from {source_snapshot_id}"),
            branch_name=branch_name,
            source_snapshot_id=source_snapshot_id,
        )
        return {
            "status": "branched",
            "source_snapshot_id": source_snapshot_id,
            "target_session_id": branch_session_id,
            "snapshot": branched_snapshot,
        }


@router.post("/api/session/restore")
async def restore_session_snapshot(body: dict):
    if not state.memory:
        return {"error": "Memory store not initialized"}
    snapshot_id = body.get("snapshot_id", "")
    if not snapshot_id:
        return {"error": "snapshot_id is required"}
    snapshot = await state.memory.get_snapshot(snapshot_id)
    if not snapshot:
        return {"error": f"Snapshot not found: {snapshot_id}"}

    session_id = body.get("session_id", snapshot["session_id"])
    as_new_session = bool(body.get("as_new_session", False))
    target_session_id = body.get("target_session_id")
    if not target_session_id:
        target_session_id = f"{session_id}:restore:{str(uuid4())[:6]}" if as_new_session else session_id

    async with _legacy_context_guard((snapshot["session_id"], target_session_id), "restore"):
        state.memory.working_replace(target_session_id, snapshot.get("working", []))
        if state.orchestrator:
            state.orchestrator.conversation_history[target_session_id] = snapshot.get("history", [])

        restore_snapshot = await state.memory.snapshot_session(
            session_id=target_session_id,
            history=snapshot.get("history", []),
            label=body.get("label", f"restore {snapshot_id}"),
            branch_name=snapshot.get("branch_name", "main"),
            source_snapshot_id=snapshot_id,
        )
        return {
            "status": "restored",
            "target_session_id": target_session_id,
            "restored_from_snapshot_id": snapshot_id,
            "snapshot": restore_snapshot,
        }
