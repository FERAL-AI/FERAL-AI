"""Memory, knowledge graph, wiki, and episode endpoints."""

import importlib.util
import json
import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from api.state import state
from config.loader import feral_home
from memory.ingest import MemoryIngestor

logger = logging.getLogger("feral.memory.api")
router = APIRouter()


# RC fix: this route used to validate the WRONG module tree
# (``memory.backends.*``) which is NOT what boot wires — boot uses
# ``memory.vector_index_backends.*`` (audit-r12 D4). That mismatch made
# the "installed" check pass for chroma even when ``chromadb`` was
# missing, let the operator persist a brick-the-brain selection, and
# always reported ``pending_unapplied``. We now use the SAME registry +
# loader the boot path uses, report the brain's ACTUAL runtime backend,
# and preflight a switch before persisting it.

# Optional dependency that each first-party backend needs at runtime.
# Used for a cheap "is it installed?" check without constructing a
# client (which would touch disk / network).
_BACKEND_DEP: dict[str, str | None] = {
    "sqlite_vec": None,  # built-in; degrades to FTS if the extension is absent
    "chroma": "chromadb",
    "qdrant": "qdrant_client",
}


def _known_backends() -> list[str]:
    from memory.vector_index_backends.base import _REGISTRY
    return sorted(_REGISTRY.keys())


def _backend_available(backend_id: str) -> bool:
    """Cheap availability probe: the backend module imports AND its
    optional runtime dependency is importable. No client construction,
    so no disk/network side effects."""
    from memory.vector_index_backends.base import _REGISTRY
    module_path = _REGISTRY.get(backend_id)
    if module_path is None:
        return False
    if importlib.util.find_spec(module_path) is None:
        return False
    dep = _BACKEND_DEP.get(backend_id, None)
    if dep is None:
        # sqlite_vec / unknown community backend: assume the backend
        # module's own import is the gate.
        return True
    return importlib.util.find_spec(dep) is not None


def _runtime_backend_id() -> str:
    """The backend the RUNNING brain actually stores vectors in."""
    mem = getattr(state, "memory", None)
    return str(getattr(mem, "_backend_id", "sqlite_vec") or "sqlite_vec")


async def _preflight_backend(backend_id: str) -> tuple[bool, str | None]:
    """Actually try to construct the backend (the same way boot does)
    so a switch can be rejected BEFORE it's persisted and bricks the
    next boot. Returns ``(ok, error)``. Constructs and immediately
    closes the backend on success."""
    if backend_id == "sqlite_vec":
        return True, None
    try:
        from memory.embeddings import EmbeddingProvider
        from memory.vector_index_backends import load_vector_index

        settings_path = feral_home() / "settings.json"
        backend_config = {}
        if settings_path.exists():
            try:
                cfg = json.loads(settings_path.read_text()).get("memory") or {}
                backend_config = cfg.get("backend_config") or {}
            except Exception:  # noqa: BLE001
                backend_config = {}
        dim = EmbeddingProvider().dimension
        backend = load_vector_index(backend_id, dim=dim, **backend_config)
        try:
            close = getattr(backend, "close", None)
            if close is not None:
                await close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        return True, None
    except Exception as exc:  # noqa: BLE001 — surface the real reason
        return False, str(exc)


