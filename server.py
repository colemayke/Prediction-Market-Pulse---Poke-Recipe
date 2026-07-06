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
            return json.loads(STATE_FILE.read_text())
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
#   GET {GAMMA}/public-search?q=<query>
#     -> {"events": [{..., "markets": [<market>, ...]}], "pagination": {...}}
#   Each <market> carries:
#     "question"      str
#     "slug"          str
#     "active"        bool, "closed" bool, "archived" bool
#     "clobTokenIds"  JSON-encoded string: '["<yes_id>", "<no_id>"]'
#     "outcomes"      JSON-encoded string: '["Yes", "No"]'
#     "outcomePrices" JSON-encoded string: '["0.07", "0.93"]'
#   clobTokenIds/outcomes/outcomePrices need a second json.loads and are
#   index-aligned, so the YES token/price sit at outcomes.index("Yes").
#
#   GET {CLOB}/midpoint?token_id=<id>
#     -> {"mid": "0.07"}   (string, 0..1)


def _parse_market(m: dict) -> dict | None:
    """Pull question/slug/yes token/yes price out of a raw Gamma market dict.
    Returns None for markets that are closed or missing orderbook data."""
    if not m.get("active") or m.get("closed") or m.get("archived"):
        return None
    try:
        token_ids = json.loads(m.get("clobTokenIds") or "[]")
        outcomes = json.loads(m.get("outcomes") or "[]")
        prices = json.loads(m.get("outcomePrices") or "[]")
    except json.JSONDecodeError:
        return None
    if not token_ids:
        return None
    yes_idx = next(
        (i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), 0
    )
    if yes_idx >= len(token_ids):
        return None
    return {
        "question": m.get("question"),
        "slug": m.get("slug"),
        "yes_token_id": token_ids[yes_idx],
        "yes_price": float(prices[yes_idx]) if yes_idx < len(prices) else None,
    }


async def search_polymarket(query: str, limit: int = 8) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{GAMMA}/public-search", params={"q": query})
        r.raise_for_status()
        events = r.json().get("events") or []
    out: list[dict] = []
    seen: set[str] = set()
    for ev in events:
        for m in ev.get("markets") or []:
            parsed = _parse_market(m)
            if parsed is None or parsed["yes_token_id"] in seen:
                continue
            seen.add(parsed["yes_token_id"])
            out.append(parsed)
            if len(out) >= limit:
                return out
    return out


def _parse_mid(payload: dict) -> float | None:
    mid = payload.get("mid")
    return float(mid) if mid is not None else None


async def fetch_price(token_id: str) -> float | None:
    """Async YES-token midpoint (0..1), used by tool handlers."""
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
async def search_markets(query: str) -> list[dict]:
    """Search open Polymarket prediction markets by keyword (e.g. "fed",
    "election"). Returns up to 8 markets, each with: question (the market
    title), slug, yes_token_id (pass this to watch_market), and yes_price
    (current YES probability, 0 to 1)."""
    return await search_polymarket(query)


@mcp.tool()
async def watch_market(
    yes_token_id: str, label: str, threshold_points: float = 5.0
) -> str:
    """Start watching a Polymarket market for odds moves. Records the current
    YES probability as the baseline; the market counts as "moved" once the
    probability shifts by at least threshold_points percentage points from
    that baseline. Get yes_token_id from search_markets. Pick a short,
    human-readable label -- it is how the watch is referenced later."""
    try:
        price = await fetch_price(yes_token_id)
    except httpx.HTTPError as e:
        return f"Could not reach Polymarket for token {yes_token_id}: {e}"
    if price is None:
        return f"Could not read a price for token {yes_token_id}."
    uid = current_user()
    with _lock:
        STATE.setdefault(uid, {})[label] = {
            "yes_token_id": yes_token_id,
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
    """List the markets currently being watched for this user. Each entry has
    label, yes_price (the baseline YES probability, 0 to 1), and threshold
    (alert trigger, in percentage points)."""
    uid = current_user()
    with _lock:
        return [
            {"label": k, "yes_price": v["last_price"], "threshold": v["threshold"]}
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
                    f"{CLOB}/midpoint", params={"token_id": w["yes_token_id"]}
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
                            mid = fetch_price_sync(client, w["yes_token_id"])
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
