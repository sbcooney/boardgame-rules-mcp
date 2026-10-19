"""SQLite storage and search for indexed rulebooks.

Everything lives under BGRULES_HOME (default ~/.bgrules): downloaded PDFs and
index.sqlite. Nothing here is ever committed to the repo.

Search is SQLite FTS5 (porter stemming) over passages. Each game edition has
its own FTS table, so ranking statistics (how rare a word is) come from that
edition's text alone: installing another game or edition never changes this
one's results. Each result reports `coverage`, the share of the query's content
words it contains; when the best result's coverage is below MIN_COVERAGE the
search reports found=false instead of returning weak matches.
"""

import json
import os
import re
import sqlite3
from pathlib import Path

MIN_COVERAGE = 0.5  # tune against the M3 eval set
CANDIDATES = 50  # keyword matches scored for coverage; a full-coverage passage ranked lower by bm25 still counts

STOPWORDS = set(
    """a an and are as at be but by can could do does did for from get got has have
    how i if in into is it its may me might must my of on or our should so than that
    the their them then there these they this to under up us was we were what when
    where which while who why will with would you your rule rules allowed""".split()
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    game TEXT, edition TEXT, doc_id TEXT, title TEXT, pdf_path TEXT, sha256 TEXT,
    PRIMARY KEY (game, edition, doc_id)
);
CREATE TABLE IF NOT EXISTS sections (
    section_id TEXT PRIMARY KEY, game TEXT, edition TEXT, doc_id TEXT, ord INTEGER,
    path TEXT, heading TEXT, page_start INTEGER, page_end INTEGER, text TEXT, spans TEXT
);
CREATE TABLE IF NOT EXISTS passages (
    citation_id TEXT PRIMARY KEY, section_id TEXT, game TEXT, edition TEXT, doc_id TEXT,
    ord INTEGER, page INTEGER, text TEXT, spans TEXT
);
-- Official changes published after a rulebook was printed (errata): which
-- section of the errata document changes which section of the rulebook.
CREATE TABLE IF NOT EXISTS amendments (
    game TEXT, edition TEXT, doc_id TEXT, source_section TEXT, target_doc TEXT,
    target_section TEXT, summary TEXT, printed_rule TEXT, included_in TEXT
);
"""


def home() -> Path:
    return Path(os.environ.get("BGRULES_HOME", Path.home() / ".bgrules"))


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or home() / "index.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    _migrate_shared_fts(db)
    _migrate_doc_columns(db)
    return db


def _migrate_doc_columns(db: sqlite3.Connection) -> None:
    """Add columns introduced after the first indexes were built."""
    cols = {r["name"] for r in db.execute("PRAGMA table_info(docs)")}
    with db:
        if "source_type" not in cols:
            db.execute("ALTER TABLE docs ADD COLUMN source_type TEXT DEFAULT 'rulebook'")
        if "page_offset" not in cols:
            # printed page = PDF page + page_offset (a cover or insert shifts them)
            db.execute("ALTER TABLE docs ADD COLUMN page_offset INTEGER DEFAULT 0")
        if "spreads" not in cols:
            # JSON {"first", "last", "split"}: PDF pages that hold two printed pages side by side
            db.execute("ALTER TABLE docs ADD COLUMN spreads TEXT")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def fts_table(game: str, edition: str) -> str:
    """Name of one game edition's search table (quoted for SQL)."""
    return '"fts_' + re.sub(r"[^a-z0-9]+", "_", f"{game}__{edition}".lower()) + '"'


def rebuild_fts(db: sqlite3.Connection, game: str, edition: str) -> None:
    """(Re)build an edition's search table from its stored passages."""
    t = fts_table(game, edition)
    db.execute(f"DROP TABLE IF EXISTS {t}")
    db.execute(f"CREATE VIRTUAL TABLE {t} USING fts5(heading, text, citation_id UNINDEXED, tokenize = 'porter unicode61')")
    db.execute(
        f"""INSERT INTO {t} (heading, text, citation_id)
            SELECT (SELECT group_concat(value, ' > ') FROM json_each(s.path)), p.text, p.citation_id
            FROM passages p JOIN sections s USING (section_id)
            WHERE p.game=? AND p.edition=? ORDER BY s.ord, p.ord""",
        (game, edition),
    )