@router.get("/api/memory/stats")
async def get_memory_stats():
    """Snapshot of memory subsystem health for the dashboard.

    v2026.5.34 surfaces the D11 decay state (active vs. forgotten
    episode counts, sweep cadence config) so the operator can see
    whether the background sweeper is actually running and how
    aggressive it is. The endpoint is also the canary for HA's
    health probe.
    """
    out: dict = {"ok": True}
    decay = getattr(state, "memory_decay", None)
    if decay is not None:
        try:
            out["decay"] = await decay.stats()
        except Exception as exc:
            logger.warning("memory_decay.stats() failed: %s", exc)
            out["decay"] = {"ok": False, "error": str(exc)}
    else:
        out["decay"] = {"enabled": False, "reason": "service_not_constructed"}
    # Lightweight episode totals straight off the store (do not gate
    # the health probe on the decay service being up). RC polish: the
    # store's canonical key is ``knowledge_triples`` — the pre-fix
    # route looked up ``s["knowledge"]`` which never existed, so the
    # WebUI's Recent tab always rendered "0 knowledge". We now read
    # the canonical key, expose it under the same name to the UI, and
    # keep a ``knowledge`` alias for any older client still reading
    # the legacy field. The store also returns ``ok: false`` /
    # ``reason: "stats_timeout"`` on a degraded path; pass those
    # through so the dashboard can render an honest unavailable chip
    # instead of a misleading row of zeros.
    try:
        s = await state.memory.stats()
        knowledge_count = int(s.get("knowledge_triples", 0) or 0)
        out["totals"] = {
            "episodes": int(s.get("episodes", 0) or 0),
            "notes": int(s.get("notes", 0) or 0),
            "knowledge_triples": knowledge_count,
            "knowledge": knowledge_count,
        }
        if s.get("ok") is False:
            out["ok"] = False
            reason = s.get("reason")
            if reason:
                out["reason"] = reason
    except Exception as exc:
        # The response does carry ok=False, but the reason died here, so
        # the one field that could explain a broken memory tile was dropped.
        logger.warning("memory.stats() failed in /api/memory/stats: %s", exc)
        out["totals"] = {}
        out["ok"] = False
        out["reason"] = "stats_error"
    return out


@router.post("/api/memory/forget/{episode_id}")
async def memory_forget(episode_id: str):
    """Mark an episode as forgotten *now*. Operator escape hatch for
    privacy / mistaken-input cases. Returns 404-shaped JSON when the
    id is unknown.
    """
    decay = getattr(state, "memory_decay", None)
    if decay is None:
        raise HTTPException(status_code=503, detail="memory_decay service not running")
    result = await decay.forget(episode_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "not_found"))
    return result


@router.post("/api/memory/recall/{episode_id}")
async def memory_recall(episode_id: str):
    """Reverse a forget. Hard-deleted episodes cannot be recalled —
    they're gone from disk — and return 404."""
    decay = getattr(state, "memory_decay", None)
    if decay is None:
        raise HTTPException(status_code=503, detail="memory_decay service not running")
    result = await decay.recall(episode_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "not_found"))
    return result


@router.post("/api/memory/decay/now")
async def memory_decay_now():
    """Trigger a one-shot decay sweep immediately. Useful for manual
    operator runs, tests, and the dashboard's "Run sweep now"
    button."""
    decay = getattr(state, "memory_decay", None)
    if decay is None:
        raise HTTPException(status_code=503, detail="memory_decay service not running")
    return await decay.run_once()


@router.post("/api/memory/compact")
async def memory_compact(session_id: str | None = None):
    """F2 — manually trigger session compaction.

    Without ``session_id`` this compacts every active orchestrator
    session. With a session_id it compacts that one session only.

    Compaction promotes summarisable turns into a real episode row
    (see ``memory.context_builder.compact_session``). Returns a list
    of per-session results so the dashboard / CLI can show what
    landed (``episode_id``, ``key_entities``, sizes).
    """
    if not state.memory or not state.orchestrator:
        raise HTTPException(status_code=503, detail="memory or orchestrator not initialized")

    sessions: list[str]
    if session_id:
        sessions = [session_id]
    else:
        sessions = list(state.orchestrator.conversation_history.keys())

    out: list[dict] = []
    for sid in sessions:
        history = state.orchestrator.conversation_history.get(sid, [])
        if not history:
            out.append({"session_id": sid, "compacted": False, "reason": "empty"})
            continue
        result = await state.memory.compact_session(
            sid, history, llm=state.orchestrator.llm,
        )
        if result.get("compacted") and result.get("history"):
            state.orchestrator.conversation_history[sid] = result["history"]
        result["session_id"] = sid
        out.append(result)
    return {"results": out, "count": len(out)}


