"""Pin the ``/api/memory/stats`` response shape for the RC polish bundle.

The Recent tab in the WebUI reads ``totals.knowledge_triples`` (canonical
backend key) and falls back to the legacy ``totals.knowledge`` alias for
older brains. When the underlying ``MemoryStore.stats()`` returns a
degraded payload (``ok: False, reason: "stats_timeout"``), the route must
propagate ``ok`` and ``reason`` so the UI can show an unavailable chip
instead of a misleading row of zeros.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.routes import memory as memory_route


@pytest.fixture
def patch_route_state(monkeypatch):
    def _patch(*, stats_return=None, decay=None):
        memory = SimpleNamespace(stats=AsyncMock(return_value=stats_return or {}))
        fake_state = SimpleNamespace(memory=memory, memory_decay=decay)
        monkeypatch.setattr(memory_route, "state", fake_state)
        return fake_state
    return _patch


@pytest.mark.asyncio
async def test_memory_stats_exposes_canonical_knowledge_triples(patch_route_state):
    """Healthy store -> route returns ``knowledge_triples`` AND keeps a
    ``knowledge`` alias so older WebUIs don't break overnight."""
    patch_route_state(stats_return={
        "ok": True,
        "notes": 12,
        "episodes": 340,
        "knowledge_triples": 88,
    })
    out = await memory_route.get_memory_stats()
    assert out["totals"]["episodes"] == 340
    assert out["totals"]["notes"] == 12
    assert out["totals"]["knowledge_triples"] == 88
    # Legacy alias still present for back-compat.
    assert out["totals"]["knowledge"] == 88


@pytest.mark.asyncio
async def test_memory_stats_propagates_stats_timeout_reason(patch_route_state):
    """When MemoryStore.stats() returns the degraded shape, the route
    must surface ``ok: False`` AND the ``reason`` string so the
    dashboard can render an honest unavailable chip."""
    patch_route_state(stats_return={
        "ok": False,
        "reason": "stats_timeout",
        "notes": 0,
        "episodes": 0,
        "knowledge_triples": 0,
    })
    out = await memory_route.get_memory_stats()
    assert out["ok"] is False
    assert out["reason"] == "stats_timeout"
    # Totals are still present and zeroed — keeps the UI's
    # ``Number(t.x ?? 0)`` parsing happy on the degraded path.
    assert out["totals"] == {
        "episodes": 0,
        "notes": 0,
        "knowledge_triples": 0,
        "knowledge": 0,
    }


@pytest.mark.asyncio
async def test_memory_stats_surfaces_error_when_store_raises(monkeypatch):
    """If the store call itself blows up, the route swallows the
    exception but flips ``ok=False`` + ``reason=stats_error`` so the
    UI still gets a truthful signal."""
    memory = SimpleNamespace(stats=AsyncMock(side_effect=RuntimeError("boom")))
    fake_state = SimpleNamespace(memory=memory, memory_decay=None)
    monkeypatch.setattr(memory_route, "state", fake_state)

    out = await memory_route.get_memory_stats()
    assert out["ok"] is False
    assert out["reason"] == "stats_error"
    assert out["totals"] == {}
