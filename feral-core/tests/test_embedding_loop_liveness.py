"""Embedding must not run on the event loop thread.

AUDIT-FIXES F-04. ``EmbeddingProvider._embed_impl`` is ``async`` and is
awaited from ordinary request paths, but both local provider branches ran
synchronously on the loop thread:

    memory/embeddings.py:1219   return self._fastembed_embed(text)
    memory/embeddings.py:1221   return self._local_embed(text)
    memory/embeddings.py:1200   return self._fastembed_batch(texts)

``_local_embed`` ends in ``SentenceTransformer.encode()``, a full
transformer forward pass. ``_detect_provider`` defaults to ``auto``
(memory/embeddings.py:1024), which resolves to exactly these two
branches, so this is the default install rather than an exotic
configuration.

The consequence is not slowness, it is that everything else stops.
Voice streaming, websocket heartbeats and concurrent HTTP all stall for
the duration, because a coroutine that never awaits holds the loop.

Measurement follows the pattern already established in
tests/perf/test_memory_latency.py: count the ticks of a 1 ms pulse
coroutine running concurrently, rather than timing wall clock. Wall clock
tells you the work took N ms either way; only the pulse count tells you
whether anything else could run while it did.

The embedder is stubbed with a deliberate blocking sleep, so this asserts
the offload, not the speed of any real model, and it needs no downloaded
weights to be meaningful.
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from memory.embeddings import EmbeddingProvider

# Long enough that a blocked loop is unambiguous, short enough to keep the
# suite quick. A free loop should tick ~200 times in this window; a blocked
# one ticks approximately zero.
_BLOCK_SECONDS = 0.20
_PULSE_MS = 0.001


async def _count_pulses_during(coro) -> tuple[int, object]:
    """Run *coro*, counting 1 ms loop ticks that fire alongside it."""
    ticks = 0
    stop = False

    async def _pulse():
        nonlocal ticks
        while not stop:
            await asyncio.sleep(_PULSE_MS)
            ticks += 1

    pulse_task = asyncio.create_task(_pulse())
    try:
        result = await coro
    finally:
        stop = True
        pulse_task.cancel()
        try:
            await pulse_task
        except asyncio.CancelledError:
            pass
    return ticks, result


@pytest.fixture
def blocking_provider(monkeypatch):
    """A provider whose local branch blocks the thread it runs on."""
    provider = EmbeddingProvider.__new__(EmbeddingProvider)
    provider._provider = "sentence_transformers"
    provider._dimension = 4
    provider._dim = 4
    # `degraded` reads this; without it the provider short-circuits to the
    # hash fallback and the test would measure nothing.
    provider._degraded_until = 0.0

    def _blocking_local_embed(text):
        time.sleep(_BLOCK_SECONDS)          # stands in for encode()
        return np.zeros(4, dtype=np.float32)

    monkeypatch.setattr(provider, "_local_embed", _blocking_local_embed,
                        raising=False)
    return provider


class TestTheLoopKeepsRunning:
    @pytest.mark.asyncio
    async def test_a_single_embed_does_not_freeze_the_loop(self, blocking_provider):
        """The regression. Before the fix the pulse count is ~0 because the
        coroutine never yields between entering _embed_impl and returning."""
        ticks, vec = await _count_pulses_during(
            blocking_provider._embed_impl("hello")
        )

        assert vec is not None
        assert ticks > 20, (
            f"the event loop ticked only {ticks} times during a "
            f"{_BLOCK_SECONDS}s embed, so the embed ran on the loop thread "
            f"and every other coroutine was frozen"
        )

    @pytest.mark.asyncio
    async def test_other_coroutines_make_progress_during_an_embed(
        self, blocking_provider
    ):
        """States the user-visible property directly: a second task must
        finish while an embed is in flight. This is voice streaming and
        websocket heartbeats staying alive."""
        progressed = asyncio.Event()

        async def _other_work():
            await asyncio.sleep(_BLOCK_SECONDS / 4)
            progressed.set()

        other = asyncio.create_task(_other_work())
        await blocking_provider._embed_impl("hello")
        await asyncio.wait_for(other, timeout=1.0)

        assert progressed.is_set()


class TestTheOffloadIsRealNotCosmetic:
    @pytest.mark.asyncio
    async def test_the_blocking_call_runs_off_the_loop_thread(
        self, blocking_provider, monkeypatch
    ):
        """A wrapper that awaits something trivial and then still calls the
        blocking function inline would satisfy a tick count on a fast
        machine. Assert the work happens on a different thread."""
        import threading

        loop_thread = threading.get_ident()
        seen: dict = {}

        def _recording_embed(text):
            seen["thread"] = threading.get_ident()
            return np.zeros(4, dtype=np.float32)

        monkeypatch.setattr(blocking_provider, "_local_embed", _recording_embed,
                            raising=False)
        await blocking_provider._embed_impl("hello")

        assert seen["thread"] != loop_thread, (
            "the embedder ran on the event loop thread"
        )
