"""PR 10: wiki/ingest/pdf accepts multipart, upload_id, OR JSON path.

The web composer ships ``multipart/form-data`` with a ``file`` part.
Before PR 10 the route only accepted JSON ``{"path": "..."}`` which
made the wiki ingestion silently broken for every real client.

This test pins the three accepted input shapes and asserts that an
input with NONE of them returns 400 (no silent success)."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def wiki_client(tmp_path):
    from api.routes import memory as memory_module
    from memory.uploads import UploadStore

    store = UploadStore(root=tmp_path / "uploads")

    class _FakeIngestor:
        last_path = None

        def __init__(self, _memory):
            pass

        async def ingest_pdf(self, *, path, compile_after=True, expected_sha256=None, filename=None):
            _FakeIngestor.last_path = path
            return {"ingested": path, "compile_after": compile_after}

    class _State:
        memory = MagicMock()
        uploads = store

    with patch.object(memory_module, "state", _State()), \
         patch.object(memory_module, "MemoryIngestor", _FakeIngestor):
        app = FastAPI()
        app.include_router(memory_module.router)
        yield TestClient(app, raise_server_exceptions=False), store, _FakeIngestor


def test_multipart_file_is_stored_and_ingested(wiki_client):
    client, store, ingestor = wiki_client
    resp = client.post(
        "/api/wiki/ingest/pdf",
        files={"file": ("a.pdf", io.BytesIO(b"%PDF-1.4 minimal"), "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    assert ingestor.last_path  # stored, ingested
    assert "uploads" in ingestor.last_path
    assert store.stats()["count"] == 1


def test_upload_id_route_uses_stored_record(wiki_client):
    client, store, ingestor = wiki_client
    rec = store.store(data=b"%PDF stub", filename="pre.pdf", content_type="application/pdf")
    resp = client.post(
        "/api/wiki/ingest/pdf",
        data={"upload_id": rec.upload_id, "compile_after": "true"},
    )
    assert resp.status_code == 200
    assert ingestor.last_path == rec.path


def test_upload_id_unknown_returns_404(wiki_client):
    client, _store, _ingestor = wiki_client
    resp = client.post(
        "/api/wiki/ingest/pdf",
        data={"upload_id": "no-such-id"},
    )
    assert resp.status_code == 404


def test_no_input_returns_400(wiki_client):
    """Pre-PR 10 this route silently 200'd via the legacy JSON body
    even when ``path`` was empty (the ingestor would then fail
    deeper). Now the route refuses up front."""
    client, _store, _ingestor = wiki_client
    resp = client.post("/api/wiki/ingest/pdf")
    assert resp.status_code == 400
    assert "Provide" in resp.json()["detail"]
