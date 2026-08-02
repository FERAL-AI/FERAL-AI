"""Tests for audit-r12 D4: ``memory.backend`` selector is no longer
theater.

Before D4, ``MemoryStore`` hard-wired :class:`memory.embeddings.VectorIndex`
at boot — ``settings.memory.backend`` was readable but read nowhere. This
suite pins the new wiring:

* the sync :class:`VectorIndexBackend` Protocol is the contract;
* the registry resolves ``sqlite_vec``, ``chroma``, ``qdrant`` to the
  shipped adapters;
* ``load_vector_index`` fails loudly (``ValueError`` / ``ImportError``)
  rather than silently falling back;
* ``MemoryStore`` honours an injected backend end-to-end;
* the default round-trip (add → embed-queue drain → vector search)
  still works with the sqlite-vec adapter on top of the Protocol.

The Chroma and Qdrant adapters get import-failure tests here; their full
add/search round-trip lives behind extras and is gated on the optional
package being installed (skipif).
"""

from __future__ import annotations

import sys
import importlib
import tempfile
from pathlib import Path
from typing import Iterable

import numpy as np
import pytest

# Ensure feral-core is importable when tests run from the repo root.
_FERAL_CORE = Path(__file__).resolve().parent.parent
if str(_FERAL_CORE) not in sys.path:
    sys.path.insert(0, str(_FERAL_CORE))

from memory.vector_index_backends import (  # noqa: E402
    VectorIndexBackend,
    load_vector_index,
    register_backend,
)
from memory.vector_index_backends.sqlite_vec import SQLiteVecIndex  # noqa: E402
from memory.store import MemoryStore  # noqa: E402


# ─────────────────────────────────────────────
# Protocol conformance
# ─────────────────────────────────────────────


class _FakeBackend:
    """In-test backend used to assert MemoryStore actually USES the
    injected object rather than constructing its own."""

    backend_id = "fake"

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim
        self.indexed = True
        self.upserts: list[tuple[str, np.ndarray]] = []
        self.deletes: list[str] = []

    async def upsert(self, chunk_id: str, embedding: np.ndarray) -> None:
        self.upserts.append((chunk_id, np.asarray(embedding)))

    async def upsert_batch(self, items: Iterable[tuple[str, np.ndarray]]) -> None:
        for chunk_id, vec in items:
            await self.upsert(chunk_id, vec)

    async def delete(self, chunk_id: str) -> None:
        self.deletes.append(chunk_id)

    async def count(self) -> int:
        return len(self.upserts)

    async def search(self, query_vec: np.ndarray, limit: int = 20):
        return [(cid, 0.0) for cid, _ in self.upserts[:limit]]

    async def search_cosine(self, query_vec: np.ndarray, limit: int = 20):
        return [(cid, 1.0) for cid, _ in self.upserts[:limit]]

    def close(self) -> None:
        pass


def test_sqlite_vec_adapter_satisfies_protocol():
    with tempfile.TemporaryDirectory() as tmp:
        idx = SQLiteVecIndex(dim=8, db_path=str(Path(tmp) / "vec.db"))
        assert isinstance(idx, VectorIndexBackend)
        assert idx.backend_id == "sqlite_vec"


def test_fake_backend_satisfies_protocol_runtime_check():
    # Pins that ``runtime_checkable`` catches an in-test stand-in too,
    # so plugin authors get a clear error from ``load_vector_index``
    # if their factory returns the wrong shape.
    assert isinstance(_FakeBackend(), VectorIndexBackend)


# ─────────────────────────────────────────────
# Loader: fail-loud on unknown id / missing dep
# ─────────────────────────────────────────────


def test_load_vector_index_unknown_id_raises_value_error():
    with pytest.raises(ValueError) as excinfo:
        load_vector_index("not_a_real_backend", dim=8)
    # Error message must list the known ids so the operator can recover.
    msg = str(excinfo.value)
    assert "sqlite_vec" in msg
    assert "chroma" in msg
    assert "qdrant" in msg


def test_load_vector_index_missing_factory_raises_import_error(tmp_path):
    # Register a module that exists but exposes no `create` factory and
    # confirm the loader complains loudly instead of returning None.
    fake_mod = "memory.vector_index_backends._test_no_factory"
    sys.modules[fake_mod] = type(sys)("test_no_factory")
    register_backend("__test_no_factory__", fake_mod)
    try:
        with pytest.raises(ImportError, match="no.*create.*factory"):
            load_vector_index("__test_no_factory__", dim=8)
    finally:
        sys.modules.pop(fake_mod, None)


