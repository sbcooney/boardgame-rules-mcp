# boardgame-rules-mcp

An MCP server that answers board game rules questions from the **official rulebook**, and points you to the exact section and page every answer comes from.

> Status: early development (milestone M0 — PDF extraction spike).

## How it works

- You add a game; the server downloads the official rulebook PDF (or uses one you provide) and indexes it **locally**. No rulebook content is stored in this repo.
- Your AI assistant (Claude, ChatGPT, etc.) calls the server's tools, which return verbatim rulebook passages with citations.
- The assistant answers only from those passages, or says the rulebook doesn't address the question.

## Supported games (planned)

| Game | Rulebook source |
| --- | --- |
| Catan (2020 edition) | Official PDF, auto-downloaded |
| Wingspan | User-supplied PDF |
| Spirit Island | User-supplied PDF |

## Results

_Eval results (accuracy and citation precision with vs. without the server) will go here._

## License

MIT
