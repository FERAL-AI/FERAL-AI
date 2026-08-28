"""Provider precedence: local-first, paid only when explicitly asked for.

The regression these pin down is a billing one. Until v2026.6
``_detect_provider`` selected OpenAI whenever ``OPENAI_API_KEY`` existed in
the environment, so a key exported for chat completions turned every memory
write into a paid API call nobody asked for. Measured before the fix: a plain
``pytest tests/test_memory.py`` with a key exported made real POSTs to
``api.openai.com`` from 11 tests.

Every test here constructs providers only, never embeds over the network, so
the run must stay at zero outbound calls under
``FERAL_STRICT_TEST_NETWORK=1``.
"""
from __future__ import annotations

import importlib.util
import sys
import types

import numpy as np
import pytest

from memory.embeddings import (
    LOCAL_DIM,
    OPENAI_DIM,
    EmbeddingDimensionMismatch,
    EmbeddingProvider,
    VectorIndex,
    cosine_similarity,
    reset_local_backend_failures,
    sqlite_vec_available,
)

DUMMY_KEY = "sk-dummy-not-a-real-key"


@pytest.fixture(autouse=True)
def _clean_embed_env(monkeypatch):
    """Start every test from a known env.

    ``monkeypatch.delenv`` on an already-absent name registers no undo, which
    is exactly the leak the conftest env guard reports, so set-then-delete is
    not used here: delenv with ``raising=False`` plus monkeypatch's own
    restore is enough because each test sets what it needs explicitly.
    """
    monkeypatch.delenv("FERAL_EMBED_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FERAL_EMBED_FALLBACK", raising=False)


def _spec_faker(available: set[str]):
    """Stand-in for ``importlib.util.find_spec`` that reports only ``available``.

    ``_detect_provider`` probes with ``find_spec`` rather than importing, so
    faking it is enough to exercise every branch of the local chain on a
    machine that has neither backend installed.
    """
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name in ("fastembed", "sentence_transformers"):
            return object() if name in available else None
        return real_find_spec(name, *args, **kwargs)

    return fake_find_spec


# ──────────────────────────────────────────────────────────────────
# The money regression
# ──────────────────────────────────────────────────────────────────


class TestKeyPresenceIsNotConsent:
    def test_key_set_without_explicit_provider_never_selects_openai(self, monkeypatch):
        """THE regression. A key in the environment must not spend money."""
        monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
        p = EmbeddingProvider()
        assert p.provider_name != "openai"
        assert p.provider_mode == "auto"
        assert p.dimension == LOCAL_DIM

    def test_key_set_with_auto_never_selects_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "auto")
        assert EmbeddingProvider().provider_name != "openai"

    def test_key_set_with_local_never_selects_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "local")
        p = EmbeddingProvider()
        assert p.provider_name != "openai"
        assert p.dimension == LOCAL_DIM

    def test_auto_prefers_fastembed_over_openai_even_with_key(self, monkeypatch):
        """Local wins outright: it is not a fallback for a missing key."""
        monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
        monkeypatch.setattr(
            importlib.util, "find_spec", _spec_faker({"fastembed", "sentence_transformers"})
        )
        assert EmbeddingProvider().provider_name == "fastembed"


# ──────────────────────────────────────────────────────────────────
# FERAL_EMBED_PROVIDER precedence
# ──────────────────────────────────────────────────────────────────


