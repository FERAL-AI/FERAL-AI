"""Context builder helpers — async-native since v2026.5.33 (Option C).

The legacy sync ``build_context_for_llm`` was removed in v2026.5.33;
the only entry point is :func:`build_context_for_llm_async`. The
``MemoryStore.build_context_for_llm`` and
``MemoryStore.build_context_for_llm_async`` wrappers both route here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from memory.fts_query import BROAD as FTS_BROAD

logger = logging.getLogger("feral.memory")

# Rough chars→tokens ratio, used to spend ``max_tokens_budget`` as the
# token budget its name promises.
_CHARS_PER_TOKEN = 4

# ── Consolidation tunables ────────────────────────────────────────────
#
# F1. The map stage used to be ``for chunk in chunks: await
# llm.chat(...)``. The chunks are independent by construction, so one
# compaction cost 2-10 strictly sequential generations (2 for 20 short
# turns, 10 for 60). LightMem measured a 1.67-12.45x latency reduction
# from gathering exactly this loop.
#
# The bound is 3, and it is deliberately small. The overwhelmingly
# common deployment here is a local model behind Ollama or llama.cpp,
# which serves a small fixed number of parallel slots (Ollama's
# ``OLLAMA_NUM_PARALLEL`` defaults to 1 or 4 depending on available
# VRAM; llama.cpp's ``--parallel`` defaults to 1). Firing ten
# generations at a one-slot server does not make them concurrent, it
# makes them queue while each one's share of the KV cache shrinks. 3
# collects most of the wall-clock win at every slot count >= 2 and
# degrades to the old sequential behaviour, not worse than it, at slot
# count 1. Operators on a hosted endpoint can raise it via
# ``memory.compaction.map_concurrency``.
MAP_CONCURRENCY = 3

# F4. Chunk target, in characters, applied on MESSAGE boundaries.
#
# 12000, not the 6000 this pipeline inherited. Measured on a mixed
# 20-turn window (45 messages, 46KB: short user turns, medium assistant
# replies, a 4KB tool result every fourth turn):
#
#   segment size   generations   waves at concurrency 3
#     6000            10             4
#     8000             8             3
#    12000             5             2
#    16000             4             2
#
# and on a 60-turn window (135 messages, 139KB): 30/10, 23/8, 15/5,
# 10/4. The old pipeline issued 3 and 9 generations respectively but
# only because it threw away two thirds of the transcript first, and it
# ran them one at a time, so 3 and 9 WAVES. At 6000 the redesign is a
# net wall-clock LOSS (4 waves vs 3, 10 vs 9); at 12000 it is a win on
# both (2 vs 3, 5 vs 9) while still showing the model every message.
#
# 12000 chars is ~3000 tokens, and the largest single segment observed
# was 9513 chars (~2378 tokens), which fits a 4K-context local model
# with room for the reply. 16000 was rejected on exactly that: its
# largest segment was ~3878 tokens.
CHUNK_CHARS = 12000

# F3. There is no longer a per-message truncation in the normal case.
# This is a safety valve for a pathological single message (a 4MB tool
# result), and even then it keeps the head AND the tail, because
# leading-N truncation is the single worst choice available:
# arXiv:2210.16732 measures ~80% of the information needed to
# reconstruct a summary lost at a 1K-token leading cut, and finds
# salience ANTI-correlated with position near the cut.
PER_MESSAGE_HARD_CAP = 20000

_ELIDED = "\n[... middle elided ...]\n"

# F5. Key under which a compaction stamps its watermark onto the
# ``[Session Summary]`` system message it injects. Its presence is how
# the NEXT compaction tells a derived summary from a raw turn, so a
# summary is never fed back to the summariser.
WATERMARK_KEY = "feral_consolidation"

# How many prior summary blocks ride along in the transcript before the
# oldest are collapsed by RE-DERIVING from their raw source turns.
MAX_CARRIED_SUMMARIES = 3

# Episode ``event_type`` values that are one conversational turn. PR
# #224 made these durable per turn, which is what gives compaction real
# row ids to point at (F7).
TURN_EVENT_TYPES = ("user_command", "assistant_reply")

_MAP_HEADER = (
    "Summarize this conversation segment concisely. Preserve:\n"
    "- Key facts and decisions\n"
    "- User preferences and personal info\n"
    "- Tool call results and outcomes\n"
    "- Any unresolved questions or tasks\n\n"
)

_REDUCE_HEADER = (
    "Below are ordered summaries of consecutive parts of one "
    "conversation. Merge them into a single consolidated summary that "
    "keeps every fact, decision, preference, tool outcome and open "
    "question from ALL of them. Do not drop the later parts.\n\n"
)

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


def _head_tail(text: str, cap: int) -> str:
    """Keep the head AND the tail of ``text`` within ``cap`` chars.

    The replacement for every ``text[:n]`` in this pipeline. Leading-N
    truncation is measurably the worst option (arXiv:2210.16732): what
    a summary needs is concentrated away from the head, so cutting at
    the head throws away the salient part and keeps the greeting.
    """
    if cap <= 0 or len(text) <= cap:
        return text
    if cap <= len(_ELIDED) + 2:
        return text[: cap // 2] + text[-(cap - cap // 2):]
    room = cap - len(_ELIDED)
    head = (room * 2) // 3
    tail = room - head
    return text[:head] + _ELIDED + text[-tail:]


def render_message(m: dict) -> str:
    """One message rendered as one ``[role] body`` unit, or "".

    F3: no ``content[:500]``. The only cap left is
    :data:`PER_MESSAGE_HARD_CAP`, which is a safety valve rather than a
    routine truncation, and it keeps both ends.

    Multi-part (Anthropic-style block list) content collapses into ONE
    unit rather than one per block, so the message stays a single
    indivisible thing for the chunker.
    """
    role = m.get("role", "?")
    content = m.get("content", "")
    parts: list[str] = []
    if isinstance(content, str):
        if content:
            parts.append(content)
    elif isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                parts.append(str(c["text"]))
    if not parts:
        return ""
    return f"[{role}] {_head_tail(chr(10).join(parts), PER_MESSAGE_HARD_CAP)}"


def chunk_messages(messages: list[dict], chunk_chars: int = CHUNK_CHARS) -> list[str]:
    """Split a transcript into segments that land on MESSAGE boundaries.

    F4. The previous chunker concatenated the whole transcript and cut
    it at raw character offsets, so a segment routinely began in the
    middle of a word, a message, or a JSON tool result, and the model
    was asked to summarize a fragment whose speaker and subject had
    been cut off the top.

    A message longer than ``chunk_chars`` becomes its own segment
    rather than being split: one oversized generation is a better
    trade than a fragment nobody can summarize.
    """
    rendered = [r for r in (render_message(m) for m in messages) if r]
    if not rendered:
        return []
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for unit in rendered:
        cost = len(unit) + (1 if current else 0)
        if current and used + cost > chunk_chars:
            chunks.append("\n".join(current))
            current, used = [], 0
            cost = len(unit)
        current.append(unit)
        used += cost
    if current:
        chunks.append("\n".join(current))
    return chunks


def _fit_summaries(summaries: list[str], max_chars: int) -> str:
    """Fit N segment summaries into ``max_chars`` WITHOUT dropping any.

    F5. ``"\\n\\n".join(summaries)[:max_chars]`` silently deleted the
    tail summaries, which on a long session are the most recent part of
    the conversation. Every segment gets an equal share of the budget
    and keeps both ends of its own text, so a segment can be abridged
    but never disappear.
    """
    summaries = [s for s in summaries if s]
    if not summaries:
        return ""
    sep = "\n\n"
    n = len(summaries)
    budget = max_chars - len(sep) * (n - 1)
    if budget <= n:
        # Pathological: not even one char per segment. Nothing sane is
        # possible, so fall back to the old slice rather than return "".
        return sep.join(summaries)[:max_chars]
    share = budget // n
    return sep.join(_head_tail(s, share) for s in summaries)[:max_chars]


async def _summarize_one(chunk: str, llm) -> str:
    try:
        response = await llm.chat(
            [{"role": "user", "content": _MAP_HEADER + chunk}], tools=None
        )
        text, _ = llm.extract_response(response)
        return text or ""
    except Exception as e:
        logger.warning("Summarization chunk failed: %s", e)
        # Head AND tail, so the fallback for a failed generation is not
        # itself a leading-N truncation.
        return _head_tail(chunk, 500)


async def compact_session(
    store,
    session_id: str,
    history: list[dict],
    llm=None,
    preserve_last_n: int = 3,
    max_summary_chars: int = 16000,
    *,
    promote_to_episode: bool = True,
    map_concurrency: int | None = None,
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
    * time_range: (first_ts, last_ts) from ``meta.created_at``
      when the messages carry it, else from the ``created_at`` of the
      durable turn episodes the messages resolved to, else (0, 0).
      Ordinary chat history carries no ``meta.created_at``, so before
      the episode fallback existed this was ALWAYS (0.0, 0.0).
    * key_entities  — top entity names yielded by
      :meth:`KnowledgeGraph.extract_and_store` (when the KG is
      attached and the LLM is available); empty list otherwise.
    * source_turn_ids: message ids if the history carries them, else
      the ids of the durable turn episode rows the messages resolved
      to. Never positional indices: those pointed into a list this
      function discards two lines later.

    Returns the same shape as before plus a new ``episode_id`` field
    when an episode was written.

    v2026.8.x (consolidation redesign):

    * The window is split into RAW turns and previously-consolidated
      summary blocks (identified by :data:`WATERMARK_KEY`). Only raw
      turns are ever handed to the summariser, so a summary is never
      re-summarised. See ``_carry_or_rederive``.
    * KG extraction runs per SEGMENT over the whole window instead of
      over ``conversation_text[:3000]``.
    * ``time_range`` and the source ids are resolved against the
      durable per-turn episode rows, so they mean something.
    """
    if len(history) <= preserve_last_n + 2:
        return {"compacted": False, "reason": "too_short"}

    preserved = history[-preserve_last_n:] if preserve_last_n else []
    window = history[:-preserve_last_n] if preserve_last_n else list(history)

    # F5: the compaction cliff. ``compact_session`` injects a
    # ``[Session Summary]`` system message, and the previous
    # implementation handed that message straight back to the
    # summariser on the next round. Measured degradation on exactly
    # this pattern (arXiv:2608.22752): 53% retention after ONE round,
    # 10% after five. A watermarked message is never raw material.
    carried = [m for m in window if is_consolidated(m)]
    summarizable = [m for m in window if not is_consolidated(m)]

    if not summarizable:
        # Everything in the window is already consolidated. Compacting
        # again would only re-summarise summaries.
        return {"compacted": False, "reason": "nothing_new"}

    segments = chunk_messages(summarizable)

    if not llm or not llm.available:
        summary = heuristic_summarize(summarizable)
    else:
        summary = await llm_summarize(
            messages=summarizable, llm=llm, max_chars=max_summary_chars,
            concurrency=map_concurrency,
        )

    # F2: the knowledge graph used to see ``[:3000]`` characters of a
    # concatenated blob, which at a 20-turn threshold is roughly the
    # first 3-6 messages of each window. Every entity after that was
    # invisible to the graph PERMANENTLY, because the raw turns are
    # replaced by the summary immediately afterwards and nothing ever
    # re-reads them. Extraction now runs per segment over the whole
    # window, bounded by the same concurrency limit as the map stage.
    key_entities = await _extract_entities(store, segments, llm)

    # F7: real provenance. ``source_turn_ids`` used to be positional
    # indices into a list the compaction then discarded, and nothing
    # anywhere read them. PR #224 made per-turn ``user_command`` /
    # ``assistant_reply`` episodes durable, so there are real row ids
    # to point at.
    resolved = await _resolve_source_episodes(store, session_id, summarizable)

    explicit_ids = [str(m["id"]) for m in summarizable if m.get("id")]
    source_episode_ids = [r["episode_id"] for r in resolved]
    source_turn_ids = explicit_ids or source_episode_ids

    # ``time_range`` was always [0.0, 0.0] because ordinary chat
    # history carries no ``meta.created_at``. Fall back to the real
    # created_at of the durable turn rows before giving up.
    timestamps = [
        float(m["meta"]["created_at"]) for m in summarizable
        if isinstance(m.get("meta"), dict)
        and isinstance(m["meta"].get("created_at"), (int, float))
    ]
    if not timestamps:
        timestamps = [r["created_at"] for r in resolved if r.get("created_at")]
    time_range = (min(timestamps), max(timestamps)) if timestamps else (0.0, 0.0)

    episode_id: str | None = None
    if promote_to_episode and summarizable:
        participants = sorted({
            str(m.get("role")) for m in summarizable
            if m.get("role")
        })

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

            # The metadata block on ``detail`` is kept for the readers
            # that already parse it, but it is no longer where the
            # provenance LIVES: an HTML comment inside a text column is
            # not queryable. ``compaction_sources`` is the real record.
            extra = {
                "time_range": list(time_range),
                "key_entities": key_entities,
                "source_turn_ids": source_turn_ids,
                "source_episode_ids": source_episode_ids,
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

        if episode_id and resolved:
            try:
                await store.record_compaction_sources(episode_id, resolved)
            except AttributeError:
                logger.debug(
                    "store has no record_compaction_sources; provenance "
                    "rows not written for episode %s", episode_id,
                )
            except Exception as exc:
                logger.warning(
                    "compact_session: provenance write failed for %s: %s",
                    episode_id, exc,
                )

    watermark = {
        "episode_id": episode_id,
        "session_id": session_id,
        "turn_count": len(summarizable),
        "source_turn_ids": source_turn_ids,
        "source_episode_ids": source_episode_ids,
        "time_range": list(time_range),
        "consolidated_at": time.time(),
        # Declares the ONLY thing a re-derivation is ever allowed to
        # read. Never "summary".
        "derived_from": "raw_turns",
    }
    summary_message = {
        "role": "system",
        "content": f"[Session Summary]\n{summary}",
        WATERMARK_KEY: watermark,
    }

    carried = await _carry_or_rederive(store, carried, llm, max_summary_chars)
    compacted_history = [*carried, summary_message, *preserved]

    return {
        "compacted": True,
        "original_length": len(history),
        "new_length": len(compacted_history),
        "summary_chars": len(summary),
        "history": compacted_history,
        "episode_id": episode_id,
        "key_entities": key_entities,
        "time_range": list(time_range),
        "source_turn_ids": source_turn_ids,
        "source_episode_ids": source_episode_ids,
        "watermark": watermark,
    }


def is_consolidated(message: dict) -> bool:
    """True when ``message`` is a summary a previous compaction wrote.

    The watermark is the primary signal. The text prefix is the
    fallback for transcripts written before watermarks existed and for
    the snapshot/restore paths, which round-trip messages through JSON
    and may drop unknown keys.
    """
    if not isinstance(message, dict):
        return False
    if isinstance(message.get(WATERMARK_KEY), dict):
        return True
    content = message.get("content")
    return (
        message.get("role") == "system"
        and isinstance(content, str)
        and content.startswith("[Session Summary]")
    )


async def _extract_entities(store, segments: list[str], llm) -> list[str]:
    """F2: run KG extraction per segment, bounded, failure-isolated."""
    kg = getattr(store, "kg", None)
    if not kg or not segments:
        return []

    sem = asyncio.Semaphore(max(1, MAP_CONCURRENCY))

    async def _one(segment: str):
        async with sem:
            try:
                return await kg.extract_and_store(segment, llm)
            except Exception as e:
                # One bad segment must not cost the graph the other
                # nine. Logged rather than swallowed.
                logger.debug("KG extraction failed for a segment: %s", e)
                return []

    try:
        batches = await asyncio.gather(*[_one(s) for s in segments])
    except Exception as e:
        logger.debug("KG extraction during compaction failed: %s", e)
        return []

    key_entities: list[str] = []
    # extract_and_store returns RELATION dicts, both on the LLM path
    # (KnowledgeGraph.add_relation returns
    # ``{id, source, relation, target, confidence}``) and on the
    # heuristic path. Both ends of the triple are entities the
    # extraction created or touched, so both belong here; the
    # predicate does not.
    for extracted in batches:
        for item in extracted or []:
            if not isinstance(item, dict):
                continue
            for key in ("source", "target"):
                name = item.get(key)
                if name and str(name) not in key_entities:
                    key_entities.append(str(name))
    return key_entities


async def _resolve_source_episodes(store, session_id: str, messages: list[dict]) -> list[dict]:
    """Map each summarizable message to its durable turn episode row.

    Returns ``[{"episode_id", "position", "role", "created_at"}, ...]``
    for the messages that resolved. Messages with no durable row (a
    transcript replayed from a snapshot, a store that predates PR #224)
    are simply absent, because a provenance record that points at
    nothing is worse than no record.
    """
    if not messages:
        return []
    try:
        rows = await store.turn_episodes(session_id, limit=max(200, len(messages) * 4))
    except AttributeError:
        logger.debug("store has no turn_episodes; provenance unresolved")
        return []
    except Exception as exc:
        logger.warning("provenance resolution query failed: %s", exc)
        return []

    # Index by body text. Duplicates are consumed in chronological
    # order, so "ok" said five times maps to five different rows rather
    # than five references to the first one.
    by_text: dict[str, list[dict]] = {}
    for row in rows:
        body = (row.get("detail") or row.get("summary") or "").strip()
        if body:
            by_text.setdefault(body, []).append(row)
    for bucket in by_text.values():
        bucket.sort(key=lambda r: r.get("created_at") or 0.0)

    cursor: dict[str, int] = {}
    out: list[dict] = []
    for position, m in enumerate(messages):
        content = m.get("content")
        if not isinstance(content, str):
            continue
        body = content.strip()
        bucket = by_text.get(body)
        if not bucket:
            continue
        i = cursor.get(body, 0)
        if i >= len(bucket):
            continue
        cursor[body] = i + 1
        row = bucket[i]
        out.append({
            "episode_id": row["id"],
            "position": position,
            "role": str(m.get("role") or ""),
            "created_at": float(row.get("created_at") or 0.0),
        })
    return out


async def _carry_or_rederive(store, carried: list[dict], llm, max_chars: int) -> list[dict]:
    """Keep prior summaries, and collapse them from RAW TURNS when too many.

    Prior summary blocks ride along verbatim. That alone is already the
    fix for the cliff, because a carried summary is never re-summarised
    and so never degrades. But carrying them forever is unbounded, so
    once there are :data:`MAX_CARRIED_SUMMARIES` of them the oldest are
    collapsed into one, and the collapse reads the RAW SOURCE TURNS out
    of the episodes table via the watermark, never the summary text.
    That is the whole point of the watermark: re-derivation always has
    a path back to the originals.

    If the raw turns cannot be recovered (no store support, rows aged
    out) the summaries are carried unchanged rather than degraded. A
    slightly larger transcript beats a lossy re-summarisation.
    """
    if len(carried) < MAX_CARRIED_SUMMARIES:
        return carried
    if not llm or not getattr(llm, "available", False):
        return carried

    collapsing = carried[:-1]
    keep = carried[-1:]

    ids: list[str] = []
    for m in collapsing:
        wm = m.get(WATERMARK_KEY)
        if isinstance(wm, dict):
            ids.extend(str(i) for i in (wm.get("source_episode_ids") or []))
    if not ids:
        return carried

    try:
        rows = await store.episodes_by_ids(ids)
    except AttributeError:
        logger.debug("store has no episodes_by_ids; carrying summaries as-is")
        return carried
    except Exception as exc:
        logger.warning("re-derivation lookup failed: %s", exc)
        return carried
    if not rows:
        return carried

    raw = [
        {
            "role": "assistant" if r.get("event_type") == "assistant_reply" else "user",
            "content": r.get("detail") or r.get("summary") or "",
        }
        for r in rows
    ]
    raw = [m for m in raw if m["content"]]
    if not raw:
        return carried

    text = await llm_summarize(messages=raw, llm=llm, max_chars=max_chars)
    merged = {
        "role": "system",
        "content": f"[Session Summary]\n{text}",
        WATERMARK_KEY: {
            "episode_id": None,
            "turn_count": len(raw),
            "source_episode_ids": ids,
            "consolidated_at": time.time(),
            "derived_from": "raw_turns",
            "rederived": True,
        },
    }
    return [merged, *keep]


async def llm_summarize(
    messages: list[dict],
    llm,
    max_chars: int,
    *,
    concurrency: int | None = None,
) -> str:
    """Map-reduce LLM summarization of conversation history.

    MAP: one generation per message-boundary segment
             (:func:`chunk_messages`), gathered under a bounded
             semaphore rather than run one at a time (F1).
    REDUCE: when the map output overflows ``max_chars``, one further
             generation merges the segment summaries. Previously there
             was no reduce at all: the joined output was sliced at
             ``[:max_chars]``, which deleted the tail segments (F5).

    If the reduce is unavailable or still overflows, every segment
    summary is abridged to an equal share instead, so no segment is
    dropped outright.
    """
    chunks = chunk_messages(messages)
    if not chunks:
        return ""

    if len(chunks) == 1:
        return _head_tail(await _summarize_one(chunks[0], llm), max_chars)

    limit = concurrency if concurrency and concurrency > 0 else MAP_CONCURRENCY
    sem = asyncio.Semaphore(max(1, limit))

    async def _bounded(chunk: str) -> str:
        async with sem:
            return await _summarize_one(chunk, llm)

    summaries = list(await asyncio.gather(*[_bounded(c) for c in chunks]))

    joined = "\n\n".join(s for s in summaries if s)
    if len(joined) <= max_chars:
        return joined

    try:
        body = "\n\n".join(
            f"--- part {i + 1} of {len(summaries)} ---\n{s}"
            for i, s in enumerate(summaries) if s
        )
        response = await llm.chat(
            [{"role": "user", "content": _REDUCE_HEADER + body}], tools=None
        )
        reduced, _ = llm.extract_response(response)
        if reduced and len(reduced) <= max_chars:
            return reduced
        if reduced:
            logger.info(
                "reduce output still over budget (%d > %d); abridging "
                "segment summaries instead", len(reduced), max_chars,
            )
    except Exception as exc:
        logger.warning("Summarization reduce failed: %s", exc)

    return _fit_summaries(summaries, max_chars)


def heuristic_summarize(messages: list[dict]) -> str:
    """No-LLM fallback: the last 20 messages, abridged.

    Head AND tail per message (F3). This path is already lossy by
    design, but there is no reason for it to be lossy in the one way
    that measurably discards the most salient text.
    """
    lines = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, str) and content:
            lines.append(f"[{role}] {_head_tail(content, 100)}")
    return "\n".join(lines[-20:])


