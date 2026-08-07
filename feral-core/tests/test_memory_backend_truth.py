"""The reported vector backend must be what is RUNNING, not the default.

Incident: ``/api/memory/backend`` and ``/internal/memory/stats`` both
derived their answer from

    str(getattr(mem, "_backend_id", "sqlite_vec") or "sqlite_vec")

so any brain whose store did not expose ``_backend_id`` (and, worse, any
brain whose sqlite-vec extension never loaded) was reported as running
``sqlite_vec``. On the machine this was found on, sqlite-vec CANNOT load
at all: the interpreter was built without ``enable_load_extension``, so
``_vec_index.indexed`` is False for the whole process and every vector
query is answered by a numpy brute-force scan over ``memory_chunks``.
The same payload said ``active_vector_store: "sqlite_vec"`` next to
``degraded_semantic_search: true``.

These tests pin the truthful behaviour, including the one that matters
most: the label must NOT be the happy default when the extension failed
to load.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.routes import memory as memory_route


def _store(*, backend_id="sqlite_vec", indexed=False, stats=None):
    """A stand-in MemoryStore exposing only what the route reads."""
    return SimpleNamespace(
        _backend_id=backend_id,
        _vec_index=SimpleNamespace(indexed=indexed),
        _vector_leg_error=None,
        _embed_queue=SimpleNamespace(
            _embedder=SimpleNamespace(provider_name="fastembed")
        ),
        stats=AsyncMock(return_value=stats or {"embedded_chunks": 11610}),
    )


@pytest.fixture
def route_state(monkeypatch):
    def _patch(memory):
        monkeypatch.setattr(
            memory_route, "state", SimpleNamespace(memory=memory, memory_decay=None)
        )
    return _patch


def test_backend_label_is_not_the_happy_default_when_extension_failed(route_state):
    """THE regression: sqlite_vec configured and constructed, extension
    never loaded. Reporting "sqlite_vec" names a backend that is not
    answering a single query."""
    route_state(_store(backend_id="sqlite_vec", indexed=False))

    label = memory_route._runtime_backend_id()

    assert label != "sqlite_vec", (
        "the vector backend label fell back to the configured default "
        "even though the sqlite-vec index never came up"
    )
    assert label == memory_route.VECTOR_FALLBACK_ID == "numpy_fallback"


def test_missing_backend_attribute_is_not_reported_as_sqlite_vec(route_state):
    """A store that does not expose ``_backend_id`` is unknown, not
    sqlite_vec. The old ``getattr(..., "sqlite_vec")`` default invented
    an answer here."""
    route_state(SimpleNamespace(_vec_index=SimpleNamespace(indexed=True)))

    constructed, effective, _reason = memory_route._runtime_vector_state()

    assert constructed == "unknown"
    assert effective == "unknown"


def test_no_store_is_unknown_not_sqlite_vec(route_state):
    route_state(None)

    constructed, effective, reason = memory_route._runtime_vector_state()

    assert (constructed, effective) == ("unknown", "unknown")
    assert reason == "memory store not constructed"


def test_healthy_index_reports_its_real_backend(route_state):
    """The fix must not make a working backend look degraded."""
    route_state(_store(backend_id="qdrant", indexed=True))

    constructed, effective, reason = memory_route._runtime_vector_state()

    assert (constructed, effective, reason) == ("qdrant", "qdrant", None)
    assert memory_route._runtime_backend_id() == "qdrant"


def test_any_backend_with_a_dead_index_reports_the_fallback(route_state):
    """The numpy scan is not sqlite-vec specific: ``episode_search_hybrid``
    takes it whenever ``indexed`` is False, whichever backend is wired."""
    route_state(_store(backend_id="chroma", indexed=False))

    constructed, effective, reason = memory_route._runtime_vector_state()

    assert constructed == "chroma"
    assert effective == "numpy_fallback"
    assert "chroma" in reason and "indexed=False" in reason


@pytest.mark.asyncio
async def test_stats_cannot_claim_a_backend_it_calls_degraded(route_state):
    """``active_vector_store`` and ``degraded_semantic_search`` are now
    derived from the same probe, so the payload can no longer contradict
    itself the way it did on the reporter's machine."""
    route_state(_store(backend_id="sqlite_vec", indexed=False,
                       stats={"embedded_chunks": 11610}))

    out = await memory_route.memory_stats()
    obs = out["observability"]

    assert obs["degraded_semantic_search"] is True
    assert obs["sqlite_vec_loaded"] is False
    assert obs["active_vector_store"] == "numpy_fallback"
    assert obs["configured_vector_store"] == "sqlite_vec"
    assert obs["vector_index_degraded_reason"]


@pytest.mark.asyncio
async def test_backend_route_reports_fallback_without_faking_a_pending_restart(
    route_state, monkeypatch, tmp_path,
):
    """``runtime`` tells the truth, but ``pending_unapplied`` stays False:
    a restart does not fix an interpreter that cannot load extensions,
    and telling the operator to restart would send them round a loop."""
    (tmp_path / "settings.json").write_text('{"memory": {"backend": "sqlite_vec"}}')
    monkeypatch.setattr(memory_route, "feral_home", lambda: tmp_path)
    route_state(_store(backend_id="sqlite_vec", indexed=False))

    out = await memory_route.get_memory_backend()

    assert out["backend"] == "sqlite_vec"          # what settings.json says
    assert out["constructed_backend"] == "sqlite_vec"  # what boot built
    assert out["runtime"] == "numpy_fallback"      # what answers queries
    assert out["active_store"] == "numpy_fallback"
    assert out["vector_index_degraded"] is True
    assert out["pending_unapplied"] is False


@pytest.mark.asyncio
async def test_backend_route_still_flags_a_genuine_restart_to_apply(
    route_state, monkeypatch, tmp_path,
):
    """Guard against over-correcting: a real configured-vs-constructed
    mismatch must still raise ``pending_unapplied``."""
    (tmp_path / "settings.json").write_text('{"memory": {"backend": "qdrant"}}')
    monkeypatch.setattr(memory_route, "feral_home", lambda: tmp_path)
    route_state(_store(backend_id="sqlite_vec", indexed=True))

    out = await memory_route.get_memory_backend()

    assert out["backend"] == "qdrant"
    assert out["constructed_backend"] == "sqlite_vec"
    assert out["runtime"] == "sqlite_vec"
    assert out["vector_index_degraded"] is False
    assert out["pending_unapplied"] is True
