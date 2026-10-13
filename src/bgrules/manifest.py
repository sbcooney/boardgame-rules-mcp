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
