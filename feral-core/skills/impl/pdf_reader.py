"""
FERAL PDF Reader Skill
=======================
Production-grade PDF extraction: layout-preserving text, tables as
markdown, embedded images as base64, OCR fallback, and metadata.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any, Dict, List

from skills.base import BaseSkill
from skills.impl import register_skill

logger = logging.getLogger("feral.skill.pdf")

MAX_CHARS = 200_000
MAX_PAGES = 1000


#: Returned for a scanned page when OCR is not installed.
#:
#: The manifest quotes this literal twice and instructs the model to
#: report it verbatim rather than guess at the page contents, so it is a
#: contract, not a log line. It lives here as one constant because it
#: previously existed as two divergent copies: the implementation
#: returned an em dash and the manifest promised an ASCII hyphen.
OCR_UNAVAILABLE = "[OCR not available - install pytesseract]"


def _get_fitz():
    try:
        import fitz  # PyMuPDF
        return fitz
    except ImportError as e:
        raise RuntimeError(
            "PyMuPDF is required for pdf_reader. Install it with: pip install pymupdf"
        ) from e


@register_skill
class PDFReaderSkill(BaseSkill):
    def __init__(self):
        super().__init__(skill_id="pdf_reader")

    async def execute(self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str]) -> Dict[str, Any]:
        _ = vault
        dispatch = {
            "extract_text": self._handle_extract,
            "extract": self._handle_extract,
            "metadata": self._handle_metadata,
            "extract_images": self._handle_extract_images,
        }
        handler = dispatch.get(endpoint_id)
        if not handler:
            return {
                "success": False,
                "status_code": 404,
                "data": None,
                "error": f"Unknown endpoint: {endpoint_id}",
            }
        return await handler(args)

    # ── endpoint handlers ─────────────────────────────────────────

    async def _handle_extract(self, args: dict) -> dict:
        path, err = self._resolve_path(args)
        if err:
            return err

        page_start = max(1, int(args.get("page_start", 1) or 1))
        page_end = int(args.get("page_end", 0) or 0)
        max_chars = max(1_000, min(int(args.get("max_chars", 60_000) or 60_000), MAX_CHARS))
        include_images = str(args.get("include_images", "false")).lower() in ("true", "1", "yes")

        try:
            data = await asyncio.to_thread(
                self._extract_rich,
                path=path,
                page_start=page_start,
                page_end=page_end,
                max_chars=max_chars,
                include_images=include_images,
            )
            return {"success": True, "status_code": 200, "data": data, "error": None}
        except Exception as e:
            return {"success": False, "status_code": 500, "data": None, "error": str(e)}

    async def _handle_metadata(self, args: dict) -> dict:
        path, err = self._resolve_path(args)
        if err:
            return err
        try:
            data = await asyncio.to_thread(self._extract_metadata, path)
            return {"success": True, "status_code": 200, "data": data, "error": None}
        except Exception as e:
            return {"success": False, "status_code": 500, "data": None, "error": str(e)}

    async def _handle_extract_images(self, args: dict) -> dict:
        path, err = self._resolve_path(args)
        if err:
            return err
        page_start = max(1, int(args.get("page_start", 1) or 1))
        page_end = int(args.get("page_end", 0) or 0)
        max_images = min(int(args.get("max_images", 10) or 10), 50)
        try:
            data = await asyncio.to_thread(
                self._extract_all_images, path, page_start, page_end, max_images
            )
            return {"success": True, "status_code": 200, "data": data, "error": None}
        except Exception as e:
            return {"success": False, "status_code": 500, "data": None, "error": str(e)}

    # ── path validation ───────────────────────────────────────────

    @staticmethod
    def _resolve_path(args: dict) -> tuple[Path, dict | None]:
        raw_path = str(args.get("path", "")).strip()
        if not raw_path:
            return Path(), {"success": False, "status_code": 400, "data": None, "error": "path is required"}
        path = Path(raw_path).expanduser()
        if not path.exists():
            return path, {"success": False, "status_code": 404, "data": None, "error": f"File not found: {path}"}
        if not path.is_file():
            return path, {"success": False, "status_code": 400, "data": None, "error": f"Not a file: {path}"}
        return path, None

    # ── rich extraction (main flow) ───────────────────────────────

    @staticmethod
    def _extract_rich(
        path: Path,
        page_start: int,
        page_end: int,
        max_chars: int,
        include_images: bool = False,
    ) -> dict:
        fitz = _get_fitz()
        doc = fitz.open(str(path))
        try:
            total_pages = int(doc.page_count)
            if total_pages > MAX_PAGES:
                doc.close()
                raise ValueError(
                    f"PDF has {total_pages} pages, exceeding the {MAX_PAGES}-page limit. "
                    "Split the document or increase MAX_PAGES."
                )
            if total_pages == 0:
                return {
                    "path": str(path), "total_pages": 0, "pages_read": 0,
                    "start_page": 0, "end_page": 0, "truncated": False,
                    "text": "", "tables": [], "images": [],
                }

            start_idx = min(max(page_start - 1, 0), total_pages - 1)
            end_idx = (total_pages - 1) if page_end <= 0 else min(max(page_end - 1, start_idx), total_pages - 1)

            text_parts: list[str] = []
            all_tables: list[str] = []
            all_images: list[dict] = []
            char_count = 0
            truncated = False
            pages_read = 0

            for i in range(start_idx, end_idx + 1):
                page = doc.load_page(i)

                structured = PDFReaderSkill._extract_structured(page)
                if not structured or len(structured.strip()) < 20:
                    structured = PDFReaderSkill._ocr_page(page)

                block = f"\n\n--- Page {i + 1} ---\n{structured}".strip()

                tables = PDFReaderSkill._extract_tables(page)
                if tables:
                    block += "\n\n" + "\n\n".join(tables)
                    all_tables.extend(tables)

                if include_images:
                    imgs = PDFReaderSkill._extract_images(page, doc)
                    for img in imgs:
                        img["page"] = i + 1
                    all_images.extend(imgs)

                remaining = max_chars - char_count
                if remaining <= 0:
                    truncated = True
                    break
                if len(block) > remaining:
                    text_parts.append(block[:remaining])
                    char_count += remaining
                    pages_read += 1
                    truncated = True
                    break
                text_parts.append(block)
                char_count += len(block)
                pages_read += 1

            return {
                "path": str(path),
                "total_pages": total_pages,
                "pages_read": pages_read,
                "start_page": start_idx + 1,
                "end_page": end_idx + 1,
                "truncated": truncated,
                "text": "".join(text_parts).strip(),
                "tables": all_tables,
                "images": all_images[:20],
            }
        finally:
            doc.close()

    # ── layout-preserving text extraction ─────────────────────────

    @staticmethod
    def _extract_structured(page: Any) -> str:
        """Extract text preserving layout structure."""
        try:
            blocks = page.get_text("dict")["blocks"]
        except Exception:
            return page.get_text("text") or ""

        text_parts: list[str] = []
        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                line_text = "".join(s["text"] for s in spans)
                if spans and spans[0].get("size", 12) > 16:
                    line_text = f"## {line_text}"
                elif spans and spans[0].get("size", 12) > 14:
                    line_text = f"### {line_text}"
                text_parts.append(line_text)
            text_parts.append("")
        return "\n".join(text_parts)

    # ── table extraction ──────────────────────────────────────────

    @staticmethod
    def _extract_tables(page: Any) -> List[str]:
        """Extract tables from a page as markdown."""
        tables: list[str] = []
        try:
            found = page.find_tables()
            for table in found.tables:
                rows = table.extract()
                if not rows:
                    continue
                header = rows[0]
                md = "| " + " | ".join(str(c or "") for c in header) + " |\n"
                md += "| " + " | ".join("---" for _ in header) + " |\n"
                for row in rows[1:]:
                    md += "| " + " | ".join(str(c or "") for c in row) + " |\n"
                tables.append(md)
        except Exception:
            pass
        return tables

    # ── image extraction ──────────────────────────────────────────

    @staticmethod
    def _extract_images(page: Any, doc: Any, max_images: int = 5) -> List[dict]:
        """Extract embedded images as base64 PNG."""
        fitz = _get_fitz()
        images: list[dict] = []
        for img_info in page.get_images()[:max_images]:
            xref = img_info[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.n > 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                img_bytes = pix.tobytes("png")
                if len(img_bytes) < 2_000_000:
                    images.append({
                        "format": "png",
                        "width": pix.width,
                        "height": pix.height,
                        "base64": base64.b64encode(img_bytes).decode(),
                    })
            except Exception:
                continue
        return images

    # ── OCR fallback ──────────────────────────────────────────────

    @staticmethod
    def _ocr_page(page: Any) -> str:
        """OCR a page that has no extractable text.

        The first attempt is a plain text extraction, because a page can
        reach here with a text layer the structured parser did not like.
        It only counts when it returns something: ``get_text`` answers
        ``''`` for a scanned page rather than raising, so returning its
        result unconditionally meant the OCR branch below was
        unreachable for the exact case this method exists to handle.

        Measured on a PDF page drawn with shapes and no text layer:
        ``get_text`` returned ``''`` and ``_ocr_page`` returned ``''``.
        pytesseract was never called even when installed, and the
        documented "OCR not available" literal was never produced. The
        model received a blank page and, per the manifest's own warning,
        was left to guess at its contents.
        """
        fitz = _get_fitz()
        try:
            text = page.get_text(
                "text",
                flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_DEHYPHENATE,
            )
        except Exception:
            text = ""
        if text and text.strip():
            return text

        try:
            import pytesseract
            from PIL import Image

            pix = page.get_pixmap(dpi=300)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            return pytesseract.image_to_string(img)
        except ImportError:
            return OCR_UNAVAILABLE
        except Exception:
            # pytesseract imports fine without the tesseract binary and
            # raises TesseractNotFoundError only when asked to run. The
            # manifest names both requirements, so both failures report
            # the same thing rather than surfacing a stack trace as page
            # content.
            logger.warning("OCR failed for a scanned page", exc_info=True)
            # ASCII hyphen, not an em dash. The manifest documents this
            # exact literal twice and tells the model to report it
            # verbatim instead of guessing at the page contents, so the
            # two strings have to be byte-identical. They were not: this
            # returned U+2014 while the manifest promised '-'.
            return OCR_UNAVAILABLE

    # ── metadata extraction ───────────────────────────────────────

    @staticmethod
    def _extract_metadata(path: Path) -> dict:
        fitz = _get_fitz()
        doc = fitz.open(str(path))
        try:
            meta = doc.metadata or {}
            return {
                "title": meta.get("title", ""),
                "author": meta.get("author", ""),
                "subject": meta.get("subject", ""),
                "creator": meta.get("creator", ""),
                "producer": meta.get("producer", ""),
                "creation_date": meta.get("creationDate", ""),
                "mod_date": meta.get("modDate", ""),
                "page_count": len(doc),
                "keywords": meta.get("keywords", ""),
            }
        finally:
            doc.close()

    # ── bulk image extraction across pages ────────────────────────

    @staticmethod
    def _extract_all_images(
        path: Path, page_start: int, page_end: int, max_images: int
    ) -> dict:
        fitz = _get_fitz()
        doc = fitz.open(str(path))
        try:
            total_pages = int(doc.page_count)
            start_idx = min(max(page_start - 1, 0), total_pages - 1)
            end_idx = (total_pages - 1) if page_end <= 0 else min(max(page_end - 1, start_idx), total_pages - 1)

            all_images: list[dict] = []
            for i in range(start_idx, end_idx + 1):
                if len(all_images) >= max_images:
                    break
                page = doc.load_page(i)
                per_page = max_images - len(all_images)
                imgs = PDFReaderSkill._extract_images(page, doc, max_images=per_page)
                for img in imgs:
                    img["page"] = i + 1
                all_images.extend(imgs)

            return {
                "path": str(path),
                "total_pages": total_pages,
                "image_count": len(all_images),
                "images": all_images,
            }
        finally:
            doc.close()
