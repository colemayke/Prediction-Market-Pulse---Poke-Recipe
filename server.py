"""
Prediction Market Pulse - Poke MCP server plus a proactive push poller.

Two jobs in one process:

  1. MCP tools (search_markets, watch_market, list_watches, unwatch,
     check_moves) that Poke calls during conversations. Served over
     streamable HTTP at /mcp so `npx poke tunnel http://localhost:3000/mcp`
     can reach it.

  2. A background poller that checks watched Polymarket markets and, when a
     market moves past its threshold, texts you via the Poke API. This is the
     proactive push that makes the recipe feel alive.

Phase 1 (PUSH_MODE=1): single user. A Poke API key pushes to its owner, so the
poller alerts your own thread. That is the demo.

Phase 2 (published, multi-user): a Poke API key cannot push to everyone from
one key, so run with PUSH_MODE=0 and let each user's Poke automation call
check_moves() on a schedule. State is keyed by X-Poke-User-Id either way.

Polymarket field names below were verified against the live APIs on
2026-07-06; see README.md for the confirmed shapes.

Requires Python 3.10+.  pip install -r requirements.txt
"""

import hmac
import json
import os
import sys
import threading
import time
from collections import Counter
from contextvars import ContextVar
from pathlib import Path

# Line-buffer stdout so print() diagnostics reach platform logs (Railway
# captures stdout) as they happen instead of on process exit.
sys.stdout.reconfigure(line_buffering=True)

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.types import ASGIApp, Receive, Scope, Send

# --- configuration -----------------------------------------------------------

POKE_API_KEY = os.environ.get("POKE_API_KEY", "")
POKE_API_URL = "https://poke.com/api/v1/inbound/api-message"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# Railway injects PORT at runtime and requires binding 0.0.0.0; 3000 is the
# local-dev fallback.
PORT = int(os.environ.get("PORT", "3000"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))

# Off by default: the published recipe drives check_moves from a per-user Poke
# automation, and one API key can only push to its owner anyway. Set
# PUSH_MODE=1 only for the single-user local demo.
PUSH_MODE = os.environ.get("PUSH_MODE", "0") == "1"

# Bearer token for the public endpoint. When set, every request must carry
# Authorization: Bearer <token>. Unset = open (local development only).
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")

# State lands on the Railway volume when one is attached (Railway exposes its
# mount path as RAILWAY_VOLUME_MOUNT_PATH, e.g. /data); explicit STATE_FILE
# wins, and bare watches.json is the local fallback.
_volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
STATE_FILE = Path(
    os.environ.get("STATE_FILE")
    or (os.path.join(_volume, "watches.json") if _volume else "watches.json")
)
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

# --- per-request user id -----------------------------------------------------
# Poke sends X-Poke-User-Id on every request. Middleware below drops it into
# this contextvar so tools can scope state per user without threading it
# around. The server runs stateless streamable HTTP (see FastMCP(...) below)
# so each request is handled inside the ASGI call and the contextvar set by
# the middleware is visible to the tool handler.

_user_id: ContextVar[str] = ContextVar("user_id", default="owner")


def current_user() -> str:
    return _user_id.get()


# --- tiny JSON store: { user_id: { label: watch } } --------------------------

_lock = threading.Lock()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            # Watches written before outcome-aware tracking stored the token
            # under "yes_token_id"; normalize so the rest of the code only
            # ever sees "token_id".
            for watches in state.values():
                for w in watches.values():
                    if "token_id" not in w and "yes_token_id" in w:
                        w["token_id"] = w.pop("yes_token_id")
            return state
        except (json.JSONDecodeError, OSError) as e:
            print(f"[state] could not read {STATE_FILE}: {e}; starting empty")
    return {}


def save_state(state: dict) -> None:
    # Atomic replace so a crash mid-write never corrupts the watchlist.
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


STATE = load_state()


