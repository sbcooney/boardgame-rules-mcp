"""Load the bundled manifest of official rulebook sources."""

from functools import cache
from importlib.resources import files
from urllib.parse import quote, urlencode

import yaml


@cache
def load() -> dict:
    return yaml.safe_load(files("bgrules").joinpath("manifest.yaml").read_text())


def editions(game: str) -> dict:
    games = load()
    if game not in games:
        raise KeyError(f"Unknown game {game!r}. Known games: {', '.join(sorted(games))}")
    return games[game]["editions"]


def available(include_untested: bool = False) -> list[dict]:
    """Every game this release can install, and which documents users must download themselves.
    Editions whose extraction hasn't been checked (status: untested) are left out unless asked for."""
    out = []
    for game, entry in load().items():
        editions = []
        for edition, ed in entry["editions"].items():
            if ed.get("status") == "untested" and not include_untested:
                continue
            docs = [
                {"document": d["id"], "title": d["title"], "downloads_automatically": bool(d.get("url")),
                 **({"download_page": d["download_page"]} if not d.get("url") and d.get("download_page") else {})}
                for d in ed["docs"]
            ]
            editions.append({"edition": edition, **({"status": ed["status"]} if ed.get("status") else {}), "documents": docs})
        if editions:
            out.append({"game": game, "name": entry.get("name", game), "editions": editions})
    return out


REQUEST_URL = "https://github.com/sbcooney/boardgame-rules-mcp/issues/new"


def request_link(game: str | None = None) -> str:
    """A link to the game-request issue form, prefilled with the game's name when given."""
    params = {"template": "game-request.yml"}
    if game:
        params |= {"title": f"Game request: {game}", "game": game}
    return f"{REQUEST_URL}?{urlencode(params, quote_via=quote)}"
