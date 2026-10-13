"""MCP server: verbatim rulebook passages with exact citations.

The server never writes answers. It returns rulebook text exactly as printed,
with where it is (section path, page, link); the client model writes the
answer from those passages.
"""

from mcp.server.mcpserver import MCPServer

from . import store

INSTRUCTIONS = """\
This server gives you the official rulebook text for board games the user has installed.
When answering a rules question:
- Answer ONLY from passages returned by these tools. Never rely on your own memory of a game's rules.
- Quote the passage text that supports each claim, and cite it as: section path, page (e.g. "Almanac > Robber, p. 11").
- If search_rules returns found=false, or the returned passages do not actually settle the question, say the rulebook
  does not address it. Do not guess. You may mention the nearest sections the tool lists.
- Rules differ between editions; always state which edition you are citing.
- Each citation includes open_link, which opens the PDF at that page; offer it when the user wants to check."""

mcp = MCPServer("bgrules", instructions=INSTRUCTIONS)


def _call(fn, *args, **kwargs) -> dict:
    db = store.connect()
    try:
        return fn(db, *args, **kwargs)
    except store.LookupError_ as e:
        return {"error": str(e)}
    finally:
        db.close()


@mcp.tool()
def list_games() -> dict:
    """List the games, editions and rulebook documents installed on this machine."""
    return _call(lambda db: {"installed": store.installed(db)})


@mcp.tool()
def search_rules(game: str, query: str, edition: str | None = None, max_results: int = 5) -> dict:
    """Search a game's rulebook and return verbatim passages with citations.

    Use for any rules question. `game` is the id from list_games (e.g. "catan").
    `edition` is required only when several editions are installed.
    Returns found=false (plus the nearest sections) when nothing matches well:
    then the rulebook likely does not address the question; say so rather than guess.
    Each result's `quote` is exact rulebook text; `coverage` is the share of the
    question's key words the passage contains.
    """
    return _call(store.search, game, query, edition=edition, max_results=max(1, min(max_results, 10)))


@mcp.tool()
def get_section(section_id: str) -> dict:
    """Return the full verbatim text of one rulebook section, with its pages and the
    ids of the sections before and after it. Use when a search passage is cut off
    or you need surrounding context."""
    return _call(store.get_section, section_id)


@mcp.tool()
def lookup_term(game: str, term: str, edition: str | None = None) -> dict:
    """Look up a section by its exact title, such as a glossary or almanac entry
    ("Robber", "Longest Road", "Distance Rule"). Returns found=false with similar
    titles when there is no exact match."""
    return _call(store.lookup_term, game, term, edition=edition)


@mcp.prompt(name="ask-rules")
def ask_rules(game: str, question: str) -> str:
    """Answer a rules question strictly from the rulebook, with citations."""
    return (
        f"Answer this {game} rules question using only the bgrules tools: {question}\n\n"
        "Search first. Quote the exact rulebook text you rely on and cite section and page for every claim. "
        "If the passages don't settle it, say the rulebook doesn't address it. Do not use outside knowledge."
    )


def run() -> None:
    mcp.run()
