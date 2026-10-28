"""MCP server: verbatim rulebook passages with exact citations.

The server never writes answers. It returns rulebook text exactly as printed,
with where it is (section path, page, link); the client model writes the
answer from those passages.
"""

import hmac
import os
import platform
import shutil
import subprocess
import threading
import webbrowser
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer

from . import ingest, manifest, render, store

_INSTRUCTIONS = """\
This server gives you the official rulebook text for board games the user has installed.
When answering a rules question:
- Answer ONLY from passages returned by these tools. Never rely on your own memory of a game's rules.
- Quote the passage text that supports each claim, and cite it as: section path, page (e.g. "Almanac > Robber, p. 11").
- If search_rules returns found=false, or the returned passages do not actually settle the question, say the rulebook
  does not address it. Do not guess. You may mention the nearest sections the tool lists.
- A rulebook's silence is not a ruling. Don't turn it into one ("so you can't", "so it's allowed"); say the
  players decide, and quote any related rule as related, not as the answer.
- But a general rule does settle the specific cases it covers. If a returned rule applies to the situation
  (a rule about "any 1 die" covers a die that was just swapped), apply it: quote it and give the ruling it
  implies. Say the rulebook doesn't address a question only when no rule covers the situation.
- If the user uses a word the rulebook doesn't (a nickname for a piece or term), say the rulebook doesn't use
  it and ask, or offer the likely rulebook term as a possibility. Don't assume what they meant.
- Rules differ between editions; always state which edition you are citing.
- Copy quotes exactly from `quote`. If you shorten or splice a quote, check it with verify_quote first.
- Show the user the rule itself: once you know which passage your answer rests on, call show_passage for it,
  with `quote` set to the words you quote from it, so they see the printed rule with the quote underlined.
  Once per answer, for the main citation only, and as its own step: not in parallel with other tool calls,
  since some apps then shrink the image to a thumbnail. Skip it when the rulebook doesn't address the question.
  If its `highlight` is "approximate" because of "ocr" or "repeated", say the underline may be slightly off.
{open_pdf}
Official changes (errata):
- Some rules were officially changed after older rulebooks were printed. A result with `amended_by` is one of
  them. Then give BOTH: what the printed rule says (cite the rulebook), and the official change (cite its
  source document and page), and say which printings include the change - the player's own rulebook may show
  either version. Do not silently give only one.
- list_games lists each game's official changes. When a user starts playing or sets up a game, mention that
  game's official changes up front, before they matter.
- Only official, publisher-issued documents are indexed. Don't present fan FAQs or wikis as official rules.

{adding}"""

_LOCAL_ADDING = """\
Adding games:
- If the user asks about a game that isn't installed, check list_games' `available` list and offer to add it
  with add_game. Some documents can't be downloaded automatically: give the user the `download_page`, ask
  where they saved the PDF, and pass that path as add_game's `file`.
- If the game isn't in `available` either, say it isn't supported yet and give the user a link to request it:
  add_game's `request_link` (prefilled with the game) or list_games' `request_a_game`. Don't point them to
  unofficial rules sources instead.
- Also offer to add their own copy of the game's rulebook: if they have it as a PDF (from the publisher's
  website, or a scan of the printed one), ask where it is and call add_game with the game's name, `file` set to
  its path, and `edition` if they know it; then use the `game` id in the result. If they have a link to the
  official rulebook PDF instead, pass it as `url`. Use only a link the user gives you: don't search for one,
  since search turns up fan-hosted copies and summaries. Only the game's own rulebook: don't suggest adding fan
  FAQs, wikis or rules summaries. Its layout hasn't been checked, so citations are only as good as the reading
  of its headings: tell them so, pass on any `warning` in the result, and if it reads badly, suggest requesting
  the game instead. A game added from a link comes with a `request_link` that includes the link, so the user
  can ask for it to be supported properly.
- A document with `user_provided` is the user's own copy, not a checked publisher edition. When citing it, say
  it comes from the rulebook they added."""

