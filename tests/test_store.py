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


def test_other_games_do_not_change_a_games_ranking(db, tmp_path):
    before = store.search(db, "demo", "How is the robber moved?")
    # Another game full of the same words must not shift demo's scores.
    other = Section(heading="Robber Rules", page_start=1)
    other.lines = [_line("robber robber moved moved robber " * 5, 1, 100)]
    store.replace_doc(db, "other", "1", {"id": "rules", "title": "Rules"}, tmp_path / "o.pdf", "x", [other])
    after = store.search(db, "demo", "How is the robber moved?")
    assert [(r["citation_id"], r["coverage"]) for r in after["results"]] == [(r["citation_id"], r["coverage"]) for r in before["results"]]
    assert all(r["game"] == "demo" for r in after["results"])


def test_page_contents_lists_passages_on_page(db):
    info, _ = store.page_contents(db, "demo", 11)
    assert [p["section_path"] for p in info["passages"]] == [["Almanac", "Robber"], ["Almanac", "Roads"]]
    with pytest.raises(store.LookupError_):
        store.page_contents(db, "demo", 99)
