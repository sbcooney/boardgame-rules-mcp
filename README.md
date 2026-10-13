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
| Wingspan | User-supplied PDF | Planned |
| Spirit Island | User-supplied PDF | Planned |

## Results

### Retrieval (server only, no model)

47 Catan 2020 questions in [`evals/catan_2020.yaml`](evals/catan_2020.yaml): direct lookups, rules arguments and edge cases, questions worded differently from the rulebook ("thief" for robber), and questions the rulebook doesn't answer. A hit means a section that settles the question is among the top results.

| Category | n | Hit@1 | Hit@3 | Hit@5 | Correctly said "not found" |
| --- | --- | --- | --- | --- | --- |
| lookup | 15 | 53% | 73% | 73% | – |
| edge | 22 | 77% | 95% | 95% | – |
| vocab | 5 | 40% | 40% | 60% | – |
| not_addressed | 5 | – | – | – | 40% |
| **all answerable** | 42 | 64% | 81% | 83% | – |

Run it: `uv run python evals/retrieval.py --misses`

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
