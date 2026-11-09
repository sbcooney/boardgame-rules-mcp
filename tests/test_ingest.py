"""Adding a game: file handling and checksums (no real rulebook needed)."""

import hashlib

import pytest

from bgrules import cli, ingest, manifest, server
from test_own_rulebook import make_pdf


def other_pdf(path):
    """A real PDF that isn't the published one, so it fails the checksum."""
    return make_pdf(path, [[("A different edition.", "R", 10)]])


@pytest.fixture
def game(tmp_path, monkeypatch):
    """A one-document game whose official copy is `good`; returns (download source, installed path)."""
    monkeypatch.setenv("BGRULES_HOME", str(tmp_path / "home"))
    source = tmp_path / "published.pdf"
    doc = {"id": "rules", "title": "Rules", "url": source.as_uri(), "sha256": hashlib.sha256(b"good").hexdigest()}
    monkeypatch.setattr(manifest, "load", lambda: {"demo": {"editions": {"1": {"docs": [doc]}}}})
    installed = tmp_path / "home" / "pdfs" / "demo" / "1" / "rules.pdf"
    installed.parent.mkdir(parents=True)
    return source, installed


def test_refused_download_leaves_installed_copy(game):
    source, installed = game
    installed.write_bytes(b"older copy")
    other_pdf(source)
    with pytest.raises(ingest.IngestError, match="Checksum mismatch.*uv tool upgrade boardgame-rules-mcp.*Claude Desktop extension"):
        ingest.add("demo", log=lambda m: None)
    assert installed.read_bytes() == b"older copy"
    assert not installed.with_suffix(".part").exists()


def test_refused_file_leaves_installed_copy(game, tmp_path):
    _, installed = game
    installed.write_bytes(b"good")
    other = tmp_path / "other.pdf"
    other_pdf(other)
    with pytest.raises(ingest.IngestError, match="Checksum mismatch"):
        ingest.add("demo", file=str(other), log=lambda m: None)
    assert installed.read_bytes() == b"good"


def test_missing_file_is_a_clear_error(game, capsys):
    assert cli.main(["add", "demo", "--file", "/no/such/rules.pdf"]) == 1
    assert "No such file: /no/such/rules.pdf" in capsys.readouterr().err


NOT_A_PDF = b"<!DOCTYPE html><html><body>Dropbox preview</body></html>"


def test_file_that_isnt_a_pdf_is_a_one_line_error(game, tmp_path, capsys):
    _, installed = game
    installed.write_bytes(b"good")
    page = tmp_path / "notes.pdf"
    page.write_bytes(NOT_A_PDF)
    assert cli.main(["add", "demo", "--file", str(page)]) == 1
    err = capsys.readouterr().err
    assert err.strip() == ("error: notes.pdf isn't a PDF: it may be a web page saved under the rulebook's name. "
                           "demo downloads its rulebook from the publisher: add it again without a file. "
                           "Any installed copy is unchanged.")
    assert installed.read_bytes() == b"good"
    assert not installed.with_suffix(".part").exists()


def test_file_that_isnt_a_pdf_names_the_download_page(game, tmp_path, monkeypatch):
    doc = {"id": "rules", "title": "Rules", "source": "user_supplied", "download_page": "https://example.com/files"}
    monkeypatch.setattr(manifest, "load", lambda: {"demo": {"editions": {"1": {"docs": [doc]}}}})
    page = tmp_path / "notes.pdf"
    page.write_bytes(NOT_A_PDF)
    with pytest.raises(ingest.IngestError, match="Download the rulebook again from https://example.com/files and"):
        ingest.add("demo", file=str(page), log=lambda m: None)


def test_download_that_isnt_a_pdf_leaves_installed_copy(game):
    source, installed = game
    installed.write_bytes(b"older copy")
    source.write_bytes(NOT_A_PDF)
    with pytest.raises(ingest.IngestError, match="The file downloaded from .* couldn't be read as a PDF, so nothing was changed"):
        ingest.add("demo", log=lambda m: None)
    assert installed.read_bytes() == b"older copy"
    assert not installed.with_suffix(".part").exists()


def test_add_game_reports_a_file_that_isnt_a_pdf(game, tmp_path):
    page = tmp_path / "notes.pdf"
    page.write_bytes(NOT_A_PDF)
    result = server._add_job("demo", None, str(page), None)
    assert result["added"] is False
    assert result["error"].startswith("notes.pdf isn't a PDF")


