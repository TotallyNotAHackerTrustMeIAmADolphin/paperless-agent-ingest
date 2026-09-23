"""Overlay text, checkmarks and a signature onto flat, non-interactive forms: scanned pages (one
embedded image per page, an OCR text layer, no AcroForm fields, no vector table lines) as
`prepare`/`ocr.py` produce them, or a born-digital layout PDF with no fillable fields either.

Built after repeated form-filling sessions (see LOCAL.md for the instance-specific ones) kept
re-deriving the same things by hand, each via several rounds of fill -> render -> eyeball ->
adjust: where a label sits (read out `page.get_text('words')` for a y-range and guess), where a
scanned table's gridlines are (`page.get_drawings()` returns nothing - the whole page is one
image, there is no vector line to find), how small a font has to be to fit one line without
touching the next column (guess a size, render, see it collide, guess smaller), and how to
position a value inside a cell without it landing on a gridline or drifting off-center. This
module makes each of those one function call instead of a manual round trip.

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


def find_label(
    page: pymupdf.Page,
    text: str,
    case_sensitive: bool = False,
    after_y: float = 0.0,
) -> pymupdf.Rect | None:
    """Locates a label by substring match against the page's words in reading order (the OCR
    text layer on a scanned page, or the real text layer on a born-digital one), and returns the
    tight union rect of the matching run of words, or None if not found. Use this instead of
    hardcoding coordinates read off one render - a bundle that gets re-split or re-OCR'd shifts
    every y-coordinate, and this module exists so that doesn't mean redoing the layout by hand.

    Returns the FIRST match in reading order at or below `after_y`. A short, common label is not
    automatically unique: searching a page for a column header like 'Von' can silently match the
    same word inside a different, unrelated sentence higher up the page (ordinary text like "...
    Niveau von A1 ...") and place an entire table's worth of values under the wrong header, with
    no error - it is valid text, just not the label being looked for. Prefer the longest
    substring that is still exactly what's printed (e.g. 'Abschluss,' with its trailing comma
    rather than 'Abschluss'), and pass `after_y` (typically the bottom of a nearby, unambiguous
    label you already found) to disambiguate a short/common one instead of guessing it worked.

    OCR on a real scan is not perfect (a checkbox glyph fuses into the next word - '☐ m' reads
    as 'Om', '☐ ledig' as '[ledig'; umlauts sometimes come out as a replacement character) so a
    literal label string can still fail to match. This is a best-effort lookup, not a guarantee:
    always render and look at the result before trusting a placement (see `preview`)."""
    words = [w for w in page.get_text("words") if w[1] >= after_y]  # (x0,y0,x1,y1,word,block,line,word_no)
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
    reads as a smudge, not text - shrinking a long value that far just to force it onto one line
    is the wrong trade; give it a second line at a normal size instead)."""
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


def _detect_lines(
    pdf_path: Path,
    page_index: int,
    x_range_pt: tuple[float, float],
    y_range_pt: tuple[float, float],
    axis: int,
    dpi: int,
    dark_threshold: int,
    min_dark_frac: float,
    merge_gap_px: int,
) -> list[float]:
    """Shared implementation for detect_row_lines (axis=1: dark image ROWS -> horizontal lines,
    coordinates returned along y) and detect_col_lines (axis=0: dark image COLUMNS -> vertical
    lines, coordinates returned along x)."""
    image = render_page(pdf_path, page_index, dpi=dpi)
    scale = dpi / 72.0
    x0, x1 = (int(v * scale) for v in x_range_pt)
    y0, y1 = (int(v * scale) for v in y_range_pt)
    gray = np.array(image.convert("L"))
    region = gray[y0:y1, x0:x1]
    if region.size == 0:
        return []
    dark_frac = (region < dark_threshold).mean(axis=axis)
    hits = np.nonzero(dark_frac >= min_dark_frac)[0]
    if hits.size == 0:
        return []
    lines_px = []
    run = [hits[0]]
    for v in hits[1:]:
        if v - run[-1] <= merge_gap_px:
            run.append(v)
        else:
            lines_px.append(sum(run) / len(run))
            run = [v]
    lines_px.append(sum(run) / len(run))
    origin_px, scale_origin = (y0, scale) if axis == 1 else (x0, scale)
    return sorted((origin_px + px) / scale_origin for px in lines_px)


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
    otherwise reports one gridline as two adjacent points a pixel apart.

    See `detect_col_lines` for the vertical counterpart - together, a table's row lines crossed
    with its column lines give every cell's full box (all four walls), which `fill_centered` can
    then fill without needing a hand-picked offset from a label at all."""
    return _detect_lines(
        pdf_path, page_index, x_range_pt, y_range_pt, 1, dpi, dark_threshold, min_dark_frac, merge_gap_px
    )


def detect_col_lines(
    pdf_path: Path,
    page_index: int,
    x_range_pt: tuple[float, float],
    y_range_pt: tuple[float, float],
    dpi: int = 200,
    dark_threshold: int = 200,
    min_dark_frac: float = 0.3,
    merge_gap_px: int = 3,
) -> list[float]:
    """Vertical counterpart to `detect_row_lines`: finds a scanned table's vertical gridlines
    within x_range_pt by looking for image columns within y_range_pt that are mostly dark pixels.
    Returns x-coordinates in PDF points, sorted. Same parameters and same reasoning for the
    defaults - see `detect_row_lines`'s docstring."""
    return _detect_lines(
        pdf_path, page_index, x_range_pt, y_range_pt, 0, dpi, dark_threshold, min_dark_frac, merge_gap_px
    )


