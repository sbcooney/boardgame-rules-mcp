"""Load the manifest of rulebook sources: the official ones bundled with this
release, plus rulebooks users added themselves (BGRULES_HOME/manifest.yaml)."""

import re
from functools import cache
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote, urlencode

import yaml

USER_HEADER = """\
# Rulebooks you added yourself (`bgrules add <name> --file ...`, or add_game in
# Claude Desktop). Same format as the bundled manifest
# (https://github.com/sbcooney/boardgame-rules-mcp/blob/main/src/bgrules/manifest.yaml).
# If a layout reads badly, tune a document's `extraction` hints here, then
# re-index it with `bgrules add <game> --edition <edition>`.
"""


@cache
def bundled() -> dict:
    return yaml.safe_load(files("bgrules").joinpath("manifest.yaml").read_text())


def user_path() -> Path:
    from .store import home

    return home() / "manifest.yaml"


def user_games() -> dict:
    path = user_path()
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


def save_user_games(games: dict) -> None:
    path = user_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(USER_HEADER + "\n" + yaml.safe_dump(games, sort_keys=False, allow_unicode=True))
    tmp.replace(path)


def load() -> dict:
    """Bundled games, and games users added. A bundled game's id is never taken over."""
    return {**user_games(), **bundled()}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def game_id(name: str) -> str:
    """The id for a game a user names: a known game's id when the name is that
    id or that game's name ("Brass: Birmingham"), otherwise the name as a slug
    ("Root" -> "root")."""
    slug = _slug(name)
    for gid, entry in load().items():
        if slug in (gid, _slug(str(entry.get("name", "")))):
            return gid
    return slug


def is_user_added(game: str, edition: str, doc_id: str) -> bool:
    docs = load().get(game, {}).get("editions", {}).get(edition, {}).get("docs", [])
    return any(d["id"] == doc_id and d.get("user_added") for d in docs)


class UnknownGame(KeyError):
    """A game this release has no manifest entry for."""


def editions(game: str) -> dict:
    games = load()
    if game not in games:
        raise UnknownGame(f"Unknown game {game!r}. Known games: {', '.join(sorted(games))}")
    return games[game]["editions"]


def available(include_untested: bool = False) -> list[dict]:
    """Every game this release can install, and which documents users must download themselves.
    Editions whose extraction hasn't been checked (status: untested) are left out unless asked for,
    and so are rulebooks users added themselves (those are listed as installed)."""
    out = []
    for game, entry in load().items():
        editions = []
        for edition, ed in entry["editions"].items():
            if ed.get("status") == "untested" and not include_untested:
                continue
            if all(d.get("user_added") for d in ed["docs"]):
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