def test_a_pdf_that_fails_to_index_leaves_installed_copy(game, tmp_path, monkeypatch):
    # e.g. damaged past the header, or scanned pages on a system without OCR
    _, installed = game
    installed.write_bytes(b"older copy")
    doc = {"id": "rules", "title": "Rules", "source": "user_supplied", "download_page": "https://example.com/files"}
    monkeypatch.setattr(manifest, "load", lambda: {"demo": {"editions": {"1": {"docs": [doc]}}}})
    seen = []

    def broken(path, hints):
        seen.append(path)
        raise RuntimeError("Pages [1] have no text, only images, so they need OCR.")

    monkeypatch.setattr(ingest, "extract_lines", broken)
    with pytest.raises(ingest.IngestError, match="need OCR. Any installed copy is unchanged."):
        ingest.add("demo", file=str(other_pdf(tmp_path / "scan.pdf")), log=lambda m: None)
    assert seen == [str(installed.with_suffix(".part"))]
    assert installed.read_bytes() == b"older copy"
    assert not installed.with_suffix(".part").exists()


def test_a_damaged_pdf_is_a_one_line_error(game, tmp_path, monkeypatch):
    _, installed = game
    installed.write_bytes(b"older copy")
    doc = {"id": "rules", "title": "Rules", "source": "user_supplied", "download_page": "https://example.com/files"}
    monkeypatch.setattr(manifest, "load", lambda: {"demo": {"editions": {"1": {"docs": [doc]}}}})
    broken = tmp_path / "rules.pdf"
    broken.write_bytes(b"%PDF-1.4\nnot really")
    with pytest.raises(ingest.IngestError) as e:
        ingest.add("demo", file=str(broken), log=lambda m: None)
    assert str(e.value) == ("rules.pdf couldn't be read as a PDF: it may be damaged or only partly downloaded. Download "
                            "the rulebook again from https://example.com/files and add that file. Any installed copy is unchanged.")
    assert installed.read_bytes() == b"older copy"


def test_a_damaged_installed_copy_is_named_on_reindex(game, monkeypatch):
    # no link to blame: a document the user downloads, already installed
    _, installed = game
    installed.write_bytes(b"%PDF-1.4\nnot really")
    doc = {"id": "rules", "title": "Rules", "source": "user_supplied", "download_page": "https://example.com/files"}
    monkeypatch.setattr(manifest, "load", lambda: {"demo": {"editions": {"1": {"docs": [doc]}}}})
    with pytest.raises(ingest.IngestError, match=r"The installed copy of Rules \(.*rules.pdf\) couldn't be read as a PDF"):
        ingest.add("demo", log=lambda m: None)
    assert installed.exists()


def test_unknown_game_message(game, capsys):
    assert cli.main(["add", "chess"]) == 1
    assert capsys.readouterr().err.strip() == (
        "error: Unknown game 'chess'. Known games: demo\n"
        "Request it: https://github.com/sbcooney/boardgame-rules-mcp/issues/new?template=game-request.yml&title=Game%20request%3A%20chess&game=chess\n"
        "Or add your own copy of its rulebook: bgrules add chess --file /path/to/rules.pdf (or --url <link to the PDF>)"
    )


def test_add_installs_what_it_can_and_lists_files_to_supply(game, monkeypatch):
    source, installed = game
    source.write_bytes(b"good")
    rules = manifest.load()["demo"]["editions"]["1"]["docs"][0]
    guide = {"id": "guide", "title": "Guide", "source": "user_supplied", "download_page": "https://example.com/files"}
    monkeypatch.setattr(manifest, "load", lambda: {"demo": {"editions": {"1": {"docs": [rules, guide]}}}})
    monkeypatch.setattr(ingest, "extract_lines", lambda path, hints: [])
    logged = []
    ingest.add("demo", log=logged.append)
    assert installed.read_bytes() == b"good"
    assert any("Download it from https://example.com/files" in m for m in logged)
    with pytest.raises(ingest.IngestError, match="Download it from"):
        ingest.add("demo", doc_id="guide", log=lambda m: None)


def test_installed_copy_of_a_user_supplied_document_is_reindexed(game, monkeypatch):
    # e.g. a bundled hints fix for a rulebook only the user can download
    _, installed = game
    installed.write_bytes(b"good")
    doc = {"id": "rules", "title": "Rules", "source": "user_supplied", "download_page": "https://example.com/files",
           "expected_sha256": hashlib.sha256(b"good").hexdigest()}
    monkeypatch.setattr(manifest, "load", lambda: {"demo": {"editions": {"1": {"docs": [doc]}}}})
    extracted = []
    monkeypatch.setattr(ingest, "extract_lines", lambda path, hints: extracted.append(path) or [])
    assert ingest.add("demo", log=lambda m: None)[0]["doc"] == "Rules"
    assert extracted == [str(installed)]
