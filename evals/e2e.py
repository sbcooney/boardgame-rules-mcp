"""End-to-end eval: does a model answer rules questions better WITH this server
than from memory alone?

Runs through Claude Code's headless mode (`claude -p`), so it uses the Claude
subscription you're logged in with, not API billing. Never pass --bare: that
mode only authenticates with an API key.

Each question is asked under two conditions:
  memory  - no tools at all; the model answers from what it knows
  server  - only the bgrules MCP server (search/lookup/section/verify tools)
A grader call then scores each answer against the expected ruling. Quotes in
answers are checked locally against the indexed rulebook (no model needed).

Answers and grades are cached in evals/results/ (gitignored: they contain
rulebook quotes), keyed on everything that affects them, so reruns only redo
what changed.

Usage:
  uv run python evals/e2e.py --dry-run              # print commands, call nothing
  uv run python evals/e2e.py --limit 1              # smoke test: 1 question
  uv run python evals/e2e.py                        # core subset (~15 questions)
  uv run python evals/e2e.py --all                  # every question
Options: --conditions memory,server  --model sonnet  --grader-model haiku  --fresh
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import yaml

from bgrules import store

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
CALL_TIMEOUT = 300

SERVER_TOOLS = ["search_rules", "get_section", "lookup_term", "verify_quote", "list_games"]
# Never offered in evals: open_in_rulebook opens browser tabs; images cost usage.
BLOCKED_TOOLS = ["open_in_rulebook", "show_passage", "get_page"]

ANSWER_PROMPT = """\
You answer board game rules questions. The game is {game_name}, {edition_desc}.
Answer the question concisely. Cite the rulebook section and page for each rule you rely on,
and quote the rulebook's exact words where you can. If the rulebook does not address the question, say so."""

GRADER_PROMPT = """\
You grade answers to board game rules questions. Reply with ONLY a JSON object, no prose, no code fences.

Question: {question}
{expectation}

Answer to grade:
<answer>
{answer}
</answer>

Return:
{{"ruling": "correct" | "partial" | "incorrect",
  "abstained": true if the answer says the rulebook does not address or settle the question, else false,
  "cited_sections": [section names the answer cites as its source, e.g. "Robber" or "Almanac > Distance Rule"; [] if none],
  "note": "one short sentence on what was wrong, or empty"}}

Grading rules:
- "correct": states the expected ruling, including any exception it names. "partial": right core ruling but misses
  or garbles part of it. "incorrect": wrong ruling, or no ruling.
- For a question the rulebook does not address, "correct" means the answer says the rulebook doesn't cover it
  (sensible house-rule suggestions are fine if clearly labelled); stating an invented official rule is "incorrect"."""


def sha(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def claude_cmd(prompt: str, system: str, condition: str, mcp_config: Path | None, model: str | None) -> list[str]:
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--system-prompt", system,
        "--tools", "",  # no built-in tools in any condition
        "--strict-mcp-config",
        "--setting-sources", "project",  # cwd is an empty temp dir: no user hooks or settings
        "--no-session-persistence",
    ]
    if condition == "server":
        cmd += ["--mcp-config", str(mcp_config)]
        cmd += ["--allowedTools", ",".join(f"mcp__bgrules__{t}" for t in SERVER_TOOLS)]
        cmd += ["--disallowedTools", ",".join(f"mcp__bgrules__{t}" for t in BLOCKED_TOOLS)]
    if model:
        cmd += ["--model", model]
    return cmd


def run_claude(cmd: list[str], workdir: Path) -> dict:
    """Run one headless call and summarise its stream-json transcript."""
    proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=CALL_TIMEOUT)
    out = {"tools_available": None, "tool_calls": [], "result": None, "is_error": False, "cost_usd": None, "num_turns": None}
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            out["tools_available"] = ev.get("tools", [])
        elif ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    out["tool_calls"].append({"name": block["name"], "input": block.get("input", {})})
        elif ev.get("type") == "result":
            out["result"] = ev.get("result")
            out["is_error"] = ev.get("is_error", False)
            out["cost_usd"] = ev.get("total_cost_usd")
            out["num_turns"] = ev.get("num_turns")
    if out["result"] is None:
        raise RuntimeError(f"claude -p produced no result (exit {proc.returncode}): {proc.stderr.strip()[:500]}")
    return out


def check_isolation(condition: str, tools: list[str] | None) -> None:
    tools = tools or []
    ours = [t for t in tools if t.startswith("mcp__bgrules__")]
    others = [t for t in tools if not t.startswith("mcp__bgrules__")]
    if condition == "memory" and tools:
        raise RuntimeError(f"memory condition must have no tools, but had: {tools}")
    if condition == "server":
        if not ours:
            raise RuntimeError("server condition has no bgrules tools; is the server failing to start?")
        if others:
            raise RuntimeError(f"server condition has non-bgrules tools: {others}")


# ---- local quote check ------------------------------------------------------


