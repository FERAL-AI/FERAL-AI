"""Tests for FERAL memory embedding utilities, vector index, and provider degrade.

The degrade-and-fallback tests cover the production failure mode that
previously flooded logs every cycle: OpenAI embeddings returning HTTP 429
``insufficient_quota`` repeatedly. The new behaviour: degrade once, route
through the configured fallback, throttle warnings.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import numpy as np
import pytest

from memory.embeddings import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    LOCAL_DIM,
    OPENAI_DIM,
    EmbeddingProvider,
    EmbeddingSkipped,
    EmbedQueue,
    VectorIndex,
    _LogThrottle,
    blob_to_vec,
    chunk_text,
    cosine_similarity,
    vec_to_blob,
)


@pytest.fixture
def temp_db_path():
    """Temporary SQLite file path for VectorIndex tests."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def chunks_db_path(tmp_path):
    """SQLite path with the ``memory_chunks`` schema EmbedQueue writes into."""
    db_path = str(tmp_path / "chunks.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE memory_chunks (
            id TEXT PRIMARY KEY,
            source_table TEXT NOT NULL,
            source_id TEXT NOT NULL,
            chunk_index INTEGER DEFAULT 0,
            text_content TEXT NOT NULL,
            embedding BLOB,
            created_at REAL NOT NULL
        )"""
    )
    conn.commit()
    conn.close()
    return db_path


def _force_hash_provider(ep: EmbeddingProvider) -> None:
    ep._provider = "hash"
    ep._dim = LOCAL_DIM
    ep._model = None


def _force_openai_provider(ep: EmbeddingProvider) -> None:
    """Pretend OpenAI is the active primary so degrade paths exercise."""
    ep._provider = "openai"
    ep._dim = OPENAI_DIM
    ep._model = None


def _make_http_status_error(status: int, body: str) -> httpx.HTTPStatusError:
    """Construct a real httpx.HTTPStatusError with a parseable response body."""
    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    response = httpx.Response(status_code=status, content=body.encode(), request=request)
    return httpx.HTTPStatusError(f"{status} error", request=request, response=response)


class TestChunkText:
    """Tests for chunk_text word-based splitting."""

    def test_short_text_single_chunk(self):
        text = "hello world"
        chunks = chunk_text(text)
        assert chunks == [text]

    def test_splitting_and_overlap(self):
        words = [f"w{i}" for i in range(CHUNK_SIZE + 50)]
        text = " ".join(words)
        chunks = chunk_text(text, max_tokens=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
        assert len(chunks) > 1
        first_words = chunks[0].split()
        second_words = chunks[1].split()
        overlap_words = set(first_words[-CHUNK_OVERLAP:]) & set(second_words[:CHUNK_OVERLAP])
        assert len(overlap_words) == CHUNK_OVERLAP


class TestVecBlobRoundtrip:
    """vec_to_blob / blob_to_vec symmetry."""

    def test_roundtrip_list(self):
        vec = [0.25, -1.5, 2.0]
        blob = vec_to_blob(vec)
        out = blob_to_vec(blob)
        assert out.dtype == np.float32
        np.testing.assert_allclose(out, np.array(vec, dtype=np.float32))

    def test_roundtrip_ndarray(self):
        vec = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        out = blob_to_vec(vec_to_blob(vec))
        np.testing.assert_allclose(out, vec.astype(np.float32))


class TestCosineSimilarity:
    """cosine_similarity edge cases."""

    def test_identical_vectors_are_one(self):
        a = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        assert abs(cosine_similarity(a, a) - 1.0) < 1e-6

    def test_orthogonal_vectors_are_zero(self):
        a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        b = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        assert abs(cosine_similarity(a, b)) < 1e-6


class TestEmbeddingProvider:
    """Hash fallback dimension and basic embed API."""

    @pytest.mark.asyncio
    async def test_hash_fallback_consistent_vectors(self):
        with patch.object(EmbeddingProvider, "_detect_provider", _force_hash_provider):
            p = EmbeddingProvider()
        assert p.provider_name == "hash"
        v1 = await p.embed("same text")
        v2 = await p.embed("same text")
        np.testing.assert_allclose(v1, v2)
        assert v1.shape == (LOCAL_DIM,)

    def test_dimension_property(self):
        with patch.object(EmbeddingProvider, "_detect_provider", _force_hash_provider):
            p = EmbeddingProvider()
        assert p.dimension == LOCAL_DIM


class TestVectorIndex:
    """VectorIndex with temp DB; search may be empty without sqlite-vec."""

    def test_create_upsert_search(self, temp_db_path):
        dim = 8
        idx = VectorIndex(temp_db_path, dimension=dim)
        v = np.ones(dim, dtype=np.float32)
        idx.upsert("chunk-1", v)
        results = idx.search(v, limit=5)
        assert isinstance(results, list)
        if not idx.indexed:
            assert results == []


class TestEmbedQueue:
    """EmbedQueue pending counter + persist behaviour under failure."""

    def test_enqueue_increments_pending(self, temp_db_path):
        mock_embedder = MagicMock()
        mock_embedder.embed = AsyncMock(return_value=np.zeros(LOCAL_DIM, dtype=np.float32))
        mock_embedder.degraded = False
        q = EmbedQueue(mock_embedder)
        q.enqueue("cid", "hello", "memory_chunks", "src1", 0, temp_db_path)
        assert q.pending == 1


# ──────────────────────────────────────────────────────────────────
# Anti-spam log throttle
# ──────────────────────────────────────────────────────────────────


class TestLogThrottle:
    """The throttle is the foundation of the no-spam guarantee."""

    def test_first_call_allowed_subsequent_suppressed(self):
        throttle = _LogThrottle(interval_seconds=10_000.0)
        allowed, suppressed = throttle.should_log("k")
        assert allowed is True
        assert suppressed == 0
        for _ in range(10):
            allowed_n, _ = throttle.should_log("k")
            assert allowed_n is False

    def test_suppressed_count_resets_on_next_log(self):
        throttle = _LogThrottle(interval_seconds=10_000.0)
        throttle.should_log("k")
        for _ in range(3):
            throttle.should_log("k")
        # Simulate the cooldown elapsing so the next call logs and flushes
        # the suppressed counter.
        throttle._last["k"] = time.monotonic() - 20_000.0
        allowed, suppressed = throttle.should_log("k")
        assert allowed is True
        assert suppressed == 3

    def test_independent_keys(self):
        throttle = _LogThrottle(interval_seconds=10_000.0)
        a1, _ = throttle.should_log("a")
        b1, _ = throttle.should_log("b")
        assert a1 is True and b1 is True


# ──────────────────────────────────────────────────────────────────
# OpenAI 429 → degrade & fallback behaviour
# ──────────────────────────────────────────────────────────────────


def _build_openai_provider(monkeypatch, *, fallback: str = "hash", threshold: int = 3):
    """Construct a provider configured for the OpenAI primary path under test."""
    # A key alone no longer selects OpenAI (that billed users who exported one
    # for chat completions), so the paid path has to be asked for explicitly.
    # See tests/test_embeddings_local_first.py for the precedence itself.
    monkeypatch.setenv("FERAL_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("FERAL_EMBED_FALLBACK", fallback)
    monkeypatch.setenv("FERAL_EMBED_RATE_LIMIT_THRESHOLD", str(threshold))
    monkeypatch.setenv("FERAL_EMBED_DEGRADE_LOG_INTERVAL_S", "10000")
    p = EmbeddingProvider()
    assert p.provider_name == "openai"
    assert p.dimension == OPENAI_DIM
    return p


class TestProviderDegrade429:
    """Persistent 429 / insufficient_quota → degrade and fall back without log spam."""

    @pytest.mark.asyncio
    async def test_insufficient_quota_degrades_on_first_event(self, monkeypatch):
        p = _build_openai_provider(monkeypatch, fallback="hash")
        body = (
            '{"error": {"message": "You exceeded your current quota, please '
            'check your plan and billing details.", "type": "insufficient_quota", '
            '"code": "insufficient_quota"}}'
        )
        err = _make_http_status_error(429, body)

        async def _raise(_self, _text):
            raise err

        monkeypatch.setattr(EmbeddingProvider, "_openai_embed", _raise)

        vec = await p.embed("first call")
        assert vec.shape == (OPENAI_DIM,)
        assert p.degraded is True
        assert p.degrade_reason == "insufficient_quota"
        assert p.active_provider == "fallback:hash"

    @pytest.mark.asyncio
    async def test_repeated_429s_eventually_degrade_at_threshold(self, monkeypatch):
        p = _build_openai_provider(monkeypatch, fallback="hash", threshold=3)
        err = _make_http_status_error(
            429,
            '{"error": {"message": "rate limit exceeded", "type": "rate_limit_exceeded"}}',
        )

        async def _raise(_self, _text):
            raise err

        monkeypatch.setattr(EmbeddingProvider, "_openai_embed", _raise)

        with pytest.raises(httpx.HTTPStatusError):
            await p.embed("call-1")
        assert p.degraded is False
        with pytest.raises(httpx.HTTPStatusError):
            await p.embed("call-2")
        assert p.degraded is False

        vec = await p.embed("call-3")
        assert vec.shape == (OPENAI_DIM,)
        assert p.degraded is True
        assert p.degrade_reason == "rate_limit"

    @pytest.mark.asyncio
    async def test_subsequent_calls_skip_openai_when_degraded(self, monkeypatch):
        p = _build_openai_provider(monkeypatch, fallback="hash")
        err = _make_http_status_error(
            429,
            '{"error": {"code": "insufficient_quota", "message": "exceeded your current quota"}}',
        )
        call_count = {"n": 0}

        async def _raise(_self, _text):
            call_count["n"] += 1
            raise err

        monkeypatch.setattr(EmbeddingProvider, "_openai_embed", _raise)

        await p.embed("trigger degrade")
        assert call_count["n"] == 1
        assert p.degraded is True

        for i in range(20):
            v = await p.embed(f"post-degrade-{i}")
            assert v.shape == (OPENAI_DIM,)
        assert call_count["n"] == 1, "OpenAI must not be called while degraded"

    @pytest.mark.asyncio
    async def test_repeat_degrade_warnings_are_throttled(self, monkeypatch, caplog):
        p = _build_openai_provider(monkeypatch, fallback="hash")
        err = _make_http_status_error(
            429,
            '{"error": {"code": "insufficient_quota", "message": "exceeded your current quota"}}',
        )

        async def _raise(_self, _text):
            raise err

        monkeypatch.setattr(EmbeddingProvider, "_openai_embed", _raise)
        caplog.set_level(logging.WARNING, logger="feral.memory.embeddings")

        await p.embed("warm-up")
        # Force-clear cache so subsequent identical text re-enters the provider.
        p._cache.clear()
        # Force-clear degrade so we re-trigger the same warning condition.
        p._degraded_until = 0.0
        await p.embed("warm-up-2")
        p._cache.clear()
        p._degraded_until = 0.0
        await p.embed("warm-up-3")

        degrade_warnings = [
            r for r in caplog.records
            if "embedding_provider_degraded" in r.getMessage()
            and "reason=insufficient_quota" in r.getMessage()
        ]
        assert len(degrade_warnings) == 1, (
            f"expected exactly one throttled degrade warning, got "
            f"{[w.getMessage() for w in degrade_warnings]}"
        )

    @pytest.mark.asyncio
    async def test_skip_mode_raises_embedding_skipped(self, monkeypatch):
        p = _build_openai_provider(monkeypatch, fallback="skip")
        err = _make_http_status_error(
            429,
            '{"error": {"code": "insufficient_quota", "message": "exceeded your current quota"}}',
        )

        async def _raise(_self, _text):
            raise err

        monkeypatch.setattr(EmbeddingProvider, "_openai_embed", _raise)

        with pytest.raises(EmbeddingSkipped):
            await p.embed("first")
        assert p.degraded is True
        with pytest.raises(EmbeddingSkipped):
            await p.embed("second")

    @pytest.mark.asyncio
    async def test_hard_auth_failure_degrades_immediately(self, monkeypatch):
        p = _build_openai_provider(monkeypatch, fallback="hash")
        err = _make_http_status_error(
            401,
            '{"error": {"code": "invalid_api_key", "message": "Incorrect API key"}}',
        )

        async def _raise(_self, _text):
            raise err

        monkeypatch.setattr(EmbeddingProvider, "_openai_embed", _raise)

        vec = await p.embed("first")
        assert vec.shape == (OPENAI_DIM,)
        assert p.degraded is True
        assert p.degrade_reason == "auth_invalid"

    @pytest.mark.asyncio
    async def test_success_after_transient_resets_counters(self, monkeypatch):
        p = _build_openai_provider(monkeypatch, fallback="hash", threshold=5)
        err = _make_http_status_error(429, '{"error": {"type": "rate_limit_exceeded"}}')
        good = np.ones(OPENAI_DIM, dtype=np.float32)
        seq = [err, err, good]

        async def _next(_self, _text):
            v = seq.pop(0)
            if isinstance(v, Exception):
                raise v
            return v

        monkeypatch.setattr(EmbeddingProvider, "_openai_embed", _next)

        with pytest.raises(httpx.HTTPStatusError):
            await p.embed("a")
        with pytest.raises(httpx.HTTPStatusError):
            await p.embed("b")
        assert p._consecutive_rate_limits == 2
        out = await p.embed("c")
        assert out.shape == (OPENAI_DIM,)
        assert p._consecutive_rate_limits == 0
        assert p.degraded is False


# ──────────────────────────────────────────────────────────────────
# EmbedQueue cooperation with degrade
# ──────────────────────────────────────────────────────────────────


class TestEmbedQueueDegradeBehaviour:
    """Queue must persist chunk text + throttle warnings on persistent failure."""

    @pytest.mark.asyncio
    async def test_queue_persists_chunk_when_provider_skips(self, chunks_db_path):
        embedder = MagicMock()
        embedder.embed = AsyncMock(side_effect=EmbeddingSkipped("primary degraded"))
        embedder.degraded = True
        embedder.provider_name = "openai"
        embedder.degrade_reason = "insufficient_quota"

        q = EmbedQueue(embedder)
        item = {
            "chunk_id": "c1", "text": "hello world",
            "source_table": "episodes", "source_id": "e1",
            "chunk_index": 0, "db_path": chunks_db_path,
        }
        await q._handle_item(item)

        conn = sqlite3.connect(chunks_db_path)
        row = conn.execute(
            "SELECT id, text_content, embedding FROM memory_chunks WHERE id = ?", ("c1",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[1] == "hello world"
        assert row[2] is None, "embedding must be NULL when chunk was skipped"
        assert q.stats["skipped"] == 1
        # provider was called exactly once — no retry storm under skip
        assert embedder.embed.await_count == 1

    @pytest.mark.asyncio
    async def test_queue_persists_chunk_when_provider_persistently_fails(self, chunks_db_path):
        embedder = MagicMock()
        embedder.embed = AsyncMock(side_effect=RuntimeError("openai down"))
        embedder.degraded = True
        embedder.provider_name = "openai"
        embedder.degrade_reason = "insufficient_quota"

        q = EmbedQueue(embedder)
        item = {
            "chunk_id": "c2", "text": "lorem ipsum",
            "source_table": "episodes", "source_id": "e2",
            "chunk_index": 0, "db_path": chunks_db_path,
        }
        await q._handle_item(item)

        conn = sqlite3.connect(chunks_db_path)
        row = conn.execute(
            "SELECT id, text_content, embedding FROM memory_chunks WHERE id = ?", ("c2",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[1] == "lorem ipsum"
        assert row[2] is None
        assert q.stats["failed"] == 1
        assert embedder.embed.await_count == 1, "degraded provider must not retry"

    @pytest.mark.asyncio
    async def test_queue_warning_throttled_under_persistent_failure(
        self, chunks_db_path, caplog, monkeypatch,
    ):
        monkeypatch.setenv("FERAL_EMBED_QUEUE_LOG_INTERVAL_S", "10000")
        embedder = MagicMock()
        embedder.embed = AsyncMock(side_effect=EmbeddingSkipped("degraded"))
        embedder.degraded = True
        embedder.provider_name = "openai"
        embedder.degrade_reason = "insufficient_quota"

        q = EmbedQueue(embedder)
        caplog.set_level(logging.WARNING, logger="feral.memory.embeddings")

        for i in range(25):
            item = {
                "chunk_id": f"c{i}", "text": f"text-{i}",
                "source_table": "episodes", "source_id": f"e{i}",
                "chunk_index": 0, "db_path": chunks_db_path,
            }
            await q._handle_item(item)

        skip_warnings = [
            r for r in caplog.records
            if "embed_queue_chunk_skipped" in r.getMessage()
        ]
        assert len(skip_warnings) == 1, (
            f"queue must throttle skip warnings; got {len(skip_warnings)} log records"
        )
        assert q.stats["skipped"] == 25

    @pytest.mark.asyncio
    async def test_queue_retries_transient_failure_when_not_degraded(self, chunks_db_path):
        good = np.ones(OPENAI_DIM, dtype=np.float32)
        embedder = MagicMock()
        embedder.embed = AsyncMock(side_effect=[RuntimeError("transient"), good])
        embedder.degraded = False
        embedder.provider_name = "openai"

        q = EmbedQueue(embedder)
        item = {
            "chunk_id": "c-retry", "text": "retry me",
            "source_table": "episodes", "source_id": "er",
            "chunk_index": 0, "db_path": chunks_db_path,
        }

        with patch("memory.embeddings.asyncio.sleep", new=AsyncMock(return_value=None)):
            await q._handle_item(item)

        assert embedder.embed.await_count == 2
        assert q.stats["succeeded"] == 1

        conn = sqlite3.connect(chunks_db_path)
        row = conn.execute(
            "SELECT embedding FROM memory_chunks WHERE id = ?", ("c-retry",),
        ).fetchone()
        conn.close()
        assert row is not None and row[0] is not None


# ──────────────────────────────────────────────────────────────────
# Fallback embed paths
# ──────────────────────────────────────────────────────────────────


class TestFallbackEmbed:
    """Hash fallback must match the primary's dimension so vec0 stays valid."""

    def test_hash_fallback_matches_primary_dim(self, monkeypatch):
        p = _build_openai_provider(monkeypatch, fallback="hash")
        vec = p._fallback_embed("anything")
        assert vec.shape == (OPENAI_DIM,)
        assert vec.dtype == np.float32

    def test_skip_fallback_raises(self, monkeypatch):
        p = _build_openai_provider(monkeypatch, fallback="skip")
        with pytest.raises(EmbeddingSkipped):
            p._fallback_embed("anything")

    def test_unknown_fallback_value_normalises_to_hash(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("FERAL_EMBED_FALLBACK", "totally-bogus")
        p = EmbeddingProvider()
        assert p.fallback_mode == "hash"
