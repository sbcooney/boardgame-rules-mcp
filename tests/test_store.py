"""Store behaviour on a small synthetic index (no PDF needed)."""

import pytest

from bgrules import store
from bgrules.extract import Line, Section


def _line(text, page=1, top=0.0):
    return Line(text=text, page=page, column=0, bbox=(10.0, top, 200.0, top + 10), is_heading=False)


@pytest.fixture
def db(tmp_path):
    db = store.connect(tmp_path / "index.sqlite")
    robber = Section(heading="Robber", page_start=11, part="Almanac")
    robber.lines = [
        _line("The robber begins the game in the desert. It is moved only by", 11, 100),
        _line("rolling a “7” or playing a Knight card.", 11, 112),
    ]
    roads = Section(heading="Roads", page_start=11, part="Almanac")
    roads.lines = [_line("Only 1 road may be built on each path.", 11, 300)]
    store.replace_doc(db, "demo", "1", {"id": "rules", "title": "Rules"}, tmp_path / "rules.pdf", "abc", [robber, roads])
    return db


def test_search_finds_and_cites(db):
    r = store.search(db, "demo", "How is the robber moved?")
    assert r["found"] is True
    top = r["results"][0]
    assert top["section_path"] == ["Almanac", "Robber"]
    assert top["printed_page"] == 11
    assert top["quote"].startswith("The robber begins the game in the desert.")


def test_search_not_found_gives_next_steps(db):
    r = store.search(db, "demo", "How do zombies spawn at night?")
    assert r["found"] is False
    assert "do not answer from memory" in r["next_steps"]


def test_verify_quote_normalizes_quotes_and_allows_ellipsis(db):
    cid = store.search(db, "demo", "robber moved")["results"][0]["citation_id"]
    assert store.verify_quote(db, cid, 'moved only by rolling a "7" or playing a Knight card')["verified"]
    assert store.verify_quote(db, cid, "The robber begins the game … playing a Knight card.")["verified"]
    # Fragments must appear in order.
    assert not store.verify_quote(db, cid, "playing a Knight card … The robber begins")["verified"]
    bad = store.verify_quote(db, cid, "The robber may never return to the desert.")
    assert bad["verified"] is False and "closest_source_text" in bad


def test_verify_quote_ignores_pdf_spacing_and_editorial_brackets(tmp_path):
    db = store.connect(tmp_path / "i.sqlite")
    sec = Section(heading="Build", page_start=3)
    sec.lines = [_line("You may build roads and/ or cities (see the costs card ). Add 1 additional on the card.", 3, 100)]
    store.replace_doc(db, "demo", "1", {"id": "rules"}, tmp_path / "r.pdf", "x", [sec])
    cid = store.search(db, "demo", "build roads cities")["results"][0]["citation_id"]
    assert store.verify_quote(db, cid, "You may build roads and/or cities (see the costs card).")["verified"]
    assert store.verify_quote(db, cid, "Add 1 additional [Blight] on the card.")["verified"]
    assert not store.verify_quote(db, cid, "Add 2 additional [Blight] on the card.")["verified"]


def test_other_games_do_not_change_a_games_ranking(db, tmp_path):
    before = store.search(db, "demo", "How is the robber moved?")
    # Another game full of the same words must not shift demo's scores.
    other = Section(heading="Robber Rules", page_start=1)
    other.lines = [_line("robber robber moved moved robber " * 5, 1, 100)]
    store.replace_doc(db, "other", "1", {"id": "rules", "title": "Rules"}, tmp_path / "o.pdf", "x", [other])
    after = store.search(db, "demo", "How is the robber moved?")
    assert [(r["citation_id"], r["coverage"]) for r in after["results"]] == [(r["citation_id"], r["coverage"]) for r in before["results"]]
    assert all(r["game"] == "demo" for r in after["results"])


