"""Local, no-network PDF mechanics: blank-page detection, rotation, split/merge."""
import io
from pathlib import Path

import pikepdf
import pymupdf
import pytesseract
from pdf2image import convert_from_path
from PIL import Image

RENDER_DPI = 150
BLANK_INK_THRESHOLD = 0.003  # fraction of dark pixels below which a page counts as blank
LOW_TEXT_CONFIDENCE_THRESHOLD = 60  # mean tesseract word confidence below which OCR is unreliable


def render_page(pdf_path: Path, page_number: int, dpi: int = RENDER_DPI) -> Image.Image:
    """page_number is 0-indexed."""
    images = convert_from_path(
        str(pdf_path), dpi=dpi, first_page=page_number + 1, last_page=page_number + 1
    )
    return images[0]


def ink_coverage(image: Image.Image) -> float:
    """Fraction of pixels darker than mid-gray, as a proxy for how much content is on the page."""
    gray = image.convert("L")
    histogram = gray.histogram()
    dark_pixels = sum(histogram[:128])
    total_pixels = gray.width * gray.height
    return dark_pixels / total_pixels


def is_blank_page(image: Image.Image, threshold: float = BLANK_INK_THRESHOLD) -> bool:
    return ink_coverage(image) < threshold


def _mean_ocr_confidence(image: Image.Image) -> float:
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    confidences = [int(c) for c in data["conf"] if int(c) >= 0]
    return sum(confidences) / len(confidences) if confidences else 0.0


def detect_rotation(image: Image.Image) -> int:
    """Returns the clockwise degrees (0/90/180/270) the page needs to be rotated by to be
    upright. Tesseract's OSD orientation guess is unreliable on its own (its confidence score
    does not cleanly separate correct from incorrect guesses on sparse/logo-heavy pages) so the
    guess is only accepted if actually rotating the image improves mean OCR word confidence.
    Requires the 'osd' language data."""
    try:
        osd = pytesseract.image_to_osd(image, config="--psm 0")
    except pytesseract.TesseractError:
        return 0
    degrees = 0
    for line in osd.splitlines():
        if line.startswith("Rotate:"):
            degrees = int(line.split(":")[1].strip())
    if not degrees:
        return 0
    conf_before = _mean_ocr_confidence(image)
    conf_after = _mean_ocr_confidence(image.rotate(-degrees, expand=True))
    return degrees if conf_after > conf_before else 0


def page_count(pdf_path: Path) -> int:
    with pikepdf.open(pdf_path) as pdf:
        return len(pdf.pages)


def analyze_pdf(pdf_path: Path) -> list[dict]:
    """Per-page analysis: [{'page': i, 'blank': bool, 'rotation': int, 'text_confidence':
    float | None, 'low_confidence': bool}, ...].

    `text_confidence` is tesseract's mean per-word confidence for the page (None for blank
    pages, where it's meaningless). A page can look non-blank and still OCR badly - faint
    dot-matrix invoice print is read fine on the bold letterhead but silently drops the actual
    line items/amounts, leaving a page that still contains real (if incomplete and misleading)
    text rather than an error. `low_confidence=True` is the flag for that: don't assume a
    sparse-seeming or repetitive-looking page is genuinely blank/boilerplate/a duplicate copy
    without rendering and visually checking it first - low OCR confidence text can look
    superficially plausible while silently missing the actual content."""
    # Rendered one page at a time: rendering a whole 50+ page scan at 150 DPI in one
    # convert_from_path call holds every page bitmap in memory at once (~10 MB each).
    results = []
    for i in range(page_count(pdf_path)):
        image = render_page(pdf_path, i)
        blank = is_blank_page(image)
        confidence = None if blank else _mean_ocr_confidence(image)
        results.append(
            {
                "page": i,
                "blank": blank,
                "rotation": detect_rotation(image),
                "text_confidence": confidence,
                "low_confidence": confidence is not None and confidence < LOW_TEXT_CONFIDENCE_THRESHOLD,
            }
        )
    return results


