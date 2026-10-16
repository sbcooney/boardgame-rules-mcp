# Contributing

Thanks for your interest. This is a personal project, so changes are reviewed as time allows, and a pull request may be declined if it doesn't fit the project's direction. Opening an issue first is the easiest way to check.

## Welcome

- **Bug reports**: a wrong citation, a garbled or out-of-order quote, a search that misses a section the rulebook has. Include the game, the question or search, and what you expected.
- **New games**, following [Adding a game](README.md#adding-a-game).
- **Fixes to extraction or search**, with a test.

## The source rule

Only publisher-issued documents are indexed: rulebooks, and official errata in a publisher's own document. Community FAQs, wikis and forum posts can point to a rule change, but can't be the source for one. A publisher's document that's only available on a community site (such as a Villain Guide PDF uploaded to BoardGameGeek) can be added as `user_supplied` with a `download_page`. It is never downloaded automatically, and its title says where it came from.

Don't commit rulebook PDFs, page images, or other publisher artwork. The manifest points to where users get each file.

## Before opening a pull request

1. Run the tests: `uv run pytest`.
2. If you changed extraction, re-index every installed game and check that no other game's text changed unless you meant it to. Extraction fixes for one rulebook can easily scramble another; that's why most layout fixes are opt-in hints in the manifest.
3. Run the free retrieval eval for each game you touched: `uv run python evals/retrieval.py evals/<game>_<edition>.yaml`. Say in the pull request whether scores changed.

The end-to-end eval (`evals/e2e.py`) calls Claude and uses your subscription or API credits, so it's optional.

Changes reach `main` only through a pull request whose tests pass.

## Releasing (maintainer)

1. In a pull request, bump `version` in `pyproject.toml`, and merge it.
2. Pull `main`, then tag it with the same version and push the tag: `git tag v0.1.1`, then `git push origin v0.1.1`.
3. The release workflow checks that the tag matches the version (and stops if not), runs the tests, publishes to PyPI, and attaches the Claude Desktop extension (`.mcpb`) to a GitHub release.
