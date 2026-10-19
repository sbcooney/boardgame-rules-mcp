"""bgrules command line: add games, inspect the index, run the MCP server."""

import argparse
import json
import sys
from importlib import metadata

from . import ingest, manifest, store


def _version() -> str:
    try:
        return metadata.version("boardgame-rules-mcp")
    except metadata.PackageNotFoundError:
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bgrules", description="Board game rules MCP server")
    ap.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="download (or copy) and index a game's rulebook")
    add.add_argument("game")
    add.add_argument("--edition")
    add.add_argument("--file", help="path to a rulebook PDF you already have")
    add.add_argument("--doc", help="which document --file is, when an edition has several (e.g. rulebook, appendix)")

    sub.add_parser("list", help="list installed games")

    search = sub.add_parser("search", help="search a rulebook (debugging)")
    search.add_argument("game")
    search.add_argument("query")
    search.add_argument("--edition")
    search.add_argument("-n", type=int, default=3)

    sub.add_parser("serve", help="run the MCP server over stdio")

    args = ap.parse_args(argv)

    if args.cmd == "add":
        try:
            ingest.add(args.game, edition=args.edition, file=args.file, doc_id=args.doc, log=lambda m: print(m, file=sys.stderr))
        except KeyError as e:  # not a game this release knows
            print(f"error: {e.args[0]}\nRequest it: {manifest.request_link(args.game)}", file=sys.stderr)
            return 1
        except (ingest.IngestError, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    elif args.cmd == "list":
        for row in store.installed(store.connect()):
            print(f"{row['game']:<15} {row['edition']:<8} {row['title']}  ({row['sections']} sections)")
    elif args.cmd == "search":
        try:
            result = store.search(store.connect(), args.game, args.query, edition=args.edition, max_results=args.n)
        except store.LookupError_ as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.cmd == "serve":
        from .server import run

        run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
