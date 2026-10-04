"""Native first-use models must stay cache-only, including fallback loaders."""
import sys
import types

import pytest

from memory.embeddings import EmbeddingProvider, reset_local_backend_failures


@pytest.fixture(autouse=True)
def clean_failures():
    reset_local_backend_failures()
    yield
    reset_local_backend_failures()


@pytest.mark.parametrize("backend", ["fastembed", "sentence-transformers"])
@pytest.mark.parametrize("cached", [True, False])
def test_cache_only_passed_to_local_loader(monkeypatch, backend, cached):
    monkeypatch.setenv("FERAL_EMBED_MODEL_CACHE_ONLY", "1")
    calls = []
    model = object()

    def construct(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs["local_files_only"] is True
        if not cached:
            raise FileNotFoundError("No local model cache")
        return model

    module_name = "fastembed" if backend == "fastembed" else "sentence_transformers"
    module = types.ModuleType(module_name)
    setattr(module, "TextEmbedding" if backend == "fastembed" else "SentenceTransformer", construct)
    monkeypatch.setitem(sys.modules, module_name, module)
    provider = EmbeddingProvider.__new__(EmbeddingProvider)
    provider._model = None
    provider._fastembed_model = None
    provider._fastembed_unavailable = False
    load = provider._ensure_fastembed_model if backend == "fastembed" else provider._ensure_local_model
    assert load() is cached
    assert load() is cached
    assert len(calls) == 1, "Missing caches must not cause repeated model loads"


@pytest.mark.parametrize("backend", ["fastembed", "sentence-transformers"])
def test_other_launchers_keep_existing_download_policy(monkeypatch, backend):
    monkeypatch.delenv("FERAL_EMBED_MODEL_CACHE_ONLY", raising=False)
    calls = []
    module_name = "fastembed" if backend == "fastembed" else "sentence_transformers"
    module = types.ModuleType(module_name)

    def construct(*args, **kwargs):
        calls.append(kwargs)
        return object()

    setattr(module, "TextEmbedding" if backend == "fastembed" else "SentenceTransformer", construct)
    monkeypatch.setitem(sys.modules, module_name, module)
    provider = EmbeddingProvider.__new__(EmbeddingProvider)
    provider._model = None
    provider._fastembed_model = None
    provider._fastembed_unavailable = False
    load = provider._ensure_fastembed_model if backend == "fastembed" else provider._ensure_local_model
    assert load()
    assert len(calls) == 1
    assert "local_files_only" not in calls[0]