# --- Polymarket helpers ------------------------------------------------------
#
# Verified live 2026-07-06:
#   GET {GAMMA}/public-search?q=<query>&events_status=active
#     -> {"events": [<event>, ...], "pagination": {...}}
#   The unit of search is the EVENT, which groups one or more markets. A
#   multi-outcome board (sports matchup, election, "Fed decision") is one
#   event holding one binary Yes/No market PER OUTCOME; e.g. the World Cup
#   match event "United States vs. Belgium" (slug fifwc-usa-bel-2026-07-06)
#   holds three markets: "Will United States win...", "...end in a draw?",
#   "Will Belgium win...". A plain "will X happen" question is an event with
#   a single binary market. Matching on q is against title text — "usa" does
#   NOT match "United States" (see _expand_team_abbreviations below).
#
#   Each <event> carries:
#     "title"   str          "slug"     str (the canonical URL slug, see below)
#     "active"/"closed"/"archived"  bools
#     "markets" [<market>, ...]
#   Each <market> carries:
#     "question"       str
#     "groupItemTitle" str — the outcome label on a grouped event's board,
#                      e.g. "United States", "Draw (United States vs. Belgium)"
#     "active" / "closed" / "archived"  bools (markets settle individually)
#     "clobTokenIds"   JSON-encoded string: '["<token>", "<token>"]'
#     "outcomes"       JSON-encoded string: '["Yes", "No"]'
#     "outcomePrices"  JSON-encoded string: '["0.365", "0.635"]'
#   The three quoted fields need a second json.loads and are index-aligned:
#   leg i of the market is (outcomes[i], clobTokenIds[i], outcomePrices[i]).
#
#   https://polymarket.com/event/<event.slug> always resolves: plain events
#   serve directly, sports events 307-redirect to their canonical page
#   (e.g. /sports/world-cup/fifwc-usa-bel-2026-07-06). Never hand-build a
#   slug from the title — that was the bug this layer replaces.
#
#   GET {GAMMA}/teams?abbreviation=<abbr>&limit=50   (abbr must be lowercase)
#     -> [{"name": "United States", "league": "rl", ...}, ...]
#   Maps team/country abbreviations to canonical names across leagues.
#
#   GET {GAMMA}/markets?clob_token_ids=<token_id>
#     -> [<market>] for an open market, [] for unknown OR settled tokens
#     (closed markets are not returned), so it validates watchability.
#
#   GET {CLOB}/midpoint?token_id=<id>
#     -> {"mid": "0.365"}  (string, 0..1); 404 for unknown/settled tokens.

MAX_EVENTS = 8      # search results returned
MAX_OUTCOMES = 12   # outcomes listed per event (big boards hold 100+ markets)


def _is_open(obj: dict) -> bool:
    """True for an event or market that is live and watchable."""
    return bool(obj.get("active")) and not obj.get("closed") and not obj.get("archived")


def _market_legs(m: dict) -> list[dict]:
    """All tradable legs of one market: [{outcome, token_id, price}, ...].
    Raises ValueError on a shape we don't understand."""
    token_ids = json.loads(m.get("clobTokenIds") or "[]")
    outcomes = json.loads(m.get("outcomes") or "[]")
    prices = json.loads(m.get("outcomePrices") or "[]")
    if not token_ids or not isinstance(token_ids, list):
        raise ValueError("no clobTokenIds")
    return [
        {
            "outcome": str(outcomes[i]) if i < len(outcomes) else f"Outcome {i + 1}",
            "token_id": token_ids[i],
            "price": float(prices[i]) if i < len(prices) else None,
        }
        for i in range(len(token_ids))
    ]


def _event_to_result(ev: dict) -> dict | None:
    """Turn a raw Gamma event into a source-agnostic search result:
    {title, url, outcomes: [{outcome, token_id, price}, ...]}.
    Returns None for settled events; skips malformed markets with a log line.

    A single-market event exposes that market's own legs (the binary Yes/No
    case, or an old-style market carrying 3+ outcomes in one array). A
    multi-market event exposes one outcome per open market — its YES leg,
    labelled by groupItemTitle — which is exactly the Polymarket board."""
    if not _is_open(ev):
        return None
    open_markets = [m for m in ev.get("markets") or [] if _is_open(m)]
    outcomes: list[dict] = []
    for m in open_markets:
        try:
            legs = _market_legs(m)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            print(f"[search] skipping malformed market {m.get('slug')!r}: {e}")
            continue
        if len(open_markets) == 1:
            outcomes = legs
        else:
            yes = next(
                (l for l in legs if l["outcome"].lower() == "yes"), legs[0]
            )
            outcomes.append(
                {
                    "outcome": m.get("groupItemTitle") or m.get("question"),
                    "token_id": yes["token_id"],
                    "price": yes["price"],
                }
            )
    if not outcomes or not ev.get("slug"):
        return None
    result = {
        "title": ev.get("title"),
        # Slug straight from the API; /event/<slug> always resolves (sports
        # events redirect to their canonical /sports/... page).
        "url": f"https://polymarket.com/event/{ev['slug']}",
        "outcomes": outcomes,
    }
    if len(outcomes) > MAX_OUTCOMES:
        outcomes.sort(key=lambda o: o["price"] if o["price"] is not None else -1, reverse=True)
        result["outcomes"] = outcomes[:MAX_OUTCOMES]
        result["note"] = (
            f"Showing top {MAX_OUTCOMES} of {len(outcomes)} outcomes by price."
        )
    return result