class TestProviderModes:
    def test_explicit_openai_with_key_selects_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "openai")
        p = EmbeddingProvider()
        assert p.provider_name == "openai"
        assert p.dimension == OPENAI_DIM

    def test_explicit_openai_without_key_degrades_quietly(self, monkeypatch, caplog):
        """Missing key is an operator error to report, not a crash.

        Embedding runs on a background queue; raising here would take memory
        writes down for a misconfiguration that has a working fallback.
        """
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "openai")
        monkeypatch.setattr(importlib.util, "find_spec", _spec_faker(set()))
        with caplog.at_level("WARNING", logger="feral.memory.embeddings"):
            p = EmbeddingProvider()
        assert p.provider_name != "openai"
        assert p.provider_name == "hash"
        assert p.dimension == LOCAL_DIM
        assert any("OPENAI_API_KEY" in r.message for r in caplog.records)

    def test_explicit_openai_without_key_still_takes_local_if_present(self, monkeypatch):
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "openai")
        monkeypatch.setattr(importlib.util, "find_spec", _spec_faker({"fastembed"}))
        assert EmbeddingProvider().provider_name == "fastembed"

    def test_auto_picks_fastembed_when_importable(self, monkeypatch):
        monkeypatch.setattr(
            importlib.util, "find_spec", _spec_faker({"fastembed", "sentence_transformers"})
        )
        p = EmbeddingProvider()
        assert p.provider_name == "fastembed"
        assert p.dimension == LOCAL_DIM

    def test_auto_picks_sentence_transformers_when_only_that_is_importable(self, monkeypatch):
        monkeypatch.setattr(
            importlib.util, "find_spec", _spec_faker({"sentence_transformers"})
        )
        p = EmbeddingProvider()
        assert p.provider_name == "sentence_transformers"
        assert p.dimension == LOCAL_DIM

    def test_auto_falls_to_hash_when_neither_importable(self, monkeypatch):
        monkeypatch.setattr(importlib.util, "find_spec", _spec_faker(set()))
        p = EmbeddingProvider()
        assert p.provider_name == "hash"
        assert p.dimension == LOCAL_DIM

    def test_local_mode_follows_the_same_chain(self, monkeypatch):
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "local")
        monkeypatch.setattr(importlib.util, "find_spec", _spec_faker({"sentence_transformers"}))
        assert EmbeddingProvider().provider_name == "sentence_transformers"

    def test_hash_mode_forces_hash_even_with_local_backends_available(self, monkeypatch):
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "hash")
        monkeypatch.setattr(
            importlib.util, "find_spec", _spec_faker({"fastembed", "sentence_transformers"})
        )
        p = EmbeddingProvider()
        assert p.provider_name == "hash"
        assert p.dimension == LOCAL_DIM

    def test_unknown_mode_warns_and_uses_auto(self, monkeypatch, caplog):
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "gpt-please")
        monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
        with caplog.at_level("WARNING", logger="feral.memory.embeddings"):
            p = EmbeddingProvider()
        assert p.provider_mode == "auto"
        assert p.provider_name != "openai"
        assert any("FERAL_EMBED_PROVIDER" in r.message for r in caplog.records)

    def test_mode_is_case_and_whitespace_insensitive(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "  OpenAI ")
        assert EmbeddingProvider().provider_name == "openai"


class TestExistingBehaviourPreserved:
    """The degrade/fallback surface must not move under the new precedence."""

    def test_fallback_mode_still_read_and_validated(self, monkeypatch):
        monkeypatch.setenv("FERAL_EMBED_FALLBACK", "skip")
        assert EmbeddingProvider().fallback_mode == "skip"

        monkeypatch.setenv("FERAL_EMBED_FALLBACK", "nonsense")
        assert EmbeddingProvider().fallback_mode == "hash"

    def test_hash_provider_produces_local_dim_vectors(self, monkeypatch):
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "hash")
        p = EmbeddingProvider()
        vec = p._hash_embed("some text", p.dimension)
        assert vec.shape == (LOCAL_DIM,)
        assert vec.dtype == np.float32

    def test_openai_fallback_hash_still_matches_openai_dim(self, monkeypatch):
        """Fallback vectors must keep the index's shape, whatever the primary."""
        monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "openai")
        monkeypatch.setenv("FERAL_EMBED_FALLBACK", "hash")
        p = EmbeddingProvider()
        assert p._fallback_embed("anything").shape == (OPENAI_DIM,)

    def test_embed_sync_returns_none_for_hash_and_openai(self, monkeypatch):
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "hash")
        assert EmbeddingProvider().embed_sync("hello") is None

        monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "openai")
        assert EmbeddingProvider().embed_sync("hello") is None

    def test_embed_sync_does_not_construct_a_model(self, monkeypatch):
        """fastembed is sync-capable, but a sync caller must never trigger the
        model download. No model loaded yet means None, not a load."""
        monkeypatch.setattr(importlib.util, "find_spec", _spec_faker({"fastembed"}))
        p = EmbeddingProvider()
        assert p.provider_name == "fastembed"
        assert p._fastembed_model is None
        assert p.embed_sync("hello") is None
        assert p._fastembed_model is None

    @pytest.mark.asyncio
    async def test_hash_mode_embeds_without_network(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "hash")
        p = EmbeddingProvider()
        vec = await p.embed("no network here")
        assert vec.shape == (LOCAL_DIM,)
        batch = await p.embed_batch(["a", "b"])
        assert [v.shape for v in batch] == [(LOCAL_DIM,), (LOCAL_DIM,)]

    @pytest.mark.asyncio
    async def test_auto_with_key_embeds_without_network(self, monkeypatch):
        """End to end: a key exported, nothing configured, no HTTP call made.

        This is the acceptance shape of the whole change. Under
        FERAL_STRICT_TEST_NETWORK=1 an outbound call raises; without it the
        conftest guard still records the attempt in its end-of-run report.
        """
        monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
        monkeypatch.setattr(importlib.util, "find_spec", _spec_faker(set()))
        p = EmbeddingProvider()
        vec = await p.embed("nothing should leave this machine")
        assert vec.shape == (LOCAL_DIM,)


