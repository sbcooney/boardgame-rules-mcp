# boardgame-rules-mcp

Local-first MCP server that answers board game rules questions **only** from official rulebooks, with exact section/page citations.

## Plan

The full plan is a Claude Doc: https://claude.ai/code/artifact/01c25fbe-d66d-4052-a474-f496b25828b2
Read it with the docs tools before starting work. Its milestone checklist (M0–M5) is the source of truth for status.

## Non-negotiable rules

- **The server never writes answers.** Tools return verbatim rulebook text plus locations (game, edition, section path, printed page, PDF page, bbox). The client model writes the answer.
- **No hallucination.** Low-confidence search returns an explicit `found: false`, never padded weak matches. Searches are always scoped to one edition.
- **Never commit rulebook content.** PDFs, extracted text and indexes live under `data/` (gitignored). The repo holds only `src/bgrules/manifest.yaml` with official links and checksums.

## Stack

Python ≥3.11, official MCP Python SDK v2 (MCPServer), pdfplumber + pypdfium2 (permissive licenses; project is MIT, so no PyMuPDF/AGPL), SQLite FTS5, pytest. Managed with `uv`.

## Commands

- `uv sync` — install deps
- `uv run pytest` — tests
