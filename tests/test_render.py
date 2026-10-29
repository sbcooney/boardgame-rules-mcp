"""Page rendering helpers."""

import io

import pytest
from PIL import Image

from bgrules.render import merge_regions


def test_overlapping_crop_regions_merge_but_columns_stay_separate():
    # Two overlapping boxes in one text block merge; a box in the next column stays its own crop.
    regions = [(10, 10, 100, 40), (50, 30, 120, 60), (300, 10, 400, 40)]
    assert merge_regions(regions) == [(10, 10, 120, 60), (300, 10, 400, 40)]


LINES = [
    {"page": 1, "bbox": [72, 40, 300, 52], "text": "1. Each player takes a board."},
    {"page": 1, "bbox": [72, 58, 300, 70], "text": "2. The player who most recently cooked is the first"},
    {"page": 1, "bbox": [72, 76, 300, 88], "text": "player and takes the token. Play goes clock-"},
    {"page": 1, "bbox": [320, 40, 548, 52], "text": "wise.", "ocr": True},  # continues in the next column
]


def test_quotes_are_placed_on_the_lines_that_hold_them():
    from bgrules.render import quote_lines

    unnumbered = quote_lines(LINES[:2], "Each player takes a board")  # starts after the list number
    assert unnumbered["reasons"] == ["partial_line"] and unnumbered["boxes"][0]["bbox"][0] > 72
    exact = quote_lines(LINES[:2], "1. Each player takes a board")  # the full stop may be left off
    assert exact["highlight"] == "exact" and exact["boxes"] == [{"page": 1, "bbox": [72, 40, 300, 52]}]

    two = quote_lines(LINES, "The player who most recently cooked is the first player")
    assert [round(b["bbox"][1]) for b in two["boxes"]] == [58, 76]  # one underline per line
    assert 72 < two["boxes"][0]["bbox"][0] < 90 and two["boxes"][1]["bbox"][2] < 120  # trimmed to the quote
    assert two["reasons"] == ["partial_line"]

    across = quote_lines(LINES, "Play goes clockwise.")  # hyphenated at a line end, into the next column
    assert [b["bbox"][0] for b in across["boxes"]][1] == 320 and "ocr" in across["reasons"]
    assert quote_lines(LINES, "Each player ... takes the token")["reasons"] == ["partial_line"]
    assert "repeated" in quote_lines(LINES, "player")["reasons"]
    assert quote_lines(LINES, "The youngest player goes first") is None
    assert quote_lines(LINES, "takes the token ... Each player") is None  # fragments out of order


def test_show_passage_underlines_the_quote(tmp_path, monkeypatch):
    from bgrules import ingest, server, store
    from test_own_rulebook import make_pdf, quiet

    monkeypatch.setenv("BGRULES_HOME", str(tmp_path / "home"))
    pdf = make_pdf(tmp_path / "rules.pdf", [[
        ("Setup", "R", 16),
        ("Each player takes a board and five coins.", "R", 10),
        ("The player who most recently cooked goes first.", "R", 10),
    ]])
    ingest.add("Cooking", file=str(pdf), log=quiet)
    cid = store.search(store.connect(), "cooking", "who goes first")["results"][0]["citation_id"]

    shown = server.show_passage(cid, "The player who most recently cooked goes first.")
    assert shown[0]["highlight"] == "exact" and len(shown) == 2
    assert server.show_passage(cid, "most recently cooked")[0]["highlight_reasons"] == ["partial_line"]
    missing = server.show_passage(cid, "The youngest player goes first")
    assert missing[0]["highlight"] == "whole_passage" and "verify_quote" in missing[0]["note"] and len(missing) == 2
    assert "highlight" not in server.show_passage(cid)[0]

    db = store.connect()  # an index built before lines were stored
    with db:
        db.execute("UPDATE passages SET lines = NULL")
    old = server.show_passage(cid, "The player who most recently cooked goes first.")
    assert old[0]["highlight"] == "whole_passage" and "re-adding it later" in old[0]["note"]


@pytest.mark.parametrize("inset", [0, 21])
def test_underline_sits_under_the_text_when_the_page_is_cropped(tmp_path, inset):
    # Extracted boxes are measured from the media box, but only the crop box is rendered (#40).
    import pdfplumber
    from PIL import Image

    from bgrules.render import MARK, render
    from test_own_rulebook import make_pdf

    pdf = make_pdf(tmp_path / "rules.pdf", [[("The player who most recently cooked goes first.", "R", 10)]], crop_inset=inset)
    with pdfplumber.open(pdf) as doc:
        w = doc.pages[0].extract_words()
    line = [w[0]["x0"], w[0]["top"], w[-1]["x1"], w[-1]["bottom"]]
    img = Image.open(io.BytesIO(render(str(pdf), 1, [line], scale=1, underline=[line]))).convert("RGB")
    px = img.load()
    blue = [(x, y) for y in range(img.height) for x in range(img.width) if px[x, y] == MARK]
    ink = [(x, y) for y in range(img.height) for x in range(img.width) if max(px[x, y]) < 100]
    assert blue and ink
    assert 0 <= min(y for _, y in blue) - max(y for _, y in ink) <= 3  # just below the letters
    assert abs(min(x for x, _ in blue) - min(x for x, _ in ink)) <= 2


def test_a_quote_is_cropped_to_its_own_lines(tmp_path):
    # On a phone the image is a thumbnail: a crop of the quote alone reads larger than the whole passage (#41).
    from bgrules.render import render
    from test_own_rulebook import make_pdf

    pdf = make_pdf(tmp_path / "rules.pdf", [[(f"Rule line {i} of a long passage about setup.", "R", 10) for i in range(12)]])
    passage = [[72, 50, 300, 230]]
    whole = Image.open(io.BytesIO(render(str(pdf), 1, passage, crop_margin=36)))
    quote = Image.open(io.BytesIO(render(str(pdf), 1, passage, crop_margin=36, underline=[[72, 100, 200, 110]])))
    assert quote.height < whole.height / 2 and quote.width < whole.width