class TestFastembedLoaderIsQuietWhenMissing:
    @pytest.fixture(autouse=True)
    def _isolate_backend_memo(self):
        """Keep the process-wide load memo out of these tests, and them
        out of it.

        Clearing before means the assertions are about the loader rather
        than about which test ran first. Clearing after means the fake
        always-failing backends installed below cannot leave a recorded
        failure that makes a later test skip a load it should attempt.
        """
        reset_local_backend_failures()
        yield
        reset_local_backend_failures()

    def test_missing_fastembed_warns_once_not_per_call(self, monkeypatch, caplog):
        """_fallback_embed can hit the loader on every embed of a degrade
        window, so a failed import must not log per call."""
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "hash")
        p = EmbeddingProvider()
        with caplog.at_level("WARNING", logger="feral.memory.embeddings"):
            results = [p._ensure_fastembed_model() for _ in range(5)]
        if any(results):
            pytest.skip("fastembed is installed in this environment")
        assert results == [False] * 5
        assert sum("fastembed lazy load failed" in r.message for r in caplog.records) == 1

    def test_a_failed_load_is_not_retried_by_the_next_provider(self, monkeypatch):
        """The second provider in a process must not re-pay the load.

        Constructing a local backend blocks for the full model-download
        timeout when the package is importable but the model is not
        cached (~39s on a CI runner). Caching that failure per instance
        meant every new EmbeddingProvider paid it again, which is what
        pushed the CI matrix job past its timeout: 50 stalls, 33 of the
        job's 45 minutes.
        """
        attempts = {"n": 0}

        fake = types.ModuleType("fastembed")

        def _TextEmbedding(*_a, **_k):
            attempts["n"] += 1
            raise RuntimeError("Could not load model from any source")

        fake.TextEmbedding = _TextEmbedding
        monkeypatch.setitem(sys.modules, "fastembed", fake)
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "hash")

        assert EmbeddingProvider()._ensure_fastembed_model() is False
        assert attempts["n"] == 1

        # Four more providers, each asking several times.
        for _ in range(4):
            p = EmbeddingProvider()
            assert [p._ensure_fastembed_model() for _ in range(3)] == [False] * 3

        assert attempts["n"] == 1, (
            f"the loader ran {attempts['n']} times; a failed load must be "
            "remembered for the whole process, not per provider"
        )

    def test_a_failed_sentence_transformers_load_is_not_retried_per_call(
        self, monkeypatch
    ):
        """Same property for the sentence-transformers backend.

        This one had no negative caching at all: a failed load left
        ``self._model`` as None, which is exactly the condition that
        triggers a retry, so it re-ran the constructor on every embed.
        """
        attempts = {"n": 0}

        fake = types.ModuleType("sentence_transformers")

        def _SentenceTransformer(*_a, **_k):
            attempts["n"] += 1
            raise RuntimeError("hub unreachable")

        fake.SentenceTransformer = _SentenceTransformer
        monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
        monkeypatch.setenv("FERAL_EMBED_PROVIDER", "hash")

        for _ in range(3):
            p = EmbeddingProvider()
            for _ in range(5):
                assert p._ensure_local_model() is False

        assert attempts["n"] == 1, (
            f"the loader ran {attempts['n']} times; it must run once per "
            "process, not once per embed"
        )


# ──────────────────────────────────────────────────────────────────
# Dimension safety
# ──────────────────────────────────────────────────────────────────