def _migrate_shared_fts(db: sqlite3.Connection) -> None:
    """Indexes built before per-edition search tables kept one shared table;
    split it into per-edition tables once, from the stored passages."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='passages_fts'").fetchone() is None:
        return
    with db:
        for r in db.execute("SELECT DISTINCT game, edition FROM docs").fetchall():
            rebuild_fts(db, r["game"], r["edition"])
        db.execute("DROP TABLE passages_fts")


# ---- writing -------------------------------------------------------------


def replace_doc(db: sqlite3.Connection, game: str, edition: str, doc: dict, pdf_path: Path, sha256: str, sections) -> int:
    """Replace everything indexed for one document. Returns passage count."""
    doc_id = doc["id"]
    with db:
        for table in ("passages", "sections", "docs", "amendments"):
            db.execute(f"DELETE FROM {table} WHERE game=? AND edition=? AND doc_id=?", (game, edition, doc_id))

        db.execute(
            "INSERT INTO docs (game, edition, doc_id, title, pdf_path, sha256, source_type, page_offset, spreads) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (game, edition, doc_id, doc.get("title", doc_id), str(pdf_path), sha256,
             doc.get("source_type", "rulebook"), doc.get("extraction", {}).get("printed_page_offset", 0),
             json.dumps(doc["spreads"]) if doc.get("spreads") else None),
        )
        for a in doc.get("amends", []):
            db.execute(
                "INSERT INTO amendments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (game, edition, doc_id, a["source_section"], a["target_doc"], a["target_section"],
                 a["summary"], a.get("printed_rule", ""), a.get("included_in", "")),
            )
        seen: set[str] = set()
        n_passages = 0
        for ord_, sec in enumerate(sections):
            section_id = f"{game}-{edition}-{doc_id}-" + slug("-".join(sec.path))
            base, i = section_id, 2
            while section_id in seen:
                section_id, i = f"{base}-{i}", i + 1
            seen.add(section_id)
            db.execute(
                "INSERT INTO sections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (section_id, game, edition, doc_id, ord_, json.dumps(sec.path), sec.heading,
                 sec.page_start, sec.page_end, sec.text, json.dumps(sec.spans)),
            )
            for p_ord, passage in enumerate(sec.passages(), start=1):
                citation_id = f"{section_id}-{p_ord}"
                db.execute(
                    "INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (citation_id, section_id, game, edition, doc_id, p_ord, passage.page,
                     passage.text, json.dumps(passage.spans)),
                )
                n_passages += 1
        rebuild_fts(db, game, edition)
    return n_passages


# ---- reading -------------------------------------------------------------


class LookupError_(Exception):
    """A request the caller can fix (unknown game, ambiguous edition)."""


def installed(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        """SELECT d.game, d.edition, d.doc_id, d.title, d.source_type, COUNT(s.section_id) AS sections
           FROM docs d LEFT JOIN sections s USING (game, edition, doc_id)
           GROUP BY d.game, d.edition, d.doc_id ORDER BY d.game, d.edition"""
    )
    return [dict(r) for r in rows]


def resolve_edition(db: sqlite3.Connection, game: str, edition: str | None) -> str:
    editions = [r["edition"] for r in db.execute("SELECT DISTINCT edition FROM docs WHERE game=?", (game,))]
    if not editions:
        games = sorted({r["game"] for r in db.execute("SELECT game FROM docs")})
        raise LookupError_(f"Game {game!r} is not installed. Installed games: {games or 'none'}. Install it with add_game (or `bgrules add {game}`).")
    if edition is None:
        if len(editions) > 1:
            raise LookupError_(f"{game} has several editions installed {editions}; pass edition= (rules differ between editions).")
        return editions[0]
    if edition not in editions:
        raise LookupError_(f"Edition {edition!r} of {game} is not installed. Installed: {editions}")
    return edition


def _doc(db: sqlite3.Connection, game: str, edition: str, doc_id: str) -> sqlite3.Row:
    return db.execute(
        "SELECT title, pdf_path, source_type, page_offset, spreads FROM docs WHERE game=? AND edition=? AND doc_id=?",
        (game, edition, doc_id),
    ).fetchone()


# Printed page numbers. Most PDFs hold one printed page per PDF page, shifted by
# `page_offset` (an unnumbered cover). Some hold two-page spreads: PDF pages
# first..last each carry two printed pages, left and right of `split`.


def printed_page(doc: sqlite3.Row, pdf_page: int, x: float = 0.0) -> int:
    """The printed page at horizontal position `x` (points) of a PDF page."""
    page = pdf_page + (doc["page_offset"] or 0)
    if doc["spreads"]:
        sp = json.loads(doc["spreads"])
        page += max(0, min(pdf_page, sp["last"] + 1) - sp["first"])  # earlier spreads' extra pages
        if sp["first"] <= pdf_page <= sp["last"] and x >= sp["split"]:
            page += 1
    return page


def pdf_location(doc: sqlite3.Row, page: int) -> tuple[int, tuple[float, float] | None]:
    """The PDF page holding printed page `page`, and the x range of that half
    when it is one side of a spread (None for a whole page)."""
    if not doc["spreads"]:
        return page - (doc["page_offset"] or 0), None
    sp = json.loads(doc["spreads"])
    for pdf_page in range(1, page + 1):
        if printed_page(doc, pdf_page) == page:
            return pdf_page, ((0.0, sp["split"]) if sp["first"] <= pdf_page <= sp["last"] else None)
        if sp["first"] <= pdf_page <= sp["last"] and printed_page(doc, pdf_page, sp["split"]) == page:
            return pdf_page, (sp["split"], float("inf"))
    return page, None


def _span_x(spans: list[dict], pdf_page: int, last: bool = False) -> float:
    """Horizontal centre of the first (or last) highlighted box on a PDF page."""
    boxes = [s["bbox"] for s in spans if s["page"] == pdf_page]
    if not boxes:
        return 0.0
    b = boxes[-1] if last else boxes[0]
    return (b[0] + b[2]) / 2


def _within(path: str, section: str) -> bool:
    """True if a section path ("A > B > C") is `section` or one of its subsections."""
    return path == section or path.startswith(section + " > ")


def _section_paths(db: sqlite3.Connection, game: str, edition: str, doc_id: str) -> list[tuple[str, str]]:
    """(section_id, "A > B > C") for a document's sections, in reading order."""
    rows = db.execute("SELECT section_id, path FROM sections WHERE game=? AND edition=? AND doc_id=? ORDER BY ord", (game, edition, doc_id))
    return [(r["section_id"], " > ".join(json.loads(r["path"]))) for r in rows]


