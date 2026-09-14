# Beyond the Backtest

Retail investors have better backtesting tools than they have ever had, and
those tools are systematically misleading. Platforms default to zero commission
and zero slippage. A parameter sweep is presented as optimisation rather than as
a multiple-comparisons problem. A single in-sample equity curve is the headline
number. The result is that a strategy which looks excellent on screen has no
demonstrated edge.

This project is a guided strategy-discovery and validation pipeline that runs
the correct statistical protocol automatically and reports a single
plain-English verdict — ROBUST, WEAK, or OVERFIT — to someone with no statistics
background.

The proposal is in [`proposal/`](proposal/); the architecture plan behind the
current structure is in `proposal/PLAN.md`.

## Layout

```
backend/          Python — data layer, engine, statistics, worker
  btb/            the package
  tests/          regression tests
  universe.json   the curated symbol list
web/              Next.js application (Weeks 9–11)
proposal/         proposal and plan
```

The two halves never call each other. Postgres is the seam: the web app inserts
into a `jobs` table and returns immediately; the Python worker claims work with
`SELECT … FOR UPDATE SKIP LOCKED` and writes results back.

## Status — Week 1, the data layer

Complete. Everything above it inherits whatever is wrong down here, so it is
built first and audited on every fetch.

```bash
cd backend
pip install -r requirements.txt

python -m btb.data init      # schema + register the universe
python -m btb.data build     # ingest the curated universe
python -m btb.data list      # what is stored, and where it came from
python -m btb.data stale     # what needs refreshing (one query, no network)
python -m btb.data verify    # re-audit; non-zero exit if anything is wrong
python -m pytest tests/ -q
```

Inspect a series in any adjustment view:

```bash
python -m btb.data show --symbol AAPL --timeframe 1d --adjust split
python -m btb.data events --symbol AAPL
```

## Storage model

Postgres, running in Docker on the project's Oracle Cloud ARM box, reachable
only over the Docker network — never published to a public interface.

**`bars` holds raw prints.** No adjustment is applied on the way in. Storing
split-adjusted prices looks immutable but is not: when a symbol splits, every
adjusted price before it silently changes meaning without a single row being
written. Storing what actually traded, and computing adjustment at read time
from an `events` table, makes each row a permanent fact — which is what lets the
database be the single source of truth with no snapshot tier beside it.

Exactly one row per series is ever rewritten: **the forming bar.** Its period
has not elapsed, so its "close" is just the current price. It is stored with
`partial = true`, excluded from every research read, and overwritten once the
period ends. The manifest's `last_ts` tracks the newest *complete* bar, so
staleness is measured against real data rather than a placeholder.

Reproducibility comes from `as_of` rather than from immutability alone —
immutable rows are not enough by themselves, because the same query next year
returns more bars. Every run records the bound it read at.

## The findings that shaped the design

Each of these is a case where the request **succeeded** and returned something
wrong. None produced an error message; all were found by checking.

1. **Yahoo's OHLC is already split-adjusted**, yet ships the event stream
   anyway, so the obvious implementation applies every split twice. Measured on
   QQQ: 22 years of prices halved, and an invented 108% one-day gain at the 2000
   split. Caught by comparing `close/adjclose`, which is flat across the split
   date. Now verified continuously — our total-return view agrees with Yahoo's
   independently computed `adjclose` to **0.05%** over 27 years.

2. **Kraken caps OHLC at ~720 candles and ignores `since`.** Binance and Bybit
   are geo-blocked; OKX, KuCoin, Bitget and MEXC return an empty list for a
   historical `since`, indistinguishable from "no such market". Bitstamp was the
   one venue of seven that paginates honestly.

3. **A short page is not the end of the data.** Bitstamp serves the window
   `[since, since + limit×step]`, so a window straddling a listing date comes
   back partly filled. Treating that as a terminator truncated ETH to 42 of its
   19,892 bars — 1% of the real history, and entirely plausible-looking.

4. **The listing-date probe could not reach back far enough.** One page of 4h
   bars is 166 days, so a linear search needs ~90 steps to reach 2011. Capped at
   40, it returned nothing — and twelve 4h crypto series were stored **empty**.
   Now an exponential search plus a bisection, which also must not end on a
   window starting at *now*: that window lies entirely in the future, returns
   empty, and made the search conclude "no data" one step past fifteen years
   of it.

5. **An empty series was reported as PASS.** The bug above was invisible because
   the audit was short-circuited for empty frames. A fetch that produced nothing
   is a failure, not a series with no findings — precisely the silent-success
   mode this layer exists to prevent.

6. **Equity bars are anchored to the session, not the clock.** SPY hourly bars
   open at 13:30 UTC and the anchor shifts with daylight saving, so an
   epoch-grid check flagged all 3,473 as corrupt. The same naïveté in reverse
   reported 3,817 "missing bars" in a clean SPY daily series — every weekend,
   counted as data loss.

7. **Real corporate actions were flagged as bad data.** Since bars are stored
   raw, AAPL's five splits *are* five genuine step-changes. Jumps are now matched
   against the event stream: an explained one is reported as explained, and only
   an unexplained one warns. That check then found AAPL's real −52% day in
   September 2000 and NVDA's dot-com drops, while accounting for every split.

8. **The most common split was invisible to that check.** A 2:1 split moves the
   price exactly 50%, and the threshold was strictly greater than 50%. Lowered
   to 35% for instruments that have corporate actions; crypto keeps 50%, since
   XRP really did move 180% in a day.

A theme runs through findings 5 to 8: **a warning that fires on healthy data is
worse than no warning**, because it trains everyone to ignore the channel.

## Design commitments

- **Gaps are reported, never filled.** A forward-filled bar is an invented
  price, indistinguishable from a real one two layers later.
- **A failing series is stored and labelled failing**, not silently dropped.
  Refusing to write leaves nothing to inspect and guarantees the next person
  re-discovers the same problem.
- **One ingest path.** Everything — CLI, backfill, refresher, the app's
  freshness check — goes through `ingest_series`, so there is exactly one place
  the audit could be skipped, which is to say nowhere.
- **The audit runs on the stored series, not the fetched page**, because only
  that reveals a gap introduced at the seam between two fetches.
- **Adjustment mode travels with the data.** A strategy compared across two
  adjustment modes is being compared across two different assets.

## Known gap

**Equity intraday history is not adequate for research.** Yahoo serves 53 days
of 5m/15m/30m and 725 days of 1h — measured, not assumed. Those series are
ingested so the pipeline is exercised end to end and they carry a thin-sample
warning, but nothing should be concluded from them. Fixing it needs Alpaca
(free key, minute data back to 2016), which the proposal already names for
paper trading.

## Next

Weeks 3–5: the strategy-spec compiler and the vectorised backtest engine —
explicit costs, stops, position sizing, and the multi-timeframe shift that makes
higher-timeframe lookahead structurally impossible.