async def _search_events(client: httpx.AsyncClient, query: str) -> list[dict]:
    r = await client.get(
        f"{GAMMA}/public-search",
        params={"q": query, "events_status": "active"},
    )
    r.raise_for_status()
    return r.json().get("events") or []


async def _expand_team_abbreviations(
    client: httpx.AsyncClient, query: str
) -> str | None:
    """public-search matches title text, and sports event titles use full
    team names ("United States vs. Belgium"), so a query like "usa belgium"
    misses them. For each short alpha token, ask /teams whether it is a known
    team abbreviation; if one canonical name clearly dominates (>= 4 teams
    across leagues share it — filters flukes like "will" or "fed"), swap it
    in. Returns the expanded query, or None if nothing was expanded."""
    words = query.split()
    expanded: list[str] = []
    changed = False
    for w in words:
        name = None
        if 2 <= len(w) <= 4 and w.isalpha():
            try:
                r = await client.get(
                    f"{GAMMA}/teams",
                    params={"abbreviation": w.lower(), "limit": 50},
                )
                r.raise_for_status()
                counts = Counter(t["name"] for t in r.json() if t.get("name"))
                top = counts.most_common(1)
                if top and top[0][1] >= 4 and top[0][0].lower() != w.lower():
                    name = top[0][0]
            except (httpx.HTTPError, ValueError) as e:
                print(f"[search] team lookup for {w!r} failed: {e}")
        expanded.append(name or w)
        changed = changed or name is not None
    return " ".join(expanded) if changed else None


async def search_polymarket(query: str, limit: int = MAX_EVENTS) -> list[dict]:
    """Search open events for a query, interleaving results for the raw query
    with results for the team-abbreviation-expanded variant (when one exists),
    deduplicated by event slug."""
    async with httpx.AsyncClient(timeout=20) as client:
        raw = await _search_events(client, query)
        variant = await _expand_team_abbreviations(client, query)
        alt = await _search_events(client, variant) if variant else []
    out: list[dict] = []
    seen: set[str] = set()
    # Interleave so the best hit of either phrasing lands near the top.
    for i in range(max(len(raw), len(alt))):
        for ev in (raw[i : i + 1] + alt[i : i + 1]):
            slug = ev.get("slug")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            parsed = _event_to_result(ev)
            if parsed is not None:
                out.append(parsed)
                if len(out) >= limit:
                    return out
    return out


async def lookup_market_by_token(token_id: str) -> dict | None:
    """The Gamma market carrying this CLOB token, or None for tokens that are
    unknown or already settled (Gamma omits closed markets from this query)."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{GAMMA}/markets", params={"clob_token_ids": token_id}
        )
        r.raise_for_status()
        markets = r.json()
    return markets[0] if markets else None


def _parse_mid(payload: dict) -> float | None:
    mid = payload.get("mid")
    return float(mid) if mid is not None else None


async def fetch_price(token_id: str) -> float | None:
    """Async outcome-token midpoint (0..1), used by tool handlers."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{CLOB}/midpoint", params={"token_id": token_id})
        r.raise_for_status()
        return _parse_mid(r.json())


def fetch_price_sync(client: httpx.Client, token_id: str) -> float | None:
    """Sync twin of fetch_price, used by the background poller thread."""
    r = client.get(f"{CLOB}/midpoint", params={"token_id": token_id})
    r.raise_for_status()
    return _parse_mid(r.json())


