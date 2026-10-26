# web — Next.js application

Status: **login and the Data tab are built and running.** This records what
exists, the decisions behind it, and what is deliberately not built yet.

```bash
cd web
npm run dev        # http://localhost:3100
npm run build && npm run start
npm run user -- <username> <password>   # create or reset a login
npm run db:studio  # Prisma Studio, for browsing the data
npm run db:pull    # re-read the schema after a backend change
```

Requires an SSH tunnel to the database: `ssh -L 55432:127.0.0.1:5432 ubuntu@<host>`.

---

## Architecture

Next.js 15 (App Router) and TypeScript. The app reads Postgres directly and will
enqueue work by inserting into `jobs`; it never calls the Python backend
synchronously. Postgres is the seam.

```
src/
  app/
    page.tsx              dashboard (server component, auth gate)
    login/page.tsx        login (redirects if already signed in)
    layout.tsx            Inter via next/font
    globals.css           the design system
    api/
      auth/login          POST — verify, set session cookie
      auth/logout         POST — destroy session
      markets             GET  — curated universe joined to stored series
      bars                GET  — one series, adjusted at read time
  components/
    Dashboard.tsx         client shell, owns selection state
    MarketList.tsx        market picker
    CandleChart.tsx       canvas renderer
    LoginForm.tsx, Brand.tsx
  lib/
    db.ts                 Prisma singleton
    auth.ts               argon2id, sessions, login throttling
    adjust.ts             read-time corporate-action adjustment
scripts/create-user.ts    account creation (no public sign-up)
```

### Prisma is introspection-only

The schema is owned by `backend/btb/schema.sql` and applied by Python. Prisma
reads it with `db pull` and **never** runs `migrate`. Two tools with an opinion
about the same tables means one of them silently drifts, and the drift is
invisible until a query returns the wrong shape.

After any backend schema change: `npm run db:pull`.

---

## Authentication

Username and password in this project's own Postgres. No OAuth, by decision.

**Passwords are hashed with argon2id, not encoded.** This is the one place the
implementation deliberately differs from the instruction as phrased, because the
distinction is a real vulnerability rather than a preference:

- Encoding (base64) is reversible — anyone who reads the table reads every
  password, including whatever the user reuses elsewhere.
- A fast hash (SHA-256, MD5) is irreversible but a GPU tries billions of guesses
  per second against it, and real passwords do not survive that.
- argon2id is deliberately slow and memory-hard. Parameters are the OWASP
  baseline (19 MiB, 2 passes) and live inside each digest, so they can be raised
  later without invalidating existing rows.

Migration applied: `users` gained `username text UNIQUE` and `password_hash
text`, and `email` became nullable since login is by username.

### Sessions

`iron-session`: an encrypted, signed cookie. Chosen over a `sessions` table
because there is no server-side state to keep in sync and no extra query per
request. The trade-off is that revoking one session before expiry is impossible;
if that becomes necessary — "sign out everywhere", or revoking a compromised
session — it moves to a table. Not needed for one user.

Cookie is `httpOnly` (out of reach of page scripts, so an XSS bug cannot steal a
session), `secure` in production, `sameSite: lax` (blocks cross-site POSTs, the
CSRF case that matters), 7-day expiry.

### Login hardening

- **Generic error.** "Invalid username or password" for both a missing user and
  a wrong password. Distinguishing them hands an attacker username enumeration.
- **Throttling.** 8 attempts per username, then a 10-minute lockout. This also
  protects the server: argon2 is intentionally expensive, so unthrottled
  attempts are a denial-of-service vector on a 2-core box as well as a
  credential one.
- **No public sign-up.** Accounts come from `npm run user`. Open registration on
  a publicly reachable app is an invitation to queue CPU work.

### Known limitation

The throttle counter is **in-memory**. It resets on restart and does not span
instances. Adequate for one server; with two app containers it must move to a
table or Redis. Recorded rather than pretended away.

---

## Data handling

### Adjustment happens on read

`bars` holds raw prints. `src/lib/adjust.ts` is a direct port of
`backend/btb/data/adjust.py`, and the two must agree — if the chart shows a
different split-adjusted price than the engine backtests on, one of them is
lying and there is no way to tell which by looking at either.

Verified through the API on AAPL (5 splits, 92 dividends):

| Mode | 2018-09-26 close | Today |
|---|---|---|
| `none` | 220.42 | 332.27 |
| `split` | 55.10 | 332.27 |
| `total` | 52.07 | 332.27 |

Raw is the real 2018 print; split-adjusted is that ÷4 for the 2020 4:1; total
return is further reduced by dividends. All three converge at the live edge,
which is what correct back-adjustment looks like.

### The forming bar

The newest bar may be `partial = true` — its period has not elapsed, so its
close is only the current price. It is returned flagged and drawn **hollow**, so
it cannot be mistaken for a settled candle. A backtest must exclude it.

