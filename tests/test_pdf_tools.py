import pymupdf
from PIL import Image, ImageDraw

from pipeline import pdf_tools


def _canvas(background="white", size=(200, 100)):
    return Image.new("RGB", size, background)


def test_content_bbox_finds_drawn_rectangle():
    image = _canvas()
    ImageDraw.Draw(image).rectangle((40, 20, 79, 59), fill="black")
    assert pdf_tools.content_bbox(image) == (40, 20, 80, 60)


def test_content_bbox_works_on_dark_canvas():
    image = _canvas(background=(30, 30, 30))
    ImageDraw.Draw(image).line((10, 50, 150, 50), fill=(0, 200, 0), width=3)
    left, top, right, bottom = pdf_tools.content_bbox(image)
    assert (left, right) == (10, 151)
    assert top <= 50 < bottom


def test_content_bbox_ignores_single_specks():
    image = _canvas()
    ImageDraw.Draw(image).rectangle((40, 20, 79, 59), fill="black")
    image.putpixel((5, 5), (0, 0, 0))
    image.putpixel((195, 95), (0, 0, 0))
    assert pdf_tools.content_bbox(image) == (40, 20, 80, 60)


def test_content_bbox_region_skips_chrome_and_returns_image_coordinates():
    image = _canvas()
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 199, 9), fill=(0, 0, 128))  # title bar
    draw.rectangle((100, 50, 119, 69), fill="black")
    assert pdf_tools.content_bbox(image, region=(0, 10, 200, 100)) == (100, 50, 120, 70)


def test_content_bbox_pad_is_clamped():
    image = _canvas()
    ImageDraw.Draw(image).rectangle((2, 2, 197, 97), fill="black")
    assert pdf_tools.content_bbox(image, pad=10) == (0, 0, 200, 100)


def test_empty_image_has_no_box_and_is_not_cropped():
    image = _canvas()
    assert pdf_tools.content_bbox(image) is None
    assert pdf_tools.crop_to_content(image).size == image.size


def test_crop_page_content(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=600)
    page.draw_rect(pymupdf.Rect(100, 200, 200, 250), color=(0, 0, 0), fill=(0, 0, 0))
    pdf = tmp_path / "page.pdf"
    doc.save(pdf)
    cropped = pdf_tools.crop_page(pdf, 0, "content", dpi=72)
    assert abs(cropped.width - 100) <= 2
    assert abs(cropped.height - 50) <= 2
