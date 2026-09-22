"""OCR and text extraction, used both to make PDFs searchable and to get text for classification
(title/correspondent/tags/merge decisions)."""
import shutil
from pathlib import Path

import ocrmypdf
import pymupdf
import pypdf

from pipeline.config import OCR_LANGUAGES as LANGUAGES

MIN_CHARS_PER_PAGE = 20  # below this, a page's existing text layer (if any) is not usable
PAGE_MARKER = "--- Page {n} of {total} ---"


def needs_ocr(pdf_path: Path) -> bool:
    """False for born-digital PDFs (emailed invoices, contracts, ...) that already carry a
    real text layer - those don't need scanning-artifact cleanup, and re-running OCR on a
    Tagged PDF discards its structural markup for no benefit."""
    reader = pypdf.PdfReader(pdf_path)
    if not reader.pages:
        return False
    total_chars = sum(len((page.extract_text() or "").strip()) for page in reader.pages)
    return (total_chars / len(reader.pages)) < MIN_CHARS_PER_PAGE


def run_ocr(pdf_path: Path, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not needs_ocr(pdf_path):
        shutil.copy(pdf_path, out_path)
        return out_path
    ocrmypdf.ocr(
        pdf_path,
        out_path,
        language=LANGUAGES,
        skip_text=True,  # keep any existing text layer, only OCR image-only pages
        deskew=True,  # only affects the pages actually OCR'd (skip_text leaves the rest alone)
        # No rotate_pages: orientation is fixed before this step by pdf_tools.fix_pdf, whose
        # OCR-confidence-validated check is more reliable than tesseract's OSD score alone.
        progress_bar=False,
    )
    return out_path


def extract_text(pdf_path: Path) -> str:
    """Pull the (by now OCR'd) text layer out as plain text with an explicit marker line before
    every page. The markers are what make a bundled scan splittable by reading: page N of the
    text is page index N-1 for `pdf_tools.split_pdf`/`assemble_pages`, no guessing where one
    page ends. (markitdown was used before 2026-09-22; it produced nicer tables but no page
    boundaries at all, which made a 54-page bundle a per-page pypdf excavation.)"""
    import re

    parts = []
    with pymupdf.open(pdf_path) as doc:
        total = len(doc)
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text", sort=True)
            # Keep column alignment but not the 80-space runs layout extraction pads with.
            lines = [re.sub(r" {4,}", "   ", line.rstrip()) for line in text.splitlines()]
            text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
            parts.append(f"{PAGE_MARKER.format(n=i, total=total)}\n{text}\n")
    return "\n".join(parts)
