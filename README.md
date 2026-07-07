# Prediction Market Pulse — Poke MCP server

A single-process Python service that gives [Poke](https://poke.com) tools for
tracking Polymarket prediction markets, and proactively texts you when a
watched market's odds move past a threshold.

**Tools exposed over MCP** (streamable HTTP at `/mcp`):

| Tool | What it does |
|---|---|
| `search_markets(query)` | Search open Polymarket events by keyword. Returns up to 8 events, each with `title`, `url` (the live polymarket.com page, taken from the API), and `outcomes` — **every** selectable outcome as `{outcome, token_id, price}` (price 0–1). A yes/no market is two outcomes; a sports matchup is three (e.g. USA / Draw / Belgium); an election board is one per candidate (capped at the top 12 by price, with a note). Settled markets are excluded. |
| `watch_market(token_id, label, threshold_points=5)` | Watch **one specific outcome** (its `token_id` from `search_markets`); baseline is that outcome's current price; threshold is in percentage points (default 5, minimum 1 — see "Alert quality" below). Watching a settled or unknown token returns a clear message instead of a bad watch. |
| `list_watches()` | Your watches with baseline and threshold. |
| `unwatch(label)` | Remove a watch by label. |
| `check_moves()` | Watches that crossed their threshold since the last check; resets their baselines so one move alerts exactly once. Each move carries `label`, `outcome`, `market`, `old_pct`/`new_pct` (site-style whole percents), `delta_pts`, `url`, and `summary` (a ready-to-relay sentence with all of the above). |

The binary yes/no case is just the two-outcome special case of the same
mechanism — watching a yes/no market means watching its "Yes" outcome's
token. Poke is instructed (via the tool docstrings) to present the outcomes
and ask which one to track before calling `watch_market`.

## Alert quality

The rule every alert must satisfy: a user reading the alert and then opening
Polymarket should see consistent numbers and agree a move happened.

- **Detection runs at full precision, display matches the site.** Crossings
  are detected on the raw CLOB midpoint (0–1) so the logic is exact, but
  alert text shows whole percents rounded half-up — the way polymarket.com
  displays prices. If whole-percent rounding would collapse old and new to
  the same figure, the text shows one decimal instead, so no alert ever
  reads "37% to 37%".
- **1-point threshold floor.** Polymarket displays whole percentages, so a
  move smaller than 1 point is invisible on the site and would read as a
  false alert. The default threshold is 5 points; requests below 1 are
  accepted but clamped to 1, and the `watch_market` reply explains why. The
  floor is also enforced at detection time, so watches stored before the
  floor existed can't fire on sub-visible noise either.
- **Named and linked.** Every alert names the outcome and its market or
  matchup (captured from the API at watch time — the token lookup's embedded
  `events[0]` supplies the matchup title and canonical slug) and carries the
  working `polymarket.com/event/<slug>` link, e.g.: `'United States' in
  United States vs. Belgium moved 27% to 37% (+10.0 pts).
  https://polymarket.com/event/fifwc-usa-bel-2026-07-06`.
- **Exactly one alert per move.** The baseline resets to the alerted price
  the moment a crossing is reported (in both the poller and `check_moves`),
  so the same move is never re-reported on the next cycle.

All state is scoped per user via the `X-Poke-User-Id` header Poke sends on
every request (falls back to `"owner"` for local testing), persisted to a JSON
file (`STATE_FILE`).

## Install and run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # optional locally; see comments in the file

.venv/bin/python server.py
# MCP on http://localhost:3000/mcp
```

Requires Python 3.10+ (developed and pinned on 3.14, see `.python-version`).

With `MCP_AUTH_TOKEN` unset the endpoint is **open** — fine on localhost,
never in production. When it is set, every request must carry
`Authorization: Bearer <token>` or it gets a 401.

## Test

With the server running:

```bash
.venv/bin/python test_smoke.py
```

This first runs in-process unit tests of the alert layer (display rounding,
the 1-point noise floor, single-alert baseline reset), then completes an MCP
initialize handshake over streamable HTTP, checks all five tools are listed,
and exercises both market shapes against live data: a binary yes/no market
(search → two outcomes → watch → list → unwatch) and a multi-outcome board
(3+ labelled outcomes with distinct token ids), plus the failure modes
(bogus token, nonsense query, sub-1-point threshold clamped with an
explanation). To test an auth-enabled server (including the deployed one),
pass the token and URL:

```bash
MCP_URL=https://<your-app>.up.railway.app/mcp MCP_AUTH_TOKEN=<token> \
  .venv/bin/python test_smoke.py
```

## Confirmed Polymarket API shapes (verified live 2026-07-06)

Field names were confirmed with real requests during development; a wrong
field name here fails silently (no alerts ever fire), so re-verify these if
Polymarket changes its APIs.

**Search — `GET https://gamma-api.polymarket.com/public-search?q=<query>&events_status=active`**

Returns `{"events": [...], "pagination": {...}}`. **The unit of search is the
event, which groups one or more markets** — this grouping is the whole story
for multi-outcome correctness:

- A multi-outcome board (sports matchup, election, "Fed decision in July?")
  is **one event holding one binary Yes/No market per outcome**. The World
  Cup match event "United States vs. Belgium" (slug
  `fifwc-usa-bel-2026-07-06`) holds three markets: "Will United States
  win…", "…end in a draw?", "Will Belgium win…". Each market's
  `groupItemTitle` is the outcome label shown on the board ("United States",
  "Draw (United States vs. Belgium)", "Belgium"), and the outcome's price is
  that market's YES price. The three YES prices sum to ~1 across the board.
- A plain "will X happen" question is an event with a single binary market;
  its two outcomes are the market's own legs.
- `events_status=active` filters settled events server-side; markets also
  settle **individually** inside a live event, so we additionally keep only
  `active && !closed && !archived` at both levels.

Per event: `title`, `slug`, `active`/`closed`/`archived`, `markets`. Per
market:

- `question` (str), `slug` (str), `groupItemTitle` (str — outcome label)
- `active` / `closed` / `archived` (bools)
- `clobTokenIds` — **JSON-encoded string**, e.g. `'["2718…", "4095…"]'`
- `outcomes` — **JSON-encoded string**, e.g. `'["Yes", "No"]'`
- `outcomePrices` — **JSON-encoded string**, e.g. `'["0.365", "0.635"]'`

The three quoted fields need a second `json.loads` and are index-aligned:
leg *i* of a market is `(outcomes[i], clobTokenIds[i], outcomePrices[i])`.

`q` matches **title text only** — "usa" does *not* match "United States vs.
Belgium". See the teams lookup below for how we bridge that.

Note: the plain `GET /markets?order=volume` sort used by an earlier draft is
**not** a real sort field on the live API (it returns near-zero-volume
markets); `public-search` handles relevance ranking server-side, so we use it
instead. `volumeNum` / `volume24hr` are the working sort fields if you ever
need `/markets` ordering.

**Links — `https://polymarket.com/event/<event.slug>`**

Always resolves: plain events serve directly (200), sports events
307-redirect to their canonical page (e.g.
`/sports/world-cup/fifwc-usa-bel-2026-07-06`). The slug must come from the
API — a hand-built `/event/usa-vs-belgium` 404s, which was the original
link bug.

**Team abbreviations — `GET https://gamma-api.polymarket.com/teams?abbreviation=<abbr>&limit=50`**

Maps abbreviations to canonical team/country names across leagues (`usa` →
"United States" ×13, `bel` → "Belgium" ×13). **The abbreviation must be
lowercase** (`USA` returns `[]`). `search_markets` expands short query
tokens through this endpoint and searches both phrasings, so "usa belgium"
finds "United States vs. Belgium". A token is only expanded when ≥ 4 teams
share the top name, which filters flukes like "fed" or "will".

**Watch validation — `GET https://gamma-api.polymarket.com/markets?clob_token_ids=<token_id>`**

Returns `[<market>]` for an open market and `[]` for unknown **or settled**
tokens (closed markets are omitted from this query), so it doubles as the
watchability check in `watch_market`.

**Price — `GET https://clob.polymarket.com/midpoint?token_id=<id>`**

Returns `{"mid": "0.365"}` — field name `mid`, value a **string** between 0
and 1 (parse with `float`). The outcome-token midpoint stands in for implied
probability. Unknown or settled token ids return HTTP 404.

**Push — `POST https://poke.com/api/v1/inbound/api-message`**

Headers `Authorization: Bearer $POKE_API_KEY`, body `{"message": "..."}`.

## PUSH_MODE: one alert path at a time

Both proactive paths share the same move-detection logic and the same
baselines, so **never run both at once or alerts will be swallowed** —
whichever path checks first resets the baseline and the other sees no move.

- **`PUSH_MODE=0` (the default, and the deployed setting).** No poller, no
  API key needed. Each user's Poke automation calls the `check_moves` tool on
  a schedule; state is keyed per `X-Poke-User-Id`, so every user gets their
  own baselines and alerts. A single `POKE_API_KEY` can only push to its own
  owner, which is why the published multi-user server must not run the
  poller.
- **`PUSH_MODE=1` (single-user local demo only).** A background poller checks
  every watch each `POLL_SECONDS` and POSTs a short instruction-shaped
  message to the Poke inbound API, which lands in the key owner's thread.
  Requires `POKE_API_KEY`. Never set this on the deployed server.

## Deploy to Railway (publishing — manual steps)

The repo ships everything Railway needs: [railway.json](railway.json) (Railpack
builder, `python server.py` start command, restart on failure),
[.python-version](.python-version) (Railpack reads it to pin the runtime), and
[requirements.txt](requirements.txt). The server binds `0.0.0.0` on the `PORT`
Railway injects, and when a volume is attached it automatically stores state
at `$RAILWAY_VOLUME_MOUNT_PATH/watches.json`.

Do these in order:

1. **Push this repo to GitHub** (public is fine — no secrets are committed;
   `.env` and `watches.json` are git-ignored).
2. **Create a Railway project** from the GitHub repo
   (railway.com → New Project → Deploy from GitHub repo).
3. **Attach a volume** to the service and mount it at `/data`. The server
   picks the mount up via `RAILWAY_VOLUME_MOUNT_PATH`; no extra config needed.
4. **Set service variables** in Railway:
   - `MCP_AUTH_TOKEN` — a strong random token: `openssl rand -hex 32`
   - `PUSH_MODE=0` (also the code default; set it explicitly for clarity)
   - `STATE_FILE=/data/watches.json` (optional — this is already the derived
     default once the volume is mounted at `/data`)
   - Do **not** set `POKE_API_KEY`; the deployed server never pushes.
5. **Deploy**, then generate/copy the public HTTPS domain Railway assigns
   (Settings → Networking) and append `/mcp`:
   `https://<your-app>.up.railway.app/mcp`.
6. **In Poke Kitchen** ([poke.com/kitchen](https://poke.com/kitchen)), open the
   integration template: Server URL = that `/mcp` URL, **Auth Type = Bearer**,
   token = the same `MCP_AUTH_TOKEN` value. Poke will then send
   `Authorization: Bearer <token>` on every request, which is exactly what the
   server's auth middleware checks. Hit Test connection — it should list the
   five tools.
7. **Attach the integration to the recipe.**
8. **In the recipe, add the recurring automation** that calls `check_moves`
   on a schedule (e.g. every 15 minutes). `check_moves` returns crossings for
   the calling user only and resets their baselines, so Poke messages each
   user exactly once per crossing.

Deploy log sanity check — startup should print `[auth] bearer enforced` and
`[state] /data/watches.json`. If it prints `bearer DISABLED`, the token
variable is missing: fix it before wiring Kitchen.

## Local tunnel demo (alternative to deploying)

Start the server first, then in another terminal:

```bash
npx poke@latest login
npx poke@latest tunnel http://localhost:3000/mcp -n "Prediction Market Pulse"
```

To mint a shareable recipe link instead (manage it afterwards in
[Kitchen](https://poke.com/kitchen)):

```bash
npx poke@latest tunnel http://localhost:3000/mcp -n "Prediction Market Pulse" --recipe
```

The tunnel only forwards the port — it does not start the server. Keep it
running; stopping it takes the connection offline. Poke re-syncs the tool
list about every 5 minutes.

## Phase 2 note

The published, multi-user version drops the poller (`PUSH_MODE=0`) and leans
on `check_moves` + per-user Poke automations; no code changes needed. Tool
interfaces are deliberately source-agnostic (`token_id` is just an opaque
outcome id to callers, and search results are `{title, url, outcomes}` with
nothing Polymarket-specific in the shape), so a second provider (e.g. Kalshi)
can slot in behind the same tool names later.