def check_amendments(db: sqlite3.Connection, game: str, edition: str) -> list[str]:
    """Official changes whose errata section, or the rulebook section they change,
    isn't among the indexed sections (e.g. after a heading was renamed). A target
    document that isn't installed yet is not a problem."""
    problems = []
    for a in db.execute("SELECT * FROM amendments WHERE game=? AND edition=?", (game, edition)).fetchall():
        if not any(_within(p, a["source_section"]) for _, p in _section_paths(db, game, edition, a["doc_id"])):
            problems.append(f"{a['doc_id']}: no section {a['source_section']!r}")
        targets = _section_paths(db, game, edition, a["target_doc"])
        if targets and not any(_within(p, a["target_section"]) for _, p in targets):
            problems.append(f"{a['target_doc']}: no section {a['target_section']!r} (changed by {a['doc_id']})")
    return problems


def amendments_for(db: sqlite3.Connection, game: str, edition: str, doc_id: str, path: list[str]) -> list[dict]:
    """Official changes to this rulebook section, each with a citation of the
    errata text, what the printed rule says, and which printings include it."""
    path_str = " > ".join(path)
    out = []
    for a in db.execute("SELECT * FROM amendments WHERE game=? AND edition=? AND target_doc=?", (game, edition, doc_id)).fetchall():
        if not _within(path_str, a["target_section"]):
            continue
        entry = {"summary": a["summary"], "printed_rule": a["printed_rule"], "included_in": a["included_in"]}
        source = next((sid for sid, p in _section_paths(db, game, edition, a["doc_id"]) if _within(p, a["source_section"])), None)
        if source is not None:
            src = db.execute("SELECT * FROM passages WHERE section_id=? ORDER BY ord LIMIT 1", (source,)).fetchone()
            sdoc = _doc(db, game, edition, a["doc_id"])
            spath = db.execute("SELECT path FROM sections WHERE section_id=?", (source,)).fetchone()["path"]
            entry["source"] = {
                "citation_id": src["citation_id"], "document": sdoc["title"], "source_type": sdoc["source_type"],
                "section_path": json.loads(spath), "quote": src["text"],
                "printed_page": printed_page(sdoc, src["page"], _span_x(json.loads(src["spans"]), src["page"])),
            }
        out.append(entry)
    return out


