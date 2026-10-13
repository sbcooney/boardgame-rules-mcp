"""Retrieval eval: does search_rules surface a section that settles each question,
and does it report found=false for questions the rulebook doesn't address?

No model involved: deterministic, free, fast. Measures the server alone.

Usage: uv run python evals/retrieval.py [evals/catan_2020.yaml] [--misses] [--markdown]
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import yaml

from bgrules import store

CATEGORY_ORDER = ["lookup", "edge", "vocab", "vocab_holdout", "independent", "not_addressed"]


def is_hit(got: str, expected: list[str]) -> bool:
    """A result hits if it is an expected section or a subsection of one
    ("Game Rules > 3. Build > b) Settlement ..." is within "Game Rules > 3. Build")."""
    return any(got == e or got.startswith(e + " > ") for e in expected)


def run(path: Path, k_values=(1, 3, 5)) -> tuple[list[dict], dict]:
    spec = yaml.safe_load(path.read_text())
    db = store.connect()
    rows = []
    for q in spec["questions"]:
        res = store.search(db, spec["game"], q["question"], edition=spec["edition"], max_results=max(k_values))
        got = [" > ".join(r["section_path"]) for r in res.get("results", [])]
        row = {"id": q["id"], "category": q["category"], "found": res["found"], "got": got, "expected": q["sections"]}
        if q["category"] == "not_addressed":
            row["correct"] = not res["found"]
        else:
            for k in k_values:
                row[f"hit@{k}"] = any(is_hit(s, q["sections"]) for s in got[:k])
            row["correct"] = row[f"hit@{max(k_values)}"]
        rows.append(row)

    summary = defaultdict(dict)
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    for cat, rs in by_cat.items():
        s = summary[cat]
        s["n"] = len(rs)
        if cat == "not_addressed":
            s["abstained"] = sum(r["correct"] for r in rs) / len(rs)
        else:
            s["found"] = sum(r["found"] for r in rs) / len(rs)
            for k in k_values:
                s[f"hit@{k}"] = sum(r[f"hit@{k}"] for r in rs) / len(rs)
    answerable = [r for r in rows if r["category"] != "not_addressed"]
    summary["all answerable"] = {
        "n": len(answerable),
        "found": sum(r["found"] for r in answerable) / len(answerable),
        **{f"hit@{k}": sum(r[f"hit@{k}"] for r in answerable) / len(answerable) for k in k_values},
    }
    return rows, summary


def table(summary: dict) -> str:
    pct = lambda v: f"{v:.0%}" if isinstance(v, float) else "–"
    lines = [
        "| Category | n | Found | Hit@1 | Hit@3 | Hit@5 | Correctly said \"not found\" |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for cat in [*CATEGORY_ORDER, "all answerable"]:
        if cat not in summary:
            continue
        s = summary[cat]
        lines.append(
            f"| {cat} | {s['n']} | {pct(s.get('found'))} | {pct(s.get('hit@1'))} | {pct(s.get('hit@3'))} "
            f"| {pct(s.get('hit@5'))} | {pct(s.get('abstained'))} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec", nargs="?", default=str(Path(__file__).with_name("catan_2020.yaml")))
    ap.add_argument("--misses", action="store_true", help="list each failed question")
    args = ap.parse_args()

    try:
        rows, summary = run(Path(args.spec))
    except store.LookupError_ as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(table(summary))
    if args.misses:
        print("\nMisses:")
        for r in rows:
            if not r["correct"]:
                print(f"- [{r['category']}] {r['id']}: found={r['found']} got={r['got'][:3]} expected={r['expected'][:2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