# A remote instance (`serve --http`) is a copy of the user's ~/.bgrules, rebuilt by `bgrules deploy`.
_REMOTE_ADDING = """\
Adding games:
- This server is the user's private copy in Google Cloud, built from the rulebooks on their computer. It can't
  add games itself. If the user asks about a game that isn't in list_games' `installed` list, say this copy
  doesn't have it, and explain how to add it:
  1. On their computer, add the game as usual: ask Claude Desktop or Claude Code to add it, or run
     `bgrules add <game>`. It goes into ~/.bgrules, the folder this copy is built from.
  2. On the same computer, run the deploy command again: the same one line they ran the first time
     (`bgrules deploy`, or `uvx boardgame-rules-mcp@latest deploy`). It sends a fresh copy of ~/.bgrules to Google
     Cloud, which rebuilds this server in a few minutes.
  3. Nothing changes on their phone: the connector's link stays the same, and the game is there once the
     deploy finishes. Until then, this copy has the games from the previous deploy.
- If the game isn't in list_games' `available` list either, they can add their own copy of its rulebook PDF in
  step 1, or request the game with list_games' `request_a_game` link. Don't point them to unofficial rules
  sources instead.
- For a game this copy doesn't have, and only then, you may answer from general knowledge (an exception to
  "Answer ONLY from passages" above), but say plainly that it isn't from the rulebook, and still give the
  steps above.
- A document with `user_provided` is the user's own copy, not a checked publisher edition. When citing it, say
  it comes from the rulebook they added."""

INSTRUCTIONS = _INSTRUCTIONS.format(
    open_pdf="- open_in_rulebook opens the PDF on the user's computer at the cited page, to read the passage in context.\n",
    adding=_LOCAL_ADDING,
)
REMOTE_INSTRUCTIONS = _INSTRUCTIONS.format(open_pdf="", adding=_REMOTE_ADDING)

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
    """List the games, editions and rulebook documents installed,
    each game's official changes (errata) that older printed rulebooks lack, and
    the games that can be installed (`available`), and a link for requesting a game
    that isn't available (`request_a_game`). Installed documents marked
    `user_provided` are rulebooks the user added themselves. `how_to_add`, when
    present, says how to add a game that isn't installed."""
    return _call(lambda db: {
        "installed": [
            {**row, **({"user_provided": True} if manifest.is_user_added(row["game"], row["edition"], row["doc_id"]) else {})}
            for row in store.installed(db)
        ],
        "official_changes": store.official_changes(db),
        "available": manifest.available(),
        "request_a_game": manifest.request_link(),
        **({"how_to_add": store.REMOTE_HOW_TO_ADD} if store.REMOTE else {}),
    })


# Indexing a long rulebook can take a minute or two, longer than a client may
# wait for one tool call. add_game runs it in the background and waits a while;
# calling it again with the same arguments keeps waiting for the same job.
_ADD_WAIT_SECONDS = 40
_adding: dict[tuple, Future] = {}
_adding_lock = threading.Lock()
_add_executor = ThreadPoolExecutor(max_workers=1)


def _add_job(game: str, edition: str | None, file: str | None, document: str | None, url: str | None = None) -> dict:
    notes = []
    hint = lambda g, e, d: f'then call add_game with game="{g}", edition="{e}", document="{d}" and file set to the PDF\'s path.'
    try:
        report = ingest.add(game, edition=edition, file=file, doc_id=document, log=notes.append, supply_hint=hint,
                            allow_untested=False, url=url)
    except manifest.UnknownGame as e:
        return {"added": False, "error": e.args[0], "request_link": manifest.request_link(game),
                "own_copy": "If the user has this game's rulebook, call add_game again with `file` set to the PDF's path, "
                            "or `url` set to a link to it that they give you.",
                "notes": notes}
    except (ingest.IngestError, OSError) as e:  # urllib's errors are OSErrors
        return {"added": False, "error": str(e), "notes": notes}
    result = {"added": True, "game": manifest.game_id(game), "documents": report, "notes": notes}
    if url:
        result["request_link"] = manifest.request_link(game, url)
    return result