def test_errata_amend_rulebook_sections_and_use_printed_page_numbers(db, tmp_path):
    change = Section(heading="Significant Changes", page_start=6, subheading="BLIGHT ERRATA")
    change.lines = [_line("Start the game with exactly 1 additional Blight on the card.", 6, 100)]
    errata = {
        "id": "changes", "title": "Later Rulebook (Significant Changes)", "source_type": "errata",
        "extraction": {"printed_page_offset": -1},
        "amends": [{
            "source_section": "Significant Changes > BLIGHT ERRATA", "target_doc": "rules",
            "target_section": "Almanac > Robber", "summary": "Add 1 Blight at setup.",
            "printed_rule": "Older printings add none.", "included_in": "14th printing onward.",
        }],
    }
    store.replace_doc(db, "demo", "1", errata, tmp_path / "later.pdf", "y", [change])

    robber = store.search(db, "demo", "How is the robber moved?")["results"][0]
    [amendment] = robber["amended_by"]
    assert amendment["printed_rule"] == "Older printings add none."
    assert amendment["source"]["source_type"] == "errata"
    assert amendment["source"]["printed_page"] == 5  # PDF page 6, cover unnumbered
    assert amendment["source"]["quote"].startswith("Start the game with exactly 1 additional Blight")

    # Sections the erratum doesn't target are untouched.
    roads = store.search(db, "demo", "How many roads per path?")["results"][0]
    assert roads["section_path"] == ["Almanac", "Roads"] and "amended_by" not in roads

    # The erratum itself is searchable, labelled as errata, cited by printed page.
    hit = store.search(db, "demo", "additional Blight at the start")["results"][0]
    assert (hit["source_type"], hit["printed_page"], hit["pdf_page"]) == ("errata", 5, 6)
    assert store.official_changes(db) == {"demo 1": [{"summary": "Add 1 Blight at setup.", "included_in": "14th printing onward."}]}

    # Page lookups take printed page numbers.
    info, _ = store.page_contents(db, "demo", 5, document="changes")
    assert (info["printed_page"], info["_pdf_page"]) == (5, 6)


def test_page_contents_lists_passages_on_page(db):
    info, _ = store.page_contents(db, "demo", 11)
    assert [p["section_path"] for p in info["passages"]] == [["Almanac", "Robber"], ["Almanac", "Roads"]]
    with pytest.raises(store.LookupError_):
        store.page_contents(db, "demo", 99)


def test_check_amendments_reports_sections_that_no_longer_match(db, tmp_path):
    change = Section(heading="Significant Changes", page_start=6, subheading="BLIGHT ERRATA")
    change.lines = [_line("Start with 1 additional Blight.", 6, 100)]
    errata = {"id": "changes", "amends": [
        {"source_section": "Significant Changes > BLIGHT ERRATA", "target_doc": "rules", "target_section": "Almanac > Robber", "summary": "ok"},
        {"source_section": "Significant Changes > BLIGHT ERRATA", "target_doc": "rules", "target_section": "Almanac > Robbers", "summary": "renamed"},
        {"source_section": "Significant Changes > FEAR ERRATA", "target_doc": "rules", "target_section": "Almanac > Roads", "summary": "missing"},
    ]}
    store.replace_doc(db, "demo", "1", errata, tmp_path / "later.pdf", "y", [change])
    assert store.check_amendments(db, "demo", "1") == [
        "rules: no section 'Almanac > Robbers' (changed by changes)",
        "changes: no section 'Significant Changes > FEAR ERRATA'",
    ]


def test_printed_pages_on_two_page_spreads():
    # Brass: PDF page 1 is the cover (printed 1), PDF pages 2-6 are spreads (2|3 ... 10|11), PDF page 7 is printed 12.
    doc = {"page_offset": 0, "spreads": '{"first": 2, "last": 6, "split": 816.5}'}
    assert [store.printed_page(doc, p) for p in (1, 2, 3, 6, 7)] == [1, 2, 4, 10, 12]
    assert store.printed_page(doc, 2, 900) == 3 and store.printed_page(doc, 6, 1200) == 11
    assert store.pdf_location(doc, 1) == (1, None)
    assert store.pdf_location(doc, 5) == (3, (816.5, float("inf")))
    assert store.pdf_location(doc, 12) == (7, None)
    plain = {"page_offset": -1, "spreads": None}
    assert store.printed_page(plain, 6) == 5 and store.pdf_location(plain, 5) == (6, None)