def citation(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    sec = db.execute("SELECT path FROM sections WHERE section_id=?", (row["section_id"],)).fetchone()
    doc = _doc(db, row["game"], row["edition"], row["doc_id"])
    path = json.loads(sec["path"])
    out = {
        "citation_id": row["citation_id"],
        "section_id": row["section_id"],
        "game": row["game"],
        "edition": row["edition"],
        "document": doc["title"],
        "source_type": doc["source_type"] or "rulebook",
        "section_path": path,
        "printed_page": printed_page(doc, row["page"], _span_x(json.loads(row["spans"]), row["page"])),
        "pdf_page": row["page"],
        "quote": row["text"],
        "spans": json.loads(row["spans"]),
        "open_link": Path(doc["pdf_path"]).as_uri() + f"#page={row['page']}",
    }
    amended = amendments_for(db, row["game"], row["edition"], row["doc_id"], path)
    if amended:
        out["amended_by"] = amended
    return out


def official_changes(db: sqlite3.Connection) -> dict:
    """Per game edition, the official changes not in older printed rulebooks."""
    out: dict[str, list[dict]] = {}
    for a in db.execute("SELECT game, edition, summary, included_in FROM amendments ORDER BY game"):
        out.setdefault(f"{a['game']} {a['edition']}", []).append({"summary": a["summary"], "included_in": a["included_in"]})
    return out


def query_terms(query: str) -> list[str]:
    # Ordinals become their number: "the 10th spot" matches "action space (10)".
    words = [re.sub(r"^(\d+)(?:st|nd|rd|th)$", r"\1", w) for w in re.findall(r"[a-z0-9]+", query.lower())]
    return list(dict.fromkeys(w for w in words if w not in STOPWORDS))


def expand_terms(game: str, terms: list[str]) -> list[list[str]]:
    """Each query term plus the rulebook words players mean by it (manifest
    `synonyms`), e.g. "thief" -> ["thief", "robber"]. Plurals use the singular entry."""
    from . import manifest

    synonyms = manifest.load().get(game, {}).get("synonyms", {})
    groups = []
    for t in terms:
        extra = synonyms.get(t) or (synonyms.get(t[:-1]) if t.endswith("s") else None) or []
        groups.append(list(dict.fromkeys([t, *(str(s).lower() for s in extra)])))
    return groups


def _fts_any(words: list[str]) -> str:
    return " OR ".join(f'"{w}"' for w in words)


def search(db: sqlite3.Connection, game: str, query: str, edition: str | None = None, max_results: int = 5) -> dict:
    edition = resolve_edition(db, game, edition)
    terms = query_terms(query)
    if not terms:
        return {"found": False, "game": game, "edition": edition, "reason": "Query has no searchable words.", "results": []}

    groups = expand_terms(game, terms)
    t = fts_table(game, edition)
    rows = db.execute(
        f"SELECT citation_id, bm25({t}, 4.0, 1.0) AS score FROM {t} WHERE {t} MATCH ? ORDER BY score LIMIT ?",
        (_fts_any([w for g in groups for w in g]), CANDIDATES),
    ).fetchall()

    scored = []
    for r in rows:
        # A term is covered if the passage has it or any of its rulebook synonyms.
        hits = sum(
            db.execute(f"SELECT 1 FROM {t} WHERE {t} MATCH ? AND citation_id=?", (_fts_any(g), r["citation_id"])).fetchone() is not None
            for g in groups
        )
        scored.append((hits / len(terms), r["score"], r["citation_id"]))
    # Rank by coverage first, then bm25 (lower is better).
    scored.sort(key=lambda s: (-s[0], s[1]))

    results = []
    for coverage, _, cid in scored[:max_results]:
        row = db.execute("SELECT * FROM passages WHERE citation_id=?", (cid,)).fetchone()
        results.append({**citation(db, row), "coverage": round(coverage, 2)})

    found = bool(results) and results[0]["coverage"] >= MIN_COVERAGE
    out = {"found": found, "game": game, "edition": edition, "query_terms": terms}
    if found:
        out["results"] = [r for r in results if r["coverage"] >= MIN_COVERAGE]
    else:
        out["reason"] = "No passage matches enough of the question. The rulebook may not address this."
        out["nearest"] = [{"section_path": r["section_path"], "section_id": r["section_id"], "printed_page": r["printed_page"]} for r in results[:3]]
        out["next_steps"] = (
            "Before concluding the rulebook is silent: retry with the rulebook's own terms "
            "(e.g. 'intersection' rather than 'space'), or open a likely section with get_section/lookup_term. "
            "If that also fails, tell the user the rulebook does not address this; do not answer from memory."
        )
    return out


def get_passage(db: sqlite3.Connection, citation_id: str) -> tuple[dict, str]:
    """A passage's citation plus the local PDF path it came from."""
    row = db.execute("SELECT * FROM passages WHERE citation_id=?", (citation_id,)).fetchone()
    if row is None:
        raise LookupError_(f"No passage {citation_id!r}. Use citation_id values returned by search_rules.")
    doc = db.execute(
        "SELECT pdf_path FROM docs WHERE game=? AND edition=? AND doc_id=?", (row["game"], row["edition"], row["doc_id"])
    ).fetchone()
    return citation(db, row), doc["pdf_path"]


def page_contents(db: sqlite3.Connection, game: str, page: int, edition: str | None = None, document: str | None = None) -> tuple[dict, str]:
    """What is on one printed page: the passages there, and the PDF to render.
    `document` (a doc id such as "appendix") is needed when an edition has several."""
    edition = resolve_edition(db, game, edition)
    docs = db.execute("SELECT doc_id, title, pdf_path FROM docs WHERE game=? AND edition=?", (game, edition)).fetchall()
    if document is not None:
        docs = [d for d in docs if d["doc_id"] == document]
        if not docs:
            known = [r["doc_id"] for r in db.execute("SELECT doc_id FROM docs WHERE game=? AND edition=?", (game, edition))]
            raise LookupError_(f"No document {document!r} for {game} {edition}. Installed: {known}")
    if len(docs) > 1:
        raise LookupError_(f"{game} {edition} has several documents {[d['doc_id'] for d in docs]}; pass document=.")
    doc = docs[0]
    pdf_page, half = pdf_location(_doc(db, game, edition, doc["doc_id"]), page)

    def here(span: dict) -> bool:
        x = (span["bbox"][0] + span["bbox"][2]) / 2
        return span["page"] == pdf_page and (half is None or half[0] <= x < half[1])
    rows = db.execute(
        """SELECT p.* FROM passages p JOIN sections s USING (section_id)
           WHERE p.game=? AND p.edition=? AND p.doc_id=? ORDER BY s.ord, p.ord""",
        (game, edition, doc["doc_id"]),
    ).fetchall()
    on_page = [r for r in rows if any(here(s) for s in json.loads(r["spans"]))]
    if not on_page:
        raise LookupError_(f"No indexed rules text on page {page} of {doc['title']}.")
    passages = [
        {"citation_id": r["citation_id"], "section_path": json.loads(db.execute("SELECT path FROM sections WHERE section_id=?", (r["section_id"],)).fetchone()["path"])}
        for r in on_page
    ]
    boxes = [s["bbox"] for r in on_page for s in json.loads(r["spans"]) if here(s)]
    return {
        "game": game,
        "edition": edition,
        "document": doc["title"],
        "printed_page": page,
        "passages": passages,
        "open_link": Path(doc["pdf_path"]).as_uri() + f"#page={pdf_page}",
        "_boxes": boxes,
        "_pdf_page": pdf_page,
    }, doc["pdf_path"]


def _normalize(text: str) -> str:
    text = text.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "­": ""}))
    return " ".join(text.split()).lower()