@router.get("/api/memory/context")
async def get_memory_context(limit: int = 20):
    """Return the recent `## Memory` blocks the Brain assembled per LLM turn.

    Every system-prompt build records what multi-memory surfaced (working,
    known facts, episodes, recent actions) into a bounded in-process ring.
    The v2 `/memory/context` inspector reads this so users can prove the
    memory stack really does fire on every turn — not just `working_context`.
    """
    from agents.identity_loader import recent_memory_snapshots

    snapshots = recent_memory_snapshots(limit=max(1, min(limit, 50)))
    return {"count": len(snapshots), "snapshots": snapshots}


@router.get("/api/memory/backend")
async def get_memory_backend():
    """Return the configured memory backend AND what the running brain
    actually loaded.

    RC fix: this now reports the TRUTH using the wired vector-index
    backend system. ``backend`` is what ``settings.json`` says,
    ``runtime`` is what the live :class:`MemoryStore` is actually using,
    and ``pending_unapplied`` is true only when they genuinely differ
    (a real "restart to apply" or "boot fell back" condition). If the
    configured backend failed to construct at boot, ``boot_error`` /
    ``fell_back`` explain why (e.g. ``chromadb`` not installed) so the
    dashboard can show an actionable message instead of a silent lie.
    """
    from api.state import MEMORY_BACKEND_STATUS

    settings_path = feral_home() / "settings.json"
    current = "sqlite_vec"
    if settings_path.exists():
        try:
            current = (json.loads(settings_path.read_text()).get("memory") or {}).get(
                "backend", "sqlite_vec"
            )
        except Exception as exc:  # noqa: BLE001 — surface for ops, default for prod
            logger.warning("get_memory_backend: settings.json read failed: %s", exc)

    runtime = _runtime_backend_id()
    return {
        "backend": current,
        "runtime": runtime,
        "active_store": runtime,
        "pending_unapplied": current != runtime,
        "fell_back": bool(MEMORY_BACKEND_STATUS.get("fell_back")),
        "boot_error": MEMORY_BACKEND_STATUS.get("error"),
        "available": {name: _backend_available(name) for name in _known_backends()},
        # Boot status is not the whole truth. A backend can construct
        # perfectly at boot and still be unable to answer a single query,
        # which is exactly what happens when the stored vectors were
        # written by a different embedder than the one now configured:
        # every vector query raises, hybrid search silently degrades to
        # keyword-only, and this endpoint reported fell_back=false with
        # boot_error=null the whole time. Its own docstring claimed it
        # "now reports the TRUTH instead of a silent lie". It did not.
        **_semantic_health(),
    }


def _semantic_health() -> dict:
    """Whether semantic search can actually answer, not whether it booted.

    Reads the live store rather than boot status. ``None`` for the error
    means no vector query has failed in this process, which is not the
    same as proof that one would succeed, and the field is named to say
    so rather than implying a green light.
    """
    mem = getattr(state, "memory", None)
    if mem is None:
        return {"semantic_search": "unknown", "vector_leg_error": None}
    err = getattr(mem, "_vector_leg_error", None)
    return {
        "semantic_search": "degraded" if err else "ok",
        "vector_leg_error": err,
    }


