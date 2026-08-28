"""Async Chroma backend for :class:`VectorIndexBackend`.

Installed via ``pip install feral-ai[memory-chroma]`` or as a
``kind=memory-vec`` item on registry.feral.sh. Uses Chroma's
:class:`PersistentClient` so the database lives under
``~/.feral/chroma/`` by default — no external server required.

Chroma does NOT ship an in-process async client (only ``AsyncHttpClient``
which requires running Chroma as a separate HTTP server). To honour the
async :class:`VectorIndexBackend` Protocol while keeping the
zero-server default, this adapter wraps each sync ``PersistentClient``
call in :func:`asyncio.to_thread`. The bridge lives strictly at the
adapter boundary (NOT around :class:`MemoryStore` method calls) — the
upper memory layers stay event-loop friendly because Chroma's worker
runs on a thread.

Operators who want true native async with Chroma can stand up a Chroma
HTTP server and register a separate ``chroma_http`` backend wrapping
``AsyncHttpClient``; that's a follow-up.

Collections are scoped per embedding dimensionality
(``feral_vec_dim_<n>``) to avoid silent shape drift when a user swaps
embedding models.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

from config.loader import feral_data_home

logger = logging.getLogger("feral.memory.vector_index_backends.chroma")

_COLLECTION_PREFIX = "feral_vec_dim_"


class ChromaVectorIndex:
    """Chroma-backed vector index satisfying the async
    :class:`VectorIndexBackend`. Internally wraps Chroma's sync
    ``PersistentClient`` via ``asyncio.to_thread``."""

    backend_id: str = "chroma"

    def __init__(
        self,
        *,
        dim: int,
        persist_dir: Optional[str] = None,
        collection: Optional[str] = None,
    ) -> None:
        try:
            import chromadb  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "chroma backend requires `pip install "
                "feral-ai[memory-chroma]`. The wheels for `chromadb` "
                "are large; we don't pull them in by default. "
                f"Underlying error: {exc}"
            ) from exc

        self.dim = dim
        if persist_dir is None:
            persist_dir = str(feral_data_home() / "chroma")
        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        self._persist_dir = persist_dir
        # PersistentClient construction is sync; it runs once at boot
        # which is itself sync, so we don't bridge here.
        self._client = chromadb.PersistentClient(path=persist_dir)
        self._collection_name = collection or f"{_COLLECTION_PREFIX}{dim}"
        self._coll = self._client.get_or_create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": "cosine", "feral.dim": dim},
        )

    @property
    def indexed(self) -> bool:
        return True

    async def count(self) -> int:
        try:
            return int(await asyncio.to_thread(self._coll.count))
        except Exception as exc:
            logger.debug("chroma count() failed: %s", exc)
            return 0

    async def upsert(self, chunk_id: str, embedding: np.ndarray) -> None:
        vec = np.asarray(embedding, dtype=np.float32)
        if vec.shape[0] != self.dim:
            raise ValueError(
                f"chroma backend dim mismatch: collection dim={self.dim}, "
                f"vector dim={vec.shape[0]}"
            )
        await asyncio.to_thread(
            self._coll.upsert, ids=[chunk_id], embeddings=[vec.tolist()]
        )

    async def upsert_batch(self, items: Iterable[tuple[str, np.ndarray]]) -> None:
        ids: list[str] = []
        vecs: list[list[float]] = []
        for chunk_id, embedding in items:
            vec = np.asarray(embedding, dtype=np.float32)
            if vec.shape[0] != self.dim:
                raise ValueError(
                    f"chroma backend dim mismatch on chunk_id={chunk_id!r}: "
                    f"collection dim={self.dim}, vector dim={vec.shape[0]}"
                )
            ids.append(chunk_id)
            vecs.append(vec.tolist())
        if not ids:
            return
        await asyncio.to_thread(self._coll.upsert, ids=ids, embeddings=vecs)

    async def delete(self, chunk_id: str) -> None:
        try:
            await asyncio.to_thread(self._coll.delete, ids=[chunk_id])
        except Exception as exc:
            logger.debug("chroma delete(%r) failed: %s", chunk_id, exc)

    async def search(
        self, query_vec: np.ndarray, limit: int = 20
    ) -> list[tuple[str, float]]:
        vec = np.asarray(query_vec, dtype=np.float32)
        if vec.shape[0] != self.dim:
            return []
        try:
            res = await asyncio.to_thread(
                self._coll.query,
                query_embeddings=[vec.tolist()],
                n_results=max(1, int(limit)),
                include=["distances"],
            )
        except Exception as exc:
            logger.debug("chroma search failed: %s", exc)
            return []
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        return [(cid, float(d)) for cid, d in zip(ids, dists)]

    async def search_similarity(
        self, query_vec: np.ndarray, limit: int = 20
    ) -> list[tuple[str, float]]:
        """Top-k as ``(chunk_id, 1.0 - distance)``, best first.

        Here the value IS a cosine similarity, because the collection is
        created with ``hnsw:space=cosine`` so Chroma's distance is the
        cosine distance. The default ``sqlite_vec`` backend's version of
        this method is not, which is why the method is not called
        ``search_cosine``: callers cannot assume the scale without
        knowing which backend they got, so they must not threshold it.
        """
        return [(cid, 1.0 - dist) for cid, dist in await self.search(query_vec, limit)]

    async def close(self) -> None:
        # PersistentClient has no explicit close; the underlying DB
        # handles flush on drop.
        self._client = None  # type: ignore[assignment]


def create(
    *,
    dim: int,
    persist_dir: Optional[str] = None,
    collection: Optional[str] = None,
    **_: Any,
) -> ChromaVectorIndex:
    return ChromaVectorIndex(dim=dim, persist_dir=persist_dir, collection=collection)
