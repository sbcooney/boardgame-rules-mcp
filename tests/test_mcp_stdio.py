"""End-to-end: start `bgrules serve` over stdio and call it as an MCP client would.

Needs Catan 2020 installed (`bgrules add catan --edition 2020`); skipped otherwise.
"""

import asyncio
import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from bgrules import store


def _catan_installed() -> bool:
    return any(r["game"] == "catan" and r["edition"] == "2020" for r in store.installed(store.connect()))


pytestmark = pytest.mark.skipif(not _catan_installed(), reason="Catan 2020 not installed")


async def _session_calls():
    params = StdioServerParameters(command=sys.executable, args=["-m", "bgrules.cli", "serve"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            search = await session.call_tool("search_rules", {"game": "catan", "query": "What happens when I move the robber?"})
            term = await session.call_tool("lookup_term", {"game": "catan", "term": "distance rule"})
            missing = await session.call_tool("search_rules", {"game": "catan", "query": "How do zombies spawn at night?"})
            return init, tools, search, term, missing


def _payload(result) -> dict:
    return result.structured_content or json.loads(result.content[0].text)


def test_server_over_stdio():
    init, tools, search, term, missing = asyncio.run(_session_calls())

    assert "Answer ONLY from passages" in init.instructions
    assert {t.name for t in tools.tools} >= {"list_games", "search_rules", "get_section", "lookup_term"}

    found = _payload(search)
    assert found["found"] is True
    top = found["results"][0]
    assert top["section_path"] == ["Almanac", "Robber"]
    assert top["printed_page"] == 11
    assert top["quote"].startswith("The robber begins the game in the desert.")
    assert top["open_link"].endswith("#page=11")

    entry = _payload(term)
    assert entry["found"] is True
    assert entry["sections"][0]["section_path"] == ["Almanac", "Distance Rule"]

    assert _payload(missing)["found"] is False