### Symbols are checked against the universe

`/api/bars` rejects any symbol not in the `universe` table. Not a formality: on
a public app, without it the query string picks the table scan.

### Paging

Bars are fetched newest-first with `take`, then reversed in memory. Ordering
descending in SQL makes this an index scan of `limit` rows rather than a scan of
the whole series — which matters when one series is 315,000 bars.

---

## The chart

Canvas, drawn on demand. **No `requestAnimationFrame` loop, no
`backdrop-filter`, no CSS 3D transforms.** An earlier prototype used all three
and was unusable: a permanent render loop repainting ~60×/sec, plus blur and 3D
transforms forcing the compositor to re-rasterize layers every frame. Everything
now paints once per change — data, resize, crosshair, pan — and then stops.

Three.js was evaluated and dropped *for the chart*. The depth effect it bought
did not justify the bundle, the GPU cost, or react-three-fiber in the build.
The receding window-less look is a per-candle alpha ramp in the canvas instead,
which costs nothing and lets the series fade behind the panel. (It is used on
the landing, under strict limits; see below.)

The market list over the candles is one of the few surfaces that does blur
(see "Glass" below): the candles move under it when panning, so blur there is
what makes it read as glass. The chart itself still paints only on change.

TradingView conventions: green up / red down bodies with thin wicks, right-hand
price axis with gridlines, bottom time axis, dashed last-price line with a
coloured tag, volume histogram tinted per candle. The last-price line tracks the
newest bar of the **series**, not of the visible window, so panning into history
does not relabel an old level as "current price".

Interaction: drag or scroll to pan, crosshair with OHLC readout on hover.
`ResizeObserver` rather than a window listener, because the canvas also resizes
when the panel beside it does.

---

## Design

A dark reading of the Navexa reference: pill tab group with a raised active tab,
oversized page title, a floating translucent list panel, and an unframed central
visualisation that continues behind it.

Everything in the reference this product does not actually do was dropped — KPI
strip, status badges, coverage meters, bottom metric cards, decorative icon
buttons. Chrome for features that do not exist is how a prototype starts lying
about what has been built.

Weeks 5–6 replaced the solid ground with glass on a static space backdrop
(next section). Accents are softer still (`#7fb89d`, `#c98a84`), because a
colour that reads calm on white reads lit-up on near-black. The candles are the
exception: they keep their cleaner `#2f9e79` / `#cf5a52`, which the pastel
accents made look washed out. Colour appears only
where it carries state, plus one fixed meaning: buy-and-hold is blue
(`#6f9bd6`) on every chart.

---

## Glass

Clear glass, faintly tinted pink to yellow (`#f2d2f0` to `#ffe879`), on a
near-black space backdrop with a few dim stars and two faint patches of colour.
A surface is three parts: an almost invisible fill, a hairline rim a little
brighter at the top-left, and a very faint reflection across the top. It all
lives in `src/app/glass.css`, imported last, as tokens plus one material applied
to every surface, so no screen hand-tunes its own copy.

**The blur budget.** This replaces the old "no `backdrop-filter`" rule, which
existed because blur plus a render loop made an earlier prototype unusable.
Blur re-rasterises whatever is beneath a layer on every paint, so:

- **The backdrop is painted once and is already soft.** Blurring it would change
  almost nothing, so cards and panels over it are glass *without* blur.
- **Real blur only over moving, sharp content:** the nav pill, the market list
  over the candles, menus and tooltips. Never inside a scrolling area: blur
  there is redrawn on every frame of the scroll, which is why the Results
  filters panel does not blur.
- **Cards are cheap glass.** A grid of cards uses an inset hairline for its rim
  instead of the masked gradient the larger panels use, hover changes one
  colour with no motion, off-screen cards skip rendering (`content-visibility`),
  and while the list scrolls the cards ignore the pointer, so a still cursor
  does not set off a hover repaint on every card passing beneath it.
