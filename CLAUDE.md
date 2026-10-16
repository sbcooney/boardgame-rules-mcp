# boardgame-rules-mcp

Local-first MCP server that answers board game rules questions **only** from official rulebooks, with exact section/page citations.

## Plan

The full plan is a Claude Doc: https://claude.ai/code/artifact/01c25fbe-d66d-4052-a474-f496b25828b2
Read it with the docs tools before starting work. Its milestone checklist (M0–M5) is the source of truth for status.

## Non-negotiable rules

- **The server never writes answers.** Tools return verbatim rulebook text plus locations (game, edition, section path, printed page, PDF page, bbox). The client model writes the answer.
- **No hallucination.** Low-confidence search returns an explicit `found: false`, never padded weak matches. Searches are always scoped to one edition.
- **Only publisher-issued sources.** Rulebooks and errata published by the publisher (e.g. in a later rulebook) only. Community FAQs, wikis and forum posts are leads, never sources, even when a rulebook links to them. A publisher's own document that is only available on a community site (e.g. a Ravensburger Villain Guide PDF on BoardGameGeek) may be added as `user_supplied` with a `download_page`, never auto-downloaded, its title saying where it came from. An erratum is linked to the rulebook section it changes (`amends` in the manifest), and answers must give both the printed rule and the official change, with which printings include it.
- **Never commit rulebook content.** PDFs, extracted text and indexes live in `~/.bgrules` (`BGRULES_HOME`), outside the repo. The repo holds only `src/bgrules/manifest.yaml` with official links and checksums. No publisher artwork or page images either.

## Stack

Python ≥3.11, official MCP Python SDK v2 (MCPServer), pdfplumber + pypdfium2 (permissive licenses; project is MIT, so no PyMuPDF/AGPL), SQLite FTS5, pytest. Managed with `uv`.

## Commands

- `uv sync` — install deps
- `uv run pytest` — tests

## Workflow

- **Every change goes through a pull request.** `main` is protected: no direct pushes or force-pushes, and the `test (3.11)` and `test (3.13)` checks must pass. Work on a branch, push it, open a PR; the user merges.
- **After any extraction change, re-index every installed game** and diff each section's text against before. Only the targeted rulebook should change; layout fixes are opt-in manifest hints for this reason.
- **Releasing to PyPI** (see CONTRIBUTING.md):
  1. In a PR, bump `version` in `pyproject.toml` (semver: 0.1.1 for fixes, 0.2.0 for new features or games). Merge it.
  2. Pull `main`, then tag and push the tag: `git tag v0.1.1`, then `git push origin v0.1.1`. Confirm with the user first: a published version can't be reused.
  3. `.github/workflows/release.yml` refuses a tag that doesn't match the version, runs the tests, builds, and publishes with trusted publishing.
  4. Check the release on a clean setup: `uvx boardgame-rules-mcp --version` with a fresh `BGRULES_HOME` and `UV_CACHE_DIR`.