def quotes_in(answer: str, min_words: int = 6) -> list[str]:
    # A "> " blockquote is one quote, even when it contains quotation marks of its own.
    blocks = re.findall(r"(?:^>[^\n]*(?:\n|$))+", answer, re.M)
    for b in blocks:
        answer = answer.replace(b, "\n")
    blocks = [re.sub(r"^>\s?", "", b, flags=re.M) for b in blocks]
    # Drop attribution lines ("— Almanac > Robber, p. 11") from blockquotes.
    blocks = [re.sub(r"^\s*[—–]\s.*$", "", b, flags=re.M).strip().strip("“”\"") for b in blocks]
    found = re.findall(r"“([^”]+)”|\"([^\"]+)\"", answer)
    quotes = blocks + [q for pair in found for q in pair]
    # A quoted section path ("The Four Actions > Option 1") is a citation, not a quote.
    return [q for q in quotes if q and len(q.split()) >= min_words and " > " not in q]


def fabricated_quotes(answer: str, rulebook_text: str) -> tuple[int, list[str]]:
    """(number of quotes checked, quotes not found verbatim in the rulebook), matched the
    same way as the server's verify_quote (store.quote_fragments, store.quote_key), except
    that fragments may come from anywhere in the rulebook and under 3 words are skipped."""
    quotes = quotes_in(answer)
    source = store.quote_key(rulebook_text)
    bad = []
    for q in quotes:
        if any(len(f.split()) >= 3 and store.quote_key(f) not in source for f in store.quote_fragments(q)):
            bad.append(q)
    return len(quotes), bad


# ---- grading -----------------------------------------------------------------


def parse_grade(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"grader returned no JSON: {text[:200]}")
    return json.loads(m.group(0))


def cited_correctly(cited: list[str], expected: list[str]) -> bool:
    """A cited section counts if it names an expected section (by its last path part)."""
    names = {e.split(" > ")[-1].lower() for e in expected}
    return any(any(n in c.lower() or c.lower().split(" > ")[-1] in names for n in names) for c in cited)


# ---- main ----------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="?", default=str(ROOT / "catan_2020.yaml"))
    ap.add_argument("--all", action="store_true", help="every question, not just the core subset")
    ap.add_argument("--limit", type=int, help="only the first N questions (after subsetting)")
    ap.add_argument("--ids", help="only these comma-separated question ids (from any subset)")
    ap.add_argument("--conditions", default="memory,server")
    ap.add_argument("--model", help="answering model (default: your Claude Code default)")
    ap.add_argument("--grader-model", default="haiku")
    ap.add_argument("--fresh", action="store_true", help="ignore cached answers and grades")
    ap.add_argument("--dry-run", action="store_true", help="print the commands without calling claude")
    args = ap.parse_args()

    if not shutil.which("claude"):
        print("error: `claude` CLI not found", file=sys.stderr)
        return 1

    spec = yaml.safe_load(Path(args.spec).read_text())
    questions = spec["questions"]
    if args.ids:
        ids = set(args.ids.split(","))
        questions = [q for q in questions if q["id"] in ids]
    elif not args.all:
        core = set(spec.get("core", []))
        questions = [q for q in questions if q["id"] in core]
    if args.limit:
        questions = questions[: args.limit]
    conditions = args.conditions.split(",")

    db = store.connect()
    rulebook = store._normalize(
        " ".join(r["text"] for r in db.execute("SELECT text FROM sections WHERE game=? AND edition=?", (spec["game"], spec["edition"])))
    )
    if not rulebook:
        print(f"error: {spec['game']} {spec['edition']} is not installed (bgrules add ...)", file=sys.stderr)
        return 1

    system = ANSWER_PROMPT.format(game_name=spec.get("game_name", spec["game"]), edition_desc=spec.get("edition_desc", f"{spec['edition']} edition"))
    workdir = Path(tempfile.mkdtemp(prefix="bgrules-eval-"))
    mcp_config = workdir.parent / f"{workdir.name}-mcp.json"
    mcp_config.write_text(json.dumps({"mcpServers": {"bgrules": {"command": sys.executable, "args": ["-m", "bgrules.cli", "serve"]}}}))

    # Server answers depend on the server's code and index: changing either
    # invalidates them, so a rerun after an improvement actually measures it.
    src = Path(store.__file__).parent
    server_fingerprint = sha(rulebook, *[p.read_text() for p in sorted(src.glob("*.py"))])

    calls = len(questions) * len(conditions) * 2
    print(f"{len(questions)} questions × {len(conditions)} conditions = {calls} claude calls at most (fewer with cache)\n", file=sys.stderr)

    rows = []
    try:
        for q in questions:
            for cond in conditions:
                key = sha(q["question"], system, cond, args.model, (SERVER_TOOLS, server_fingerprint) if cond == "server" else None)
                cache = RESULTS / cond / f"{q['id']}-{key}.json"
                cmd = claude_cmd(q["question"], system, cond, mcp_config, args.model)
                if args.dry_run:
                    print(f"[{cond}] {q['id']}: " + " ".join(repr(c) if " " in c or not c else c for c in cmd))
                    continue

                if cache.exists() and not args.fresh:
                    rec = json.loads(cache.read_text())
                else:
                    print(f"[{cond}] {q['id']} …", file=sys.stderr)
                    rec = {"id": q["id"], "category": q["category"], "condition": cond, "answer": run_claude(cmd, workdir)}
                    check_isolation(cond, rec["answer"]["tools_available"])
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps(rec, indent=2, ensure_ascii=False))

                answer_text = rec["answer"]["result"] or ""
                expectation = (
                    "Expected: the rulebook does NOT address this question."
                    if q["category"] == "not_addressed"
                    else f"Expected ruling: {q['answer']}\nRulebook sections that settle it: {', '.join(q['sections'])}"
                )
                if q["category"] == "errata":
                    expectation += (
                        "\nThis rule was officially changed after older rulebooks were printed. 'correct' requires BOTH the "
                        "official change and what older printed rulebooks say, with which printings have which; an answer "
                        "giving only one of them is 'partial'."
                    )
                grade_key = sha(key, expectation, args.grader_model, GRADER_PROMPT)
                if rec.get("grade_key") != grade_key or args.fresh:
                    gcmd = ["claude", "-p", GRADER_PROMPT.format(question=q["question"], expectation=expectation, answer=answer_text),
                            "--output-format", "stream-json", "--verbose", "--tools", "", "--strict-mcp-config",
                            "--setting-sources", "project", "--no-session-persistence",
                            "--system-prompt", "You are a strict, fair grader. Output JSON only.",
                            "--model", args.grader_model]
                    rec["grade"] = parse_grade(run_claude(gcmd, workdir)["result"])
                    rec["grade_key"] = grade_key
                    cache.write_text(json.dumps(rec, indent=2, ensure_ascii=False))

                n_quotes, bad = fabricated_quotes(answer_text, rulebook)
                g = rec["grade"]
                rows.append({
                    "id": q["id"], "category": q["category"], "condition": cond,
                    "score": {"correct": 1.0, "partial": 0.5}.get(g.get("ruling"), 0.0),
                    "abstained": bool(g.get("abstained")),
                    "cited": bool(g.get("cited_sections")),
                    "cited_right": cited_correctly(g.get("cited_sections", []), q["sections"]) if q["sections"] else None,
                    "quotes": n_quotes, "fabricated": len(bad), "bad_quotes": bad,
                    "tool_calls": len(rec["answer"]["tool_calls"]),
                    "note": g.get("note", ""),
                })
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        mcp_config.unlink(missing_ok=True)

    if args.dry_run:
        return 0
    report(rows, conditions)
    return 0


