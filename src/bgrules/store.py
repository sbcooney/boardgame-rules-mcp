"""SQLite storage and search for indexed rulebooks.

Everything lives under BGRULES_HOME (default ~/.bgrules): downloaded PDFs and
index.sqlite. Nothing here is ever committed to the repo.

Search is SQLite FTS5 (porter stemming) over passages, scoped to one game and
edition. Each result reports `coverage`, the share of the query's content words
it contains; when the best result's coverage is below MIN_COVERAGE the search
reports found=false instead of returning weak matches.
"""

import json
import os
import re
import sqlite3
from pathlib import Path

MIN_COVERAGE = 0.5  # tune against the M3 eval set

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
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
    heading, text, citation_id UNINDEXED, game UNINDEXED, edition UNINDEXED,
    tokenize = 'porter unicode61'
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
    return db


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# ---- writing -------------------------------------------------------------


def replace_doc(db: sqlite3.Connection, game: str, edition: str, doc: dict, pdf_path: Path, sha256: str, sections) -> int:
    """Replace everything indexed for one document. Returns passage count."""
    doc_id = doc["id"]
    with db:
        old = [r["citation_id"] for r in db.execute("SELECT citation_id FROM passages WHERE game=? AND edition=? AND doc_id=?", (game, edition, doc_id))]
        db.executemany("DELETE FROM passages_fts WHERE citation_id=?", [(c,) for c in old])
        for table in ("passages", "sections", "docs"):
            db.execute(f"DELETE FROM {table} WHERE game=? AND edition=? AND doc_id=?", (game, edition, doc_id))

        db.execute(
            "INSERT INTO docs VALUES (?, ?, ?, ?, ?, ?)",
            (game, edition, doc_id, doc.get("title", doc_id), str(pdf_path), sha256),
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
                db.execute(
                    "INSERT INTO passages_fts VALUES (?, ?, ?, ?, ?)",
                    (" > ".join(sec.path), passage.text, citation_id, game, edition),
                )
                n_passages += 1
    return n_passages


# ---- reading -------------------------------------------------------------


class LookupError_(Exception):
    """A request the caller can fix (unknown game, ambiguous edition)."""


def installed(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        """SELECT d.game, d.edition, d.doc_id, d.title, COUNT(s.section_id) AS sections
           FROM docs d LEFT JOIN sections s USING (game, edition, doc_id)
           GROUP BY d.game, d.edition, d.doc_id ORDER BY d.game, d.edition"""
    )
    return [dict(r) for r in rows]


def resolve_edition(db: sqlite3.Connection, game: str, edition: str | None) -> str:
    editions = [r["edition"] for r in db.execute("SELECT DISTINCT edition FROM docs WHERE game=?", (game,))]
    if not editions:
        games = sorted({r["game"] for r in db.execute("SELECT game FROM docs")})
        raise LookupError_(f"Game {game!r} is not installed. Installed games: {games or 'none'}. Install with: bgrules add {game}")
    if edition is None:
        if len(editions) > 1:
            raise LookupError_(f"{game} has several editions installed {editions}; pass edition= (rules differ between editions).")
        return editions[0]
    if edition not in editions:
        raise LookupError_(f"Edition {edition!r} of {game} is not installed. Installed: {editions}")
    return edition


def citation(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    sec = db.execute("SELECT path FROM sections WHERE section_id=?", (row["section_id"],)).fetchone()
    doc = db.execute(
        "SELECT title, pdf_path FROM docs WHERE game=? AND edition=? AND doc_id=?",
        (row["game"], row["edition"], row["doc_id"]),
    ).fetchone()
    return {
        "citation_id": row["citation_id"],
        "section_id": row["section_id"],
        "game": row["game"],
        "edition": row["edition"],
        "document": doc["title"],
        "source_type": "rulebook",
        "section_path": json.loads(sec["path"]),
        # M0: printed page numbers match PDF pages for the documents indexed so far.
        "printed_page": row["page"],
        "pdf_page": row["page"],
        "quote": row["text"],
        "spans": json.loads(row["spans"]),
        "open_link": Path(doc["pdf_path"]).as_uri() + f"#page={row['page']}",
    }


def query_terms(query: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", query.lower())
    return list(dict.fromkeys(w for w in words if w not in STOPWORDS))


def search(db: sqlite3.Connection, game: str, query: str, edition: str | None = None, max_results: int = 5) -> dict:
    edition = resolve_edition(db, game, edition)
    terms = query_terms(query)
    if not terms:
        return {"found": False, "game": game, "edition": edition, "reason": "Query has no searchable words.", "results": []}

    match = " OR ".join(f'"{t}"' for t in terms)
    rows = db.execute(
        """SELECT citation_id, bm25(passages_fts, 4.0, 1.0) AS score FROM passages_fts
           WHERE passages_fts MATCH ? AND game=? AND edition=? ORDER BY score LIMIT ?""",
        (match, game, edition, max_results * 3),
    ).fetchall()

    scored = []
    for r in rows:
        hits = sum(
            db.execute("SELECT 1 FROM passages_fts WHERE passages_fts MATCH ? AND citation_id=?", (f'"{t}"', r["citation_id"])).fetchone() is not None
            for t in terms
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


def page_contents(db: sqlite3.Connection, game: str, page: int, edition: str | None = None) -> tuple[dict, str]:
    """What is on one printed page: the passages there, and the PDF to render."""
    edition = resolve_edition(db, game, edition)
    docs = db.execute("SELECT doc_id, title, pdf_path FROM docs WHERE game=? AND edition=?", (game, edition)).fetchall()
    if len(docs) != 1:
        raise LookupError_(f"{game} {edition} has {len(docs)} documents; page lookup supports single-document editions for now.")
    doc = docs[0]
    rows = db.execute(
        """SELECT p.* FROM passages p JOIN sections s USING (section_id)
           WHERE p.game=? AND p.edition=? AND p.doc_id=? ORDER BY s.ord, p.ord""",
        (game, edition, doc["doc_id"]),
    ).fetchall()
    on_page = [r for r in rows if any(s["page"] == page for s in json.loads(r["spans"]))]
    if not on_page:
        raise LookupError_(f"No indexed rules text on page {page} of {doc['title']}.")
    passages = [
        {"citation_id": r["citation_id"], "section_path": json.loads(db.execute("SELECT path FROM sections WHERE section_id=?", (r["section_id"],)).fetchone()["path"])}
        for r in on_page
    ]
    boxes = [s["bbox"] for r in on_page for s in json.loads(r["spans"]) if s["page"] == page]
    return {
        "game": game,
        "edition": edition,
        "document": doc["title"],
        "printed_page": page,
        "passages": passages,
        "open_link": Path(doc["pdf_path"]).as_uri() + f"#page={page}",
        "_boxes": boxes,
    }, doc["pdf_path"]


def _normalize(text: str) -> str:
    text = text.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "­": ""}))
    return " ".join(text.split()).lower()


def verify_quote(db: sqlite3.Connection, citation_id: str, text: str) -> dict:
    """Check that `text` appears in the cited section. Ellipses ("..." or "…")
    may join fragments, which must then appear in order."""
    row = db.execute("SELECT section_id FROM passages WHERE citation_id=?", (citation_id,)).fetchone()
    if row is None:
        raise LookupError_(f"No passage {citation_id!r}. Use citation_id values returned by search_rules.")
    source = _normalize(db.execute("SELECT text FROM sections WHERE section_id=?", (row["section_id"],)).fetchone()["text"])
    fragments = [f.strip(" .,;:") for f in _normalize(text).replace("…", "...").split("...")]
    fragments = [f for f in fragments if f]
    if not fragments:
        return {"verified": False, "citation_id": citation_id, "reason": "Empty quote."}

    pos = 0
    for frag in fragments:
        found_at = source.find(frag, pos)
        if found_at < 0:
            import difflib

            m = difflib.SequenceMatcher(None, source, frag, autojunk=False).find_longest_match(0, len(source), 0, len(frag))
            closest = source[max(0, m.a - 40): m.a + m.size + 40]
            return {
                "verified": False,
                "citation_id": citation_id,
                "failed_fragment": frag,
                "closest_source_text": closest,
                "reason": "This text is not in the cited section. Quote the rulebook exactly, or do not present it as a quote.",
            }
        pos = found_at + len(frag)
    return {"verified": True, "citation_id": citation_id}


def get_section(db: sqlite3.Connection, section_id: str) -> dict:
    sec = db.execute("SELECT * FROM sections WHERE section_id=?", (section_id,)).fetchone()
    if sec is None:
        raise LookupError_(f"No section {section_id!r}. Use search_rules or lookup_term to find section ids.")
    doc = db.execute("SELECT title, pdf_path FROM docs WHERE game=? AND edition=? AND doc_id=?", (sec["game"], sec["edition"], sec["doc_id"])).fetchone()
    neighbours = db.execute(
        "SELECT section_id, path, ord FROM sections WHERE game=? AND edition=? AND doc_id=? AND ord IN (?, ?) ORDER BY ord",
        (sec["game"], sec["edition"], sec["doc_id"], sec["ord"] - 1, sec["ord"] + 1),
    ).fetchall()
    return {
        "section_id": sec["section_id"],
        "game": sec["game"],
        "edition": sec["edition"],
        "document": doc["title"],
        "section_path": json.loads(sec["path"]),
        "printed_page_start": sec["page_start"],
        "printed_page_end": sec["page_end"],
        "text": sec["text"],
        "spans": json.loads(sec["spans"]),
        "open_link": Path(doc["pdf_path"]).as_uri() + f"#page={sec['page_start']}",
        "previous_section": next(({"section_id": n["section_id"], "section_path": json.loads(n["path"])} for n in neighbours if n["ord"] < sec["ord"]), None),
        "next_section": next(({"section_id": n["section_id"], "section_path": json.loads(n["path"])} for n in neighbours if n["ord"] > sec["ord"]), None),
    }


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
