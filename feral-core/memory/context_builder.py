"""Context builder helpers — async-native since v2026.5.33 (Option C).

The legacy sync ``build_context_for_llm`` was removed in v2026.5.33;
the only entry point is :func:`build_context_for_llm_async`. The
``MemoryStore.build_context_for_llm`` and
``MemoryStore.build_context_for_llm_async`` wrappers both route here.
"""

from __future__ import annotations

import json
import logging

from memory.fts_query import BROAD as FTS_BROAD

logger = logging.getLogger("feral.memory")

# Rough chars→tokens ratio, used to spend ``max_tokens_budget`` as the
# token budget its name promises.
_CHARS_PER_TOKEN = 4

async def build_context_for_llm_async(
    store,
    session_id: str,
    query: str = "",
    max_tokens_budget: int = 2000,
    memory_filter: str = "",
) -> str:
    """Async context builder using hybrid search + knowledge graph.

    ``memory_filter``: when a SpecialistAgent is routing the turn, its
    ``memory_filter`` topic is passed in and we drop episodes / recent
    actions that don't mention it. Keeps the journaling specialist from
    leaking into the coding specialist's context, etc. Empty string =
    pre-memory-filter behaviour (no filtering).
    """
    sections = []
    # ``max_tokens_budget`` is a TOKEN budget and is now spent as one.
    # It used to be sliced straight onto strings as a character count,
    # so every caller silently got a quarter of the context it asked
    # for.
    budget_chars_per_section = (max_tokens_budget // 4) * _CHARS_PER_TOKEN

    working = store.working_context_string(session_id, limit=8)
    if working:
        sections.append(f"## Recent Context\n{_tail_within(working, budget_chars_per_section)}")

    # The store methods sanitise their own FTS input, so pass the raw
    # utterance and just declare the recall we want. Pre-building an
    # expression here and handing it to a layer that sanitises again
    # quoted the OR into a literal term.
    if query:
        graph_ctx = ""
        if store._kg:
            try:
                graph_ctx = await store._kg.build_graph_context(query, max_chars=budget_chars_per_section)
            except Exception as exc:
                logger.debug("build_graph_context failed: %s", exc)
                graph_ctx = ""
        if graph_ctx:
            sections.append(graph_ctx)
        else:
            knowledge = await store.knowledge_search(
                query, limit=5, fts_mode=FTS_BROAD
            )
            if knowledge:
                k_lines = [f"- {k['subject']} {k['predicate']} {k['object']}" for k in knowledge]
                sections.append("## Known Facts\n" + "\n".join(k_lines)[:budget_chars_per_section])

    if query:
        try:
            episodes = await store.episode_search_hybrid(
                query, limit=3, fts_mode=FTS_BROAD
            )
        except Exception as exc:
            logger.debug("episode_search_hybrid failed, falling back to FTS: %s", exc)
            episodes = await store.episode_search(query, limit=3, fts_mode=FTS_BROAD)
        if not episodes:
            episodes = await store.episode_search(query, limit=3, fts_mode=FTS_BROAD)
    else:
        episodes = await store.episode_recent(limit=3, session_id=session_id)
    if memory_filter:
        episodes = [e for e in episodes if _topic_match(e, memory_filter)]
    if episodes:
        ep_lines = [f"- [{e['event_type']}] {e['summary']}" for e in episodes]
        sections.append("## Past Events\n" + "\n".join(ep_lines)[:budget_chars_per_section])

    recent_execs = await store.log_recent(limit=5)
    if memory_filter:
        recent_execs = [ex for ex in recent_execs if _topic_match(ex, memory_filter)]
    if recent_execs:
        ex_lines = [f"- {ex.get('skill_id', '?')}: {ex.get('result_status', '?')}" for ex in recent_execs]
        sections.append("## Recent Actions\n" + "\n".join(ex_lines)[:budget_chars_per_section])

    return "\n\n".join(sections) if sections else ""


def _tail_within(text: str, max_chars: int) -> str:
    """Keep the NEWEST whole lines of ``text`` that fit in ``max_chars``.

    ``working_context_string`` joins its entries oldest→newest, so the
    previous ``text[:max_chars]`` kept the OLDEST fragment and shipped
    it under the heading "Recent Context". That is how a question the
    user had moved on from three turns earlier resurfaced as the
    model's idea of what was being discussed.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    kept: list[str] = []
    used = 0
    for line in reversed(text.split("\n")):
        cost = len(line) + (1 if kept else 0)
        if used + cost > max_chars:
            break
        kept.append(line)
        used += cost
    if not kept:
        # A single entry longer than the whole budget — keep its tail.
        return text[-max_chars:]
    kept.reverse()
    return "\n".join(kept)


def _topic_match(item: dict, topic: str) -> bool:
    """Case-insensitive substring match across common fields."""
    if not topic:
        return True
    needle = topic.lower().strip()
    if not needle:
        return True
    fields = (
        item.get("event_type"),
        item.get("summary"),
        item.get("skill_id"),
        item.get("tags"),
        item.get("topic"),
        item.get("category"),
    )
    for f in fields:
        if f is None:
            continue
        if isinstance(f, (list, tuple, set)):
            if any(needle in str(x).lower() for x in f):
                return True
        elif needle in str(f).lower():
            return True
    return False


async def compact_session(
    store,
    session_id: str,
    history: list[dict],
    llm=None,
    preserve_last_n: int = 3,
    max_summary_chars: int = 16000,
    *,
    promote_to_episode: bool = True,
) -> dict:
    """Multi-stage session compaction.

    v2026.5.34 (PR 2 F2): when ``promote_to_episode`` is true (the
    default), the compacted summary lands as a *real* episode row
    with structured metadata — participants, time_range,
    summary_chars, key_entities, source_turn_ids — alongside the
    in-memory transcript edit. Pre-F2 compaction returned an edited
    history but never persisted anything to the episodes table, so
    the "compacted" memory was lost the moment the session ended.

    The metadata derivation is heuristic on the message list:

    * participants  — unique non-empty ``role`` values from the
      summarizable turns.
    * time_range    — (first_ts, last_ts) when ``meta.created_at`` is
      present on the messages; else (0, 0) and the caller can
      backfill.
    * key_entities  — top entity names yielded by
      :meth:`KnowledgeGraph.extract_and_store` (when the KG is
      attached and the LLM is available); empty list otherwise.
    * source_turn_ids — message ids if the history carries them,
      else integer indices.

    Returns the same shape as before plus a new ``episode_id`` field
    when an episode was written.
    """
    if len(history) <= preserve_last_n + 2:
        return {"compacted": False, "reason": "too_short"}

    preserved = history[-preserve_last_n:]
    summarizable = history[:-preserve_last_n]

    if not llm or not llm.available:
        summary = heuristic_summarize(summarizable)
    else:
        summary = await llm_summarize(messages=summarizable, llm=llm, max_chars=max_summary_chars)

    compacted_history = [
        {"role": "system", "content": f"[Session Summary]\n{summary}"},
        *preserved,
    ]

    key_entities: list[str] = []
    if store.kg:
        try:
            conversation_text = " ".join(
                m.get("content", "") for m in summarizable if isinstance(m.get("content"), str)
            )
            if conversation_text:
                extracted = await store.kg.extract_and_store(conversation_text[:3000], llm)
                # extract_and_store returns a list of {entity, ...} dicts;
                # surface the names so callers can inspect what landed.
                for item in extracted or []:
                    if isinstance(item, dict):
                        name = item.get("name") or item.get("entity") or item.get("subject")
                        if name and name not in key_entities:
                            key_entities.append(str(name))
        except Exception as e:
            logger.debug("KG extraction during compaction failed: %s", e)

    episode_id: str | None = None
    if promote_to_episode and summarizable:
        participants = sorted({
            str(m.get("role")) for m in summarizable
            if m.get("role")
        })
        timestamps = [
            float(m["meta"]["created_at"]) for m in summarizable
            if isinstance(m.get("meta"), dict) and isinstance(m["meta"].get("created_at"), (int, float))
        ]
        time_range = (min(timestamps), max(timestamps)) if timestamps else (0.0, 0.0)
        source_turn_ids = [
            str(m.get("id") or i) for i, m in enumerate(summarizable)
        ]

        try:
            episode = await store.episode_save(
                session_id=session_id,
                event_type="session_compaction",
                summary=summary[:500],
                detail=summary,
                emotions=[],
                location="",
                participants=participants,
                importance=0.6,
            )
            episode_id = episode.get("id")

            # Backfill the structured metadata into the episode row.
            # episode_save doesn't accept these fields natively (they
            # live on the row as JSON-on-detail or columns we don't
            # have), so we stash them on the detail body alongside the
            # summary in a delimited block.
            extra = {
                "time_range": list(time_range),
                "key_entities": key_entities,
                "source_turn_ids": source_turn_ids,
            }
            new_detail = (
                f"{summary}\n\n"
                f"<!-- compaction-metadata\n{json.dumps(extra, ensure_ascii=False)}\n-->"
            )
            conn = await store._conn()
            try:
                await conn.execute(
                    "UPDATE episodes SET detail = ? WHERE id = ?",
                    (new_detail, episode_id),
                )
                await conn.commit()
            finally:
                await store._release(conn)
        except Exception as exc:
            logger.warning("compact_session: episode promotion failed: %s", exc)
            episode_id = None

    return {
        "compacted": True,
        "original_length": len(history),
        "new_length": len(compacted_history),
        "summary_chars": len(summary),
        "history": compacted_history,
        "episode_id": episode_id,
        "key_entities": key_entities,
    }


async def llm_summarize(messages: list[dict], llm, max_chars: int) -> str:
    """Multi-stage LLM summarization of conversation history."""
    text_parts = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, str):
            text_parts.append(f"[{role}] {content[:500]}")
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    text_parts.append(f"[{role}] {c['text'][:500]}")

    full_text = "\n".join(text_parts)

    chunk_size = 6000
    if len(full_text) <= chunk_size:
        chunks = [full_text]
    else:
        chunks = [full_text[i : i + chunk_size] for i in range(0, len(full_text), chunk_size)]

    summaries = []
    for chunk in chunks:
        prompt = (
            "Summarize this conversation segment concisely. Preserve:\n"
            "- Key facts and decisions\n"
            "- User preferences and personal info\n"
            "- Tool call results and outcomes\n"
            "- Any unresolved questions or tasks\n\n"
            f"{chunk}"
        )
        try:
            response = await llm.chat([{"role": "user", "content": prompt}], tools=None)
            text, _ = llm.extract_response(response)
            summaries.append(text)
        except Exception as e:
            logger.warning("Summarization chunk failed: %s", e)
            summaries.append(chunk[:500])

    result = "\n\n".join(summaries)
    return result[:max_chars]


def heuristic_summarize(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, str) and content:
            lines.append(f"[{role}] {content[:100]}")
    return "\n".join(lines[-20:])


async def search_all(store, query: str, limit: int = 10) -> list[dict]:
    """Search across all memory tiers using hybrid ranking."""
    results = []

    episodes = await store.episode_search_hybrid(query, limit=limit)
    for item in episodes:
        results.append({**item, "tier": "episode", "score": item.get("relevance_score", 0)})

    notes = await store.search(query, limit=limit)
    for note in notes:
        results.append({**note, "tier": "note", "score": note.get("relevance_score", 0.3)})

    knowledge = await store.knowledge_search(query, limit=limit)
    for item in knowledge:
        results.append(
            {
                "tier": "knowledge",
                "score": 0.5,
                "summary": f"{item['subject']} {item['predicate']} {item['object']}",
                **item,
            }
        )

    if store._kg:
        entities = await store._kg.search_entities(query, limit=5)
        for entity in entities:
            results.append(
                {
                    "tier": "entity",
                    "score": entity.get("score", 0.5),
                    "summary": f"Entity: {entity['name']} ({entity.get('type', 'thing')})",
                    **entity,
                }
            )

    results.sort(key=lambda item: item.get("score", 0), reverse=True)
    return results[:limit]