def report(rows: list[dict], conditions: list[str]) -> None:
    by = defaultdict(list)
    for r in rows:
        by[r["condition"]].append(r)
    pct = lambda n, d: f"{n / d:.0%}" if d else "–"
    print("| Metric | " + " | ".join(conditions) + " |")
    print("| --- |" + " --- |" * len(conditions))
    metrics = [
        ("Ruling accuracy (answerable)", lambda rs: pct(sum(r["score"] for r in rs if r["category"] != "not_addressed"), sum(r["category"] != "not_addressed" for r in rs))),
        ("Said \"not covered\" when it isn't", lambda rs: pct(sum(r["abstained"] for r in rs if r["category"] == "not_addressed"), sum(r["category"] == "not_addressed" for r in rs))),
        ("Wrongly said \"not covered\"", lambda rs: pct(sum(r["abstained"] for r in rs if r["category"] != "not_addressed"), sum(r["category"] != "not_addressed" for r in rs))),
        ("Cited a correct section", lambda rs: pct(sum(bool(r["cited_right"]) for r in rs if r["cited_right"] is not None), sum(r["cited_right"] is not None for r in rs))),
        ("Quotes checked", lambda rs: str(sum(r["quotes"] for r in rs))),
        ("Quotes not in the rulebook", lambda rs: f"{sum(r['fabricated'] for r in rs)} ({pct(sum(r['fabricated'] for r in rs), sum(r['quotes'] for r in rs))})"),
    ]
    for name, fn in metrics:
        print(f"| {name} | " + " | ".join(fn(by[c]) for c in conditions) + " |")
    print("\nPer question (score memory/server):")
    ids = list(dict.fromkeys(r["id"] for r in rows))
    for i in ids:
        cells = {r["condition"]: r for r in rows if r["id"] == i}
        line = "  ".join(f"{c}={cells[c]['score']:.1f}{'*' if cells[c]['fabricated'] else ''}" for c in conditions if c in cells)
        notes = "; ".join(cells[c]["note"] for c in conditions if c in cells and cells[c]["note"])
        print(f"- {i}: {line}" + (f"   ({notes})" if notes else ""))
    print("\n* = answer contains a quote that is not in the rulebook")


if __name__ == "__main__":
    sys.exit(main())