def test_load_vector_index_returns_non_protocol_raises_type_error():
    fake_mod_name = "memory.vector_index_backends._test_bad_factory"
    mod = type(sys)("test_bad_factory")

    def create(*, dim, **_):
        return object()

    mod.create = create  # type: ignore[attr-defined]
    sys.modules[fake_mod_name] = mod
    register_backend("__test_bad_factory__", fake_mod_name)
    try:
        with pytest.raises(TypeError, match="VectorIndexBackend Protocol"):
            load_vector_index("__test_bad_factory__", dim=8)
    finally:
        sys.modules.pop(fake_mod_name, None)


def test_load_vector_index_chroma_missing_raises_import_error_with_extras_hint(monkeypatch):
    # Hide chromadb (whether installed or not) and assert the loader
    # raises ImportError that points at the right `feral-ai[memory-chroma]`
    # extras name.
    monkeypatch.setitem(sys.modules, "chromadb", None)
    # Drop any cached module so a fresh import reattempts.
    sys.modules.pop("memory.vector_index_backends.chroma", None)
    with pytest.raises(ImportError) as excinfo:
        load_vector_index("chroma", dim=8)
    msg = str(excinfo.value)
    assert "memory-chroma" in msg or "chromadb" in msg


def test_load_vector_index_qdrant_missing_raises_import_error_with_extras_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "qdrant_client", None)
    sys.modules.pop("memory.vector_index_backends.qdrant", None)
    with pytest.raises(ImportError) as excinfo:
        load_vector_index("qdrant", dim=8)
    msg = str(excinfo.value)
    assert "memory-qdrant" in msg or "qdrant" in msg


# ─────────────────────────────────────────────
# MemoryStore actually uses the injected backend
# ─────────────────────────────────────────────


async def test_memory_store_uses_injected_backend(tmp_path):
    db = tmp_path / "mem.db"
    fake = _FakeBackend(dim=384)
    store = MemoryStore(db_path=str(db), vec_index=fake)
    assert store._vec_index is fake
    assert store._backend_id == "fake"
    # ``MemoryStore`` reads vector-index health from the injected
    # backend's Protocol surface — not from a hardcoded VectorIndex.
    stats = await store.stats()
    assert stats["vec_index_count"] == 0
    # Drive a vector through MemoryStore's underlying surface and prove
    # the injected backend (not the legacy VectorIndex) saw the upsert.
    vec = np.ones(384, dtype=np.float32)
    await store._vec_index.upsert("chunk-id-1", vec)
    assert fake.upserts and fake.upserts[0][0] == "chunk-id-1"
    # ``stats()`` serves rapid polls from a short-TTL cache
    # (``_STATS_CACHE_TTL_S``, 15s) so dashboard polling cannot starve the
    # writer pool. This test drives the backend directly via
    # ``store._vec_index``, deliberately bypassing ``MemoryStore``, so
    # nothing can invalidate that cache on its behalf and the second call
    # would otherwise replay the first one's zero. Expire it explicitly:
    # the assertion is about the injected backend being the source of
    # truth, not about cache timing.
    store._stats_cache_at = 0.0
    assert (await store.stats())["vec_index_count"] == 1


def test_memory_store_defaults_to_sqlite_vec_adapter(tmp_path):
    # No backend injected -> MemoryStore picks the SQLiteVecIndex
    # adapter (which forwards to memory.embeddings.VectorIndex). Pins
    # that the default path still goes through the Protocol layer
    # rather than constructing VectorIndex directly.
    db = tmp_path / "mem.db"
    store = MemoryStore(db_path=str(db))
    assert isinstance(store._vec_index, SQLiteVecIndex)
    assert store._backend_id == "sqlite_vec"


# ─────────────────────────────────────────────
# Boot-time selector wiring (state.py)
# ─────────────────────────────────────────────


def test_brain_state_helper_returns_none_for_default_backend(monkeypatch):
    # `_load_configured_vec_index_or_default` is the seam BrainState
    # uses. For the default backend it MUST return None (the in-
    # MemoryStore default path stays untouched) — anything else is a
    # regression that would double-construct the index.
    from api import state as state_mod
    import config.loader as loader_mod

    monkeypatch.setattr(
        loader_mod, "load_settings",
        lambda: {"memory": {"backend": "sqlite_vec", "backend_config": {}}},
    )
    assert state_mod._load_configured_vec_index_or_default() is None


