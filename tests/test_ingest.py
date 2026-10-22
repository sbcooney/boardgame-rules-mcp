"""Adding a game: file handling and checksums (no real rulebook needed)."""

import hashlib

import pytest

from bgrules import cli, ingest, manifest


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
    source.write_bytes(b"publisher changed it")
    with pytest.raises(ingest.IngestError, match="Checksum mismatch.*uv tool upgrade boardgame-rules-mcp.*Claude Desktop extension"):
        ingest.add("demo", log=lambda m: None)
    assert installed.read_bytes() == b"older copy"
    assert not installed.with_suffix(".part").exists()


def test_refused_file_leaves_installed_copy(game, tmp_path):
    _, installed = game
    installed.write_bytes(b"good")
    other = tmp_path / "other.pdf"
    other.write_bytes(b"a different edition")
    with pytest.raises(ingest.IngestError, match="Checksum mismatch"):
        ingest.add("demo", file=str(other), log=lambda m: None)
    assert installed.read_bytes() == b"good"


def test_missing_file_is_a_clear_error(game, capsys):
    assert cli.main(["add", "demo", "--file", "/no/such/rules.pdf"]) == 1
    assert "No such file: /no/such/rules.pdf" in capsys.readouterr().err


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
