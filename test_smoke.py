"""
Smoke test for the Prediction Market Pulse MCP server.

Run the server first (PUSH_MODE=0 recommended so the poller stays quiet):

    PUSH_MODE=0 .venv/bin/python server.py

then:

    .venv/bin/python test_smoke.py

Connects over streamable HTTP, completes the MCP initialize handshake, lists
tools, then exercises the market-resolution layer against live Polymarket
data:

  - binary regression: a "will X happen" market still resolves to two
    outcomes and can be watched/unwatched (the original yes/no path);
  - multi-outcome: a grouped event (election board) returns 3+ outcomes,
    each with its own label, token id, and price;
  - links come from the API (https://polymarket.com/event/<slug>), never
    hand-built;
  - watching a bogus token returns a clear message instead of a bad watch;
  - a nonsense query returns a helpful message, not an error;
  - a sub-1-point threshold is clamped to the 1-point floor with an
    explanation (Polymarket displays whole percentages).

It also runs in-process unit tests of the alert layer first (no network):
site-style display rounding (whole percent, half-up, one-decimal fallback so
no alert reads "37% to 37%"), the 1-point noise floor, single-alert baseline
reset, and legacy-watch formatting.

Queries are chosen to stay live for a long time ("recession" markets run to
end of year; "presidential election" boards run for years).
"""

import asyncio
import json
import os
import sys
import tempfile

# Import the server module in-process for the detection/formatting unit tests
# (no network, no MCP). A scratch STATE_FILE keeps it away from real state;
# the running server being smoke-tested is a separate process and unaffected.
os.environ["STATE_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="pmp-test-"), "watches.json"
)
import server

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


def parse_events(result) -> list[dict]:
    """search_markets returns a list of event dicts, or a plain string
    message when nothing matched. Return the dicts (empty for a message)."""
    out = []
    for c in result.content:
        try:
            item = json.loads(c.text)
        except json.JSONDecodeError:
            continue  # plain "nothing found" message
        if isinstance(item, dict):
            out.append(item)
    return out


def assert_well_formed(events: list[dict]) -> None:
    for ev in events:
        assert ev.get("title"), f"event missing title: {ev}"
        assert str(ev.get("url", "")).startswith("https://polymarket.com/event/"), (
            f"event url not API-sourced: {ev.get('url')}"
        )
        outcomes = ev.get("outcomes")
        assert outcomes and len(outcomes) >= 2, f"event lacks outcomes: {ev}"
        for o in outcomes:
            assert o.get("outcome"), f"outcome missing label: {o}"
            assert o.get("token_id"), f"outcome missing token_id: {o}"
            assert o.get("price") is None or 0 <= o["price"] <= 1, (
                f"price out of range: {o}"
            )


