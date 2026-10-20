"""How well do rulebooks nobody has tuned read? Re-indexes each installed
rulebook with the default extraction hints for user-added rulebooks
(extract.default_hints) instead of its own, into a scratch index, and runs the
retrieval eval questions against it.

The default sections are named differently from the tuned ones, so a result
can't be matched by section path. It counts as a hit when its text overlaps
the text of a tuned section that settles the question: at least half of the
passage's 6-word runs are in that section, or it holds at least half of that
section's runs (a short section inside a longer passage). The bundled games'
synonyms still apply, so this measures extraction alone.

Usage: uv run python evals/default_extraction.py [evals/*.yaml] [--hints default|none|tuned]
(needs the games installed: `bgrules add <game>`; OCR'd documents take a few minutes)
"""

import argparse
import json
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

import yaml

from bgrules import store
from bgrules.extract import build_sections, default_hints, extract_lines


def shingles(text: str, n: int = 6) -> set[tuple]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {tuple(words[i:i + n]) for i in range(max(len(words) - n + 1, 1))}


def within(path: str, expected: list[str]) -> bool:
    return any(path == e or path.startswith(e + " > ") for e in expected)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("specs", nargs="*", default=sorted(str(p) for p in Path(__file__).parent.glob("*.yaml")))
    ap.add_argument("--hints", choices=["default", "none", "tuned"], default="default",
                    help="default hints, no hints at all, or the manifest's own (checks the overlap measure)")
    args = ap.parse_args()

    tuned = store.connect()
    scratch = tuned if args.hints == "tuned" else store.connect(Path(tempfile.mkdtemp()) / "index.sqlite")
    total = Counter()
    print("| Game | Edition | Sections | Hit@5 |\n| --- | --- | --- | --- |")
    for spec_path in args.specs:
        spec = yaml.safe_load(Path(spec_path).read_text())
        game, edition = spec["game"], spec["edition"]
        docs = tuned.execute("SELECT * FROM docs WHERE game=? AND edition=?", (game, edition)).fetchall()
        if not docs:
            print(f"error: {game} {edition} isn't installed", file=sys.stderr)
            return 1
        n_sections = 0
        for d in docs if scratch is not tuned else []:
            hints = default_hints(d["pdf_path"]) if args.hints == "default" else {}
            sections = build_sections(extract_lines(d["pdf_path"], hints))
            n_sections += len(sections)
            store.replace_doc(scratch, game, edition, {"id": d["doc_id"], "title": d["title"]}, Path(d["pdf_path"]), d["sha256"], sections)
        rows = tuned.execute("SELECT path, text FROM sections WHERE game=? AND edition=?", (game, edition)).fetchall()
        hits = n = 0
        for q in spec["questions"]:
            if q["category"] == "not_addressed" or not q["sections"]:
                continue
            expected = set().union(*(shingles(r["text"]) for r in rows if within(" > ".join(json.loads(r["path"])), q["sections"])))
            results = store.search(scratch, game, q["question"], edition=edition, max_results=5).get("results", [])
            hit = any(expected and (len(s & expected) >= len(s) / 2 or len(s & expected) >= len(expected) / 2)
                      for s in (shingles(r["quote"]) for r in results))
            hits, n = hits + hit, n + 1
        total.update(hits=hits, n=n)
        print(f"| {spec.get('game_name', game)} | {edition} | {n_sections or '–'} | {hits}/{n} = {hits / n:.0%} |")
    print(f"| All | | | {total['hits']}/{total['n']} = {total['hits'] / total['n']:.0%} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