async def search_all(store, query: str, limit: int = 10) -> list[dict]:
    """Search across all memory tiers using hybrid ranking.

    Tier isolation, and why it is neither "propagate" nor "swallow"
    ----------------------------------------------------------------
    This is an aggregator over four independent tiers, and it had no error
    handling at all. When ``entities.embedding`` was left at 1536 dims by a
    partial re-embed, the entity tier raised
    :class:`~memory.embeddings.EmbeddingDimensionMismatch` and took out the
    three tiers that had just answered correctly, at all three call sites
    that use this function (``gateway/protocol.py`` memory.search RPC,
    ``agents/taskflow.py`` memory.search step, ``api/routes/memory.py``).
    Measured on a copy of the real store: 5 of 5 queries raised, and
    episode + note + knowledge recall was collateral damage.

    Wrapping the whole thing in ``except Exception`` and returning ``[]``
    would be worse than the crash. An empty result set from a search
    function reads as "nothing matched", so the store looks *empty* rather
    than *broken*, and nobody investigates. That is exactly how this bug
    survived to a release.

    So each tier is isolated and its failure is DECLARED three ways: an
    ERROR log naming the fix, ``store._vector_leg_error`` (which
    ``/internal/memory/stats`` and ``feral doctor`` already surface as
    ``semantic_search: degraded``), and ``store.last_search_degradations``
    for anything that wants the structured form. The list return type is
    consumed by four languages, so the degradation is NOT smuggled into it
    as a fake row.

    If every tier fails, the exception propagates. At that point there is
    no partial answer to give, and ``[]`` would be a lie about the store's
    contents rather than a reduced answer.
    """
    results = []
    degradations: list[dict] = []
    tiers_attempted = 0

    async def _tier(name: str, coro_factory):
        """Run one tier; record and continue instead of killing the rest."""
        nonlocal tiers_attempted
        tiers_attempted += 1
        try:
            return await coro_factory()
        except Exception as exc:
            degradations.append({"tier": name, "error": f"{type(exc).__name__}: {exc}"})
            logger.error(
                "search_all: the %s tier failed and returned nothing for %r: "
                "%s: %s. The other tiers still answered; this result set is "
                "INCOMPLETE, not empty.",
                name, query, type(exc).__name__, exc,
            )
            try:
                store._vector_leg_error = f"{name} tier: {exc}"
            except Exception:  # pragma: no cover - plain attribute
                logger.debug("could not record degradation on store", exc_info=True)
            return None

    episodes = await _tier("episode", lambda: store.episode_search_hybrid(query, limit=limit))
    for item in episodes or []:
        results.append({**item, "tier": "episode", "score": item.get("relevance_score", 0)})

    notes = await _tier("note", lambda: store.search(query, limit=limit))
    for note in notes or []:
        results.append({**note, "tier": "note", "score": note.get("relevance_score", 0.3)})

    knowledge = await _tier("knowledge", lambda: store.knowledge_search(query, limit=limit))
    for item in knowledge or []:
        results.append(
            {
                "tier": "knowledge",
                "score": 0.5,
                "summary": f"{item['subject']} {item['predicate']} {item['object']}",
                **item,
            }
        )

    if store._kg:
        entities = await _tier("entity", lambda: store._kg.search_entities(query, limit=5))
        for entity in entities or []:
            results.append(
                {
                    "tier": "entity",
                    "score": entity.get("score", 0.5),
                    "summary": f"Entity: {entity['name']} ({entity.get('type', 'thing')})",
                    **entity,
                }
            )

    try:
        store.last_search_degradations = degradations
    except Exception:  # pragma: no cover - stores are plain objects
        logger.debug("could not publish last_search_degradations", exc_info=True)
    if degradations and len(degradations) == tiers_attempted:
        # Nothing survived. Returning [] here would report an empty store.
        raise RuntimeError(
            "every memory tier failed for query "
            f"{query!r}: "
            + "; ".join(f"{d['tier']}: {d['error']}" for d in degradations)
        )

    results.sort(key=lambda item: item.get("score", 0), reverse=True)
    return results[:limit]
