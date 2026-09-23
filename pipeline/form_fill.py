"""Overlay text, checkmarks and a signature onto flat, non-interactive forms: scanned pages (one
embedded image per page, an OCR text layer, no AcroForm fields, no vector table lines) as
`prepare`/`ocr.py` produce them, or a born-digital layout PDF with no fillable fields either.

Built after several form-filling sessions (DLZP-SH Doc 275, Kienemann Doc 398, HAW-Kiel
Personalnachweis Doc 397 - see LOCAL.md) kept re-deriving the same three things by hand, each
one via several rounds of fill -> render -> eyeball -> adjust: where a label sits (read out
`page.get_text('words')` for a y-range and guess), where a scanned table's row lines are
(`page.get_drawings()` returns nothing - the whole page is one image, there is no vector line to
find), and how small a font has to be to fit one line without touching the next column (guess a
size, render, see it collide, guess smaller). This module makes each of those one function call
instead of a manual round trip.

Nothing here talks to Paperless or knows about any specific form's fields - that stays in the
calling script/session. `pipeline.client.update_version` uploads the result once it looks right.
"""
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image

from pipeline.pdf_tools import render_page

DEFAULT_FONT = "helv"
MIN_LEGIBLE_SIZE = 5.5  # below this, wrap to a second line instead of shrinking further


def find_label(page: pymupdf.Page, text: str, case_sensitive: bool = False) -> pymupdf.Rect | None:
    """Locates a label by substring match against the page's words in reading order (the OCR
    text layer on a scanned page, or the real text layer on a born-digital one), and returns the
    tight union rect of the matching run of words, or None if not found. Use this instead of
    hardcoding coordinates read off one render - a bundle that gets re-split or re-OCR'd shifts
    every y-coordinate, and this module exists so that doesn't mean redoing the layout by hand.

    OCR on a real scan is not perfect (a checkbox glyph fuses into the next word - '☐ m' reads
    as 'Om', '☐ ledig' as '[ledig'; umlauts sometimes come out as a replacement character) so a
    literal label string can still fail to match. This is a best-effort lookup, not a guarantee:
    always render and look at the result before trusting a placement (see `preview`)."""
    words = page.get_text("words")  # (x0, y0, x1, y1, word, block, line, word_no)
    haystack = text if case_sensitive else text.lower()
    needle_words = haystack.split()
    if not needle_words:
        return None
    n = len(needle_words)
    for i in range(len(words) - n + 1):
        candidate = [w[4] if case_sensitive else w[4].lower() for w in words[i : i + n]]
        if candidate == needle_words:
            group = words[i : i + n]
            x0 = min(w[0] for w in group)
            y0 = min(w[1] for w in group)
            x1 = max(w[2] for w in group)
            y1 = max(w[3] for w in group)
            return pymupdf.Rect(x0, y0, x1, y1)
    return None


def text_width(text: str, size: float, fontname: str = DEFAULT_FONT) -> float:
    return pymupdf.get_text_length(text, fontname=fontname, fontsize=size)


def fit_font_size(
    text: str,
    max_width: float,
    fontname: str = DEFAULT_FONT,
    start: float = 9.0,
    min_size: float = MIN_LEGIBLE_SIZE,
    step: float = 0.1,
) -> float:
    """Largest size <= start (in `step` decrements) whose rendered width fits max_width points.
    Never returns below min_size even if it still doesn't fit - a single insert_text call has no
    way to wrap on its own, so at that point the caller should split the text across two
    place_text calls on separate lines instead of shrinking further (below ~5.5pt a form entry
    reads as a smudge, not text - this was the actual mistake in an early pass on Doc 397: a
    long university name was shrunk to 5.5pt to force it onto one line instead of just being
    given a second line at a normal size)."""
    size = start
    while size > min_size and text_width(text, size, fontname) > max_width:
        size = round(size - step, 2)
    return max(size, min_size)


def place_text(
    page: pymupdf.Page,
    x: float,
    y: float,
    text: str,
    size: float = 8.0,
    max_width: float | None = None,
    fontname: str = DEFAULT_FONT,
    color: tuple[float, float, float] = (0, 0, 0),
) -> float:
    """Inserts `text` with its baseline at (x, y) in PDF points. If `max_width` is given, `size`
    is instead the starting size handed to `fit_font_size` and the size actually used is
    returned, so a caller can decide there to wrap instead of accepting a too-small result:

        used = place_text(page, x, y, long_text, size=8, max_width=col_width)
        if used <= form_fill.MIN_LEGIBLE_SIZE:
            ...place_text on two lines at a normal size instead...

    Without max_width, `size` is used as given and the return value equals it."""
    if max_width is not None:
        size = fit_font_size(text, max_width, fontname=fontname, start=size)
    page.insert_text((x, y), text, fontsize=size, fontname=fontname, color=color)
    return size


