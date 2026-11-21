"""Bounded immutable ingestion snapshots; all content is disposable test data."""
import hashlib
import pytest

from memory import ingest


class Memory:
    def __init__(self):
        self.notes = []
        self.compiles = 0

    async def save(self, **kwargs):
        self.notes.append(kwargs)

    async def wiki_compile(self):
        self.compiles += 1
        return {"compiled": True}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def test_snapshot_hash_and_symlink_refusal(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"disposable original")
    assert ingest.snapshot_file(str(path), 100, sha(b"disposable original")) == b"disposable original"
    with pytest.raises(ValueError):
        ingest.snapshot_file(str(path), 100, sha(b"changed"))
    link = tmp_path / "link.txt"
    link.symlink_to(path)
    with pytest.raises(OSError):
        ingest.snapshot_file(str(link), 100)
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        ingest.snapshot_file(str(parent_link / "file.txt"), 100)


def test_regular_and_byte_limits(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"12345")
    with pytest.raises(ValueError):
        ingest.snapshot_file(str(path), 4)
    with pytest.raises((ValueError, OSError)):
        ingest.snapshot_file(str(tmp_path), 100)


@pytest.mark.asyncio
async def test_repo_manifest_verified_before_any_notes(tmp_path):
    path = tmp_path / "fixture.py"
    path.write_bytes(b"print('disposable')\n")
    memory = Memory()
    worker = ingest.MemoryIngestor(memory)
    with pytest.raises(ValueError):
        await worker.ingest_repo(path=str(tmp_path), expected_files={"fixture.py": sha(b"wrong")})
    assert memory.notes == [] and memory.compiles == 0
    receipt = await worker.ingest_repo(path=str(tmp_path), compile_after=False, expected_files={"fixture.py": sha(path.read_bytes())})
    assert receipt["snapshot_files"] == {"fixture.py": sha(path.read_bytes())}
    assert receipt["files_processed"] == 1 and receipt["notes_saved"] > 0
    assert memory.compiles == 0


@pytest.mark.asyncio
async def test_repo_retains_captured_bytes_even_if_source_changes(tmp_path):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("first original")
    second.write_text("second original")
    memory = Memory()
    original_save = memory.save
    async def save(**kwargs):
        second.write_text("mutated after snapshot")
        await original_save(**kwargs)
    memory.save = save
    result = await ingest.MemoryIngestor(memory).ingest_repo(path=str(tmp_path), compile_after=False)
    assert result["snapshot_files"]["b.txt"] == sha(b"second original")
    assert any("second original" in note["content"] for note in memory.notes)
    assert not any("mutated after snapshot" in note["content"] for note in memory.notes)


@pytest.mark.asyncio
async def test_repo_file_limit_never_silently_truncates(tmp_path):
    (tmp_path / "a.txt").write_text("one")
    (tmp_path / "b.txt").write_text("two")
    memory = Memory()
    with pytest.raises(ValueError):
        await ingest.MemoryIngestor(memory).ingest_repo(path=str(tmp_path), max_files=1)
    assert memory.notes == []


@pytest.mark.asyncio
async def test_repo_symlink_and_oversize_are_all_or_none_preflight(tmp_path):
    (tmp_path / "a.txt").write_text("one")
    link = tmp_path / "b.txt"
    link.symlink_to(tmp_path / "a.txt")
    memory = Memory()
    with pytest.raises(ValueError):
        await ingest.MemoryIngestor(memory).ingest_repo(path=str(tmp_path))
    assert memory.notes == []
    link.unlink()
    link.write_bytes(b"a" * (ingest.MAX_REPO_FILE_BYTES + 1))
    with pytest.raises(ValueError):
        await ingest.MemoryIngestor(memory).ingest_repo(path=str(tmp_path))
    assert memory.notes == []


@pytest.mark.parametrize("limit", [0, -1, True, 101, "10"])
def test_repo_limit_is_strict(tmp_path, limit):
    with pytest.raises(ValueError):
        ingest.snapshot_repo(str(tmp_path), {".txt"}, limit)


def test_repo_traversal_entry_bound(tmp_path, monkeypatch):
    for i in range(4):
        (tmp_path / f"{i}.bin").write_bytes(b"ignored")
    monkeypatch.setattr(ingest, "MAX_REPO_ENTRIES", 3)
    with pytest.raises(ValueError):
        ingest.snapshot_repo(str(tmp_path), {".txt"}, 100)


@pytest.mark.asyncio
async def test_pdf_actual_stream_snapshot(tmp_path):
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    document.new_page().insert_text((72, 72), "Disposable native PDF fixture")
    raw = document.tobytes()
    document.close()
    path = tmp_path / "upload_without_extension"
    path.write_bytes(raw)
    memory = Memory()
    receipt = await ingest.MemoryIngestor(memory).ingest_pdf(path=str(path), filename="fixture.pdf", expected_sha256=sha(raw), compile_after=False)
    assert receipt["snapshot_sha256"] == sha(raw) and receipt["pages_read"] == 1
    assert any("Disposable native PDF fixture" in note["content"] for note in memory.notes)
    assert memory.compiles == 0


@pytest.mark.asyncio
async def test_pdf_page_and_text_bounds_before_writes(tmp_path, monkeypatch):
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    document.new_page().insert_text((72, 72), "Disposable bound fixture")
    document.new_page()
    path = tmp_path / "fixture.pdf"
    path.write_bytes(document.tobytes())
    document.close()
    memory = Memory()
    monkeypatch.setattr(ingest, "MAX_PDF_PAGES", 1)
    with pytest.raises(ValueError):
        await ingest.MemoryIngestor(memory).ingest_pdf(path=str(path))
    assert memory.notes == []
    monkeypatch.setattr(ingest, "MAX_PDF_PAGES", 100)
    monkeypatch.setattr(ingest, "MAX_EXTRACTED_BYTES", 1)
    with pytest.raises(ValueError):
        await ingest.MemoryIngestor(memory).ingest_pdf(path=str(path))
    assert memory.notes == []


def test_pdf_http_limits_and_reviewed_hash(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import memory as route
    from memory.uploads import UploadStore
    from types import SimpleNamespace
    memory = Memory()
    store = UploadStore(root=tmp_path / "uploads")
    monkeypatch.setattr(route, "state", SimpleNamespace(memory=memory, uploads=store))
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    response = client.post("/api/wiki/ingest/pdf", files={"file": ("fixture.pdf", b"%PDF-disposable", "application/pdf")}, data={"expected_sha256": "0" * 64})
    assert response.status_code == 409 and store.stats()["count"] == 0
    monkeypatch.setattr(ingest, "MAX_PDF_BYTES", 10)
    response = client.post("/api/wiki/ingest/pdf", files={"file": ("fixture.pdf", b"%PDF-disposable", "application/pdf")})
    assert response.status_code == 413 and store.stats()["count"] == 0
    assert memory.notes == []
