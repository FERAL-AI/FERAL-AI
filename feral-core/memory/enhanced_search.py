"""
FERAL Enhanced Memory Search
==============================
Advanced retrieval features on top of the base MemoryStore:
  - BM25 + vector hybrid reranking with configurable fusion weights
  - Relationship queries ("what does X know about Y")
  - Temporal decay with configurable half-life
  - Graph neighborhood visualization data
"""

from __future__ import annotations
import logging
import math
import time

logger = logging.getLogger("feral.memory.enhanced")


def bm25_score(
    query_terms: list[str],
    doc_text: str,
    avg_dl: float = 200.0,
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    """Compute a BM25 relevance score for a single document."""
    doc_terms = doc_text.lower().split()
    dl = len(doc_terms)
    if dl == 0:
        return 0.0

    tf_map: dict[str, int] = {}
    for t in doc_terms:
        tf_map[t] = tf_map.get(t, 0) + 1

    score = 0.0
    for term in query_terms:
        tf = tf_map.get(term.lower(), 0)
        if tf == 0:
            continue
        numerator = tf * (k1 + 1)
        denominator = tf + k1 * (1 - b + b * (dl / avg_dl))
        score += numerator / denominator
    return score


def temporal_decay(created_at: float, half_life_days: float = 30.0, now: float | None = None) -> float:
    """Exponential temporal decay. Returns a weight in (0, 1]."""
    now = now or time.time()
    age_days = max(0, (now - created_at) / 86400)
    return math.exp(-0.693 * age_days / half_life_days)


def hybrid_rerank(
    results: list[dict],
    query: str,
    text_weight: float = 0.3,
    vector_weight: float = 0.5,
    recency_weight: float = 0.2,
    half_life_days: float = 30.0,
) -> list[dict]:
    """Rerank search results using BM25 + vector similarity + temporal decay fusion.

    Each result dict should have:
      - text/content/summary: the document text
      - vector_score (optional): cosine similarity score
      - created_at (optional): Unix timestamp
    """
    query_terms = query.lower().split()
    now = time.time()

    texts = []
    for r in results:
        texts.append(r.get("text", r.get("content", r.get("summary", ""))))
    avg_dl = sum(len(t.split()) for t in texts) / max(len(texts), 1)

    scored = []
    for i, r in enumerate(results):
        bm25 = bm25_score(query_terms, texts[i], avg_dl)
        vec = r.get("vector_score", r.get("similarity", 0.0))
        ts = r.get("created_at", now)
        decay = temporal_decay(ts, half_life_days, now) if recency_weight > 0 else 1.0

        max_bm25 = max(bm25, 1.0)
        norm_bm25 = bm25 / max_bm25

        fused = (text_weight * norm_bm25) + (vector_weight * vec) + (recency_weight * decay)
        r["_fused_score"] = fused
        scored.append(r)

    scored.sort(key=lambda x: x["_fused_score"], reverse=True)
    return scored


async def relationship_query(kg, entity_a: str, entity_b: str, max_depth: int = 4) -> dict:
    """Query the relationship between two entities in the knowledge graph.

    Returns paths connecting entity A to entity B, along with shared
    neighbors and relationship descriptions suitable for natural language.

    Async because ``KnowledgeGraph.traverse`` is. This was a plain ``def``
    calling it without awaiting, so the two comprehensions below iterated a
    coroutine and raised ``TypeError: 'coroutine' object is not iterable``.
    The route handler turned that into a 500, which is why multi-hop
    traversal has never been reachable from any user-facing path even
    though ``traverse`` itself is correct.
    """
    paths_a = await kg.traverse(entity_a, max_depth=max_depth)
    paths_b = await kg.traverse(entity_b, max_depth=max_depth)

    targets_a = {p["target"].lower() for p in paths_a}
    targets_b = {p["target"].lower() for p in paths_b}
    shared = targets_a & targets_b

    direct_links = []
    for p in paths_a:
        if p["target"].lower() == entity_b.lower():
            direct_links.append(p)
    for p in paths_b:
        if p["target"].lower() == entity_a.lower():
            direct_links.append(p)

    shared_context = []
    for p in paths_a:
        if p["target"].lower() in shared:
            shared_context.append(p)
    for p in paths_b:
        if p["target"].lower() in shared:
            shared_context.append(p)

    summary_parts = []
    if direct_links:
        for link in direct_links:
            summary_parts.append(f"{link['source']} {link['relation']} {link['target']}")
    elif shared:
        summary_parts.append(f"{entity_a} and {entity_b} are connected through: {', '.join(sorted(shared)[:5])}")
    else:
        summary_parts.append(f"No known relationship found between {entity_a} and {entity_b}")

    return {
        "entity_a": entity_a,
        "entity_b": entity_b,
        "direct_links": direct_links,
        "shared_neighbors": sorted(shared)[:10],
        "shared_context": shared_context[:20],
        "summary": ". ".join(summary_parts),
    }


async def graph_visualization_data(kg, center_entity: str, max_depth: int = 2, limit: int = 50) -> dict:
    """Generate nodes/edges data for graph visualization in the web UI.

    Returns a structure compatible with common graph visualization libraries
    (D3.js, vis.js, cytoscape).

    Async for the same reason as :func:`relationship_query`: the ``for p in
    paths`` below iterated an un-awaited coroutine, so this endpoint has
    only ever returned 500.
    """
    paths = await kg.traverse(center_entity, max_depth=max_depth, limit=limit)

    nodes_map: dict[str, dict] = {}
    edges: list[dict] = []

    nodes_map[center_entity.lower()] = {
        "id": center_entity.lower(),
        "label": center_entity,
        "type": "center",
        "depth": 0,
    }

    for p in paths:
        src = p["source"].lower()
        tgt = p["target"].lower()
        depth = p.get("depth", 1)

        if src not in nodes_map:
            nodes_map[src] = {"id": src, "label": p["source"], "type": "entity", "depth": depth}
        if tgt not in nodes_map:
            nodes_map[tgt] = {"id": tgt, "label": p["target"], "type": "entity", "depth": depth}

        edges.append({
            "source": src,
            "target": tgt,
            "label": p["relation"],
            "depth": depth,
        })

    return {
        "nodes": list(nodes_map.values()),
        "edges": edges,
        "center": center_entity,
        "depth": max_depth,
    }
