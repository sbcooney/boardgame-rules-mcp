"""Load the bundled manifest of official rulebook sources."""

from functools import cache
from importlib.resources import files

import yaml


@cache
def load() -> dict:
    return yaml.safe_load(files("bgrules").joinpath("manifest.yaml").read_text())


def editions(game: str) -> dict:
    games = load()
    if game not in games:
        raise KeyError(f"Unknown game {game!r}. Known games: {', '.join(sorted(games))}")
    return games[game]["editions"]


def available() -> list[dict]:
    """Every game this release can install, and which documents users must download themselves."""
    out = []
    for game, entry in load().items():
        editions = []
        for edition, ed in entry["editions"].items():
            docs = [
                {"document": d["id"], "title": d["title"], "downloads_automatically": bool(d.get("url")),
                 **({"download_page": d["download_page"]} if not d.get("url") and d.get("download_page") else {})}
                for d in ed["docs"]
            ]
            editions.append({"edition": edition, **({"status": ed["status"]} if ed.get("status") else {}), "documents": docs})
        out.append({"game": game, "name": entry.get("name", game), "editions": editions})
    return out
