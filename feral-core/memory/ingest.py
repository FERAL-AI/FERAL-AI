"""
FERAL Memory Ingest Pipeline
=============================
Utilities for bulk ingestion into notes + wiki compile.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable
import hashlib
import os
import re
import stat

from memory.embeddings import chunk_text

DEFAULT_REPO_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".yaml", ".yml", ".md", ".txt",
    ".sh", ".toml", ".ini", ".cfg", ".sql", ".go", ".rs", ".java", ".swift", ".kt",
    ".html", ".css",
}

DEFAULT_IGNORED_DIRS = {
    ".git", ".idea", ".vscode", "__pycache__", "node_modules", "dist", "build", ".next",
    ".venv", "venv", ".mypy_cache", ".pytest_cache", ".cursor",
}

MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 100
MAX_EXTRACTED_BYTES = 1024 * 1024
MAX_REPO_FILES = 100
MAX_REPO_ENTRIES = 2000
MAX_REPO_DEPTH = 16
MAX_REPO_FILE_BYTES = 80_000
MAX_REPO_BYTES = 8 * 1024 * 1024


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _check_digest(raw: bytes, expected: str | None) -> None:
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or _digest(raw) != expected):
        raise ValueError("Reviewed file hash no longer matches; review again")


def _open_directory(path: Path) -> int:
    """Open each component without following symlinks, retaining the final fd."""
    absolute = Path(os.path.abspath(path.expanduser()))
    fd = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_file_at(parent: int, name: str, limit: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("Input is not a regular file within the byte limit")
        parts = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            parts.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(parts)
        after = os.fstat(fd)
        def signature(item):
            return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if len(raw) > limit or signature(before) != signature(after) or len(raw) != before.st_size:
            raise ValueError("File changed during snapshot; review again")
        return raw
    finally:
        os.close(fd)


def snapshot_file(path: str, limit: int, expected_sha256: str | None = None) -> bytes:
    value = Path(path).expanduser()
    fd = _open_directory(value.parent)
    try:
        raw = _read_file_at(fd, value.name, limit)
    finally:
        os.close(fd)
    _check_digest(raw, expected_sha256)
    return raw


def snapshot_repo(path: str, extensions: set[str], max_files: int, expected_files: dict[str, str] | None = None) -> tuple[list[tuple[str, bytes]], int]:
    if isinstance(max_files, bool) or not isinstance(max_files, int) or not 1 <= max_files <= MAX_REPO_FILES:
        raise ValueError(f"max_files must be between 1 and {MAX_REPO_FILES}")
    if expected_files is not None and (not isinstance(expected_files, dict) or len(expected_files) > max_files or any(not isinstance(k, str) or not isinstance(v, str) for k, v in expected_files.items())):
        raise ValueError("Reviewed manifest is malformed or exceeds the file limit")
    root = _open_directory(Path(path))
    captured: list[tuple[str, bytes]] = []
    entries = skipped = total = 0
    def walk(fd: int, prefix: str, depth: int) -> None:
        nonlocal entries, skipped, total
        before = os.fstat(fd)
        # Bound directory enumeration itself instead of materializing an unbounded list.
        with os.scandir(fd) as iterator:
            names = []
            for entry in iterator:
                entries += 1
                if entries > MAX_REPO_ENTRIES:
                    raise ValueError("Repository traversal exceeds the entry limit")
                names.append(entry.name)
        for name in sorted(names):
            meta = os.stat(name, dir_fd=fd, follow_symlinks=False)
            rel = prefix + name
            if stat.S_ISLNK(meta.st_mode):
                raise ValueError("Repository contains symlinks; no notes were imported")
            if stat.S_ISDIR(meta.st_mode):
                if name in DEFAULT_IGNORED_DIRS:
                    continue
                if depth >= MAX_REPO_DEPTH:
                    raise ValueError("Repository exceeds the directory depth limit")
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    walk(child, rel + "/", depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(meta.st_mode) and Path(name).suffix.lower() in extensions:
                if len(captured) >= max_files:
                    raise ValueError("Repository exceeds the selected file limit; choose a smaller folder")
                raw = _read_file_at(fd, name, MAX_REPO_FILE_BYTES)
                if not _is_text_bytes(raw):
                    skipped += 1
                    continue
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError:
                    skipped += 1
                    continue
                if not raw.strip():
                    skipped += 1
                    continue
                total += len(raw)
                if total > MAX_REPO_BYTES:
                    raise ValueError("Repository exceeds the aggregate byte limit")
                captured.append((rel, raw))
            else:
                skipped += 1
        after = os.fstat(fd)
        if before.st_mtime_ns != after.st_mtime_ns or before.st_ctime_ns != after.st_ctime_ns:
            raise ValueError("Directory changed during snapshot; review again")
    try:
        walk(root, "", 0)
    finally:
        os.close(root)
    actual = {name: _digest(raw) for name, raw in captured}
    if expected_files is not None and actual != expected_files:
        raise ValueError("Reviewed folder manifest no longer matches; review again")
    return captured, skipped


def _is_text_bytes(raw: bytes) -> bool:
    if b"\x00" in raw:
        return False
    return True


def _read_text(path: Path, max_chars: int = 60_000) -> str:
    raw = path.read_bytes()
    if not _is_text_bytes(raw):
        return ""
    text = raw.decode("utf-8", errors="ignore").strip()
    if not text:
        return ""
    return text[:max_chars]


class MemoryIngestor:
    def __init__(self, memory_store):
        self.memory = memory_store

    async def _save_chunks(self, text: str, *, source: str, tags: Iterable[str]) -> int:
        chunks = chunk_text(text, max_tokens=350, overlap=70)
        count = 0
        for i, chunk in enumerate(chunks):
            payload = chunk.strip()
            if not payload:
                continue
            await self.memory.save(
                content=payload,
                tags=list(tags),
                importance="normal",
                source=f"{source}:chunk_{i + 1}",
            )
            count += 1
        return count

    async def ingest_text(self, *, content: str, source_label: str = "manual", compile_after: bool = True) -> dict:
        payload = (content or "").strip()
        if not payload:
            raise ValueError("content is required")

        notes_saved = await self._save_chunks(
            payload,
            source=f"ingest:text:{source_label}",
            tags=["ingest", "text"],
        )
        compile_result = (await self.memory.wiki_compile()) if compile_after else {"compiled": False}
        return {
            "ok": True,
            "source": "text",
            "source_label": source_label,
            "notes_saved": notes_saved,
            "compile": compile_result,
        }

    async def ingest_pdf(self, *, path: str, compile_after: bool = True, expected_sha256: str | None = None, filename: str | None = None) -> dict:
        pdf_path = Path(path).expanduser()
        label = filename or pdf_path.name
        if Path(label).suffix.lower() != ".pdf":
            raise ValueError("path must point to a .pdf file")
        raw = snapshot_file(path, MAX_PDF_BYTES, expected_sha256)
        if not raw.startswith(b"%PDF-"):
            raise ValueError("Input does not have a PDF header")
        try:
            import fitz  # PyMuPDF
        except ImportError as e:
            raise ValueError("PyMuPDF is required for PDF ingest (`pip install pymupdf`)") from e

        pages: list[str] = []
        doc = fitz.open(stream=raw, filetype="pdf")
        try:
            if doc.page_count > MAX_PDF_PAGES or doc.needs_pass:
                raise ValueError("PDF exceeds the page limit or requires a password")
            extracted = 0
            for i in range(doc.page_count):
                page = doc.load_page(i)
                page_text = (page.get_text("text") or "").strip()
                if page_text:
                    extracted += len(page_text.encode("utf-8"))
                    if extracted > MAX_EXTRACTED_BYTES:
                        raise ValueError("PDF exceeds the extracted text limit")
                    pages.append(f"--- Page {i + 1} ---\n{page_text}")
        finally:
            doc.close()

        merged = "\n\n".join(pages).strip()
        if not merged:
            raise ValueError("PDF has no extractable text")

        notes_saved = await self._save_chunks(
            merged,
            source=f"ingest:pdf:{label}",
            tags=["ingest", "pdf"],
        )
        compile_result = (await self.memory.wiki_compile()) if compile_after else {"compiled": False}
        return {
            "ok": True,
            "source": "pdf",
            "path": str(pdf_path),
            "pages_read": len(pages),
            "snapshot_sha256": _digest(raw),
            "notes_saved": notes_saved,
            "compile": compile_result,
        }

    async def ingest_repo(
        self,
        *,
        path: str,
        extensions_filter: list[str] | None = None,
        compile_after: bool = True,
        max_files: int = MAX_REPO_FILES,
        expected_files: dict[str, str] | None = None,
    ) -> dict:
        root = Path(path).expanduser()
        if extensions_filter is not None and (not isinstance(extensions_filter, list) or len(extensions_filter) > 40 or any(not isinstance(ext, str) or not re.fullmatch(r"\.?[A-Za-z0-9]{1,12}", ext) for ext in extensions_filter)):
            raise ValueError("Extension filter is malformed")
        allowed = {ext if ext.startswith(".") else f".{ext}" for ext in (extensions_filter or DEFAULT_REPO_EXTENSIONS)}
        captured, files_skipped = snapshot_repo(path, {ext.lower() for ext in allowed}, max_files, expected_files)
        if not captured:
            raise ValueError("Folder has no supported text files")
        files_processed = len(captured)
        notes_saved = 0
        for rel, raw in captured:
            text = raw.decode("utf-8").strip()
            file_blob = f"# File: {rel}\n\n{text}"
            notes_saved += await self._save_chunks(
                file_blob,
                source=f"ingest:repo:{rel}",
                tags=["ingest", "repo"],
            )

        compile_result = (await self.memory.wiki_compile()) if compile_after else {"compiled": False}
        return {
            "ok": True,
            "source": "repo",
            "path": str(root),
            "files_processed": files_processed,
            "files_skipped": files_skipped,
            "snapshot_files": {name: _digest(raw) for name, raw in captured},
            "notes_saved": notes_saved,
            "compile": compile_result,
        }
