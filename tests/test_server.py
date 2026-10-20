"""add_game: background indexing that outlasts one tool call (no real rulebook needed)."""

import threading

import pytest

from bgrules import ingest, manifest, server


@pytest.fixture
def slow_add(monkeypatch):
    """ingest.add blocks until the test releases it; returns (release event, calls)."""
    release = threading.Event()
    calls = []

    def add(game, edition=None, file=None, doc_id=None, log=print, supply_hint=None, allow_untested=True, url=None):
        calls.append((game, edition, file, doc_id))
        log("Extracting Rules…")
        release.wait(5)
        if game == "chess":
            raise manifest.UnknownGame("Unknown game 'chess'. Known games: demo")
        if game == "broken":
            raise KeyError("title")  # a bug, not an unknown game
        return [{"doc": "Rules", "sections": 3, "passages": 4, "pages_without_text": []}]

    monkeypatch.setattr(ingest, "add", add)
    monkeypatch.setattr(server, "_ADD_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(server, "_adding", {})
    return release, calls


def test_add_game_keeps_waiting_on_the_same_job(slow_add):
    release, calls = slow_add
    assert server.add_game("demo")["status"] == "indexing"
    assert server.add_game("demo")["status"] == "indexing"
    release.set()
    result = server.add_game("demo")
    assert result["added"] is True and result["documents"][0]["sections"] == 3
    assert result["notes"] == ["Extracting Rules…"]
    assert len(calls) == 1  # the repeat calls waited instead of indexing again
    assert server._adding == {}


def test_add_game_reports_errors(slow_add):
    release, _ = slow_add
    release.set()
    result = server.add_game("chess")
    assert result["added"] is False
    assert result["error"] == "Unknown game 'chess'. Known games: demo"
    assert result["request_link"].endswith("?template=game-request.yml&title=Game%20request%3A%20chess&game=chess")


def test_add_game_expands_home_in_file_paths(slow_add, monkeypatch, tmp_path):
    release, calls = slow_add
    release.set()
    monkeypatch.setenv("HOME", str(tmp_path))
    server.add_game("demo", file="~/Downloads/rules.pdf")
    assert calls[0][2] == str(tmp_path / "Downloads" / "rules.pdf")


def test_user_supplied_documents_get_add_game_instructions(monkeypatch, tmp_path):
    monkeypatch.setenv("BGRULES_HOME", str(tmp_path))
    from bgrules import manifest

    doc = {"id": "guide", "title": "Guide", "source": "user_supplied", "download_page": "https://example.com/files"}
    monkeypatch.setattr(manifest, "load", lambda: {"demo": {"name": "Demo", "editions": {"1": {"docs": [doc]}}}})
    result = server._add_job("demo", None, None, None)
    assert result["added"] is False
    assert "Download it from https://example.com/files" in result["error"]
    assert 'call add_game with game="demo", edition="1", document="guide"' in result["error"]
    assert manifest.available() == [{
        "game": "demo", "name": "Demo",
        "editions": [{"edition": "1", "documents": [
            {"document": "guide", "title": "Guide", "downloads_automatically": False, "download_page": "https://example.com/files"},
        ]}],
    }]


def test_extension_manifest_lists_every_tool():
    import asyncio
    import json
    from pathlib import Path

    bundle = json.loads((Path(__file__).parents[1] / "mcpb" / "manifest.json").read_text())
    registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {t["name"] for t in bundle["tools"]} == registered


def test_untested_editions_are_hidden_and_refused(monkeypatch, tmp_path):
    from bgrules import manifest

    monkeypatch.setenv("BGRULES_HOME", str(tmp_path))
    doc = {"id": "rules", "title": "Rules", "source": "user_supplied"}
    monkeypatch.setattr(manifest, "load", lambda: {
        "demo": {"editions": {"old": {"docs": [doc]}, "new": {"status": "untested", "docs": [doc]}}},
        "draft": {"editions": {"1": {"status": "untested", "docs": [doc]}}},
    })
    assert [(g["game"], [e["edition"] for e in g["editions"]]) for g in manifest.available()] == [("demo", ["old"])]
    assert "isn't supported here yet" in server._add_job("demo", "new", None, None)["error"]
    assert "isn't supported here yet" in server._add_job("draft", None, None, None)["error"]
    # With the untested edition hidden, the game has one edition, so none needs naming.
    assert "can't be downloaded automatically" in server._add_job("demo", None, None, None)["error"]


def test_request_links_open_the_game_request_form():
    from pathlib import Path

    from bgrules import manifest

    form = Path(__file__).parents[1] / ".github" / "ISSUE_TEMPLATE" / "game-request.yml"
    assert "id: game\n" in form.read_text()  # the field the link prefills
    assert manifest.request_link() == "https://github.com/sbcooney/boardgame-rules-mcp/issues/new?template=game-request.yml"
    assert manifest.request_link("Ticket to Ride: Europe").endswith(
        "&title=Game%20request%3A%20Ticket%20to%20Ride%3A%20Europe&game=Ticket%20to%20Ride%3A%20Europe"
    )
    assert server.list_games()["request_a_game"] == manifest.request_link()


def test_unexpected_errors_surface_and_are_not_cached(slow_add):
    release, calls = slow_add
    release.set()
    for _ in range(2):
        with pytest.raises(KeyError, match="title"):
            server.add_game("broken")
    assert len(calls) == 2  # the retry ran again instead of replaying the failed job
    assert server._adding == {}
