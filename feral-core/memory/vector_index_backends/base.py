"""Async ``VectorIndexBackend`` Protocol — the pluggable vector store
that :class:`MemoryStore` queries on its hot path.

Async-native (v2026.5.33 / Option C). MemoryStore is async-native; the
backend Protocol matches so the brain never bridges sync/async on a
memory call. The three first-party backends each take a different
route to satisfy this contract:

* ``sqlite_vec`` (default) — speaks ``aiosqlite`` directly. True async
  I/O, no thread offload.
* ``chroma`` — Chroma's Python client is sync-only for in-process use
  (``AsyncHttpClient`` requires a separate server, which we don't ship
  by default). The adapter wraps each call in ``asyncio.to_thread``;
  this is the adapter-boundary thread bridge the Option C plan
  permits — explicitly NOT a MemoryStore-level wrapper.
* ``qdrant`` — uses :class:`qdrant_client.AsyncQdrantClient`. True
  async I/O.

Public surface (intentionally tiny):

    indexed: bool                              # backend reports it's indexed
    count: int                                 # async coroutine returning current count
    await upsert(chunk_id, embedding)          # idempotent by id
    await upsert_batch(items)                  # optimised batch path
    await delete(chunk_id)                     # silent on unknown id
    await search(query_vec, limit)             # top-k, returns (id, distance)
    await search_similarity(query_vec, limit)  # top-k, returns (id, score)
    await close()                              # release handles

``search_similarity`` was called ``search_cosine`` until v2026.8.x and
the rename is the fix for a live trap, not a tidy-up. Only two of the
three first-party backends ever returned a cosine from it:

* ``chroma`` creates its collection with ``hnsw:space=cosine`` and
  ``qdrant`` with ``Distance.COSINE``, so ``1 - distance`` is a genuine
  cosine similarity in [-1, 1].
* ``sqlite_vec``, the DEFAULT, creates its ``vec0`` table with no
  ``distance_metric``, so vec0 answers with an L2 distance and the
  adapter returns ``1 - L2``. On unit vectors that is
  ``1 - sqrt(2 - 2*cos)``: monotone decreasing in the cosine, so
  ranking is identical, but a true cosine of 0.84 arrives as 0.4343 and
  an orthogonal pair arrives as -0.4142.

So the scale is BACKEND-DEPENDENT: the value is safe to sort by and
unsafe to threshold. Applying a cosine floor to it silently kills
recall, which has happened once already. Anything that needs a real
cosine recomputes it from the stored embeddings, which is what
``MemoryStore._centered_filter`` does with this method's output.

Changing ``sqlite_vec`` to ``distance_metric=cosine`` would fix the
scale but requires rebuilding every existing vec0 table, so the name
carries the warning instead. Third-party backends that still define
only ``search_cosine`` keep working: :func:`load_vector_index` aliases
the old name onto the new one and logs a deprecation.
``tests/test_vector_index_similarity_semantics.py`` pins the numbers.

The previous sync Protocol shipped in audit-r12 (v2026.5.32) is gone —
adapters no longer expose a sync surface. Direct callers of the legacy
``memory.embeddings.VectorIndex`` are gone too; that class was the only
escape hatch and has been removed in the same release.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, AsyncIterable, Iterable, Protocol, runtime_checkable

import numpy as np

logger = logging.getLogger("feral.memory.vector_index_backends")


@runtime_checkable
class VectorIndexBackend(Protocol):
    """The minimal async contract every vector index backend must satisfy.

    ``indexed`` reports whether the backend has an index up and running
    (e.g. the sqlite-vec extension loaded successfully). ``False`` puts
    the backend in a no-op mode where ``upsert`` does nothing and
    ``search_similarity`` returns ``[]``. This policy is uniform across
    backends: silent skip rather than crash on first use.

    ``False`` does NOT mean semantic search is lost. ``MemoryStore``
    answers the vector leg over numpy instead (see
    ``memory.embeddings.cosine_similarity_bulk``), which returns the same
    ranking and measures faster than vec0 at every corpus size tested;
    the FTS5 keyword leg runs alongside it either way.

    ``count`` is exposed as an awaitable so backends backed by remote
    services (Qdrant via ``AsyncQdrantClient``) can return live counts
    without blocking. The default ``sqlite_vec`` and ``chroma`` backends
    cache cheaply.
    """

    backend_id: str
    indexed: bool

    async def count(self) -> int: ...
    async def upsert(self, chunk_id: str, embedding: np.ndarray) -> None: ...
    async def upsert_batch(self, items: Iterable[tuple[str, np.ndarray]]) -> None: ...
    async def delete(self, chunk_id: str) -> None: ...
    async def search(self, query_vec: np.ndarray, limit: int = 20) -> list[tuple[str, float]]: ...
    async def search_similarity(self, query_vec: np.ndarray, limit: int = 20) -> list[tuple[str, float]]: ...
    async def close(self) -> None: ...


# ─────────────────────────────────────────────
# Registry + sync loader
# ─────────────────────────────────────────────

_REGISTRY: dict[str, str] = {
    "sqlite_vec": "memory.vector_index_backends.sqlite_vec",
    "chroma": "memory.vector_index_backends.chroma",
    "qdrant": "memory.vector_index_backends.qdrant",
}


def register_backend(backend_id: str, module_path: str) -> None:
    """Register a backend module path so :func:`load_vector_index` can
    find it. Third-party backends published as ``kind=memory-vec`` on
    registry.feral.sh land in ``~/.feral/vector-index-backends/<id>/``
    and call this at import time."""
    _REGISTRY[backend_id] = module_path


def load_vector_index(
    backend_id: str, *, dim: int, **config: Any
) -> VectorIndexBackend:
    """Synchronously instantiate the configured vector-index backend.

    Instantiation stays sync because boot wiring (``BrainState.__init__``)
    is sync; the resulting instance's methods are all awaitable. A
    misconfigured backend MUST surface at boot, never at first query —
    no silent fall-back to sqlite-vec.

    Raises
    ------
    ValueError
        ``backend_id`` is not registered. Lists known ids in the
        error message.
    ImportError
        The backend module's optional dependency (``chromadb``,
        ``qdrant-client``, …) is not installed. Suggests the right
        ``feral-ai[memory-<id>]`` extra.
    TypeError
        The backend module's ``create`` factory returned something
        that does not satisfy :class:`VectorIndexBackend` (missing
        one of the required async methods).
    """
    if backend_id not in _REGISTRY:
        raise ValueError(
            f"unknown memory vector-index backend {backend_id!r}. "
            f"Known: {sorted(_REGISTRY.keys())}. "
            "Install a community backend via `feral install <id>` if "
            "it's on registry.feral.sh."
        )

    module_path = _REGISTRY[backend_id]
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ImportError(
            f"vector-index backend {backend_id!r} could not be imported: "
            f"{exc}. Install with `pip install feral-ai[memory-"
            f"{backend_id}]` (or `feral install <registry_item_id>` for "
            "community backends)."
        ) from exc

    factory = getattr(module, "create", None)
    if factory is None:
        raise ImportError(
            f"backend module {module_path!r} exposes no "
            "`create(dim, **cfg)` factory. Every vector-index backend "
            "must provide one."
        )

    backend = factory(dim=dim, **config)
    # ``search_cosine`` was renamed to ``search_similarity`` because two
    # of the three first-party backends never returned a cosine from it
    # (see the module docstring). Community backends are separate
    # packages on registry.feral.sh and cannot be renamed with this
    # commit, so adapt the old name rather than failing them at load or,
    # worse, at first query.
    if not hasattr(backend, "search_similarity") and hasattr(backend, "search_cosine"):
        backend.search_similarity = backend.search_cosine  # type: ignore[attr-defined]
        logger.warning(
            "vector-index backend %r defines search_cosine but not "
            "search_similarity. The old name is deprecated: its return "
            "value is a backend-specific similarity score, not necessarily "
            "a cosine, and must never be thresholded as one. Rename the "
            "method to search_similarity.",
            backend_id,
        )
    if not isinstance(backend, VectorIndexBackend):
        raise TypeError(
            f"vector-index factory for {backend_id!r} returned "
            f"{type(backend).__name__}, which does not satisfy the "
            "async VectorIndexBackend Protocol (missing one of "
            "indexed, count, upsert, upsert_batch, delete, search, "
            "search_similarity, close)."
        )
    logger.info(
        "vector index backend loaded: %s (indexed=%s)",
        backend_id, backend.indexed,
    )
    return backend