@router.post("/api/memory/backend")
async def set_memory_backend(body: dict):
    backend = (body or {}).get("backend", "")
    known = _known_backends()
    if backend not in known:
        return {"ok": False, "error": f"unknown backend '{backend}'. Known: {known}"}

    if not _backend_available(backend):
        return {
            "ok": False,
            "error": (
                f"backend '{backend}' is not installed. Run "
                f"`pip install feral-ai[memory-{backend}]` or install the "
                "matching item from registry.feral.sh, then try again."
            ),
        }

    # Preflight: actually construct it the way boot will. If this fails,
    # DO NOT persist — persisting would brick the next boot (the exact
    # bug we're fixing). The construction is closed immediately.
    ok, err = await _preflight_backend(backend)
    if not ok:
        return {
            "ok": False,
            "error": (
                f"backend '{backend}' failed to initialize and was NOT saved "
                f"(your brain is unchanged): {err}"
            ),
        }

    settings_path = feral_home() / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_memory_backend: settings.json read failed: %s", exc)
        existing = {}
    existing.setdefault("memory", {})["backend"] = backend
    settings_path.write_text(json.dumps(existing, indent=2))

    runtime = _runtime_backend_id()
    note = (
        "Saved. Restart the Brain (`feral restart`) to load the new "
        f"backend. Existing embeddings stay in '{runtime}'; semantic "
        "search re-populates as new content is embedded."
        if backend != runtime
        else "Saved. This backend is already active."
    )
    return {
        "ok": True,
        "backend": backend,
        "runtime": runtime,
        "active_store": runtime,
        "pending_unapplied": backend != runtime,
        "note": note,
    }


# ── Knowledge Graph ──

async def _knowledge_graph_d3(limit: int) -> dict:
    """Build D3-style {nodes, links} from the entity graph and legacy triples."""
    memory = state.memory
    nodes: dict[str, dict] = {}
    links: list[dict] = []

    kg = getattr(memory, "kg", None)
    if kg:
        conn = await kg._conn()
        try:
            async with conn.execute(
                """
                SELECT r.id AS rid, r.relation_type,
                       s.id AS sid, s.name AS sname, s.entity_type AS stype,
                       t.id AS tid, t.name AS tname, t.entity_type AS ttype
                FROM relations r
                JOIN entities s ON r.source_id = s.id
                JOIN entities t ON r.target_id = t.id
                ORDER BY r.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await conn.close()
        for r in rows:
            sid, tid = r["sid"], r["tid"]
            if sid not in nodes:
                nodes[sid] = {
                    "id": sid,
                    "name": r["sname"],
                    "type": (r["stype"] or "thing"),
                }
            if tid not in nodes:
                nodes[tid] = {
                    "id": tid,
                    "name": r["tname"],
                    "type": (r["ttype"] or "thing"),
                }
            links.append(
                {
                    "source": sid,
                    "target": tid,
                    "relation": r["relation_type"],
                    "id": r["rid"],
                }
            )

    if not links:
        triples = await memory.knowledge_query(limit=limit)
        seen: dict[str, str] = {}
        nxt = 0

        def nid(label: str) -> str:
            nonlocal nxt
            if label not in seen:
                seen[label] = f"k_{nxt}"
                nxt += 1
            return seen[label]

        for t in triples:
            subj, obj = t["subject"], t["object"]
            sid, tid = nid(subj), nid(obj)
            if sid not in nodes:
                nodes[sid] = {"id": sid, "name": subj, "type": "legacy"}
            if tid not in nodes:
                nodes[tid] = {"id": tid, "name": obj, "type": "legacy"}
            links.append(
                {
                    "source": sid,
                    "target": tid,
                    "relation": t["predicate"],
                    "id": t.get("id", ""),
                }
            )

    return {"nodes": list(nodes.values()), "links": links}


@router.get("/api/knowledge/graph")
async def get_knowledge_graph(limit: int = 50):
    """Return a D3-compatible graph: ``{ nodes, links }``."""
    try:
        return await _knowledge_graph_d3(limit=max(1, min(limit, 500)))
    except Exception as e:
        return {"nodes": [], "links": [], "error": str(e)}


@router.get("/api/knowledge/entities")
async def search_knowledge_entities(q: str = "", limit: int = 20):
    """Search entities in the knowledge graph (FTS + embeddings when available)."""
    lim = max(1, min(limit, 100))
    kg = getattr(state.memory, "kg", None)
    try:
        if kg and q.strip():
            entities = await kg.search_entities(q.strip(), limit=lim)
            return {"entities": entities, "source": "graph"}
        if kg and not q.strip():
            conn = await kg._conn()
            try:
                async with conn.execute(
                    """
                    SELECT id, name, entity_type AS type, mention_count AS mentions
                    FROM entities
                    ORDER BY mention_count DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (lim,),
                ) as cur:
                    rows = await cur.fetchall()
            finally:
                await conn.close()
            return {
                "entities": [
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "type": r["type"],
                        "mentions": r["mentions"],
                    }
                    for r in rows
                ],
                "source": "graph",
            }
    except Exception as e:
        return {"entities": [], "error": str(e), "source": "graph"}

    rows = (
        await state.memory.knowledge_search(q.strip(), limit=lim)
        if q.strip()
        else await state.memory.knowledge_query(limit=lim)
    )
    out = []
    for r in rows:
        if "subject" in r:
            out.append(
                {
                    "name": r["subject"],
                    "relation": r.get("predicate"),
                    "object": r.get("object"),
                }
            )
        else:
            out.append(r)
    return {"entities": out, "source": "legacy_triples"}