def _wrap_text(text: str, max_width: float, size: float, fontname: str = DEFAULT_FONT) -> list[str]:
    """Greedy word wrap: as many words per line as fit max_width at the given size. A single word
    wider than max_width on its own is still placed alone on its line rather than split."""
    words = text.split()
    if not words:
        return []
    lines = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if text_width(trial, size, fontname) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def fill_centered(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    text: str,
    fontname: str = DEFAULT_FONT,
    max_size: float = 9.0,
    min_size: float = MIN_LEGIBLE_SIZE,
    padding: float = 3.0,
    line_spacing: float = 1.15,
    color: tuple[float, float, float] = (0, 0, 0),
) -> float:
    """Fills `text` into a fully-known cell `rect` (all four walls - e.g. one row of
    `detect_row_lines` crossed with one column of `detect_col_lines`), centered both horizontally
    and vertically, at the largest font size up to `max_size` that fits. Returns the font size
    actually used.

    This exists because placing text from one corner outward (what `place_text` does, given only
    a label's position and a guessed offset) has to get that offset right for every cell shape
    and font size - in practice that took several rounds of fixes to stop text sitting on a
    gridline or crossing into the row below it. Centering in a rect whose bounds are all
    known needs no such offset: shrink the rect by `padding` on every side, fit the largest
    single line that stays inside it, and place it so equal space remains on every side. If even
    `min_size` doesn't fit on one line, wraps at `min_size` instead and centers the whole block of
    lines vertically - the same "shrink first, wrap only once you must" order as `fit_font_size`.

    The vertical placement is an approximation (PDF text is positioned by its baseline, not a
    centered bounding box, and exact ascent/descent depend on the font) tuned for Helvetica at
    ordinary form-entry sizes; render and look before trusting it on an unusual font or a very
    short/tall cell."""
    usable = pymupdf.Rect(rect.x0 + padding, rect.y0 + padding, rect.x1 - padding, rect.y1 - padding)
    if usable.width <= 0 or usable.height <= 0:
        raise ValueError(f"{rect} is too small for padding={padding} on every side")
    size = fit_font_size(text, usable.width, fontname=fontname, start=max_size, min_size=min_size)
    lines = [text]
    if text_width(text, size, fontname) > usable.width:
        size = min_size
        lines = _wrap_text(text, usable.width, size, fontname)
    line_height = size * line_spacing
    block_height = line_height * len(lines)
    block_top = usable.y0 + max(usable.height - block_height, 0) / 2
    for i, line in enumerate(lines):
        w = text_width(line, size, fontname)
        x = usable.x0 + (usable.width - w) / 2
        baseline_y = block_top + line_height * (i + 1) - size * 0.25
        page.insert_text((x, baseline_y), line, fontsize=size, fontname=fontname, color=color)
    return size


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
    out with a much shorter signature than asked for and nothing raises to say so (an early
    attempt this way came out at roughly half the intended height with no error to flag it).
    Returns the rect actually used, in case the caller wants to log or double check it against
    the row height available above the line it's meant to sit on."""
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
