"""v2026.5.33 — identity_loader is now fully async.

 ``_build_memory_context`` called ``asyncio.run`` on
``memory.build_context_for_llm_async`` which leaked coroutines when run
inside a running event loop. v2026.5.33 removes the sync/async bridge
entirely: ``_build_memory_context`` and ``build_system_prompt`` are
``async def`` and callers ``await`` them directly.

These tests pin the new invariants:
- the async builder is invoked exactly once per call with no coroutine
  leak warnings;
- a ``memory=None`` loader returns an empty memory context without
  warnings.
"""

from __future__ import annotations

import warnings

import pytest

from agents.identity_loader import IdentityLoader


class _StubMemory:
    """A minimal memory store exposing only the async builder."""

    def __init__(self) -> None:
        self.async_calls: int = 0

    async def build_context_for_llm_async(
        self,
        session_id: str,
        query: str = "",
        max_tokens_budget: int = 2000,
        memory_filter: str = "",
    ) -> str:
        self.async_calls += 1
        return "## Memory (async)\nhello"


def _make_loader(memory) -> IdentityLoader:
    """Construct an IdentityLoader without touching ~/.feral."""
    loader = IdentityLoader.__new__(IdentityLoader)
    loader.memory = memory  # type: ignore[attr-defined]
    return loader


async def test_async_builder_called_once_no_warnings() -> None:
    memory = _StubMemory()
    loader = _make_loader(memory)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = await loader._build_memory_context(
            session_id="sess-A",
            query="hello",
            memory_filter="",
        )
        leaks = [
            w for w in caught
            if issubclass(w.category, RuntimeWarning)
            and "coroutine" in str(w.message)
            and "never awaited" in str(w.message)
        ]
        assert not leaks, (
            f"async-only memory path must not leak coroutines; got: "
            f"{[str(w.message) for w in leaks]}"
        )

    assert memory.async_calls == 1
    assert "## Memory (async)" in out


async def test_async_builder_called_with_filter() -> None:
    memory = _StubMemory()
    loader = _make_loader(memory)

    out = await loader._build_memory_context(
        session_id="sess-B",
        query="world",
        memory_filter="coding",
    )
    assert memory.async_calls == 1
    assert "## Memory (async)" in out


async def test_no_memory_returns_empty_string_without_warnings() -> None:
    loader = IdentityLoader.__new__(IdentityLoader)
    loader.memory = None  # type: ignore[attr-defined]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = await loader._build_memory_context(
            session_id="sess-C",
            query="",
            memory_filter="",
        )
    assert out == ""
    assert not [
        w for w in caught
        if issubclass(w.category, RuntimeWarning)
        and "coroutine" in str(w.message)
    ]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