def mark_checkbox(page: pymupdf.Page, x: float, y: float, size: float = 9.0, mark: str = "X") -> None:
    """Marks a checkbox at (x, y) (baseline). A thin wrapper over place_text purely for call
    sites to read as 'this is a checkbox' - see `find_label`'s docstring for how to locate the
    checkbox's x via the OCR word it got fused into, and nudge left a few points off the fused
    word's x0 to land inside the box rather than on the first letter of the label."""
    page.insert_text((x, y), mark, fontsize=size, fontname=DEFAULT_FONT, color=(0, 0, 0))


def detect_row_lines(
    pdf_path: Path,
    page_index: int,
    x_range_pt: tuple[float, float],
    y_range_pt: tuple[float, float],
    dpi: int = 200,
    dark_threshold: int = 200,
    min_dark_frac: float = 0.3,
    merge_gap_px: int = 3,
) -> list[float]:
    """Finds a scanned table's horizontal gridlines within y_range_pt, by rendering the page and
    looking for image rows within x_range_pt that are mostly dark pixels - `page.get_drawings()`
    returns nothing here, since the whole page is one embedded raster with no vector paths to
    find. Returns the gridlines' y-coordinates in PDF points (72/inch), sorted; adjacent hit rows
    are collapsed into one point per line (a printed rule is a few pixels thick, not one).

    A page that is a genuinely blank/pale scan, or an x_range that lands mostly on a text row
    instead of a gridline, can both return an empty or noisy list - sanity-check the result
    against how many rows the rendered form actually shows before trusting it, the same way
    `AGENTS.md` says to render a low-confidence page before trusting its extracted text.

    `dark_threshold=200` (not near-black) is deliberate: a printed rule antialiased into a 200
    DPI render is mid-grey, not pure black, and a stricter threshold (150 was tried first) misses
    real gridlines entirely rather than just being noisier. `merge_gap_px` absorbs the 1-2 px dip
    in the middle of a rule that the antialiasing/JPEG-artifact of a scan produces, which
    otherwise reports one gridline as two adjacent points a pixel apart."""
    image = render_page(pdf_path, page_index, dpi=dpi)
    scale = dpi / 72.0
    x0, x1 = (int(v * scale) for v in x_range_pt)
    y0, y1 = (int(v * scale) for v in y_range_pt)
    gray = np.array(image.convert("L"))
    region = gray[y0:y1, x0:x1]
    if region.size == 0:
        return []
    dark_frac = (region < dark_threshold).mean(axis=1)
    hit_rows = np.nonzero(dark_frac >= min_dark_frac)[0]
    if hit_rows.size == 0:
        return []
    lines_px = []
    run = [hit_rows[0]]
    for row in hit_rows[1:]:
        if row - run[-1] <= merge_gap_px:
            run.append(row)
        else:
            lines_px.append(sum(run) / len(run))
            run = [row]
    lines_px.append(sum(run) / len(run))
    return sorted((y0 + px) / scale for px in lines_px)


def insert_signature(
    page: pymupdf.Page,
    x: float,
    y_baseline: float,
    signature_path: Path,
    height_pt: float = 40.0,
) -> pymupdf.Rect:
    """Places a signature image with its bottom-left at (x, y_baseline), scaled to exactly
    `height_pt` tall with its own aspect ratio preserved. Computes the target rect itself from
    the image's real pixel dimensions instead of taking a hand-picked pymupdf.Rect and passing
    `keep_proportion=True` to insert_image: that silently clamps to whichever of the rect's
    width/height is tighter, so a rect that is a little too narrow for the intended height comes
    out with a much shorter signature than asked for and nothing raises to say so (the first
    signature placed on Doc 397 was ~8mm tall this way; the fix was ~14mm). Returns the rect
    actually used, in case the caller wants to log or double check it against the row height
    available above the line it's meant to sit on."""
    with Image.open(signature_path) as img:
        aspect = img.width / img.height
    width_pt = height_pt * aspect
    rect = pymupdf.Rect(x, y_baseline - height_pt, x + width_pt, y_baseline)
    page.insert_image(rect, filename=str(signature_path))
    return rect


def preview(pdf_path: Path, page_indices: list[int], out_dir: Path, dpi: int = 200) -> list[Path]:
    """Renders the given 0-indexed pages to <out_dir>/page_<n>.png and returns their paths - the
    fill -> render -> look -> adjust loop this whole module exists to shorten to one call instead
    of a fresh render script each time. Read the returned files with the Read tool to actually
    look at them; this function only produces them."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in page_indices:
        image = render_page(pdf_path, i, dpi=dpi)
        out_path = out_dir / f"page_{i}.png"
        image.save(out_path)
        paths.append(out_path)
    return paths
