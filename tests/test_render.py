"""Page rendering helpers."""

from bgrules.render import merge_regions


def test_overlapping_crop_regions_merge_but_columns_stay_separate():
    # Two overlapping boxes in one text block merge; a box in the next column stays its own crop.
    regions = [(10, 10, 100, 40), (50, 30, 120, 60), (300, 10, 400, 40)]
    assert merge_regions(regions) == [(10, 10, 120, 60), (300, 10, 400, 40)]


def test_quotes_are_found_line_by_line_within_the_passage(tmp_path):
    from bgrules import render, server, store
    from test_own_rulebook import make_pdf

    pdf = str(make_pdf(tmp_path / "rules.pdf", [[
        ("1. Each player takes a board.", "R", 12),
        ("2. The player who most recently cooked is the first", "R", 12),
        ("player and takes the token. Play goes clock-", "R", 12),
        ("wise.", "R", 12),
        ("Elsewhere the first player is named again.", "R", 12),
    ]]))
    passage = [{"page": 1, "bbox": [72, 40, 400, 110]}]  # the first four lines, not the fifth

    boxes = render.quote_boxes(pdf, passage, "The player who most recently cooked is the first player")
    assert [(b["page"], round(b["bbox"][1])) for b in boxes] == [(1, 60), (1, 78)]  # one box per line
    assert boxes[1]["bbox"][2] < 150  # the second line's box stops after "player"
    assert render.quote_boxes(pdf, passage, "Each player ... clockwise.") is not None  # joined at the line break
    assert render.quote_boxes(pdf, passage, "Elsewhere the first player") is None  # outside the passage
    assert render.quote_boxes(pdf, passage, "The youngest player goes first") is None

    cite = {"citation_id": "c1", "game": "g", "edition": "1", "document": "Rules", "section_path": ["Setup"],
            "printed_page": 1, "open_link": "", "spans": passage}
    store_get = store.get_passage
    try:
        store.get_passage = lambda db, cid: (cite, pdf)
        shown = server.show_passage("c1", "is the first player")
        assert shown[0]["quote_highlighted"] is True and len(shown) == 2
        missing = server.show_passage("c1", "The youngest player goes first")
        assert missing[0]["quote_highlighted"] is False and "verify_quote" in missing[0]["note"] and len(missing) == 2
        assert "quote_highlighted" not in server.show_passage("c1")[0]
    finally:
        store.get_passage = store_get