- **Effects move only transform and opacity.** No animated gradients; still no
  render loop on any working screen (the landing's scene is the one exception).
- **No refraction.** iOS-style lensing needs SVG displacement on the backdrop,
  which only Chromium supports and which costs far more per frame.
- **Acceptance:** scrolling the Results grid and a test's page holds 60 fps.
  Measured with 104 cards in headless Chrome: 240 frames in 4 s, p95 17 ms, one
  34 ms frame; the test page had none over 20 ms.

On a test's page, glass is only on the stat tiles and the settings table; the
score row, the charts and the trades sit directly on the backdrop between
hairlines, so the page does not turn into a stack of boxes.

## Results (Weeks 5–6)

One card per test: strategy, market and timeframe, the score as a ring, the
test-window return against holding, both curves over the test window (rebased
to 1, so a line ends where the card's number says), and the settings. The
filters panel on the left searches, sorts by score / return / vs holding /
newest, and narrows by minimum score, beat-holding, market and timeframe. A card
opens the test's page. There is one card per strategy, market and timeframe:
running the same test again replaces its card with the newest run, and a
strategy is known by its title, so an edited custom strategy replaces its
older card too. Unscored tests (still scoring, or a failed sweep) sort
last under every order but Newest. The list polls while anything is scoring.

The score ring's colour is its calibrated band (`btb/verdict/rules.py` BANDS):
green from 30, a score luck reaches at most 5% of the time; amber from 12; red
below.

---

## Build notes worth keeping

- **`baseUrl` is deprecated in TypeScript 6** and removed in 7. It also silently
  broke Next's `@/*` alias resolution under webpack — every module in the app
  failed to resolve with no indication why. Imports are relative now and both
  options are gone from `tsconfig.json`.
- **TypeScript must be pinned to 6.x.** npm installs 7 by default, which Next 15
  rejects outright.
- **argon2 is a native module.** `serverExternalPackages` in `next.config.mjs`
  plus `runtime = 'nodejs'` on the auth routes keeps it out of the edge runtime.

---

## Landing and the transition into the app

Every visit opens on a landing: a dark space scene, the wordmark, and five
cards, one per section. Choosing one flies the five cards into the nav tabs and
the wordmark into the brand, while the section rises from below. The brand, or
Esc, plays it in reverse.

- **`SpaceScene.tsx` is the one continuous render loop in the app.** It uses
  three.js (plain, no react-three-fiber), is loaded with `next/dynamic` so it is
  its own chunk, runs only while the landing is on screen, stops outright once
  a section is entered, pauses on a hidden tab, and honours
  `prefers-reduced-motion`. The no-rAF-loop rule for the working screens stands.
- **Dark and quiet by design:** low-opacity stars, dim candles, faint lines. It
  is a backdrop for the wordmark, not the subject.
- **`lib/flight.ts`** does the card-to-tab flight with throwaway clones and the
  Web Animations API. Cards and tabs live in different parts of the tree, so a
  clone that starts as one and ends as the other is what makes them read as
  the same object.
- Styles for all of this, and for the Strategies flow, are in
  `src/app/experience.css`, apart from the Data tab's `globals.css`.

## Strategies (Weeks 2–3)

One flow in three phases, each crossfading into the next:

1. **Setup** — a single centred panel: market (searchable), timeframe,
   strategy (library, or your own JSON), costs. Every timeframe is always
   shown; one a market lacks is disabled with the reason (Yahoo serves no 4h
   for stocks). Custom JSON comes with a copyable prompt for ChatGPT or Claude,
   and is checked as you paste.
2. **Running** — the backtest and its sweep, with live progress from the
   worker.
3. **Results** — a page rather than a stack of boxes: the test-window return
   against buy-and-hold with a plain-English sentence; Train / Test / Train+test;
   the equity curve (it draws in once, then stops); the best settings with
   train rank beside each, plus a **rank scatter** of every combination
   (train rank against test rank); trades; the costs paid.

**Run backtest always queues the sweep too.** Finding the best settings is not
a separate decision to know to make, and a best-settings list without the
train/test comparison beside it is the misleading number this product exists
to replace. "Use" on a row re-runs the backtest with those settings.

| Route | Does |
|---|---|
| `GET /api/strategies` | Newest version of each library strategy |
| `POST /api/backtests` | `{kind, withSweep, strategy \| spec, symbol, timeframe, costs?, params?}`. Validates market, strategy, settings, cost bounds; a pasted `spec` is checked (`lib/specCheck.ts`), stored as a strategies row owned by the user (deduplicated by hash), and referenced by id. At most 4 open jobs per user. |
| `GET /api/backtests/[id]` | Status and progress; once done, the result, plus the full run's trades and curve for a backtest. Owner only. |

`lib/specCheck.ts` mirrors the engine's validator for instant feedback. It is
not the authority: the worker re-validates every spec with the full rules
before running it, and returns its exact error if it fails. Sweeps are capped
at 500 combinations in both places.

## Strategies in Pine (Weeks 7–8)

The strategy picker's second and third tabs replaced "Custom JSON":

- **Your Pine** -- a plain textarea (by decision: a code editor can come later,
  nothing depends on it) and beside it what the engine's own checker says,
  700 ms after the last keystroke: errors with line and column, then the
  inputs, the values each `// @grid` line sweeps, N, the costs the script will
  be charged, and warnings (zero costs, no grid). The checker is
  `btb/pine/check.py`; the app cannot run it (the transpiler is Python), so
  `/api/pine/check` queues a `check` job and waits for the checker service, a
  second worker that claims nothing else (`lib/pineCheck.ts`). Answers are
  cached by source hash, so checking as you type and checking again on Run is
  one job.
- **Connect your AI** -- the MCP address, steps for Claude, ChatGPT and Claude
  Code, personal access tokens (shown once, revocable), connected OAuth apps
  (disconnect revokes everything they hold), and the copy-prompt for a chatbot
  without tools.

The library is Pine now; a selected library strategy has "Edit as Pine". The
costs field shows the script's own costs when it sets any, and the editable
market defaults otherwise. A script with no `@grid` lines runs once: the run
button says so, its card shows a dashed ring and "Unscored · no @grid", and its
page explains why there is nothing to rank.

`POST /api/backtests` takes `source` instead of `spec`: checked, stored as a
`strategies` row with `language = 'pine'` (deduplicated by source hash), and
run by id. `lib/queueTest.ts` holds the whole path so the MCP `run_test` tool
queues exactly what the form does; `lib/tests.ts` holds the Results listing
for the same reason.

Tests made with the retired JSON format keep their pages and cards (marked
"Legacy JSON · read-only"); their pages hide re-runs, window dragging and the
holdout refresh, all of which would need the JSON engine to run again.

`/?test=ID` opens one test directly. The MCP tools hand out these links.

The Pine contract a model is shown lives in `backend/btb/pine/REFERENCE.md`.
The app is built without the backend beside it, so
`node scripts/sync-reference.mjs` copies it into `src/lib/pineReference.ts`;
`backend/tests/test_pine_reference.py` fails if the copy is stale.

## The MCP server (Weeks 7–8)

`/api/mcp`, streamable HTTP, on the official TypeScript SDK
(`@modelcontextprotocol/server` 2.x, `createMcpHandler`): a fresh server per
request, bound to the bearer token's user, serving both the 2026 protocol and
the 2025 stateless fallback. Six tools in the order a model uses them --
`get_reference`, `list_markets`, `check_pine`, `run_test`, `get_test` (waits up
to 50 s per call), `list_tests` -- plus a `write_strategy` prompt and the
reference as a resource. Results are JSON text.

Auth is the site acting as its own OAuth 2.1 authorization server, just enough
for connectors to attach with no manual step:

| Endpoint | Does |
|---|---|
| `/.well-known/oauth-protected-resource[/api/mcp]` | RFC 9728: where to authorize. The MCP 401 challenge points here. |
| `/.well-known/oauth-authorization-server` | RFC 8414 metadata. |
| `/oauth/register` | RFC 7591 dynamic registration. Public clients only; redirect URIs must be https, or http on localhost. |
| `/oauth/authorize` | Sign in if needed (login takes a same-site `next`), then one consent screen. A bad client or unregistered redirect URI is shown here, never redirected to. |
| `/oauth/token` | Code + PKCE S256 (required), or a refresh token. Codes are single-use; refresh tokens rotate on every use. |
| `/api/tokens` | Personal access tokens and connected apps, for the Connect panel. |

Every token is stored as its sha256 (`api_tokens`); access tokens last an
hour, refresh tokens 30 days, personal tokens until revoked. A refresh token is
not accepted as a bearer token. Checked end to end against the dev server:
registration, the consent page, a wrong verifier, a replayed code, a reused
refresh token and a disconnected app are all refused.

`BTB_PUBLIC_URL` is the issuer and the address in every link; without it the
request's own origin is used, which is right locally and wrong behind Caddy.

## Not built yet

- **Paper trading** — opens a plain "arrives in weeks 9–12" screen, so the
  product's shape reads without faking what isn't built.
- **Deployment** is written but not run: `web/Dockerfile` (Next standalone),
  `deploy/Caddyfile`, and the web, checker and Caddy services in
  `deploy/docker-compose.yml` / `up.sh`. It needs a domain pointed at the box
  and ports 80/443 opened in the Oracle security list.
- **Per-user isolation.** `user_id` columns exist on `strategies`, `jobs` and
  `runs`; nothing filters on them yet, because there is one user.

## Open questions

1. **Domain name** — needed for TLS before the app is public, and for Claude.ai
   and ChatGPT connectors, which reach the MCP server from their own servers.
2. **Data licensing.** Yahoo's terms prohibit redistribution. Serving
   Yahoo-derived prices from a public app is a different posture from private
   research use. Options: show only derived statistics, or move equities to a
   licensed source. Resolve well before Week 14.
3. **Job quotas.** A public endpoint that queues CPU work on 2 cores needs a
   per-user cap.
4. **Alpaca key** — equity intraday is capped at 53 days by Yahoo and is not
   research-grade. A free Alpaca account fixes it.
5. **PineForge licence** — the transpiler's licence treats operating it for
   other people's trading as commercial. Invited accounts only until the
   licensor confirms a public academic deployment is fine.
