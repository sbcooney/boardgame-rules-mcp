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
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.replace(dest)


def add(game: str, edition: str | None = None, file: str | None = None, doc_id: str | None = None, log=print) -> list[dict]:
    editions = manifest.editions(game)
    if edition is None:
        if len(editions) > 1:
            raise IngestError(f"{game} has several editions {list(editions)}; choose one with --edition (rules differ between editions).")
        edition = next(iter(editions))
    if edition not in editions:
        raise IngestError(f"Unknown edition {edition!r} for {game}. Known: {list(editions)}")
    docs = editions[edition]["docs"]
    doc_ids = [d["id"] for d in docs]
    if doc_id is not None:
        if doc_id not in doc_ids:
            raise IngestError(f"Unknown document {doc_id!r} for {game} {edition}. Known: {doc_ids}")
        docs = [d for d in docs if d["id"] == doc_id]
    if file and len(docs) > 1:
        raise IngestError(f"{game} {edition} has several documents {doc_ids}; say which one this file is with --doc.")

    db = store.connect()
    report = []
    for doc in docs:
        dest = store.home() / "pdfs" / game / edition / f"{doc['id']}.pdf"
        expected = doc.get("sha256") or doc.get("expected_sha256")
        if file:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, dest)
        elif doc.get("url"):
            if not (dest.exists() and expected and sha256_of(dest) == expected):
                log(f"Downloading {doc['title']} from {doc['url']}")
                download(doc["url"], dest)
        else:
            raise IngestError(
                f"{game} {edition} '{doc['title']}' has no official download link "
                f"({doc.get('source', 'no source')}). Get the PDF yourself, then run:\n"
                f"  bgrules add {game} --edition {edition} --doc {doc['id']} --file /path/to/{doc['id']}.pdf"
            )

        digest = sha256_of(dest)
        if expected and digest != expected:
            if doc.get("sha256"):
                raise IngestError(
                    f"Checksum mismatch for {doc['title']}: the file differs from the version this tool was built "
                    f"against (expected {expected[:12]}…, got {digest[:12]}…). Refusing to index a possibly different edition."
                )
            log(f"Warning: {dest.name} differs from the known version of this rulebook; citations may not match.")

        log(f"Extracting {doc['title']}…")
        lines = extract_lines(str(dest), doc.get("extraction", {}))
        sections = build_sections(lines, doc.get("parts"))
        n = store.replace_doc(db, game, edition, doc, dest, digest, sections)
        content = doc.get("extraction", {}).get("content_pages")
        pages_with_text = {ln.page for ln in lines}
        skipped = set(doc.get("extraction", {}).get("skip_pages", []))
        empty = [p for p in range(content[0], content[1] + 1) if p not in pages_with_text and p not in skipped] if content else []
        report.append({"doc": doc["title"], "sections": len(sections), "passages": n, "pages_without_text": empty})
        log(f"Indexed {len(sections)} sections, {n} passages" + (f"; WARNING no text on pages {empty}" if empty else ""))
    return report