def test_brain_state_helper_never_bricks_boot_on_bad_backend(monkeypatch):
    # RC fix: a misconfigured ``settings.memory.backend`` (unknown id,
    # or an installed backend whose optional dep is missing) used to RAISE
    # at boot — which bricked the whole brain (``feral serve`` timed out).
    # The boot seam now falls back to sqlite-vec (returns None) and records
    # the failure in ``MEMORY_BACKEND_STATUS`` so the dashboard can surface
    # an actionable message instead of a dead server. ``load_vector_index``
    # itself still fails loudly (covered above); only the boot wrapper is
    # resilient.
    from api import state as state_mod
    import config.loader as loader_mod

    monkeypatch.setattr(
        loader_mod, "load_settings",
        lambda: {
            "memory": {
                "backend": "wat_is_this_even",
                "backend_config": {},
            },
        },
    )
    result = state_mod._load_configured_vec_index_or_default()
    assert result is None  # fell back to MemoryStore's sqlite-vec default
    status = state_mod.MEMORY_BACKEND_STATUS
    assert status["configured"] == "wat_is_this_even"
    assert status["active"] == "sqlite_vec"
    assert status["fell_back"] is True
    assert status["error"] and "wat_is_this_even" in status["error"]


# ─────────────────────────────────────────────
# End-to-end add+search through the Protocol layer (sqlite-vec)
# ─────────────────────────────────────────────


def test_sqlite_vec_adapter_add_search_roundtrip(tmp_path):
    db = tmp_path / "vec.db"
    idx = SQLiteVecIndex(dim=4, db_path=str(db))
    if not idx.indexed:
        pytest.skip(
            "sqlite-vec extension not available on this host "
            "(install `feral-ai[vec]`); end-to-end search relies on it"
        )
    v1 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    v2 = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
    idx.upsert("a", v1)
    idx.upsert("b", v2)
    assert idx.count == 2
    hits = idx.search_cosine(v1, limit=2)
    assert hits, "search returned no hits"
    assert hits[0][0] == "a"
    idx.delete("a")
    idx.close()


# ─────────────────────────────────────────────
# Optional-dep backends: smoke test if installed
# ─────────────────────────────────────────────

# These two adapters expose an ASYNC interface (upsert / search_cosine /
# close are all coroutine functions). Both tests used to call them
# synchronously and assert on the returned coroutine, which fails with
# "'coroutine' object is not subscriptable". Nobody saw it because each
# test skips itself unless its optional package is installed, and neither
# is installed by default, so the bodies had never run. Installing
# qdrant-client turned the skip into exactly that failure.


@pytest.mark.skipif(
    importlib.util.find_spec("chromadb") is None,
    reason="chromadb not installed (feral-ai[memory-chroma])",
)
@pytest.mark.asyncio
async def test_chroma_adapter_add_search_roundtrip(tmp_path):
    sys.modules.pop("memory.vector_index_backends.chroma", None)
    backend = load_vector_index(
        "chroma", dim=4, persist_dir=str(tmp_path / "chroma"),
    )
    assert backend.backend_id == "chroma"
    v1 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    v2 = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
    await backend.upsert("a", v1)
    await backend.upsert("b", v2)
    hits = await backend.search_cosine(v1, limit=2)
    assert hits and hits[0][0] == "a"
    await backend.close()


@pytest.mark.skipif(
    importlib.util.find_spec("qdrant_client") is None,
    reason="qdrant_client not installed (feral-ai[memory-qdrant])",
)
@pytest.mark.asyncio
async def test_qdrant_adapter_add_search_roundtrip(tmp_path):
    sys.modules.pop("memory.vector_index_backends.qdrant", None)
    backend = load_vector_index(
        "qdrant", dim=4, persist_dir=str(tmp_path / "qdrant"),
    )
    assert backend.backend_id == "qdrant"
    v1 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    v2 = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
    await backend.upsert("a", v1)
    await backend.upsert("b", v2)
    hits = await backend.search_cosine(v1, limit=2)
    assert hits and hits[0][0] == "a"
    await backend.close()
