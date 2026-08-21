"""The PDF skill must do what its manifest says about scanned pages.

The manifest states, twice, that a page with no embedded text layer
"comes back as the literal string '[OCR not available - install
pytesseract]' rather than as text", and tells the model to report that
plainly "instead of guessing at the page contents".

Two things were wrong:

1. `_ocr_page` returned `page.get_text(...)` unconditionally. For a
   scanned page that call answers `''` rather than raising, so the
   function returned an empty string and the OCR branch below it was
   unreachable for the exact case it exists to handle. pytesseract was
   never invoked even when installed, and the documented literal was
   never produced. Measured on a PDF drawn with shapes and no text
   layer: `get_text` -> `''`, `_ocr_page` -> `''`.

2. The implementation's literal used an em dash (U+2014) while the
   manifest promised an ASCII hyphen, so the two were not the same
   string.

A blank page is the worst of the three possible answers: the model
cannot tell "this page is empty" from "I could not read this page", and
the manifest text exists precisely to stop it guessing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz", reason="PyMuPDF is required for the PDF skill")

from skills.impl.pdf_reader import OCR_UNAVAILABLE, PDFReaderSkill  # noqa: E402

MANIFEST = Path(__file__).resolve().parent.parent / "skills" / "manifests" / "pdf_reader.json"


def _scanned_page():
    """A page with graphics and no text layer, like a scan."""
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(50, 50, 300, 200), color=(0, 0, 0), fill=(0.2, 0.2, 0.2))
    page.draw_circle(fitz.Point(200, 400), 80, color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
    return doc, page


def _text_page(body: str = "Quarterly revenue was up 12 percent."):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(fitz.Point(72, 100), body)
    return doc, page


class TestAScannedPageSaysSo:
    def test_it_does_not_come_back_blank(self):
        doc, page = _scanned_page()
        try:
            assert page.get_text("text") == "", "fixture is not text-less"
            result = PDFReaderSkill._ocr_page(page)
            assert result.strip(), (
                "a scanned page returned an empty string; the model cannot "
                "tell an empty page from an unreadable one"
            )
        finally:
            doc.close()

    def test_it_returns_the_documented_literal(self):
        """pytesseract is not installed here, which is the shipped default."""
        pytest.importorskip
        try:
            import pytesseract  # noqa: F401
            pytest.skip("pytesseract is installed; this asserts the absent case")
        except ImportError:
            pass
        doc, page = _scanned_page()
        try:
            assert PDFReaderSkill._ocr_page(page) == OCR_UNAVAILABLE
        finally:
            doc.close()


class TestARealTextPageIsUnaffected:
    def test_text_still_comes_back_as_text(self):
        doc, page = _text_page()
        try:
            assert "Quarterly revenue" in PDFReaderSkill._ocr_page(page)
        finally:
            doc.close()

    def test_a_whitespace_only_page_is_treated_as_unreadable(self):
        """A page of spaces carries no more information than a blank one.

        Spaces only: PyMuPDF renders a tab as a replacement glyph, which
        is a real character and would make this page legitimately
        non-empty.
        """
        doc, page = _text_page("      ")
        try:
            extracted = page.get_text("text")
            assert not extracted.strip(), f"fixture is not blank: {extracted!r}"
            assert PDFReaderSkill._ocr_page(page) == OCR_UNAVAILABLE
        finally:
            doc.close()


class TestTheManifestAndTheCodeAgree:
    def test_the_literal_is_byte_identical(self):
        """The manifest quotes this string, so it is a contract."""
        blob = json.dumps(json.loads(MANIFEST.read_text()))
        quoted = set(re.findall(r"'(\[OCR[^']*)'", blob))
        assert quoted, "the manifest no longer quotes the OCR literal"
        assert quoted == {OCR_UNAVAILABLE}, (
            f"manifest promises {quoted}, code returns {OCR_UNAVAILABLE!r}"
        )

    def test_the_literal_is_plain_ascii(self):
        """An em dash here is how the two copies drifted apart."""
        non_ascii = [c for c in OCR_UNAVAILABLE if ord(c) > 127]
        assert not non_ascii, f"non-ascii characters in the literal: {non_ascii}"