def fix_pdf(pdf_path: Path, out_path: Path, page_analysis: list[dict]) -> int:
    """Drop blank pages and apply detected rotation to the rest. Returns pages kept."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    keep = [a for a in page_analysis if not a["blank"]]
    if page_analysis and not keep:
        raise ValueError(
            f"{pdf_path}: every page was classified blank (ink threshold "
            f"{BLANK_INK_THRESHOLD}) - refusing to write a 0-page PDF. Render the pages and "
            "check them visually; a real document should never lose every page here."
        )
    with pikepdf.open(pdf_path) as pdf:
        drop_indices = {a["page"] for a in page_analysis if a["blank"]}
        for i in sorted(drop_indices, reverse=True):
            del pdf.pages[i]
        for new_index, a in enumerate(keep):
            if a["rotation"]:
                page = pdf.pages[new_index]
                page.Rotate = (int(page.get("/Rotate", 0)) + a["rotation"]) % 360
        pdf.save(out_path)
    return len(keep)


def merge_pdfs(paths: list[Path], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.new() as merged:
        for path in paths:
            with pikepdf.open(path) as src:
                merged.pages.extend(src.pages)
        merged.save(out_path)


def assemble_pages(
    pdf_path: Path, page_indices: list[int], out_path: Path, rotations: dict[int, int] | None = None
) -> Path:
    """Build a new PDF from an arbitrary, possibly-reordered subset of pages of one source PDF.
    Used to fix scans where the pages are in the wrong order, or a single scan actually bundles
    multiple unrelated documents that need to become separate Paperless documents.
    `rotations` maps source page index -> additional clockwise degrees to apply."""
    rotations = rotations or {}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.open(pdf_path) as src, pikepdf.new() as dest:
        for src_index in page_indices:
            dest.pages.append(src.pages[src_index])
        for new_index, src_index in enumerate(page_indices):
            degrees = rotations.get(src_index)
            if degrees:
                page = dest.pages[new_index]
                page.Rotate = (int(page.get("/Rotate", 0)) + degrees) % 360
        dest.save(out_path)
    return out_path


def crop_page(
    pdf_path: Path,
    page_number: int,
    box: tuple[float, float, float, float],
    rotate: int = 0,
    dpi: int = 300,
) -> Image.Image:
    """Render one page and crop+rotate it in raster space. `box` is (left, top, right, bottom)
    as fractions of the rendered page (0..1 each) rather than pixels or points, so a caller can
    say "left half" as (0, 0, 0.5, 1) without knowing the page's pixel size. `rotate` is
    clockwise degrees applied after the crop (same convention as `fix_pdf`/`assemble_pages`).

    For recovering multiple logical A4 pages out of one oversized (A3) scan: a scanner fed at
    the wrong tray size, or a folded certificate/booklet scanned open flat, produces one large
    page that is actually two or more real pages side by side or stacked. There is no vector
    way to do this safely for a scanned (image + OCR text layer) PDF - shrinking a page's
    /MediaBox only changes what is *displayed*, the existing OCR words for the other half stay
    in the content stream and still get extracted as text. Cropping the rendered raster instead
    and re-OCRing each result with `pipeline.ocr.run_ocr` (see `build_pdf_from_images`) gives
    each output page a text layer scoped to only what is actually on it."""
    image = render_page(pdf_path, page_number, dpi=dpi)
    width, height = image.size
    left, top, right, bottom = box
    cropped = image.crop(
        (round(left * width), round(top * height), round(right * width), round(bottom * height))
    )
    if rotate:
        cropped = cropped.rotate(-rotate, expand=True)  # PIL rotates counterclockwise for +angle
    return cropped


def build_pdf_from_images(images: list[Image.Image], out_path: Path, dpi: int) -> Path:
    """Assemble a raster-only PDF (no text layer) from already-rendered/cropped page images, one
    page per image, sized to the image's true physical dimensions at `dpi`. The result has no
    text at all - pair with `pipeline.ocr.run_ocr` to get a real, page-scoped text layer."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for image in images:
        width, height = image.size
        pt_width, pt_height = width / dpi * 72, height / dpi * 72
        page = doc.new_page(width=pt_width, height=pt_height)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        page.insert_image(pymupdf.Rect(0, 0, pt_width, pt_height), stream=buf.getvalue())
    doc.save(out_path)
    doc.close()
    return out_path


def split_pdf(pdf_path: Path, ranges: list[tuple[int, int]], out_dir: Path) -> list[Path]:
    """ranges are (start, end) 0-indexed, inclusive. Returns the written file paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    with pikepdf.open(pdf_path) as pdf:
        for idx, (start, end) in enumerate(ranges):
            with pikepdf.new() as part:
                part.pages.extend(pdf.pages[start : end + 1])
                dest = out_dir / f"part_{idx}.pdf"
                part.save(dest)
                outputs.append(dest)
    return outputs
