# Prediction Market Pulse — Poke MCP server

A single-process Python service that gives [Poke](https://poke.com) tools for
tracking Polymarket prediction markets, and proactively texts you when a
watched market's odds move past a threshold.

**Tools exposed over MCP** (streamable HTTP at `/mcp`):

| Tool | What it does |
|---|---|
| `search_markets(query)` | Search open Polymarket markets by keyword. Returns `question`, `slug`, `yes_token_id`, `yes_price` (0–1). |
| `watch_market(yes_token_id, label, threshold_points=5)` | Watch a market; baseline is the current YES price; threshold is in percentage points. |
| `list_watches()` | Your watches with baseline and threshold. |
| `unwatch(label)` | Remove a watch by label. |
| `check_moves()` | Watches that crossed their threshold since the last check (`label`, `old_pct`, `new_pct`, `delta_pts`); resets their baselines. |

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

This completes an MCP initialize handshake over streamable HTTP, checks all
five tools are listed, and calls `search_markets("fed")`, asserting a
non-empty, well-formed result. To test an auth-enabled server (including the
deployed one), pass the token and URL:

```bash
MCP_URL=https://<your-app>.up.railway.app/mcp MCP_AUTH_TOKEN=<token> \
  .venv/bin/python test_smoke.py
```

## Confirmed Polymarket API shapes (verified live 2026-07-06)

Field names were confirmed with real requests during development; a wrong
field name here fails silently (no alerts ever fire), so re-verify these if
Polymarket changes its APIs.

**Search — `GET https://gamma-api.polymarket.com/public-search?q=<query>`**

Returns `{"events": [...], "pagination": {...}}`; each event has a `markets`
list. Per market:

- `question` (str), `slug` (str)
- `active` / `closed` / `archived` (bools — we keep only
  `active && !closed && !archived`)
- `clobTokenIds` — **JSON-encoded string**, e.g. `'["2718…", "4095…"]'`
- `outcomes` — **JSON-encoded string**, e.g. `'["Yes", "No"]'`
- `outcomePrices` — **JSON-encoded string**, e.g. `'["0.07", "0.93"]'`

The three quoted fields need a second `json.loads` and are index-aligned:
the YES token id and price sit at `outcomes.index("Yes")` (index 0 in
practice, but we look it up).

Note: the plain `GET /markets?order=volume` sort used by an earlier draft is
**not** a real sort field on the live API (it returns near-zero-volume
markets); `public-search` handles relevance ranking server-side, so we use it
instead. `volumeNum` / `volume24hr` are the working sort fields if you ever
need `/markets` ordering.

**Price — `GET https://clob.polymarket.com/midpoint?token_id=<id>`**

Returns `{"mid": "0.07"}` — field name `mid`, value a **string** between 0
and 1 (parse with `float`). The YES-token midpoint stands in for implied
probability. Unknown token ids return HTTP 404.

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
interfaces are deliberately source-agnostic (`yes_token_id` is just an opaque
id to callers), so a second provider (e.g. Kalshi) can slot in behind the
same tool names later.
