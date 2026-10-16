"""Add a game: fetch (or copy) its rulebook PDFs, verify them, and index them."""

import hashlib
import shutil
import urllib.request
from pathlib import Path

from . import manifest, store
from .extract import build_sections, extract_lines


class IngestError(Exception):
    pass


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "bgrules (board game rules indexer)"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def cli_hint(game: str, edition: str, doc_id: str) -> str:
    return f"then run:\n  bgrules add {game} --edition {edition} --doc {doc_id} --file /path/to/{doc_id}.pdf"


def add(game: str, edition: str | None = None, file: str | None = None, doc_id: str | None = None, log=print,
        supply_hint=cli_hint, allow_untested=True) -> list[dict]:
    """`supply_hint(game, edition, doc_id)` says how to add a document users must download themselves.
    With allow_untested=False, editions whose extraction hasn't been checked are refused."""
    editions = manifest.editions(game)
    if not allow_untested:
        untested = [e for e, ed in editions.items() if ed.get("status") == "untested"]
        if edition in untested:
            raise IngestError(
                f"{game} {edition} isn't supported here yet: its rulebook hasn't been checked, so citations could be "
                f"incomplete. Supported: {[e for e in editions if e not in untested] or 'none'}."
            )
        editions = {e: ed for e, ed in editions.items() if e not in untested}
        if not editions:
            raise IngestError(f"{game} isn't supported here yet: its rulebook hasn't been checked.")
    if edition is None:
        if len(editions) > 1:
            raise IngestError(f"{game} has several editions {list(editions)}; choose one (rules differ between editions).")
        edition = next(iter(editions))
    if edition not in editions:
        raise IngestError(f"Unknown edition {edition!r} for {game}. Known: {list(editions)}")
    if editions[edition].get("status") == "untested":
        log(f"Warning: {game} {edition} hasn't been checked yet; citations may be incomplete or out of order.")
    docs = editions[edition]["docs"]
    doc_ids = [d["id"] for d in docs]
    if doc_id is not None:
        if doc_id not in doc_ids:
            raise IngestError(f"Unknown document {doc_id!r} for {game} {edition}. Known: {doc_ids}")
        docs = [d for d in docs if d["id"] == doc_id]
    if file and len(docs) > 1:
        raise IngestError(f"{game} {edition} has several documents {doc_ids}; say which one this file is (--doc, or add_game's document).")

    db = store.connect()
    report = []
    missing = []  # documents users must download themselves, not supplied this time
    for doc in docs:
        dest = store.home() / "pdfs" / game / edition / f"{doc['id']}.pdf"
        expected = doc.get("sha256") or doc.get("expected_sha256")
        # A new copy goes to a .part file first and replaces the installed PDF only
        # once its checksum passes, so a refused file never clobbers a good one.
        incoming = dest.with_suffix(".part")
        if file:
            if not Path(file).is_file():
                raise IngestError(f"No such file: {file}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, incoming)
        elif doc.get("url"):
            if dest.exists() and expected and sha256_of(dest) == expected:
                incoming = None
            else:
                log(f"Downloading {doc['title']} from {doc['url']}")
                download(doc["url"], incoming)
        else:
            where = f" Download it from {doc['download_page']}," if doc.get("download_page") else " Get the PDF yourself,"
            missing.append(f"'{doc['title']}' can't be downloaded automatically.{where} {supply_hint(game, edition, doc['id'])}")
            continue

        if incoming is not None:
            digest = sha256_of(incoming)
            if doc.get("sha256") and digest != expected:
                incoming.unlink()
                raise IngestError(
                    f"Checksum mismatch for {doc['title']}: the file differs from the version this tool was built "
                    f"against (expected {expected[:12]}…, got {digest[:12]}…). Refusing to index a possibly different "
                    "edition; any installed copy is unchanged. The publisher may have updated the file: a newer release may "
                    "know it (`uv tool upgrade boardgame-rules-mcp`, or update the Claude Desktop extension). If that doesn't help, "
                    "report it at https://github.com/sbcooney/boardgame-rules-mcp/issues."
                )
            incoming.replace(dest)
        digest = sha256_of(dest)
        if expected and digest != expected:
            log(f"Warning: {dest.name} differs from the known version of this rulebook; citations may not match.")

        log(f"Extracting {doc['title']}…")
        lines = extract_lines(str(dest), doc.get("extraction", {}))
        ocr_pages = sorted({ln.page for ln in lines if ln.ocr})
        if ocr_pages:
            # Say so wherever this document is cited: OCR can misread a character.
            log(f"Pages {ocr_pages} have no text layer; read them by OCR. Check quotes against the page images.")
            if "OCR" not in doc.get("title", ""):
                doc = {**doc, "title": f"{doc.get('title', doc['id'])} (text read by OCR)"}
        ex = doc.get("extraction", {})
        sections = build_sections(lines, doc.get("parts"), preamble_heading=ex.get("preamble_heading", "Introduction"),
                                  column_continuations=ex.get("column_continuations", False))
        spreads = doc.get("extraction", {}).get("spreads")
        if spreads:
            # Two-page spreads split down the middle of the PDF page.
            import pdfplumber

            with pdfplumber.open(dest) as pdf:
                doc = {**doc, "spreads": {"first": spreads[0], "last": spreads[1], "split": pdf.pages[spreads[0] - 1].width / 2}}
        n = store.replace_doc(db, game, edition, doc, dest, digest, sections)
        content = doc.get("extraction", {}).get("content_pages")
        pages_with_text = {ln.page for ln in lines}
        skipped = set(doc.get("extraction", {}).get("skip_pages", []))
        empty = [p for p in range(content[0], content[1] + 1) if p not in pages_with_text and p not in skipped] if content else []
        report.append({"doc": doc["title"], "sections": len(sections), "passages": n, "pages_without_text": empty})
        log(f"Indexed {len(sections)} sections, {n} passages" + (f"; WARNING no text on pages {empty}" if empty else ""))

    if missing and not report:
        # Nothing could be installed: every requested document needs a file.
        raise IngestError(f"{game} {edition}: " + "\n".join(missing))
    for note in missing:
        log(f"Skipped {note}")

    problems = store.check_amendments(db, game, edition)
    if problems:
        raise IngestError(
            "Indexed, but these official changes no longer match a section, so answers would miss them "
            "(fix `amends` in the manifest, or the extraction hints that name the sections):\n  " + "\n  ".join(problems)
        )
    return report
