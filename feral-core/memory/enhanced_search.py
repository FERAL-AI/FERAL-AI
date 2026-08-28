"""
FERAL Enhanced Memory Search
==============================
Knowledge-graph query helpers on top of the base MemoryStore:
  - Relationship queries ("what does X know about Y")
  - Graph neighborhood visualization data

Both are reached from ``api/routes/memory.py``.

``bm25_score`` / ``temporal_decay`` / ``hybrid_rerank`` used to live here
and were removed in v2026.8.x. They had no caller anywhere in the repo,
including tests: ``hybrid_rerank`` was referenced by nothing, and the
other two only by ``hybrid_rerank``. Deleted rather than wired up,
because wiring them in would have been a regression. ``hybrid_rerank``
linearly blended a raw BM25 score with a raw cosine at fixed 0.3/0.5
weights, which is precisely the incomparable-scales error the RRF
rewrite in ``memory/store.py`` (see the "Hybrid ranking" comment block
there) was written to remove, and its normalisation step
``bm25 / max(bm25, 1.0)`` collapses every document scoring above 1.0 to
exactly 1.0, so all strong lexical matches tie. ``episode_search_hybrid``
already does this job correctly with Reciprocal Rank Fusion plus an MMR
rerank.
"""

from __future__ import annotations
import logging

logger = logging.getLogger("feral.memory.enhanced")


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