# ── Internal Memory CRUD ──

@router.post("/internal/memory/save")
async def memory_save(body: dict):
    content = body.get("content", "")
    tags = body.get("tags", [])
    importance = body.get("importance", "normal")
    if not content:
        return {"error": "content is required"}
    return await state.memory.save(content=content, tags=tags, importance=importance)


@router.get("/internal/memory/search")
async def memory_search(query: str = "", limit: int = 10):
    if not query:
        return []
    return await state.memory.search(query=query, limit=limit)


@router.get("/internal/memory/recent")
async def memory_recent(limit: int = 10):
    return await state.memory.list_recent(limit=limit)


@router.delete("/internal/memory/{note_id}")
async def memory_delete(note_id: str):
    return {"deleted": await state.memory.delete(note_id)}


@router.get("/internal/memory/stats")
async def memory_stats():
    """Memory store stats + Phase-5 observability fields.

    Adds visibility into the running brain's vector configuration so
    operators can detect ``degraded_semantic_search`` (no sqlite-vec)
    and missing embedding providers without grepping logs.
    """
    base = (await state.memory.stats()) if state.memory else {}
    if not isinstance(base, dict):
        base = {"raw": base}

    vec_index = getattr(state.memory, "_vec_index", None) if state.memory else None
    sqlite_vec_loaded = bool(getattr(vec_index, "indexed", False))

    embed_provider = ""
    embed_queue = getattr(state.memory, "_embed_queue", None) if state.memory else None
    if embed_queue is not None:
        embedder = getattr(embed_queue, "_embedder", None)
        embed_provider = str(getattr(embedder, "provider_name", "") or "")

    # ``MemoryStore.stats()`` already computes ``embedded_chunks`` from
    # ``memory_chunks``; re-use it to avoid a second SQLite round-trip.
    chunk_count = int(base.get("embedded_chunks", 0) or 0)

    base["observability"] = {
        "sqlite_vec_loaded": sqlite_vec_loaded,
        "embedding_provider": embed_provider,
        "chunk_count": chunk_count,
        "active_vector_store": _runtime_backend_id(),
        "degraded_semantic_search": (not sqlite_vec_loaded) and chunk_count > 0,
    }
    return base


@router.post("/internal/knowledge/store")
async def knowledge_store(body: dict):
    subject = body.get("subject", "")
    predicate = body.get("predicate", "")
    obj = body.get("object", "")
    if not all([subject, predicate, obj]):
        return {"error": "subject, predicate, and object are required"}
    return await state.memory.knowledge_store(subject=subject, predicate=predicate, obj=obj)


@router.get("/internal/knowledge/query")
async def knowledge_query(subject: str = "", predicate: str = "", limit: int = 20):
    return await state.memory.knowledge_query(subject=subject, predicate=predicate, limit=limit)