def unit_tests() -> None:
    """Detection and alert-formatting behaviour, no network required."""
    # Site-style display: whole percent, rounded half-up like polymarket.com.
    assert server._display_pct(0.365) == 37
    assert server._display_pct(0.364) == 36
    assert server._display_pair(0.30, 0.40) == (30, 40)
    # Rounding that would collapse to "37% to 37%" falls back to one decimal.
    assert server._display_pair(0.371, 0.374) == (37.1, 37.4)

    uid = "unit-test"
    server.STATE[uid] = {
        "usa": {
            "token_id": "tok",
            "threshold": 5.0,
            "last_price": 0.30,
            "outcome": "United States",
            "market": "United States vs. Belgium",
            "url": "https://polymarket.com/event/fifwc-usa-bel-2026-07-06",
        }
    }

    # One crossing -> exactly one alert; the baseline resets, so the same
    # price on the next poll cycle must not re-alert.
    hits = server._apply_moves(uid, {"usa": 0.40}, reset=True)
    assert len(hits) == 1, hits
    h = hits[0]
    assert (h["old_pct"], h["new_pct"], h["delta_pts"]) == (30, 40, 10.0), h
    assert "'United States' in United States vs. Belgium" in h["summary"], h
    assert "30% to 40%" in h["summary"] and "+10.0 pts" in h["summary"], h
    assert "https://polymarket.com/event/fifwc-usa-bel-2026-07-06" in h["summary"]
    assert server._apply_moves(uid, {"usa": 0.40}, reset=True) == [], (
        "same move alerted twice"
    )

    # Sub-visible noise: even a legacy watch stored with a 0.25-point
    # threshold must not fire below the 1-point floor (whole-percent display).
    server.STATE[uid]["usa"]["threshold"] = 0.25
    assert server._apply_moves(uid, {"usa": 0.404}, reset=True) == [], (
        "sub-1-point noise fired an alert"
    )
    hits = server._apply_moves(uid, {"usa": 0.412}, reset=True)
    assert len(hits) == 1, "a visible 1.2-point move should fire"

    # A watch from before display fields existed still formats an alert.
    server.STATE[uid]["legacy"] = {
        "token_id": "tok2", "threshold": 5.0, "last_price": 0.50,
    }
    (h,) = server._apply_moves(uid, {"legacy": 0.60}, reset=True)
    assert "'legacy' moved 50% to 60%" in h["summary"], h
    assert h["url"] is None

    del server.STATE[uid]
    print("unit tests ok: display rounding, single-alert reset, noise floor")


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

            # --- binary regression: yes/no market still two outcomes -------
            result = await session.call_tool(
                "search_markets", {"query": "recession"}
            )
            assert not result.isError, f"search errored: {result.content}"
            events = parse_events(result)
            assert events, "no results for 'recession'"
            assert_well_formed(events)
            binary = next(
                (
                    e
                    for e in events
                    if len(e["outcomes"]) == 2
                    and {o["outcome"].lower() for o in e["outcomes"]}
                    == {"yes", "no"}
                ),
                None,
            )
            assert binary, f"no binary yes/no event among: {[e['title'] for e in events]}"
            print(f"binary ok: {binary['title']!r} -> "
                  f"{[(o['outcome'], o['price']) for o in binary['outcomes']]}")

            # --- multi-outcome: grouped board returns 3+ labelled outcomes -
            result = await session.call_tool(
                "search_markets", {"query": "presidential election"}
            )
            assert not result.isError, f"search errored: {result.content}"
            events = parse_events(result)
            assert events, "no results for 'presidential election'"
            assert_well_formed(events)
            multi = next((e for e in events if len(e["outcomes"]) >= 3), None)
            assert multi, f"no multi-outcome event among: {[e['title'] for e in events]}"
            labels = {o["outcome"].lower() for o in multi["outcomes"]}
            assert "yes" not in labels, (
                f"grouped event leaked raw Yes legs instead of outcome labels: {multi}"
            )
            tokens = [o["token_id"] for o in multi["outcomes"]]
            assert len(set(tokens)) == len(tokens), "duplicate outcome tokens"
            print(f"multi-outcome ok: {multi['title']!r} with "
                  f"{len(multi['outcomes'])} outcomes, e.g. "
                  f"{multi['outcomes'][0]['outcome']!r} @ {multi['outcomes'][0]['price']}")

            # --- watch/unwatch round trip on the binary YES outcome --------
            yes = next(o for o in binary["outcomes"] if o["outcome"].lower() == "yes")
            label = "smoke-test binary watch"
            result = await session.call_tool(
                "watch_market",
                {"token_id": yes["token_id"], "label": label, "threshold_points": 5},
            )
            assert not result.isError, f"watch_market errored: {result.content}"
            text = result.content[0].text
            assert "Watching" in text, f"unexpected watch reply: {text}"
            print(f"watch ok: {text}")

            result = await session.call_tool("list_watches", {})
            watched = [json.loads(c.text) for c in result.content]
            assert any(w["label"] == label for w in watched), (
                f"watch not listed: {watched}"
            )

            result = await session.call_tool("unwatch", {"label": label})
            assert "Removed" in result.content[0].text, result.content[0].text
            print("list/unwatch ok")

            # --- sub-1-point threshold is clamped, with an explanation -----
            label = "smoke-test clamp watch"
            result = await session.call_tool(
                "watch_market",
                {"token_id": yes["token_id"], "label": label, "threshold_points": 0.25},
            )
            text = result.content[0].text
            assert "Watching" in text, f"clamped watch failed: {text}"
            assert "1-point minimum" in text and "whole percentages" in text, (
                f"clamp not explained: {text}"
            )
            result = await session.call_tool("list_watches", {})
            watched = [json.loads(c.text) for c in result.content]
            clamped = next(w for w in watched if w["label"] == label)
            assert clamped["threshold"] == 1.0, f"threshold not clamped: {clamped}"
            await session.call_tool("unwatch", {"label": label})
            print(f"clamp ok: {text}")

            # --- watching a bogus token fails clearly, not silently --------
            result = await session.call_tool(
                "watch_market", {"token_id": "1234567890", "label": "bogus"}
            )
            assert not result.isError, "bogus watch should reply, not error"
            text = result.content[0].text
            assert "Watching" not in text, f"bogus token was watched: {text}"
            print(f"bogus-token ok: {text}")

            # --- nonsense query returns a message, not an error ------------
            result = await session.call_tool(
                "search_markets", {"query": "zqxvbnmasdfgh"}
            )
            assert not result.isError, "empty search should reply, not error"
            assert not parse_events(result), "expected no events"
            assert "No open markets" in result.content[0].text
            print("empty-search ok")

    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    try:
        unit_tests()
        asyncio.run(main())
    except AssertionError as e:
        print(f"SMOKE TEST FAILED: {e}")
        sys.exit(1)