@mcp.tool()
def add_game(game: str, edition: str | None = None, file: str | None = None, document: str | None = None,
             url: str | None = None) -> dict:
    """Install a game's official rulebook on this computer so its rules can be searched.

    `game` is an id from list_games' `available` list. Downloads come only from the
    publisher links this tool ships with, and a file that doesn't match the known
    version is refused. For a document that can't be downloaded automatically, pass
    `file`: the path of the PDF the user downloaded (with `document` when the edition
    has several). Indexing can take a couple of minutes: if the result says
    status="indexing", call add_game again with the same arguments to keep waiting.

    For any other game, `game` is its name and `file` the path of the user's own
    rulebook PDF, or `url` a link to it that the user gave: it is indexed as a
    user-provided copy (`edition` if they know it). Calling again with neither
    re-indexes the copy already added.
    """
    if file:
        file = str(Path(file).expanduser())
    key = (game, edition, file, document, url)
    with _adding_lock:
        job = _adding.get(key)
        if job is None:
            job = _adding[key] = _add_executor.submit(_add_job, *key)
    try:
        return job.result(timeout=_ADD_WAIT_SECONDS)
    except TimeoutError:
        if job.done():  # the job itself raised TimeoutError
            raise
        return {"status": "indexing", "message": f"Still indexing {game}. Call add_game again with the same arguments to keep waiting."}
    finally:
        # A finished job, even one that failed unexpectedly, is forgotten so the next call starts afresh.
        if job.done():
            with _adding_lock:
                if _adding.get(key) is job:
                    del _adding[key]


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
def show_passage(citation_id: str, quote: str | None = None) -> list:
    """Show the cited passage as it appears in the printed rulebook: an image of
    the page region with a bar beside the passage. Pass `quote` (the words you
    quote from it; '...' may join fragments) to underline just those instead;
    `highlight` then says how exactly the underline fits ("exact", "approximate"
    with `highlight_reasons`, or "whole_passage" when it couldn't be placed)."""
    db = store.connect()
    try:
        cite, pdf_path = store.get_passage(db, citation_id)
    except store.LookupError_ as e:
        return [{"error": str(e)}]
    finally:
        db.close()
    summary = {k: cite[k] for k in ("citation_id", "game", "edition", "document", "section_path", "printed_page", "open_link") if k in cite}
    found = render.quote_lines(cite["lines"], quote) if quote and cite["lines"] else None
    if found:
        summary["highlight"] = found["highlight"]
        if found["reasons"]:
            summary["highlight_reasons"] = found["reasons"]
    elif quote:
        summary["highlight"] = "whole_passage"
        summary["note"] = (
            "This passage was indexed before quotes could be underlined, so the whole passage is marked. Don't re-add "
            "the game now; you may tell the user that re-adding it later enables underlines."
            if not cite["lines"] else
            "The quote isn't in this passage, so the whole passage is marked. Check it with verify_quote, "
            "or show the passage that contains it."
        )
    underline = found["boxes"] if found else None
    images = []
    # With a quote, only the pages it is on.
    for page in sorted({s["page"] for s in underline or cite["spans"]}):
        boxes = [s["bbox"] for s in cite["spans"] if s["page"] == page]
        on_page = [s["bbox"] for s in underline if s["page"] == page] if underline else None
        images.append(Image(data=render.render(pdf_path, page, boxes, scale=2.0, crop_margin=36, underline=on_page), format="png"))
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


# A remote instance is reached through a secret link, https://<host>/<token>/mcp: Claude's custom
# connectors can't send headers or sign in, so the token has to be in the URL.
MIN_TOKEN_LENGTH = 32


def remote_mode() -> None:
    """Drop what needs the user's own computer (adding games, opening the PDF), and
    tell the client how games get onto this copy instead."""
    mcp.remove_tool("add_game")
    mcp.remove_tool("open_in_rulebook")
    mcp._lowlevel_server.instructions = REMOTE_INSTRUCTIONS
    store.REMOTE = True


class _TokenGate:
    """Answers only /<token>/mcp. Every other path, a wrong token included, gets the
    same bare 404, so nothing shows that a server is there."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":  # starts the MCP session manager
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return
        token, _, rest = scope["path"].lstrip("/").partition("/")
        if not hmac.compare_digest(token.encode(), self.token) or rest != "mcp":
            await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"text/plain")]})
            return await send({"type": "http.response.body", "body": b"Not Found"})
        await self.app({**scope, "path": "/mcp", "raw_path": b"/mcp"}, receive, send)


def http_app(token: str):
    """The MCP server over streamable HTTP, stateless (any instance can answer any
    request, so it can scale to zero), behind the token."""
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"BGRULES_TOKEN must be at least {MIN_TOKEN_LENGTH} characters.")
    # host="0.0.0.0" turns off the SDK's localhost-only Host check: the token guards this server.
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0")
    return _TokenGate(app, token)


def run_http() -> None:
    """Serve over HTTP on $PORT, as Cloud Run expects."""
    import uvicorn

    token = os.environ.get("BGRULES_TOKEN", "")
    if len(token) < MIN_TOKEN_LENGTH:
        raise SystemExit(f"error: set BGRULES_TOKEN to a secret of at least {MIN_TOKEN_LENGTH} characters.")
    remote_mode()
    # No access log: request paths contain the token.
    uvicorn.run(http_app(token), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                access_log=False, server_header=False)