def poke_push(text: str) -> None:
    if not POKE_API_KEY:
        print("[push] POKE_API_KEY not set, skipping:", text)
        return
    try:
        r = httpx.post(
            POKE_API_URL,
            headers={
                "Authorization": f"Bearer {POKE_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"message": text},
            timeout=20,
        )
        if r.status_code >= 400:
            print(f"[push] Poke API returned {r.status_code}: {r.text[:200]}")
    except httpx.HTTPError as e:
        print("[push] failed:", e)


# --- move detection, shared by check_moves and the poller --------------------

def _snapshot_watches(uid: str) -> dict[str, dict]:
    with _lock:
        return {label: dict(w) for label, w in STATE.get(uid, {}).items()}


def _apply_moves(uid: str, prices: dict[str, float], reset: bool) -> list[dict]:
    """Compare fetched prices against baselines; return threshold crossings.
    Prices are fetched by the caller (async tools vs. the sync poller) so a
    single failed market never aborts the pass -- it is simply absent here."""
    hits: list[dict] = []
    with _lock:
        watches = STATE.get(uid, {})
        for label, price in prices.items():
            w = watches.get(label)
            if w is None:
                continue  # unwatched while we were fetching
            delta = (price - w["last_price"]) * 100
            if abs(delta) >= w["threshold"]:
                hits.append(
                    {
                        "label": label,
                        "old_pct": round(w["last_price"] * 100, 1),
                        "new_pct": round(price * 100, 1),
                        "delta_pts": round(delta, 1),
                    }
                )
                if reset:
                    w["last_price"] = price
        if reset and hits:
            save_state(STATE)
    return hits


# --- MCP server and tools ----------------------------------------------------
# stateless_http: every request is self-contained, which is what Poke expects
# and what lets the header middleware's contextvar reach the tool handlers.

mcp = FastMCP("Prediction Market Pulse", stateless_http=True)


@mcp.tool()
async def search_markets(query: str) -> list[dict] | str:
    """Search open Polymarket prediction markets by keyword (e.g. "fed",
    "election", "usa belgium"). Returns up to 8 events, each with: title,
    url (the live polymarket.com page), and outcomes — every selectable
    outcome as {outcome, token_id, price} where price is the current implied
    probability (0 to 1). A yes/no market has two outcomes; a sports matchup
    or election board has one per candidate (e.g. USA / Draw / Belgium).
    Before calling watch_market, show the user the outcomes and confirm WHICH
    one to track, then pass that outcome's token_id. Settled markets are
    excluded. Returns a plain message when nothing matches."""
    results = await search_polymarket(query)
    if not results:
        return (
            f"No open markets found for '{query}'. Try different or more "
            "specific keywords, or full team/country names (e.g. 'united "
            "states' rather than 'usa')."
        )
    return results


@mcp.tool()
async def watch_market(
    token_id: str, label: str, threshold_points: float = 5.0
) -> str:
    """Start watching one outcome of a Polymarket market for odds moves.
    token_id must be the token_id of the specific outcome to track, taken
    from search_markets (for a yes/no market that is usually the "Yes"
    outcome; for a matchup pick the side, e.g. USA-to-win's token). Records
    the current probability as the baseline; the watch counts as "moved" once
    it shifts by at least threshold_points percentage points. Pick a label
    that names BOTH the outcome and the market unambiguously, e.g. "USA to
    win (USA vs Belgium)" — it is how the watch is referenced later."""
    market = None
    try:
        market = await lookup_market_by_token(token_id)
    except (httpx.HTTPError, ValueError) as e:
        # Validation is best-effort; the midpoint fetch below still gates.
        print(f"[watch] token lookup failed for {token_id}: {e}")
    else:
        if market is None:
            return (
                f"Token {token_id} is not an open market — it may have "
                "settled or the id may be wrong. Search again and pick an "
                "outcome from the current results."
            )
        if not _is_open(market):
            return (
                f"That market ('{market.get('question')}') has settled and "
                "can no longer be watched."
            )
    try:
        price = await fetch_price(token_id)
    except httpx.HTTPError as e:
        return f"Could not reach Polymarket for token {token_id}: {e}"
    if price is None:
        return f"Could not read a price for token {token_id}."
    uid = current_user()
    with _lock:
        STATE.setdefault(uid, {})[label] = {
            "token_id": token_id,
            "threshold": float(threshold_points),
            "last_price": price,
        }
        save_state(STATE)
    return (
        f"Watching '{label}' at {price * 100:.1f}%. "
        f"Alerting on {threshold_points:.0f} point moves."
    )


@mcp.tool()
def list_watches() -> list[dict]:
    """List the outcomes currently being watched for this user. Each entry
    has label, price (the baseline probability of the watched outcome, 0 to
    1), and threshold (alert trigger, in percentage points)."""
    uid = current_user()
    with _lock:
        return [
            {"label": k, "price": v["last_price"], "threshold": v["threshold"]}
            for k, v in STATE.get(uid, {}).items()
        ]


@mcp.tool()
def unwatch(label: str) -> str:
    """Stop watching a market. Pass the label the watch was created with
    (see list_watches)."""
    uid = current_user()
    with _lock:
        removed = STATE.get(uid, {}).pop(label, None)
        if removed:
            save_state(STATE)
    return f"Removed '{label}'." if removed else f"No watch named '{label}'."


@mcp.tool()
async def check_moves() -> list[dict]:
    """Check this user's watched markets for odds moves that crossed their
    threshold since the last check, and reset the baseline for any that did.
    Returns a list of moves, each with label, old_pct, new_pct, and delta_pts
    (all in percentage points). Empty list means nothing moved enough. Meant
    to be called on a schedule by a Poke automation."""
    uid = current_user()
    watches = _snapshot_watches(uid)
    prices: dict[str, float] = {}
    async with httpx.AsyncClient(timeout=20) as client:
        for label, w in watches.items():
            try:
                r = await client.get(
                    f"{CLOB}/midpoint", params={"token_id": w["token_id"]}
                )
                r.raise_for_status()
                mid = _parse_mid(r.json())
                if mid is not None:
                    prices[label] = mid
            except (httpx.HTTPError, ValueError) as e:
                print(f"[check_moves] '{label}' fetch failed: {e}")
    return _apply_moves(uid, prices, reset=True)


# --- background poller (Phase 1 proactive push) ------------------------------

def poller() -> None:
    while True:
        try:
            with httpx.Client(timeout=20) as client:
                with _lock:
                    uids = list(STATE.keys())
                for uid in uids:
                    watches = _snapshot_watches(uid)
                    prices: dict[str, float] = {}
                    for label, w in watches.items():
                        try:
                            mid = fetch_price_sync(client, w["token_id"])
                            if mid is not None:
                                prices[label] = mid
                        except (httpx.HTTPError, ValueError) as e:
                            print(f"[poller] '{label}' fetch failed: {e}")
                    for hit in _apply_moves(uid, prices, reset=True):
                        sign = "+" if hit["delta_pts"] >= 0 else ""
                        poke_push(
                            f"Heads up: Polymarket '{hit['label']}' moved "
                            f"{hit['old_pct']}% to {hit['new_pct']}% "
                            f"({sign}{hit['delta_pts']} pts). "
                            f"Give me a one line take and the current odds."
                        )
        except Exception as e:
            print("[poller] error:", e)
        time.sleep(POLL_SECONDS)


# --- middleware: bearer auth for the public endpoint -------------------------

class BearerAuthMiddleware:
    """Reject requests without `Authorization: Bearer $MCP_AUTH_TOKEN`.
    Disabled when MCP_AUTH_TOKEN is unset so local dev needs no token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.token:
            await self.app(scope, receive, send)
            return
        auth = dict(scope.get("headers") or []).get(b"authorization", b"")
        scheme, _, credential = auth.partition(b" ")
        if scheme.lower() == b"bearer" and hmac.compare_digest(
            credential.strip(), self.token
        ):
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"error": "unauthorized"}',
            }
        )


# --- middleware: X-Poke-User-Id into the contextvar --------------------------

class UserHeaderMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"x-poke-user-id")
        token = _user_id.set(raw.decode() if raw else "owner")
        try:
            await self.app(scope, receive, send)
        finally:
            _user_id.reset(token)


# Auth wraps the user-id middleware: unauthenticated requests are rejected
# before any state is touched; authenticated ones still get per-user scoping.
app = BearerAuthMiddleware(
    UserHeaderMiddleware(mcp.streamable_http_app()), MCP_AUTH_TOKEN
)


if __name__ == "__main__":
    if PUSH_MODE:
        threading.Thread(target=poller, daemon=True).start()
        print(f"[poller] on, every {POLL_SECONDS}s")
    else:
        print("[poller] off (PUSH_MODE=0); Poke automations should call check_moves")
    print(f"[auth] bearer {'enforced' if MCP_AUTH_TOKEN else 'DISABLED (no MCP_AUTH_TOKEN; local dev only)'}")
    print(f"[state] {STATE_FILE}")
    print(f"MCP on http://localhost:{PORT}/mcp")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
