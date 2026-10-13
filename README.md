# boardgame-rules-mcp

An MCP server that answers board game rules questions from the **official rulebook**, and points you to the exact section and page every answer comes from.

> Status: early development (milestone M1: Catan MVP).

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

## Supported games

| Game | Rulebook source | Status |
| --- | --- | --- |
| Catan (2020 edition) | Official PDF, auto-downloaded | Working |
| Wingspan | User-supplied PDF | Planned |
| Spirit Island | User-supplied PDF | Planned |

## Results

_Eval results (accuracy and citation precision with vs. without the server) will go here._

## License

MIT