@router.get("/internal/knowledge/about/{entity}")
async def knowledge_about(entity: str, limit: int = 20):
    return await state.memory.knowledge_about(entity, limit=limit)


@router.get("/api/knowledge/relationship")
async def knowledge_relationship(entity_a: str = "", entity_b: str = "", max_depth: int = 4):
    """Query the relationship between two entities (e.g. 'What does X know about Y?').

    Phase 0.1 of MEMORY_SYSTEM_FIX_PLAN: ``state.memory._knowledge_graph``
    does not exist (the attribute is exposed as ``kg`` on ``MemoryStore``).
    The previous version raised ``AttributeError`` on every call, which the
    catch-all route swallowed into a 200 ``{"error": ...}`` body. Now we
    use the same ``getattr(memory, "kg", None)`` pattern that
    ``_knowledge_graph_d3`` uses, and return a structured 503 when the
    graph is unavailable.
    """
    if not entity_a or not entity_b:
        raise HTTPException(status_code=400, detail="Both entity_a and entity_b are required")
    kg = getattr(state.memory, "kg", None)
    if kg is None:
        raise HTTPException(status_code=503, detail="Knowledge graph unavailable")
    try:
        from memory.enhanced_search import relationship_query
        return relationship_query(kg, entity_a, entity_b, max_depth)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/knowledge/visualize")