def quote_key(text: str) -> str:
    """Text reduced for quote matching. Ignores case, whitespace (PDF text has stray
    spaces: "and/ or", "card )"), quotation marks (answers write 7 for the rulebook's
    “7”), markdown emphasis and list bullets."""
    return re.sub(r"[\s\"'*•]", "", _normalize(text))


def quote_fragments(quote: str) -> list[str]:
    """Split a quote into the pieces that must each appear in the source: "..." or "…"
    mark omissions, [brackets] mark editorial insertions (an icon the PDF text lacks:
    "1 additional [Blight]"), and a spaced dash joins quoted pieces ("Green Heron — Trade ...")."""
    text = _normalize(quote).replace("*", "").replace("•", " ").replace("…", "...")
    pieces = re.split(r"\.\.\.|\[[^\]]*\]|\s-\s", text)
    return [p.strip(" .,;:") for p in pieces if p.strip(" .,;:")]


def verify_quote(db: sqlite3.Connection, citation_id: str, text: str) -> dict:
    """Check that `text` appears in the cited section, its fragments in order
    (see quote_fragments and quote_key for what is ignored)."""
    row = db.execute("SELECT section_id FROM passages WHERE citation_id=?", (citation_id,)).fetchone()
    if row is None:
        raise LookupError_(f"No passage {citation_id!r}. Use citation_id values returned by search_rules.")
    section_text = db.execute("SELECT text FROM sections WHERE section_id=?", (row["section_id"],)).fetchone()["text"]
    fragments = quote_fragments(text)
    if not fragments:
        return {"verified": False, "citation_id": citation_id, "reason": "Empty quote."}

    source, pos = quote_key(section_text), 0
    for frag in fragments:
        found_at = source.find(quote_key(frag), pos)
        if found_at < 0:
            import difflib

            normalized = _normalize(section_text)
            m = difflib.SequenceMatcher(None, normalized, frag, autojunk=False).find_longest_match(0, len(normalized), 0, len(frag))
            return {
                "verified": False,
                "citation_id": citation_id,
                "failed_fragment": frag,
                "closest_source_text": normalized[max(0, m.a - 40): m.a + m.size + 40],
                "reason": "This text is not in the cited section. Quote the rulebook exactly, or do not present it as a quote.",
            }
        pos = found_at + len(quote_key(frag))
    return {"verified": True, "citation_id": citation_id}