class TestDimensionMismatch:
    """Switching provider changes vector width (1536 vs 384).

    There is no re-embedding migration in this codebase, so the pinned
    behaviour is: detect, say so loudly, and refuse, rather than silently
    return nothing (which is what the numpy scan callers did, since they all
    wrap the loop in ``except Exception: logger.debug(...)``).
    """

    def test_cosine_similarity_raises_on_mismatch(self):
        a = np.ones(LOCAL_DIM, dtype=np.float32)
        b = np.ones(OPENAI_DIM, dtype=np.float32)
        with pytest.raises(EmbeddingDimensionMismatch):
            cosine_similarity(a, b)

    def test_mismatch_error_is_a_valueerror(self):
        """np.dot already raised ValueError for this, so callers that catch
        ValueError keep behaving the way they did."""
        assert issubclass(EmbeddingDimensionMismatch, ValueError)

    def test_mismatch_is_logged_at_error_level(self, caplog):
        import memory.embeddings as emb

        emb._REPORTED_DIM_MISMATCHES.clear()
        a = np.ones(7, dtype=np.float32)
        b = np.ones(9, dtype=np.float32)
        with caplog.at_level("ERROR", logger="feral.memory.embeddings"):
            with pytest.raises(EmbeddingDimensionMismatch):
                cosine_similarity(a, b)
        assert any("embedding_dimension_mismatch" in r.message for r in caplog.records)

    def test_mismatch_logs_once_per_pair_not_once_per_row(self, caplog):
        """The mismatch is found inside per-row scan loops over every stored
        vector, so the log has to be collapsed or it emits thousands of lines."""
        import memory.embeddings as emb

        emb._REPORTED_DIM_MISMATCHES.clear()
        a = np.ones(11, dtype=np.float32)
        b = np.ones(13, dtype=np.float32)
        with caplog.at_level("ERROR", logger="feral.memory.embeddings"):
            for _ in range(50):
                with pytest.raises(EmbeddingDimensionMismatch):
                    cosine_similarity(a, b)
        assert sum("embedding_dimension_mismatch" in r.message for r in caplog.records) == 1

    def test_matching_dimensions_are_unaffected(self):
        a = np.ones(LOCAL_DIM, dtype=np.float32)
        assert abs(cosine_similarity(a, a) - 1.0) < 1e-6

    def test_declared_dim_parses_vec0_ddl(self):
        """The on-disk dimension lives only in sqlite_master.sql; no pragma
        reports it back."""
        sql = (
            "CREATE VIRTUAL TABLE vec_chunks USING vec0("
            "chunk_id TEXT PRIMARY KEY, embedding FLOAT[1536])"
        )
        assert VectorIndex._declared_dim(sql) == OPENAI_DIM
        assert VectorIndex._declared_dim("CREATE TABLE t (a INT)") is None
        assert VectorIndex._declared_dim(None) is None
        assert VectorIndex._declared_dim("embedding float [ 384 ]") == LOCAL_DIM

    @pytest.mark.skipif(
        not sqlite_vec_available(),
        reason="sqlite-vec not installed; vec0 tables cannot be created here",
    )
    def test_stale_vec0_table_is_refused(self, tmp_path):
        """CREATE VIRTUAL TABLE IF NOT EXISTS is a silent no-op against a table
        of another width, so the mismatch has to be detected explicitly."""
        db = str(tmp_path / "vec.db")
        first = VectorIndex(db, dimension=OPENAI_DIM, table_name="vec_x")
        assert first.indexed
        assert first.dim_mismatch is None

        second = VectorIndex(db, dimension=LOCAL_DIM, table_name="vec_x")
        assert second.indexed is False
        assert second.dim_mismatch == (OPENAI_DIM, LOCAL_DIM)

        # Refusing means no writes and no results, not wrong results.
        second.upsert("c1", np.ones(LOCAL_DIM, dtype=np.float32))
        assert second.search(np.ones(LOCAL_DIM, dtype=np.float32)) == []


class TestExtensionAvailabilityIsNotConnectionScoped:
    """``_SQLITE_VEC_AVAILABLE`` answers "can this interpreter load the
    sqlite-vec extension". It used to be demoted to False by a failure on
    ONE connection, and the demotion is permanent for the process.

    The failure that reaches it in practice is not an unloadable
    extension at all. ``sqlite3`` raises
    ``ProgrammingError: SQLite objects created in a thread can only be
    used in that same thread`` when a connection crosses threads, and
    FERAL is a threaded FastAPI app whose memory store owns a background
    embed-queue thread and dispatches through ``asyncio.to_thread``. One
    such touch turned vector search off process-wide for the rest of the
    brain's life, with only a debug-level trace, on an interpreter where
    the extension loads perfectly.
    """

    @pytest.mark.skipif(
        not sqlite_vec_available(),
        reason="sqlite-vec not installed; nothing to demote",
    )
    def test_a_cross_thread_connection_does_not_disable_the_extension(self, tmp_path):
        import sqlite3
        import threading

        import memory.embeddings as embeddings_module

        assert sqlite_vec_available() is True, "premise: it loads here"

        # A connection created here, used from another thread: exactly the
        # ProgrammingError the broad handler used to read as "unavailable".
        conn = sqlite3.connect(str(tmp_path / "crossthread.db"))
        errors: list[BaseException] = []

        def _touch():
            try:
                embeddings_module._try_load_sqlite_vec(conn)
            except BaseException as exc:  # noqa: BLE001 - recorded, then asserted
                errors.append(exc)

        thread = threading.Thread(target=_touch)
        thread.start()
        thread.join()
        conn.close()
        assert not errors, errors

        assert sqlite_vec_available() is True, (
            "one cross-thread connection permanently disabled sqlite-vec for "
            "the whole process"
        )
        # And a fresh index still gets a real vec0 table.
        index = VectorIndex(
            str(tmp_path / "after.db"), dimension=LOCAL_DIM, table_name="vec_after"
        )
        assert index.indexed is True, (
            "vector search stayed off after an unrelated connection failed"
        )
