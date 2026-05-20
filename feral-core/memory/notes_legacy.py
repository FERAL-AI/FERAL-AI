"""Legacy notes API. Async-native since v2026.5.33 (Option C refactor).

These functions are dispatched to from :class:`memory.store.MemoryStore`'s
back-compat methods (``save``, ``search``, ``list_recent``, ``delete``,
``count``). They take the store as their first argument so they can
share its db_path, embed queue, and sync engine without inheriting from
it.
"""

from __future__ import annotations

import json
import time
from uuid import uuid4

import aiosqlite

from memory.embeddings import chunk_text


async def save_note(
    store,
    content: str,
    tags: list[str] | None = None,
    importance: str = "normal",
    source: str = "user",
) -> dict:
    note_id = str(uuid4())[:8]
    now = time.time()
    tags = tags or []
    # Sync log first so the HLC string can land in the same INSERT —
    # required by D12 LWW on the receiving side.
    hlc = store._log_sync(
        "notes",
        "insert",
        note_id,
        {
            "id": note_id,
            "content": content,
            "tags": json.dumps(tags),
            "importance": importance,
            "source": source,
            "created_at": now,
        },
    )
    conn = await aiosqlite.connect(store.db_path)
    try:
        await conn.execute(
            "INSERT INTO notes (id, content, tags, importance, source, created_at, updated_at, hlc_string) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (note_id, content, json.dumps(tags), importance, source, now, now, hlc),
        )
        await conn.commit()
    finally:
        await conn.close()

    chunks = chunk_text(content)
    for i, chunk in enumerate(chunks):
        store._embed_queue.enqueue(
            chunk_id=f"note_{note_id}_c{i}",
            text=chunk,
            source_table="notes",
            source_id=note_id,
            chunk_index=i,
            db_path=store.db_path,
        )

    await store.knowledge_store(
        subject=f"note_{note_id}",
        predicate="says",
        obj=content[:300],
        source=f"notes:{note_id}",
    )
    return {
        "id": note_id,
        "content": content,
        "tags": tags,
        "importance": importance,
        "created_at": now,
        "status": "saved",
    }


async def search_notes(store, query: str, limit: int = 10) -> list[dict]:
    conn = await store._conn()
    try:
        try:
            async with conn.execute(
                """SELECT n.id, n.content, n.tags, n.importance, n.created_at, rank as relevance_score
                   FROM notes_fts f JOIN notes n ON f.rowid = n.rowid
                   WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?""",
                (query, limit),
            ) as cur:
                rows = await cur.fetchall()
        except Exception:
            async with conn.execute(
                "SELECT id, content, tags, importance, created_at, 0.5 as relevance_score FROM notes WHERE content LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"%{query}%", limit),
            ) as cur:
                rows = await cur.fetchall()
    finally:
        await conn.close()
    return [
        {
            "id": row["id"],
            "content": row["content"],
            "tags": json.loads(row["tags"]),
            "importance": row["importance"],
            "created_at": row["created_at"],
            "relevance_score": abs(row["relevance_score"]),
        }
        for row in rows
    ]


async def list_recent_notes(store, limit: int = 10) -> list[dict]:
    conn = await store._conn()
    try:
        async with conn.execute(
            "SELECT id, content, tags, importance, created_at FROM notes ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    finally:
        await conn.close()
    return [
        {
            "id": row["id"],
            "content": row["content"],
            "tags": json.loads(row["tags"]),
            "importance": row["importance"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


async def delete_note(store, note_id: str) -> bool:
    conn = await aiosqlite.connect(store.db_path)
    try:
        cursor = await conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        await conn.commit()
        deleted = cursor.rowcount > 0
    finally:
        await conn.close()
    return deleted


async def count_notes(store) -> int:
    conn = await aiosqlite.connect(store.db_path)
    try:
        async with conn.execute("SELECT COUNT(*) FROM notes") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0
    finally:
        await conn.close()
