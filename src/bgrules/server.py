"""MCP server: verbatim rulebook passages with exact citations.

The server never writes answers. It returns rulebook text exactly as printed,
with where it is (section path, page, link); the client model writes the
answer from those passages.
"""

import platform
import shutil
import subprocess
import webbrowser
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer

from . import render, store

INSTRUCTIONS = """\
This server gives you the official rulebook text for board games the user has installed.
When answering a rules question:
- Answer ONLY from passages returned by these tools. Never rely on your own memory of a game's rules.
- Quote the passage text that supports each claim, and cite it as: section path, page (e.g. "Almanac > Robber, p. 11").
- If search_rules returns found=false, or the returned passages do not actually settle the question, say the rulebook
  does not address it. Do not guess. You may mention the nearest sections the tool lists.
- Rules differ between editions; always state which edition you are citing.
- Copy quotes exactly from `quote`. If you shorten or splice a quote, check it with verify_quote first.
- To let the user check a citation: show_passage shows the rulebook page with the passage highlighted;
  open_in_rulebook opens the PDF on their computer at that page.

Official changes (errata):
- Some rules were officially changed after older rulebooks were printed. A result with `amended_by` is one of
  them. Then give BOTH: what the printed rule says (cite the rulebook), and the official change (cite its
  source document and page), and say which printings include the change - the player's own rulebook may show
  either version. Do not silently give only one.
- list_games lists each game's official changes. When a user starts playing or sets up a game, mention that
  game's official changes up front, before they matter.
- Only official, publisher-issued documents are indexed. Don't present fan FAQs or wikis as official rules."""

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
    """List the games, editions and rulebook documents installed on this machine,
    and each game's official changes (errata) that older printed rulebooks lack."""
    return _call(lambda db: {"installed": store.installed(db), "official_changes": store.official_changes(db)})


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


@mcp.tool(structured_output=False)
def show_passage(citation_id: str) -> list:
    """Show the cited passage as it appears in the printed rulebook: an image of
    the page region with the passage highlighted. Use when the user wants to see
    or check where an answer came from."""
    db = store.connect()
    try:
        cite, pdf_path = store.get_passage(db, citation_id)
    except store.LookupError_ as e:
        return [{"error": str(e)}]
    finally:
        db.close()
    images = []
    for page in sorted({s["page"] for s in cite["spans"]}):
        boxes = [s["bbox"] for s in cite["spans"] if s["page"] == page]
        images.append(Image(data=render.render(pdf_path, page, boxes, scale=2.0, crop_margin=36), format="png"))
    summary = {k: cite[k] for k in ("citation_id", "game", "edition", "document", "section_path", "printed_page", "open_link")}
    return [summary, *images]


@mcp.tool(structured_output=False)
def get_page(game: str, page: int, edition: str | None = None, document: str | None = None) -> list:
    """Show a whole rulebook page (printed page number) with its rules text
    highlighted, and list the passages on it with their citation ids.
    `document` (e.g. "appendix") is needed when a game has several documents;
    list_games shows them."""
    db = store.connect()
    try:
        info, pdf_path = store.page_contents(db, game, page, edition=edition, document=document)
    except store.LookupError_ as e:
        return [{"error": str(e)}]
    finally:
        db.close()
    boxes = info.pop("_boxes")
    pdf_page = info.pop("_pdf_page")
    return [info, Image(data=render.render(pdf_path, pdf_page, boxes, scale=1.25), format="png")]


def _open_pdf_at(pdf_path: str, page: int) -> str:
    """Open the PDF at a page in a viewer that honours #page=N. Returns the app used."""
    url = Path(pdf_path).as_uri() + f"#page={page}"
    if platform.system() == "Darwin":
        # Launch the browser binary directly: `open -a App file:///…#page=N` drops
        # the #page fragment, so the PDF opens at page 1. A running browser hands
        # the URL to its existing window; otherwise the browser starts detached.
        browsers = {
            "Google Chrome": "Google Chrome",
            "Firefox": "firefox",
            "Microsoft Edge": "Microsoft Edge",
            "Brave Browser": "Brave Browser",
        }
        for app, binary in browsers.items():
            exe = Path(f"/Applications/{app}.app/Contents/MacOS/{binary}")
            if exe.exists():
                subprocess.Popen([str(exe), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                return app
        subprocess.run(["open", pdf_path], check=True)  # Preview: can't jump to a page
        return "default PDF viewer (could not jump to the page)"
    if shutil.which("xdg-open"):
        subprocess.run(["xdg-open", url], check=True)
        return "xdg-open"
    webbrowser.open(url)
    return "web browser"


@mcp.tool()
def open_in_rulebook(citation_id: str) -> dict:
    """Open the rulebook PDF on the user's computer at the cited page, so they can
    read the passage in context."""
    db = store.connect()
    try:
        cite, pdf_path = store.get_passage(db, citation_id)
    except store.LookupError_ as e:
        return {"error": str(e)}
    finally:
        db.close()
    try:
        app = _open_pdf_at(pdf_path, cite["pdf_page"])
    except (OSError, subprocess.CalledProcessError) as e:
        return {"opened": False, "error": str(e), "open_link": cite["open_link"]}
    return {"opened": True, "app": app, "printed_page": cite["printed_page"], "section_path": cite["section_path"]}


@mcp.tool()
def verify_quote(citation_id: str, text: str) -> dict:
    """Check that `text` appears word for word in the cited section (ignoring
    whitespace and curly vs straight quotes; '...' may join fragments in order).
    Use before presenting a shortened or spliced quote."""
    return _call(store.verify_quote, citation_id, text)


@mcp.prompt(name="setup-game")
def setup_game(game: str, players: int) -> str:
    """Walk through setting up a game from the rulebook, flagging official changes."""
    return (
        f"Walk me through setting up {game} for {players} players, using only the bgrules tools. "
        "First call list_games and tell me about any official changes for this game that affect setup, "
        "including what older printed rulebooks say instead. Then go step by step, citing section and page."
    )


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
