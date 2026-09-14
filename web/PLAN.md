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

Three.js was evaluated and dropped. The depth effect it bought did not justify
the bundle, the GPU cost, or react-three-fiber in the build. The receding
window-less look is a per-candle alpha ramp in the canvas instead, which costs
nothing and lets the series fade behind the panel.

Panel translucency is **alpha only** (`rgba(23,26,30,0.78)`), no blur, so the
candles tint the surface they pass under without touching the compositor.

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

Solid `#0f1114` background, no gradients. Accents are desaturated (`#3f9e74`,
`#cf5a52`) well past the reference's flat iOS green, because a colour that reads
calm on white reads lit-up on near-black. Colour appears only where it carries
state.

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

## Not built yet

- **Strategies, Sweeps, Results, Paper trading** — shown disabled in the nav so
  the product shape reads. Contents land in Weeks 6–13.
- **Job enqueueing.** The `jobs` table exists; nothing writes to it yet.
- **Deployment.** Runs locally against the tunnel. Production needs the app
  containerised beside Postgres on the Docker network, Caddy for TLS on :80/:443
  (both free on the box), and a domain.
- **Per-user isolation.** `user_id` columns exist on `strategies`, `jobs` and
  `runs`; nothing filters on them yet, because there is one user.

## Open questions

1. **Domain name** — needed for TLS before the app is public.
2. **Data licensing.** Yahoo's terms prohibit redistribution. Serving
   Yahoo-derived prices from a public app is a different posture from private
   research use. Options: show only derived statistics, or move equities to a
   licensed source. Resolve well before Week 14.
3. **Job quotas.** A public endpoint that queues CPU work on 2 cores needs a
   per-user cap.
4. **Alpaca key** — equity intraday is capped at 53 days by Yahoo and is not
   research-grade. A free Alpaca account fixes it.
