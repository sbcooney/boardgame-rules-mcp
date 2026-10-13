# boardgame-rules-mcp

An MCP server that answers board game rules questions from the **official rulebook**, and points you to the exact section and page every answer comes from.

> Status: early development (milestone M2: checkable citations).

## How it works

- You add a game; the server downloads the official rulebook PDF (or uses one you provide) and indexes it **locally** in `~/.bgrules`. No rulebook content is stored in this repo.
- Your AI assistant (Claude, ChatGPT, etc.) calls the server's tools, which return verbatim rulebook passages with citations: section, page, and a link that opens the PDF at that page.
- The assistant answers only from those passages, or says the rulebook doesn't address the question.

## Quick start

Requires [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/sbcooney/boardgame-rules-mcp && cd boardgame-rules-mcp
uv sync
uv run bgrules add catan --edition 2020
uv run bgrules search catan "What happens when I move the robber?"
```

### Connect to Claude Code

```sh
claude mcp add bgrules -- uv --directory /path/to/boardgame-rules-mcp run bgrules serve
```

### Connect to Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "bgrules": {
      "command": "uv",
      "args": ["--directory", "/path/to/boardgame-rules-mcp", "run", "bgrules", "serve"]
    }
  }
}
```

If Claude Desktop can't find `uv`, use its full path (`which uv`).

## Tools

| Tool | Returns |
| --- | --- |
| `list_games` | Installed games, editions and documents |
| `search_rules(game, query, edition?)` | Verbatim passages with citations, or `found: false` with the nearest sections |
| `get_section(section_id)` | A full section, plus the sections before and after it |
| `lookup_term(game, term, edition?)` | An almanac/glossary entry by exact title |
| `show_passage(citation_id)` | An image of the rulebook page region with the cited passage highlighted |
| `get_page(game, page, edition?)` | A whole page image with rules text highlighted, plus the passages on it |
| `open_in_rulebook(citation_id)` | Opens the PDF on your computer at the cited page (Chrome/Firefox/Edge/Brave jump to the page; Preview can't) |
| `verify_quote(citation_id, text)` | Confirms a quote appears word for word in the cited section |

## Supported games

| Game | Rulebook source | Status |
| --- | --- | --- |
| Catan (2020 edition) | Official PDF, auto-downloaded | Working |
| Wingspan (base game: rulebook r24 + appendix r17) | User-supplied PDFs from [Stonemaier's official folder](https://www.dropbox.com/scl/fo/cfbnrvnwwms1fz7589byc/AI8I9LEYzDESkEkm_s3FhSo?rlkey=cykjw2zk47cehkebrl3a5wl7b&dl=0): `bgrules add wingspan --doc rulebook --file WS_Rulebook_r24.pdf`, then `--doc appendix --file WS_Appendix_r17.pdf` | Working |
| Spirit Island | User-supplied PDF | Planned |

## Results

### Retrieval (server only, no model)

Question sets cover direct lookups, rules arguments and edge cases, questions worded differently from the rulebook ("thief" for robber), and questions the rulebook doesn't answer. A hit means a section that settles the question (or a subsection of it) is among the top results.

#### Catan (2020 edition): [`evals/catan_2020.yaml`](evals/catan_2020.yaml)

| Category | n | Hit@1 | Hit@3 | Hit@5 | Hit@5 before synonyms | Correctly said "not found" |
| --- | --- | --- | --- | --- | --- | --- |
| lookup | 15 | 53% | 73% | 73% | 73% | – |
| edge | 22 | 73% | 95% | 95% | 95% | – |
| vocab | 5 | 40% | 60% | 80% | 60% | – |
| vocab, held out | 10 | 60% | 90% | 100% | 30% | – |
| independent | 5 | 60% | 80% | 80% | – | – |
| not_addressed | 5 | – | – | – | – | 40% |
| **all answerable** | 57 | 61% | 84% | 88% | – | – |

#### Wingspan (base game, rulebook r24 + appendix r17): [`evals/wingspan_1st.yaml`](evals/wingspan_1st.yaml)

| Category | n | Hit@1 | Hit@3 | Hit@5 | Hit@5 before synonyms | Correctly said "not found" |
| --- | --- | --- | --- | --- | --- | --- |
| lookup | 12 | 33% | 83% | 92% | 92% | – |
| edge | 17 | 71% | 76% | 88% | 82% | – |
| vocab | 5 | 40% | 40% | 80% | 40% | – |
| appendix (specific birds, bonus cards) | 12 | 100% | 100% | 100% | 100% | – |
| not_addressed | 4 | – | – | – | – | 0% (25%) |
| **all answerable** | 46 | 65% | 80% | 91% | 85% | – |

The appendix explains every bird power and bonus card; questions about a specific bird now find it 100% of the time. It costs a little elsewhere: a few rulebook questions rank lower or drop out of the top 5 ("How many birds per habitat?"), and the larger index gives off-topic questions more partial matches. Its bird-power pages are tables (bird names beside shared power text), read with a table layout that keeps each power sentence intact with its birds listed before it.

Wingspan's synonym list ("dice tower" → birdfeeder, "reroll" → throw, "market" → bird tray) leaves out words the rulebook already uses with a different meaning ("move" a cube, "score" a goal, player-mat "rows"), which had pushed two appendix answers down. Same caveat as Catan: the vocab questions were written by the list's author, so 80% is optimistic; "When am I allowed to reroll the birdfeeder?", an ordinary edge-case question, going from a miss to #2 is the more honest signal.

Synonyms map table talk to rulebook terms ("thief" → robber, "port" → harbor; see `synonyms` in [`manifest.yaml`](src/bgrules/manifest.yaml)). **Caveat:** the held-out questions were committed before the synonym list, but by the same author in the same session, and the list ended up covering most of their wording, so their 100% is optimistic. The *independent* questions are the fairer test: written by a Catan player without seeing the synonym list, they score 80%. The miss ("How many cards do I deal out?") assumes a mechanic Catan doesn't have; no cards are dealt, which keyword search can't bridge.

Run it: `uv run python evals/retrieval.py --misses` (Catan) or `uv run python evals/retrieval.py evals/wingspan_1st.yaml --misses`

Keyword overlap can't reliably tell that a rulebook *doesn't* cover something, so that judgment is left to the model reading the returned passages. The end-to-end eval measures it.

### End-to-end (with vs. without the server)

_Results coming soon._ The harness asks each question twice through Claude Code's headless mode: once with no tools (answering from memory), and once with only this server. A grader call scores each answer, and every quote is checked against the rulebook locally. Metrics: ruling accuracy, correct "not covered" answers, cited sections, and quotes that aren't in the rulebook.

It runs on your Claude subscription (no API key or billing), uses a 15-question core subset by default, and caches answers so reruns only redo what changed:

```sh
uv run python evals/e2e.py --dry-run   # show the commands, call nothing
uv run python evals/e2e.py --limit 1   # smoke test
uv run python evals/e2e.py             # core subset; --all for every question
```

## License

MIT