def get_section(db: sqlite3.Connection, section_id: str) -> dict:
    sec = db.execute("SELECT * FROM sections WHERE section_id=?", (section_id,)).fetchone()
    if sec is None:
        raise LookupError_(f"No section {section_id!r}. Use search_rules or lookup_term to find section ids.")
    doc = _doc(db, sec["game"], sec["edition"], sec["doc_id"])
    spans = json.loads(sec["spans"])
    neighbours = db.execute(
        "SELECT section_id, path, ord FROM sections WHERE game=? AND edition=? AND doc_id=? AND ord IN (?, ?) ORDER BY ord",
        (sec["game"], sec["edition"], sec["doc_id"], sec["ord"] - 1, sec["ord"] + 1),
    ).fetchall()
    out = {
        "section_id": sec["section_id"],
        "game": sec["game"],
        "edition": sec["edition"],
        "document": doc["title"],
        "source_type": doc["source_type"] or "rulebook",
        "section_path": json.loads(sec["path"]),
        "printed_page_start": printed_page(doc, sec["page_start"], _span_x(spans, sec["page_start"])),
        "printed_page_end": printed_page(doc, sec["page_end"], _span_x(spans, sec["page_end"], last=True)),
        "text": sec["text"],
        "spans": spans,
        "open_link": Path(doc["pdf_path"]).as_uri() + f"#page={sec['page_start']}",
        "previous_section": next(({"section_id": n["section_id"], "section_path": json.loads(n["path"])} for n in neighbours if n["ord"] < sec["ord"]), None),
        "next_section": next(({"section_id": n["section_id"], "section_path": json.loads(n["path"])} for n in neighbours if n["ord"] > sec["ord"]), None),
    }
    amended = amendments_for(db, sec["game"], sec["edition"], sec["doc_id"], json.loads(sec["path"]))
    if amended:
        out["amended_by"] = amended
    return out


def lookup_term(db: sqlite3.Connection, game: str, term: str, edition: str | None = None) -> dict:
    """Exact (case-insensitive) heading match, e.g. an almanac entry or glossary term."""
    import difflib

    edition = resolve_edition(db, game, edition)
    rows = db.execute("SELECT section_id, heading, path FROM sections WHERE game=? AND edition=?", (game, edition)).fetchall()
    exact = [r for r in rows if r["heading"].lower() == term.strip().lower()]
    if exact:
        return {"found": True, "game": game, "edition": edition, "sections": [get_section(db, r["section_id"]) for r in exact]}
    headings = {r["heading"]: r for r in rows}
    close = difflib.get_close_matches(term, list(headings), n=5, cutoff=0.5)
    close += [h for h in headings if term.lower() in h.lower() and h not in close]
    return {
        "found": False,
        "game": game,
        "edition": edition,
        "reason": f"No section titled {term!r}.",
        "similar_headings": [{"heading": h, "section_id": headings[h]["section_id"], "section_path": json.loads(headings[h]["path"])} for h in close[:5]],
    }
