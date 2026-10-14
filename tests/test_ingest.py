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
    with pytest.raises(ingest.IngestError, match="Checksum mismatch"):
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
    assert capsys.readouterr().err.strip() == "error: Unknown game 'chess'. Known games: demo"
