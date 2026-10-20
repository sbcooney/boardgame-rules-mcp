"""Adding a rulebook for a game the tool doesn't ship, from the user's own PDF."""

import pytest

from bgrules import ingest, manifest, server, store


def make_pdf(path, pages):
    """A minimal PDF: each page is a list of (text, font, size) lines, top to bottom.
    Fonts: R (Helvetica) and B (Helvetica-Bold)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"]
    kids = []
    for lines in pages:
        y, stream = 740, "BT\n"
        for text, font, size in lines:
            stream += f"/{font} {size} Tf 1 0 0 1 72 {y} Tm ({text}) Tj\n"
            y -= size * 1.5
        objs.append(f"<< /Length {len(stream) + 2} >>\nstream\n{stream}ET\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objs)} 0 R "
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
