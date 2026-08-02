"""Qdrant memory backend.

Installed via ``pip install feral-ai[memory-qdrant]`` or as a
``kind=memory`` item from registry.feral.sh. Uses Qdrant's local
on-disk mode by default (``~/.feral/qdrant/``) so no external server is
required. Power users can point ``qdrant_url`` at a remote Qdrant
cluster by editing ``~/.feral/config.yaml``.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from config.loader import feral_data_home

from .base import MemoryBackend, MemoryRecord

logger = logging.getLogger("feral.memory.backends.qdrant")

_COLLECTION_PREFIX = "feral_memory_dim_"


class QdrantBackend:
    """Qdrant-backed memory store (local or remote)."""

    backend_id: str = "qdrant"

    def __init__(
        self,
        *,
        dim: int,
        qdrant_url: Optional[str] = None,
        qdrant_api_key: Optional[str] = None,
        persist_dir: Optional[str] = None,
    ) -> None:
        self.dim = dim
        try:
            from qdrant_client import AsyncQdrantClient  # type: ignore
            from qdrant_client.http import models as qmodels  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "qdrant backend requires `pip install feral-ai[memory-qdrant]` "
                "(ships qdrant-client)."
            ) from exc

        self._qmodels = qmodels
        self._collection = f"{_COLLECTION_PREFIX}{dim}"
        self._qdrant_url = qdrant_url

        if qdrant_url:
            self._client = AsyncQdrantClient(url=qdrant_url, api_key=qdrant_api_key)
            self._persist_dir = None
        else:
            data_home = feral_data_home()
            self._persist_dir = str(Path(persist_dir) if persist_dir else data_home / "qdrant")
            Path(self._persist_dir).mkdir(parents=True, exist_ok=True)
            self._client = AsyncQdrantClient(path=self._persist_dir)

        self._collection_ready = False

    async def _ensure_collection(self) -> None:
        if self._collection_ready:
            return
        qmodels = self._qmodels
        try:
            await self._client.get_collection(self._collection)
        except Exception:
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=qmodels.VectorParams(
                    size=self.dim, distance=qmodels.Distance.COSINE
                ),
            )
        self._collection_ready = True

    # ─── MemoryBackend surface ─────────────────────────────

    async def upsert(self, records: Iterable[MemoryRecord]) -> None:
        batch = list(records)
        if not batch:
            return
        await self._ensure_collection()

        qmodels = self._qmodels
        points = []
        for rec in batch:
            if rec.embedding is None:
                logger.warning("qdrant backend skipping %s: no embedding", rec.id)
                continue
            if len(rec.embedding) != self.dim:
                raise ValueError(
                    f"embedding dim mismatch for {rec.id}: "
                    f"expected {self.dim}, got {len(rec.embedding)}"
                )
            # Qdrant prefers UUIDs but accepts any string id; persist the
            # caller-supplied id in the payload so we can round-trip it.
            point_id = _to_qdrant_id(rec.id)
            payload = {
                "caller_id": rec.id,
                "text": rec.text,
                **(rec.metadata or {}),
            }
            points.append(
                qmodels.PointStruct(
                    id=point_id, vector=list(rec.embedding), payload=payload
                )
            )
        if points:
            await self._client.upsert(collection_name=self._collection, points=points)

    async def search(
        self,
        query_vec: list[float],
        *,
        limit: int = 10,
        filter: Optional[dict[str, Any]] = None,
    ) -> list[MemoryRecord]:
        if len(query_vec) != self.dim:
            raise ValueError(
                f"query vector dim {len(query_vec)} != backend dim {self.dim}"
            )
        await self._ensure_collection()

        q_filter = None
        if filter:
            qmodels = self._qmodels
            q_filter = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key=key, match=qmodels.MatchValue(value=value)
                    )
                    for key, value in filter.items()
                ]
            )

        # `search()` was deprecated and then REMOVED from qdrant-client;
        # 1.18.0 has no such attribute and this raised AttributeError at
        # the first real query. It went unnoticed because every qdrant
        # test skips itself when the package is absent, and the package
        # is not installed by default, so the whole backend was never
        # executed. Installing qdrant-client and re-running turned two
        # skips into two failures.
        #
        # `query_points()` is the replacement: `query` rather than
        # `query_vector`, and the hits arrive under `.points` instead of
        # being the return value. Both spellings are supported here
        # because pyproject allows `qdrant-client>=1.11,<2.0` and the
        # older clients in that range still only have `search()`.
        if hasattr(self._client, "query_points"):
            response = await self._client.query_points(
                collection_name=self._collection,
                query=list(query_vec),
                limit=limit,
                query_filter=q_filter,
            )
            hits = response.points
        else:
            hits = await self._client.search(
                collection_name=self._collection,
                query_vector=list(query_vec),
                limit=limit,
                query_filter=q_filter,
            )
        records: list[MemoryRecord] = []
        for hit in hits:
            payload = dict(hit.payload or {})
            caller_id = payload.pop("caller_id", str(hit.id))
            text = payload.pop("text", "")
            records.append(
                MemoryRecord(
                    id=caller_id,
                    text=text,
                    metadata=payload,
                    score=float(hit.score),
                )
            )
        return records

    async def delete(self, ids: Iterable[str]) -> None:
        batch = [_to_qdrant_id(i) for i in ids]
        if not batch:
            return
        await self._ensure_collection()
        qmodels = self._qmodels
        await self._client.delete(
            collection_name=self._collection,
            points_selector=qmodels.PointIdsList(points=batch),
        )

    async def stats(self) -> dict[str, Any]:
        await self._ensure_collection()
        info = await self._client.get_collection(self._collection)
        count = getattr(info, "points_count", None)
        return {
            "backend": self.backend_id,
            "count": int(count) if count is not None else 0,
            "dim": self.dim,
            "collection": self._collection,
            "qdrant_url": self._qdrant_url or f"local:{self._persist_dir}",
        }

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception as exc:
            logger.debug("qdrant close error (ignored): %s", exc)


def _to_qdrant_id(caller_id: str) -> str:
    """Qdrant accepts UUID or uint64. Caller-supplied string ids get a
    stable UUIDv5 so re-inserts with the same caller id hit the same row."""
    try:
        return str(uuid.UUID(caller_id))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"feral:{caller_id}"))


# ─────────────────────────────────────────────────────────────


async def create(
    *,
    dim: int,
    qdrant_url: Optional[str] = None,
    qdrant_api_key: Optional[str] = None,
    persist_dir: Optional[str] = None,
    **_: Any,
) -> MemoryBackend:
    return QdrantBackend(
        dim=dim,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        persist_dir=persist_dir,
    )