async def knowledge_visualize(entity: str = "", depth: int = 2, limit: int = 50):
    """Return graph visualization data (nodes + edges) centered on an entity.

    See ``knowledge_relationship`` above for the Phase 0.1 attribute fix.
    """
    if not entity:
        raise HTTPException(status_code=400, detail="entity parameter required")
    kg = getattr(state.memory, "kg", None)
    if kg is None:
        raise HTTPException(status_code=503, detail="Knowledge graph unavailable")
    try:
        from memory.enhanced_search import graph_visualization_data
        return graph_visualization_data(kg, entity, max_depth=depth, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/internal/episodes/recent")
async def episodes_recent(limit: int = 10, session_id: str = ""):
    return await state.memory.episode_recent(limit=limit, session_id=session_id or None)


@router.get("/internal/execution-log")
async def execution_log(skill_id: str = "", limit: int = 20):
    return await state.memory.log_recent(skill_id=skill_id, limit=limit)


# ── Wiki ──

@router.post("/api/wiki/compile")
async def wiki_compile(body: dict | None = None):
    """Compile notes/episodes/knowledge into durable wiki pages."""
    payload = body or {}
    return await state.memory.wiki_compile(
        notes_limit=int(payload.get("notes_limit", 200)),
        episodes_limit=int(payload.get("episodes_limit", 200)),
        knowledge_limit=int(payload.get("knowledge_limit", 400)),
    )


@router.get("/api/wiki/pages")
async def wiki_pages(q: str = "", kind: str = "", limit: int = 50):
    pages = await state.memory.wiki_list_pages(query=q, kind=kind, limit=limit)
    return {"pages": pages}


@router.get("/api/wiki/pages/{page_id}")
async def wiki_page(page_id: str):
    page = await state.memory.wiki_get_page(page_id)
    if not page:
        return {"error": f"Wiki page not found: {page_id}"}
    return page


@router.get("/api/wiki/stats")
async def wiki_stats():
    return await state.memory.wiki_stats()


@router.post("/api/wiki/ingest")
async def wiki_ingest(body: dict):
    """Ingest a raw note and optionally compile wiki pages."""
    content = (body or {}).get("content", "")
    if not content:
        return {"error": "content is required"}
    tags = body.get("tags", [])
    importance = body.get("importance", "normal")
    compile_after = bool(body.get("compile_after", True))
    note = await state.memory.save(content=content, tags=tags, importance=importance, source="wiki_ingest")
    compile_result = (await state.memory.wiki_compile()) if compile_after else {"compiled": False}
    return {"note": note, "compile": compile_result}


@router.post("/api/wiki/ingest/text")
async def wiki_ingest_text(body: dict):
    if not state.memory:
        return {"error": "Memory store not initialized"}
    ingestor = MemoryIngestor(state.memory)
    try:
        return await ingestor.ingest_text(
            content=(body or {}).get("content", ""),
            source_label=(body or {}).get("source_label", "ui"),
            compile_after=bool((body or {}).get("compile_after", True)),
        )
    except Exception as e:
        return {"error": str(e)}


@router.post("/api/wiki/ingest/pdf")
async def wiki_ingest_pdf(
    file: UploadFile | None = File(default=None),
    upload_id: str | None = Form(default=None),
    path: str | None = Form(default=None),
    compile_after: bool = Form(default=True),
    body: dict | None = None,
):
    """Ingest a PDF into the memory wiki.

    PR 10 fixes the multipart-vs-JSON mismatch that left the web wiki
    upload silently broken. Three input shapes are now accepted, in
    order of preference:

    1. ``multipart/form-data`` with a ``file`` part (the web composer's
       paperclip ships this).
    2. ``multipart/form-data`` with an ``upload_id`` referencing a
       previously stored upload from ``/api/uploads`` — keeps the
       composer's drag/drop + send-later flow honest.
    3. ``application/json`` ``{"path": "..."}`` (back-compat for local
       CLI / scripted ingestion).

    Returns the underlying :class:`memory.ingest.MemoryIngestor`
    result on success. Mismatched inputs surface 400/404 truthfully —
    never a silent 200."""
    if not state.memory:
        raise HTTPException(status_code=503, detail="Memory store not initialized")

    chosen_path: str | None = None

    if file is not None and file.filename:
        # multipart upload — stream bytes into the upload store so we
        # have a stable on-disk path and dedup by sha256.
        store = getattr(state, "uploads", None)
        if store is None:
            raise HTTPException(status_code=503, detail="Upload store not initialised")
        try:
            data = await file.read()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"failed to read upload: {exc}") from exc
        if not data:
            raise HTTPException(status_code=400, detail="empty file")
        record = store.store(
            data=data,
            filename=file.filename,
            content_type=file.content_type or "application/pdf",
        )
        chosen_path = record.path

    elif upload_id:
        store = getattr(state, "uploads", None)
        if store is None:
            raise HTTPException(status_code=503, detail="Upload store not initialised")
        record = store.get(upload_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown upload_id: {upload_id}")
        chosen_path = record.path

    elif path:
        chosen_path = path

    else:
        # Last resort: JSON body (legacy)
        body = body or {}
        legacy_path = (body or {}).get("path", "")
        if legacy_path:
            chosen_path = legacy_path

    if not chosen_path:
        raise HTTPException(
            status_code=400,
            detail=(
                "Provide a `file` multipart part, an `upload_id` form field, "
                "or a JSON `path` — none were supplied."
            ),
        )

    ingestor = MemoryIngestor(state.memory)
    try:
        return await ingestor.ingest_pdf(
            path=chosen_path,
            compile_after=bool(compile_after),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/wiki/ingest/repo")
async def wiki_ingest_repo(body: dict):
    if not state.memory:
        return {"error": "Memory store not initialized"}
    raw_extensions = (body or {}).get("extensions_filter", [])
    if isinstance(raw_extensions, str):
        ext_list = [e.strip() for e in raw_extensions.split(",") if e.strip()]
    elif isinstance(raw_extensions, list):
        ext_list = [str(e).strip() for e in raw_extensions if str(e).strip()]
    else:
        ext_list = []

    ingestor = MemoryIngestor(state.memory)
    try:
        return await ingestor.ingest_repo(
            path=(body or {}).get("path", ""),
            extensions_filter=ext_list or None,
            compile_after=bool((body or {}).get("compile_after", True)),
            max_files=int((body or {}).get("max_files", 300)),
        )
    except Exception as e:
        return {"error": str(e)}
