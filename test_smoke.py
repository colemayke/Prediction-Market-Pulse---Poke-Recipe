"""
Smoke test for the Prediction Market Pulse MCP server.

Run the server first (PUSH_MODE=0 recommended so the poller stays quiet):

    PUSH_MODE=0 .venv/bin/python server.py

then:

    .venv/bin/python test_smoke.py

Connects over streamable HTTP, completes the MCP initialize handshake, lists
tools, and calls search_markets with a common query, asserting a non-empty,
well-formed result.
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("MCP_URL", "http://localhost:3000/mcp")
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
EXPECTED_TOOLS = {
    "search_markets",
    "watch_market",
    "list_watches",
    "unwatch",
    "check_moves",
}


async def main() -> None:
    headers = {"X-Poke-User-Id": "smoke-test"}
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    async with streamablehttp_client(URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"initialized: {info.serverInfo.name}")

            tools = {t.name for t in (await session.list_tools()).tools}
            missing = EXPECTED_TOOLS - tools
            assert not missing, f"missing tools: {missing}"
            print(f"tools ok: {sorted(tools)}")

            result = await session.call_tool("search_markets", {"query": "fed"})
            assert not result.isError, f"search_markets errored: {result.content}"
            markets = [json.loads(c.text) for c in result.content]
            assert markets, "search_markets returned no results for 'fed'"
            for m in markets:
                assert m.get("question"), f"market missing question: {m}"
                assert m.get("yes_token_id"), f"market missing yes_token_id: {m}"
                assert m.get("yes_price") is None or 0 <= m["yes_price"] <= 1, (
                    f"yes_price out of range: {m}"
                )
            print(f"search_markets ok: {len(markets)} markets, e.g. "
                  f"{markets[0]['question']!r} @ {markets[0]['yes_price']}")

    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"SMOKE TEST FAILED: {e}")
        sys.exit(1)
