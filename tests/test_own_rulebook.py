"""Adding a rulebook for a game the tool doesn't ship, from the user's own PDF."""

import json

import pytest

from bgrules import extract, ingest, manifest, server, store


def make_pdf(path, pages, sizes=None):
    """A minimal PDF: each page is a list of (text, font, size) lines, top to bottom,
    each optionally with an x position, or (x, y) to place it anywhere.
    Fonts: R (Helvetica) and B (Helvetica-Bold). `sizes` gives each page's
    (width, height) (default 612 x 792)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"]
    kids = []
    for n, lines in enumerate(pages):
        y, stream = 740, "BT\n"
        for text, font, size, *pos in lines:
            x, at = (pos + [None])[:2] if pos else (72, None)
            stream += f"/{font} {size} Tf 1 0 0 1 {x} {at or y} Tm ({text}) Tj\n"
            if at is None:
                y -= size * 1.5
        objs.append(f"<< /Length {len(stream) + 2} >>\nstream\n{stream}ET\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {sizes[n][0] if sizes else 612} {sizes[n][1] if sizes else 792}] /Contents {len(objs)} 0 R "
                    "/Resources << /Font << /R 3 0 R /B 4 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)
    return path


BODY = [("Each player takes a turn in order.", "R", 10), ("Then the round ends and a new one begins.", "R", 10)]
quiet = lambda m: None


@pytest.fixture
def rulebook(tmp_path, monkeypatch):
    """A two-page rulebook PDF: two large headings and one bold one at body size."""
    monkeypatch.setenv("BGRULES_HOME", str(tmp_path / "home"))
    return make_pdf(tmp_path / "root-rules.pdf", [
        [("Setup", "R", 16), *BODY, ("Taking a Turn", "B", 10), *BODY],
        [("Winning the Game", "R", 16), ("The first player to reach 30 points wins.", "R", 10), *BODY],
    ])


def test_added_under_its_name_and_labelled(rulebook):
    report = ingest.add("Root", file=str(rulebook), log=quiet)
    assert report == [{"doc": "Rulebook (user-provided copy)", "sections": 3, "passages": 3, "pages_without_text": [],
                       "user_provided": True}]
    entry = manifest.user_games()["root"]
    assert entry["name"] == "Root"
    doc = entry["editions"][ingest.OWN_EDITION]["docs"][0]
    assert doc["user_added"] and doc["extraction"]["heading_fonts"][0] == {"name": "", "min_size": 11.5}
    assert "root" not in [g["game"] for g in manifest.available()]  # listed as installed, not as available
    result = store.search(store.connect(), "root", "how many points to win")
    assert result["results"][0]["section_path"] == ["Winning the Game"]
    assert result["results"][0]["document"] == "Rulebook (user-provided copy)"
    installed = server.list_games()["installed"]
    assert [(r["game"], r.get("user_provided")) for r in installed] == [("root", True)]


def printed_pages(game, queries):
    db = store.connect()
    return {r["section_path"][-1]: r["printed_page"] for q in queries for r in store.search(db, game, q)["results"]}


def test_two_page_spreads_are_cited_by_printed_page(tmp_path, monkeypatch):
    # Cover, then one PDF page holding printed pages 2|3, then the back page (4).
    monkeypatch.setenv("BGRULES_HOME", str(tmp_path / "home"))
    pdf = make_pdf(tmp_path / "booklet.pdf", [
        [("Setup", "R", 16), *BODY],
        [("Taking a Turn", "R", 16, 72), *BODY, ("Scoring", "R", 16, 700), ("Each potion scores its points.", "R", 10, 700),
         ("2", "R", 10, 30, 30), ("3", "R", 10, 1190, 30)],
        [("Winning the Game", "R", 16), ("The first player to reach 30 points wins.", "R", 10)],
    ], sizes=[(612, 792), (1224, 792), (612, 792)])
    ingest.add("Potion Explosion", file=str(pdf), log=quiet)
    hints = manifest.user_games()["potion-explosion"]["editions"][ingest.OWN_EDITION]["docs"][0]["extraction"]
    assert hints["spreads"] == [2, 2] and "printed_page_offset" not in hints
    assert printed_pages("potion-explosion", ("potion scores points", "turn round ends", "reach 30 points")) == {
        "Setup": 1, "Taking a Turn": 2, "Scoring": 3, "Winning the Game": 4}


def test_spreads_without_a_cover_count_from_their_printed_numbers(tmp_path, monkeypatch):
    # No cover: PDF page 1 is already printed pages 2|3, then 4|5, then the back page (6).
    monkeypatch.setenv("BGRULES_HOME", str(tmp_path / "home"))
    pdf = make_pdf(tmp_path / "booklet.pdf", [
        [("Setup", "R", 16), *BODY, ("Scoring", "R", 16, 700), ("Each potion scores its points.", "R", 10, 700),
         ("2", "R", 10, 30, 30), ("3", "R", 10, 1190, 30)],
        [("Taking a Turn", "R", 16), *BODY, ("4", "R", 10, 30, 30), ("5", "R", 10, 1190, 30)],
        [("Winning the Game", "R", 16), ("The first player to reach 30 points wins.", "R", 10)],
    ], sizes=[(1224, 792), (1224, 792), (612, 792)])
    ingest.add("Potion Explosion", file=str(pdf), log=quiet)
    assert printed_pages("potion-explosion", ("potion scores points", "turn round ends", "reach 30 points")) == {
        "Setup": 2, "Scoring": 3, "Taking a Turn": 4, "Winning the Game": 6}


def test_a_file_of_only_spreads_is_found_by_its_page_numbers(tmp_path, monkeypatch):
    # Every PDF page is a spread (2|3, 4|5): nothing narrower to compare with.
    monkeypatch.setenv("BGRULES_HOME", str(tmp_path / "home"))
    pdf = make_pdf(tmp_path / "booklet.pdf", [
        [("Setup", "R", 16), *BODY, ("Scoring", "R", 16, 700), ("Each potion scores its points.", "R", 10, 700),
         ("2", "R", 10, 30, 30), ("3", "R", 10, 1190, 30)],
        [("Taking a Turn", "R", 16), *BODY, ("4", "R", 10, 30, 30), ("5", "R", 10, 1190, 30)],
    ], sizes=[(1224, 792)] * 2)
    ingest.add("Potion Explosion", file=str(pdf), log=quiet)
    assert printed_pages("potion-explosion", ("potion scores points", "turn round ends")) == {
        "Setup": 2, "Scoring": 3, "Taking a Turn": 4}
    # Landscape pages without facing page numbers are single pages.
    plain = make_pdf(tmp_path / "landscape.pdf", [BODY] * 3, sizes=[(792, 612)] * 3)
    assert "spreads" not in extract.default_hints(str(plain))


def test_copies_added_earlier_gain_new_defaults_and_keep_their_tuning(tmp_path, monkeypatch):
    monkeypatch.setenv("BGRULES_HOME", str(tmp_path / "home"))
    pdf = make_pdf(tmp_path / "booklet.pdf", [
        [("Setup", "R", 16), *BODY],
        [("Taking a Turn", "R", 16), *BODY, ("Scoring", "R", 16, 700), ("Each potion scores its points.", "R", 10, 700)],
        [("Winning the Game", "R", 16), ("The first player to reach 30 points wins.", "R", 10)],
    ], sizes=[(612, 792), (1224, 792), (612, 792)])
    ingest.add("Potion Explosion", file=str(pdf), log=quiet)

    def saved_hints():
        return manifest.user_games()["potion-explosion"]["editions"][ingest.OWN_EDITION]["docs"][0]["extraction"]

    def older_copy():  # as added before spreads were detected, then tuned by hand
        games = manifest.user_games()
        hints = games["potion-explosion"]["editions"][ingest.OWN_EDITION]["docs"][0]["extraction"]
        del hints["spreads"]
        hints["heading_max_words"] = 8
        manifest.save_user_games(games)

    older_copy()
    ingest.add("potion-explosion", log=quiet)  # re-index the installed copy
    assert saved_hints()["spreads"] == [2, 2] and saved_hints()["heading_max_words"] == 8
    assert printed_pages("potion-explosion", ("potion scores points",))["Scoring"] == 3
    older_copy()
    ingest.add("Potion Explosion", file=str(pdf), log=quiet)  # or add a new copy
    assert saved_hints()["spreads"] == [2, 2] and saved_hints()["heading_max_words"] == 8
    # A re-index that fails leaves the saved hints as they were.
    older_copy()
    before = saved_hints()

    def no_ocr(path, hints):
        raise RuntimeError("Pages [1] have no text, only images, so they need OCR")

    monkeypatch.setattr(ingest, "extract_lines", no_ocr)
    with pytest.raises(ingest.IngestError, match="need OCR"):
        ingest.add("potion-explosion", log=quiet)
    assert saved_hints() == before


def test_a_score_track_is_not_taken_for_page_numbers(tmp_path):
    # A 11-20 track across the bottom of the 2|3 spread, above its page numbers.
    track = [(str(10 + i), "R", 10, 400 + i * 40, 60) for i in range(1, 11)]
    pdf = make_pdf(tmp_path / "booklet.pdf", [BODY, [*BODY, *track, ("2", "R", 10, 30, 30), ("3", "R", 10, 1190, 30)], BODY],
                   sizes=[(612, 792), (1224, 792), (612, 792)])
    hints = extract.default_hints(str(pdf))
    assert hints["spreads"] == [2, 2] and "printed_page_offset" not in hints


def test_a_smaller_page_is_not_half_a_spread(tmp_path):
    # A letter-size rulebook ending with a small reference card.
    pdf = make_pdf(tmp_path / "rules.pdf", [BODY] * 4, sizes=[(612, 792)] * 3 + [(300, 420)])
    assert "spreads" not in extract.default_hints(str(pdf))


def test_reindexed_from_the_installed_copy(rulebook):
    ingest.add("Root", file=str(rulebook), log=quiet)
    rulebook.unlink()  # the indexed copy lives in BGRULES_HOME
    assert ingest.add("root", log=quiet)[0]["sections"] == 3


def test_another_edition_or_document_joins_the_same_game(rulebook):
    ingest.add("Root", file=str(rulebook), edition="2nd", log=quiet)
    ingest.add("Root", file=str(rulebook), edition="2nd", doc_id="law", log=quiet)
    ingest.add("Root", file=str(rulebook), edition="1st", log=quiet)
    eds = manifest.user_games()["root"]["editions"]
    assert {e: [d["id"] for d in ed["docs"]] for e, ed in eds.items()} == {"2nd": ["rulebook", "law"], "1st": ["rulebook"]}


def test_refusals_leave_nothing_behind(rulebook, tmp_path, monkeypatch):
    notes = tmp_path / "rules.txt"
    notes.write_text("not a pdf")
    with pytest.raises(ingest.IngestError, match="isn't a PDF"):
        ingest.add("Root", file=str(notes), log=quiet)

    def no_ocr(path, hints):
        raise RuntimeError("Pages [1] have no text, only images, so they need OCR")

    monkeypatch.setattr(ingest, "extract_lines", no_ocr)
    with pytest.raises(ingest.IngestError, match="need OCR"):
        ingest.add("Root", file=str(rulebook), log=quiet)
    assert manifest.user_games() == {}


def test_a_broken_new_copy_leaves_the_installed_one(rulebook, tmp_path):
    ingest.add("Root", file=str(rulebook), log=quiet)
    installed = store.home() / "pdfs" / "root" / ingest.OWN_EDITION / "rulebook.pdf"
    good = installed.read_bytes()
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nnot really")
    with pytest.raises(ingest.IngestError, match="Couldn't read broken.pdf"):
        ingest.add("Root", file=str(broken), log=quiet)
    assert installed.read_bytes() == good
    assert ingest.add("root", log=quiet)[0]["sections"] == 3


def test_warns_when_few_headings_are_found(tmp_path, monkeypatch):
    monkeypatch.setenv("BGRULES_HOME", str(tmp_path / "home"))
    pdf = make_pdf(tmp_path / "plain.pdf", [BODY] * 4)
    report = ingest.add("Plain Game", file=str(pdf), log=quiet)
    assert report[0]["sections"] == 1 and "Only 1 sections found in 4 pages" in report[0]["warning"]


def test_bundled_games_are_found_by_name_and_never_taken_over(rulebook):
    assert manifest.game_id("Brass: Birmingham") == "brass-birmingham"
    assert manifest.game_id("CATAN") == "catan"
    assert manifest.game_id("Root") == "root"
    with pytest.raises(ingest.IngestError, match="several editions"):
        ingest.add("Catan", file=str(rulebook), log=quiet)  # the bundled game, not a new one
    assert manifest.user_games() == {}


def test_names_become_safe_ids(rulebook):
    ingest.add("Root", file=str(rulebook), doc_id="Law of Root", log=quiet)
    assert [d["id"] for d in manifest.user_games()["root"]["editions"][ingest.OWN_EDITION]["docs"]] == ["law-of-root"]
    with pytest.raises(ingest.IngestError, match="can't be used as a name"):
        ingest.add("Root", file=str(rulebook), edition="../../elsewhere", log=quiet)


def test_add_game_returns_the_id_to_search_with(rulebook):
    result = server._add_job("Root", "2nd", str(rulebook), None)
    assert result["added"] is True and result["game"] == "root"
    assert server.search_rules(result["game"], "points to win")["found"] is True


@pytest.fixture
def web(monkeypatch):
    """Serve {url: bytes} in place of the internet."""
    import io

    pages = {}

    def open_url(url):
        if url not in pages:
            raise OSError(f"HTTP Error 404: Not Found ({url})")
        return io.BytesIO(pages[url])

    monkeypatch.setattr(ingest, "_open_url", open_url)
    return pages


def test_added_from_a_link_says_where_it_came_from(rulebook, web):
    link = "https://www.leder-games.com/files/root-law.pdf"
    web[link] = rulebook.read_bytes()
    result = server._add_job("Root", "2nd", None, None, link)
    assert result["added"] is True and result["documents"][0]["doc"] == "Rulebook (user-provided copy, from leder-games.com)"
    assert result["request_link"].endswith("&rulebook=https%3A%2F%2Fwww.leder-games.com%2Ffiles%2Froot-law.pdf")
    doc = manifest.user_games()["root"]["editions"]["2nd"]["docs"][0]
    assert list(doc)[:3] == ["id", "title", "source_url"] and doc["source_url"] == link
    assert not (store.home() / "downloads" / "root.pdf").exists()  # only the indexed copy is kept
    # A new copy from a file replaces the source, and keeps the hints.
    ingest.add("Root", file=str(rulebook), log=quiet)
    doc = manifest.user_games()["root"]["editions"]["2nd"]["docs"][0]
    assert doc["title"] == "Rulebook (user-provided copy)" and "source_url" not in doc


def test_links_that_arent_rulebook_pdfs_are_refused(rulebook, web):
    web["https://example.com/root"] = b"<!doctype html><title>Root | Leder Games</title>"
    with pytest.raises(ingest.IngestError, match="opens a web page"):
        ingest.add("Root", url="https://example.com/root", log=quiet)
    with pytest.raises(ingest.IngestError, match="isn't a web link"):
        ingest.add("Root", url="file:///etc/passwd", log=quiet)
    with pytest.raises(OSError, match="404"):
        ingest.add("Root", url="https://example.com/gone.pdf", log=quiet)
    with pytest.raises(ingest.IngestError, match="supported already"):
        ingest.add("Catan", url="https://example.com/catan.pdf", log=quiet)
    assert manifest.user_games() == {} and not list((store.home() / "downloads").glob("*"))


def test_unknown_game_in_claude_desktop_offers_own_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("BGRULES_HOME", str(tmp_path))
    result = server._add_job("Root", None, None, None)
    assert result["added"] is False and "request_link" in result and "file" in result["own_copy"]


def test_tables_stay_whole_in_the_section_that_introduces_them(tmp_path, monkeypatch):
    # Each cell is its own text box, as in Potion Explosion's setup table: a bold
    # header row at body size (read as a heading before) and lone numbers
    # (dropped as callouts before). A big diagram callout shares the header's row.
    monkeypatch.setenv("BGRULES_HOME", str(tmp_path / "home"))
    table = [("Number of players", "B", 10, 72, 600), ("Countdown stack", "B", 10, 200, 600), ("3", "R", 22, 450, 596)]
    for n, (players, tokens) in enumerate([("2", "4 Skill tokens"), ("3", "5 Skill tokens"), ("4", "6 Skill tokens")], 1):
        table += [(players, "R", 10, 110, 600 - 24 * n), (tokens, "R", 10, 210, 600 - 24 * n)]
    table.append(("A caption", "R", 10, 480, 564))  # in the next column, between two rows; ends nothing
    pdf = make_pdf(tmp_path / "rules.pdf", [[
        ("Game Setup", "R", 16, 72, 700), *[(t, f, s, 72, 680 - 15 * i) for i, (t, f, s) in enumerate(BODY)],
        ("The number of Skill tokens depends on the number of players (see the following table):", "R", 10, 72, 630),
        *table,
        ("You are ready to go!", "R", 10, 72, 480),
        ("7", "R", 10, 450, 400),  # a lone callout elsewhere is still dropped
    ], [
        ("\\267 End of Game \\267", "R", 16), ("The game ends when the countdown stack is empty.", "R", 10), *BODY,
    ]])
    ingest.add("Potions", file=str(pdf), log=quiet)
    db = store.connect()
    paths = [json.loads(r["path"]) for r in db.execute("SELECT path FROM sections WHERE game='potions' ORDER BY ord")]
    assert paths == [["Game Setup"], ["End of Game"]]  # no section for the table's header; no ornaments
    hit = store.search(db, "potions", "how many skill tokens with 3 players")["results"][0]
    assert hit["section_path"] == ["Game Setup"]
    assert ("(see the following table): | Number of players | Countdown stack | | 2 | 4 Skill tokens | "
            "| 3 | 5 Skill tokens | | 4 | 6 Skill tokens | You are ready to go!") in hit["quote"]
    assert " 7" not in hit["quote"]
