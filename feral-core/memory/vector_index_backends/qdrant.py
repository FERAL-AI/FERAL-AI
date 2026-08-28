"""Async Qdrant backend for :class:`VectorIndexBackend`.

Installed via ``pip install feral-ai[memory-qdrant]`` or as a
``kind=memory-vec`` item on registry.feral.sh. Uses
:class:`qdrant_client.AsyncQdrantClient` — true async I/O without
thread bridging. Defaults to Qdrant's local embedded mode
(``AsyncQdrantClient(path=...)``) so the database lives under
``~/.feral/qdrant/`` — no external server required. Operators who want
a remote Qdrant pass ``url=`` in ``settings.memory.backend_config``.

Collections are scoped per embedding dimensionality
(``feral_vec_dim_<n>``) to avoid silent shape drift when a user swaps
embedding models. ``size`` matches ``dim`` exactly; cosine distance
matches what sqlite-vec and Chroma return so ``MemoryStore`` can swap
backends without changing its hot path.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

from config.loader import feral_data_home

logger = logging.getLogger("feral.memory.vector_index_backends.qdrant")

_COLLECTION_PREFIX = "feral_vec_dim_"


class QdrantVectorIndex:
    """Qdrant-backed vector index satisfying async
    :class:`VectorIndexBackend`. Uses :class:`AsyncQdrantClient` for
    true async I/O.

    Construction (``ensure collection exists``) needs to run async too,
    but ``__init__`` is sync — we lazily ensure the collection on first
    method call via an asyncio lock. This keeps boot wiring sync while
    still using the async client end-to-end on the hot path.
    """

    backend_id: str = "qdrant"

    def __init__(
        self,
        *,
        dim: int,
        persist_dir: Optional[str] = None,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        collection: Optional[str] = None,
    ) -> None:
        try:
            from qdrant_client import AsyncQdrantClient  # type: ignore
            from qdrant_client.http.models import Distance, VectorParams  # type: ignore
            from qdrant_client.http import models as qm  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "qdrant backend requires `pip install "
                "feral-ai[memory-qdrant]`. Underlying error: " + str(exc)
            ) from exc

        self.dim = dim
        if url is not None:
            self._client = AsyncQdrantClient(url=url, api_key=api_key)
        else:
            if persist_dir is None:
                persist_dir = str(feral_data_home() / "qdrant")
            Path(persist_dir).mkdir(parents=True, exist_ok=True)
            self._client = AsyncQdrantClient(path=persist_dir)
        self._collection = collection or f"{_COLLECTION_PREFIX}{dim}"
        self._qm = qm
        self._VectorParams = VectorParams
        self._Distance = Distance
        self._ensure_lock = asyncio.Lock()
        self._ensured = False

    @property
    def indexed(self) -> bool:
        return True

    async def _ensure_collection(self) -> None:
        if self._ensured:
            return
        async with self._ensure_lock:
            if self._ensured:
                return
            existing = await self._client.get_collections()
            names = {c.name for c in existing.collections}
            if self._collection not in names:
                await self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config=self._VectorParams(
                        size=self.dim, distance=self._Distance.COSINE
                    ),
                )
            self._ensured = True

    async def count(self) -> int:
        try:
            await self._ensure_collection()
            info = await self._client.get_collection(collection_name=self._collection)
            return int(getattr(info, "points_count", 0) or 0)
        except Exception as exc:
            logger.debug("qdrant count() failed: %s", exc)
            return 0

    @staticmethod
    def _point_id(chunk_id: str) -> str:
        """Map a FERAL chunk id onto something Qdrant will accept.

        Qdrant point ids must be a UUID or a uint64. FERAL chunk ids look
        like ``note_984acdb8_c0``, so passing them through raised
        "Point id a is not a valid UUID" on every write. UUIDv5 keeps the
        mapping stable, so re-inserting the same chunk id updates the
        same point instead of duplicating it.

        ``memory/backends/qdrant.py`` already did exactly this via
        ``_to_qdrant_id``; this adapter is a separate module and never
        got it. Nothing caught the difference because the adapter test
        skips itself unless qdrant-client is installed, and it is not
        installed by default.
        """
        try:
            return str(uuid.UUID(chunk_id))
        except ValueError:
            return str(uuid.uuid5(uuid.NAMESPACE_URL, f"feral:{chunk_id}"))

    def _make_point(self, chunk_id: str, embedding: np.ndarray):
        # The caller id rides in the payload because the point id is a
        # one-way hash: without this, search returns the UUID and every
        # caller loses the chunk id it asked about.
        return self._qm.PointStruct(
            id=self._point_id(chunk_id),
            vector=embedding.tolist(),
            payload={"chunk_id": chunk_id},
        )

    async def upsert(self, chunk_id: str, embedding: np.ndarray) -> None:
        vec = np.asarray(embedding, dtype=np.float32)
        if vec.shape[0] != self.dim:
            raise ValueError(
                f"qdrant backend dim mismatch: collection dim={self.dim}, "
                f"vector dim={vec.shape[0]}"
            )
        await self._ensure_collection()
        await self._client.upsert(
            collection_name=self._collection,
            points=[self._make_point(chunk_id, vec)],
            wait=False,
        )

    async def upsert_batch(self, items: Iterable[tuple[str, np.ndarray]]) -> None:
        points = []
        for chunk_id, embedding in items:
            vec = np.asarray(embedding, dtype=np.float32)
            if vec.shape[0] != self.dim:
                raise ValueError(
                    f"qdrant backend dim mismatch on chunk_id={chunk_id!r}: "
                    f"collection dim={self.dim}, vector dim={vec.shape[0]}"
                )
            points.append(self._make_point(chunk_id, vec))
        if not points:
            return
        await self._ensure_collection()
        await self._client.upsert(
            collection_name=self._collection, points=points, wait=False,
        )

    async def delete(self, chunk_id: str) -> None:
        try:
            await self._ensure_collection()
            await self._client.delete(
                collection_name=self._collection,
                points_selector=self._qm.PointIdsList(
                    points=[self._point_id(chunk_id)]
                ),
                wait=False,
            )
        except Exception as exc:
            logger.debug("qdrant delete(%r) failed: %s", chunk_id, exc)

    async def search(
        self, query_vec: np.ndarray, limit: int = 20
    ) -> list[tuple[str, float]]:
        vec = np.asarray(query_vec, dtype=np.float32)
        if vec.shape[0] != self.dim:
            return []
        try:
            await self._ensure_collection()
            # `search()` was removed from qdrant-client (gone in 1.18);
            # `query_points()` replaces it and returns hits under
            # `.points`. Both are supported because pyproject allows
            # `qdrant-client>=1.11,<2.0`. with_payload is now True so the
            # original chunk id comes back, see `_make_point`.
            if hasattr(self._client, "query_points"):
                response = await self._client.query_points(
                    collection_name=self._collection,
                    query=vec.tolist(),
                    limit=max(1, int(limit)),
                    with_payload=True,
                    with_vectors=False,
                )
                hits = response.points
            else:
                hits = await self._client.search(
                    collection_name=self._collection,
                    query_vector=vec.tolist(),
                    limit=max(1, int(limit)),
                    with_payload=True,
                    with_vectors=False,
                )
        except Exception as exc:
            # warning, not debug: every failure above returns an empty
            # result set, which reads as "nothing matched" rather than
            # "the backend is broken". That is how a removed client
            # method and an invalid id format both stayed invisible.
            logger.warning("qdrant search failed: %s", exc)
            return []
        return [
            ((h.payload or {}).get("chunk_id") or str(h.id), 1.0 - float(h.score))
            for h in hits
        ]

    async def search_similarity(
        self, query_vec: np.ndarray, limit: int = 20
    ) -> list[tuple[str, float]]:
        """Top-k as ``(chunk_id, 1.0 - distance)``, best first.

        Here the value IS a cosine similarity: the collection is created
        with ``Distance.COSINE``, ``search`` turns Qdrant's cosine score
        into ``1 - score``, and this turns it back. The default
        ``sqlite_vec`` backend's version of this method is not a cosine,
        which is why the method is not called ``search_cosine``: the
        scale is backend-dependent, so callers must sort by it and never
        threshold it.
        """
        return [(cid, 1.0 - dist) for cid, dist in await self.search(query_vec, limit)]

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass


def create(
    *,
    dim: int,
    persist_dir: Optional[str] = None,
    url: Optional[str] = None,
    api_key: Optional[str] = None,
    collection: Optional[str] = None,
    **_: Any,
) -> QdrantVectorIndex:
    return QdrantVectorIndex(
        dim=dim,
        persist_dir=persist_dir,
        url=url,
        api_key=api_key,
        collection=collection,
    )
