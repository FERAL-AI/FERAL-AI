"""Memory Wiki helpers. Async-native since v2026.5.33.

Connection discipline (incident, v2026.7.x): every function here takes
its connection from ``MemoryStore``'s pool via ``store._conn()`` and
MUST hand it back with ``await store._release(conn)``. Four of them
called ``await conn.close()`` instead. The pool is filled exactly once
and never refills (``store.py`` ``_conn``), so each such call
permanently destroyed one of the four connections; the fifth call to
any wiki read blocked forever inside ``self._pool.get()``, with no
timeout and no error, taking the entire memory subsystem with it. Closing a
pooled connection is never correct.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

from config.loader import feral_home
from memory.fts_query import fts5_match_query


def wiki_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or "unknown"


async def wiki_upsert_page(
    store,
    *,
    page_id: str,
    title: str,
    kind: str,
    body_markdown: str,
    source_refs: list[dict] | None = None,
) -> dict:
    now = time.time()
    refs = source_refs or []
    # Pooled, like every other function in this module. This used to
    # open its own ``aiosqlite.connect`` per call, which cost a fresh
    # connection + worker thread + WAL pragma for each of the ~800
    # pages ``wiki_compile`` writes in one pass, and left the module
    # with two different connection lifecycles, the ambiguity that
    # produced the pool-exhaustion incident below.
    conn = await store._conn()
    try:
        await conn.execute(
            """
            INSERT INTO wiki_pages (id, title, kind, body_markdown, source_refs, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              title=excluded.title,
              kind=excluded.kind,
              body_markdown=excluded.body_markdown,
              source_refs=excluded.source_refs,
              updated_at=excluded.updated_at
            """,
            (page_id, title, kind, body_markdown, json.dumps(refs), now, now),
        )
        await conn.commit()
    finally:
        await store._release(conn)
    return {
        "id": page_id,
        "title": title,
        "kind": kind,
        "updated_at": now,
        "source_refs": refs,
    }


async def wiki_get_page(store, page_id: str) -> Optional[dict]:
    conn = await store._conn()
    try:
        async with conn.execute(
            "SELECT id, title, kind, body_markdown, source_refs, created_at, updated_at FROM wiki_pages WHERE id = ?",
            (page_id,),
        ) as cur:
            row = await cur.fetchone()
    finally:
        await store._release(conn)
    if not row:
        return None
    return {
        "id": row["id"],
        "title": row["title"],
        "kind": row["kind"],
        "body_markdown": row["body_markdown"],
        "source_refs": json.loads(row["source_refs"] or "[]"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def wiki_list_pages(store, *, query: str = "", kind: str = "", limit: int = 50) -> list[dict]:
    lim = max(1, min(limit, 200))
    # Same FTS5 quoting as the episode/note search paths. This one had
    # no guard at all, so "GET /api/wiki/pages?q=don't" propagated a raw
    # sqlite3.OperationalError out of the route as a 500. An empty
    # expression means the query held no indexable term, in which case
    # we fall through to the unfiltered listing below.
    match_expr = fts5_match_query(query)
    conn = await store._conn()
    try:
        rows = []
        if match_expr:
            if kind:
                async with conn.execute(
                    """
                    SELECT w.id, w.title, w.kind, w.source_refs, w.updated_at
                    FROM wiki_pages_fts f
                    JOIN wiki_pages w ON f.rowid = w.rowid
                    WHERE wiki_pages_fts MATCH ? AND w.kind = ?
                    ORDER BY rank, w.updated_at DESC
                    LIMIT ?
                    """,
                    (match_expr, kind, lim),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with conn.execute(
                    """
                    SELECT w.id, w.title, w.kind, w.source_refs, w.updated_at
                    FROM wiki_pages_fts f
                    JOIN wiki_pages w ON f.rowid = w.rowid
                    WHERE wiki_pages_fts MATCH ?
                    ORDER BY rank, w.updated_at DESC
                    LIMIT ?
                    """,
                    (match_expr, lim),
                ) as cur:
                    rows = await cur.fetchall()
        elif kind:
            async with conn.execute(
                """
                SELECT id, title, kind, source_refs, updated_at
                FROM wiki_pages
                WHERE kind = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (kind, lim),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with conn.execute(
                """
                SELECT id, title, kind, source_refs, updated_at
                FROM wiki_pages
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (lim,),
            ) as cur:
                rows = await cur.fetchall()
    finally:
        await store._release(conn)
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "kind": row["kind"],
            "source_refs": json.loads(row["source_refs"] or "[]"),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


async def wiki_stats(store) -> dict:
    conn = await store._conn()
    try:
        async with conn.execute("SELECT COUNT(*) FROM wiki_pages") as cur:
            total = (await cur.fetchone())[0]
        async with conn.execute(
            "SELECT kind, COUNT(*) AS count FROM wiki_pages GROUP BY kind ORDER BY count DESC"
        ) as cur:
            kinds = await cur.fetchall()
    finally:
        await store._release(conn)
    return {
        "pages": total,
        "kinds": [{"kind": row["kind"], "count": row["count"]} for row in kinds],
    }


async def wiki_compile(
    store,
    *,
    notes_limit: int = 200,
    episodes_limit: int = 200,
    knowledge_limit: int = 400,
) -> dict:
    conn = await store._conn()
    try:
        async with conn.execute(
            """
            SELECT id, content, tags, importance, source, created_at, updated_at
            FROM notes
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (max(1, notes_limit),),
        ) as cur:
            notes = await cur.fetchall()
        async with conn.execute(
            """
            SELECT id, session_id, event_type, summary, detail, created_at
            FROM episodes
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, episodes_limit),),
        ) as cur:
            episodes = await cur.fetchall()
        # v2026.5.35 (F1) — the wiki compiler used to read flat
        # ``knowledge`` rows. When ``settings.memory.kg.unified`` is
        # on (default) the canonical knowledge surface is the KG's
        # ``entities`` × ``relations`` JOIN, and the flat table is
        # either empty post-migration or renamed to
        # ``knowledge__deprecated``. Route through the same view
        # ``MemoryStore._knowledge_query_unified`` uses so wiki
        # compilation sees exactly what the rest of the brain sees.
        unified = False
        try:
            from config.loader import load_settings
            unified = bool(
                ((load_settings().get("memory") or {}).get("kg") or {})
                .get("unified", True)
            )
        except Exception:
            pass
        if unified:
            async with conn.execute(
                """
                SELECT r.id, e_src.name AS subject, r.relation_type AS predicate,
                       e_tgt.name AS object, r.confidence,
                       r.evidence_text AS source, r.updated_at
                FROM relations r
                JOIN entities e_src ON r.source_id = e_src.id
                JOIN entities e_tgt ON r.target_id = e_tgt.id
                ORDER BY r.updated_at DESC
                LIMIT ?
                """,
                (max(1, knowledge_limit),),
            ) as cur:
                triples = await cur.fetchall()
        else:
            async with conn.execute(
                """
                SELECT id, subject, predicate, object, confidence, source, updated_at
                FROM knowledge
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, knowledge_limit),),
            ) as cur:
                triples = await cur.fetchall()
    finally:
        # Released before the upsert loop below: ``wiki_upsert_page``
        # takes its own pooled connection, and holding this one across
        # hundreds of upserts would starve the pool.
        await store._release(conn)

    note_pages = 0
    for row in notes:
        tags = json.loads(row["tags"] or "[]")
        title = f"Note {row['id']}"
        body = (
            f"# {title}\n\n"
            f"{row['content']}\n\n"
            f"## Metadata\n"
            f"- Importance: {row['importance']}\n"
            f"- Source: {row['source']}\n"
            f"- Tags: {', '.join(tags) if tags else 'none'}\n"
            f"- Updated: {row['updated_at']}\n"
        )
        await wiki_upsert_page(
            store,
            page_id=f"note.{row['id']}",
            title=title,
            kind="note",
            body_markdown=body,
            source_refs=[
                {"type": "note", "id": row["id"], "updated_at": row["updated_at"]},
            ],
        )
        note_pages += 1

    episode_pages = 0
    for row in episodes:
        title = f"Episode {row['id']} ({row['event_type']})"
        body = (
            f"# {title}\n\n"
            f"## Summary\n{row['summary']}\n\n"
            f"## Detail\n{row['detail'] or '-'}\n\n"
            f"## Metadata\n"
            f"- Session: {row['session_id']}\n"
            f"- Created: {row['created_at']}\n"
        )
        await wiki_upsert_page(
            store,
            page_id=f"episode.{row['id']}",
            title=title,
            kind="episode",
            body_markdown=body,
            source_refs=[
                {
                    "type": "episode",
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "created_at": row["created_at"],
                },
            ],
        )
        episode_pages += 1

    triples_by_subject: dict[str, list[dict]] = {}
    for row in triples:
        triples_by_subject.setdefault(row["subject"], []).append(
            {
                "id": row["id"],
                "predicate": row["predicate"],
                "object": row["object"],
                "confidence": row["confidence"],
                "source": row["source"],
                "updated_at": row["updated_at"],
            }
        )

    entity_pages = 0
    for subject, items in triples_by_subject.items():
        page_id = f"entity.{wiki_slug(subject)}"
        title = f"Entity: {subject}"
        lines = [f"# {title}", "", "## Facts"]
        for item in items[:200]:
            lines.append(
                f"- {subject} **{item['predicate']}** {item['object']} "
                f"(confidence={item['confidence']:.2f}, source={item['source']})"
            )
        body = "\n".join(lines)
        refs = [
            {
                "type": "knowledge",
                "id": item["id"],
                "subject": subject,
                "updated_at": item["updated_at"],
            }
            for item in items
        ]
        await wiki_upsert_page(
            store,
            page_id=page_id,
            title=title,
            kind="entity",
            body_markdown=body,
            source_refs=refs,
        )
        entity_pages += 1

    identity_page_written = False
    memory_md = feral_home() / "MEMORY.md"
    if memory_md.exists():
        memory_text = Path(memory_md).read_text(encoding="utf-8", errors="replace")
        await wiki_upsert_page(
            store,
            page_id="identity.memory",
            title="Identity Memory",
            kind="identity",
            body_markdown=memory_text or "# Identity Memory\n\n(empty)",
            source_refs=[
                {"type": "identity_file", "path": str(memory_md)},
            ],
        )
        identity_page_written = True

    index_lines = [
        "# FERAL Memory Wiki",
        "",
        "## Summary",
        f"- Notes compiled: {note_pages}",
        f"- Episodes compiled: {episode_pages}",
        f"- Entity pages compiled: {entity_pages}",
        f"- Identity page: {'yes' if identity_page_written else 'no'}",
        "",
        "## How to use",
        "- Search pages with `q` in `/api/wiki/pages`.",
        "- Fetch full page content from `/api/wiki/pages/{page_id}`.",
        "- Recompile after new memory writes using `/api/wiki/compile`.",
    ]
    await wiki_upsert_page(
        store,
        page_id="index",
        title="Wiki Index",
        kind="index",
        body_markdown="\n".join(index_lines),
        source_refs=[],
    )

    stats = await wiki_stats(store)
    return {
        "compiled": True,
        "notes_pages": note_pages,
        "episode_pages": episode_pages,
        "entity_pages": entity_pages,
        "identity_page": identity_page_written,
        "total_pages": stats["pages"],
        "kinds": stats["kinds"],
    }
