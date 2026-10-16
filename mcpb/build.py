"""Build the Claude Desktop extension (.mcpb) for the current package version.

    uv run python mcpb/build.py                 # installs boardgame-rules-mcp==<version> from PyPI
    uv run python mcpb/build.py --wheel dist/x.whl   # installs a local wheel instead (testing)

Writes dist/boardgame-rules-mcp-<version>.mcpb. Needs Node (npx) for the mcpb packer.
"""

import argparse
import json
import shutil
import subprocess
import tomllib
from pathlib import Path

MCPB_CLI = "@anthropic-ai/mcpb@2.1.2"
HERE = Path(__file__).parent
ROOT = HERE.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wheel", help="local wheel to install instead of the PyPI release")
    args = ap.parse_args()

    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    requirement = f"boardgame-rules-mcp @ {Path(args.wheel).resolve().as_uri()}" if args.wheel else f"boardgame-rules-mcp=={version}"

    stage = ROOT / "dist" / "mcpb"
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(HERE, stage, ignore=shutil.ignore_patterns("build.py", "__pycache__", ".venv"))
    manifest = json.loads((HERE / "manifest.json").read_text())
    manifest["version"] = version
    (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    pyproject = (HERE / "pyproject.toml").read_text()
    (stage / "pyproject.toml").write_text(pyproject.replace("{version}", version).replace("{requirement}", requirement))

    out = ROOT / "dist" / f"boardgame-rules-mcp-{version}.mcpb"
    subprocess.run(["npx", "-y", MCPB_CLI, "validate", str(stage / "manifest.json")], check=True)
    subprocess.run(["npx", "-y", MCPB_CLI, "pack", str(stage), str(out)], check=True)
    print(out)


if __name__ == "__main__":
    main()
