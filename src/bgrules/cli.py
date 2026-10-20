"""bgrules command line: add games, inspect the index, run the MCP server."""

import argparse
import json
import shlex
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
    add.add_argument("game", help="a game's id or name; for a game this tool doesn't ship, the name to file your own rulebook under")
    add.add_argument("--edition")
    source = add.add_mutually_exclusive_group()
    source.add_argument("--file", help="path to a rulebook PDF you already have (for any game)")
    source.add_argument("--url", help="link to the official rulebook PDF, for a game this tool doesn't ship")
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
            ingest.add(args.game, edition=args.edition, file=args.file, doc_id=args.doc, url=args.url,
                       log=lambda m: print(m, file=sys.stderr))
            if args.url:
                print(f"To ask for it to be supported, with its layout checked: {manifest.request_link(args.game, args.url)}",
                      file=sys.stderr)
        except manifest.UnknownGame as e:
            print(f"error: {e.args[0]}\nRequest it: {manifest.request_link(args.game)}\n"
                  f"Or add your own copy of its rulebook: bgrules add {shlex.quote(args.game)} --file /path/to/rules.pdf "
                  "(or --url <link to the PDF>)",
                  file=sys.stderr)
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
