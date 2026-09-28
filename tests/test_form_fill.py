import pymupdf
import pytest

from pipeline import form_fill


def _page_with_label(rotation):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=600)
    # Like a rotated scan: the label is stored turned by `rotation` in raw space, so that it
    # reads upright once the page's /Rotate is applied.
    page.insert_text((150, 200), "Name:", fontsize=12, rotate=rotation)
    page.set_rotation(rotation)
    return page


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_place_text_visible_lands_upright_next_to_label(rotation):
    page = _page_with_label(rotation)
    label = form_fill.to_visible(page, form_fill.find_label(page, "Name:"))
    assert label.width > label.height, "fixture label must read upright on the visible page"

    form_fill.place_text_visible(page, label.x1 + 6, label.y1 - 3, "Meyer", size=12)

    words = {w[4]: pymupdf.Rect(w[:4]) * page.rotation_matrix for w in page.get_text("words")}
    value = words["Meyer"]
    assert value.width > value.height, "text must be horizontal on the visible page"
    assert value.x0 >= label.x1, "value sits to the right of the label"
    assert abs((value.y0 + value.y1) / 2 - (label.y0 + label.y1) / 2) < 4, "same visible line"


def test_rotation_zero_matches_plain_placement():
    page = _page_with_label(0)
    plain = _page_with_label(0)
    form_fill.place_text_visible(page, 100, 100, "Meyer", size=12)
    plain.insert_text((100, 100), "Meyer", fontsize=12, fontname=form_fill.DEFAULT_FONT)
    assert page.get_text("words") == plain.get_text("words")
