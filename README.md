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
  btb/            the package (btb/pine: strategies in Pine, through PineForge)
  tests/          regression tests
  universe.json   the curated symbol list
web/              Next.js application, and the MCP server (/api/mcp)
deploy/           what runs on the server: compose file, up.sh, Caddyfile
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

## Status — Weeks 2–3, the backtest engine and the Strategies experience

`backend/btb/engine/`. (Weeks 7–8 replaced the JSON spec with Pine; see below.)
A strategy is a JSON spec, never code; the engine turns
it into trades with costs, stops and sizing inside the fill model, and records
every run with the `as_of` and spec hash that reproduce it.

```bash
python -m btb.engine library --sync        # validate the built-in specs, load into DB
python -m btb.engine run   --strategy ema_cross_adx --symbol BTC/USD --timeframe 4h
python -m btb.engine sweep --strategy ema_cross_adx --symbol BTC/USD --timeframe 4h
python -m btb.engine worker                # claim jobs; refresh stale data when idle
python -m btb.engine schema                # the spec's JSON Schema (what an AI writes to)
python -m btb.engine run ... --csv FILE    # fully offline, no database
```

| Module | Does |
|---|---|
| `spec.py` | The spec language: composable primitives (sources, arithmetic, series ops, averages, Pine-exact named indicators, higher-timeframe nodes, conditions). Validates with exact error paths; generates its own JSON Schema. |
| `indicators.py`, `timeframes.py` | The maths. Wilder smoothing where Pine uses it; higher timeframes grouped by wall-clock bucket and shifted one bar. |
| `fills.py`, `metrics.py` | The position loop (compiled with numba when installed) and the numbers. |
| `windows.py`, `jobs.py`, `worker.py` | Train/test/holdout from bars actually held; backtest and sweep jobs; the queue worker. |
| `refresh.py` | Keeps stored series current. Runs in the worker whenever the queue is empty. |

**Parity is the gate.** `tests/reference_engine.py` is the earlier prototype's
pure-Python loop, which was reconciled bar by bar against TradingView. The
engine reproduces its trades *exactly* — every entry and exit on the same bar at
the same price, 199 trades on 14 years of BTC 4h — in
`tests/test_engine_parity.py`.

The engine deliberately differs from it in three ways. Each one is an option,
and the parity test turns all three off:

- **A stop the bar gaps through fills at the open**, not at the stop.
- **An open position at the end is closed and counted.** The reference drops it
  from the stats.
- **An account that would go below zero is liquidated.** The reference only
  checks at exits, so an unstopped short through BTC's 2013 rally sat at −160%
  mid-trade and kept going.

**A sweep ranks on the test window, never on train**, and reports train rank
beside it plus the train/test rank correlation across the whole grid. On the
live strategy's grid that correlation is 0.32, and the #1 test setting ranked
#13 on train: the in-sample-rank problem the proposal describes, measured. It
runs a fixed grid rather than an optimiser, so the number of configurations
tried (N) is known exactly, which the verdict engine needs. 162 runs take 1.5 s.

**Data now refreshes itself.** When a series' next bar is due, the worker's idle
loop fetches it through `ingest_series`, the same audited path as a backfill.
It asks at most every 5 minutes, and only once per bar period when a market is
closed.

**The web app got a front door and a real Strategies tab.** Every visit opens
on a dark 3D landing; choosing a section flies its card into the nav and raises
the section from below. Strategies is one flow: pick a market, timeframe and
strategy (from the library, or your own JSON, written with the copyable AI
prompt and checked as you paste), run it, and read a results page built around
the test-window return, the equity curve, the best settings with their train
rank, and a scatter of every combination's train rank against its test rank.
Running a backtest always runs its sweep too. Details in `web/PLAN.md`.

## Status — Week 4, the verdict engine

(Weeks 5–6 replaced the one-word label with a 0–100 score; see below. The
checks are unchanged.)

`backend/btb/verdict/`. Every sweep now ends in one word -- ROBUST, WEAK or
OVERFIT -- and one sentence saying why, from three independent checks over one
matrix of daily returns (every grid combination, run once over train + test):

| Check | Asks | Source |
|---|---|---|
| Walk-forward (`walkforward.py`) | Would picking settings this way have worked in real time? 5 anchored folds, each trading the settings that were best on everything before it. | Pardo (2008) |
| PBO by CSCV (`cscv.py`) | How often does the in-sample winner land in the bottom half out of sample? 16 blocks, all 12,870 half/half splits. | Bailey, Borwein, López de Prado & Zhu (2016) |
| Deflated Sharpe (`dsr.py`) | Is this Sharpe better than the best of N skill-less tries? N is the grid's exact size. | Bailey & López de Prado (2014) |

`rules.py` turns them into the label: any failing check is OVERFIT; any warning,
or too little data to run a check, is WEAK; ROBUST needs all of them. Walk-forward
and PBO judge the grid, the deflated Sharpe each row, so no single setting can be
ROBUST when picking from its grid is overfit. The verdict is always scored on the
default windows -- a user can drag train and test to explore, but not until the
label changes -- and never reads the holdout.

```bash
python -m btb.engine verdict   --strategy ema_cross_adx --symbol BTC/USD --timeframe 4h
python -m btb.engine calibrate --symbol SPY --timeframe 1d --walks 50
```

**Checked against known answers.** The deflated Sharpe reproduces the paper's
worked example (0.9004). PBO is ~0.5 on pure noise and ~0 with a planted edge.
Walk-forward never reads a day after its fold. Rewriting the holdout changes
nothing.

**False positives: 0 of 600.** On random walks with the real series' timestamps
and volatility -- where no strategy has an edge -- every library strategy was
scored 50 times on BTC 4h and 50 times on SPY 1d. None came out ROBUST (target:
at most 5%).

**On real data, almost everything is overfit.** On BTC/USD 1d and SPY 1d, 11 of
the 12 library strategy/market pairs come out OVERFIT and one (Bollinger + RSI on
SPY) WEAK. The live EMA-cross + ADX strategy is OVERFIT on BTC 1d (PBO 0.53,
deflated Sharpe 0.20) and WEAK on BTC 4h (PBO 0.21). Walk-forward returns that
look large still trail buy-and-hold over the same periods: +938% against
+17,083% for EMA-cross + ADX on BTC 1d, +90% against +473% for EMA cross on SPY. This is the result the project exists to surface, not a
calibration failure: the random walks show the engine is not simply harsh.

**Results is where every test lives.** The Sweeps tab is gone. A run from
Strategies lands on its page in Results: the headline return and the verdict,
the equity curve with draggable train/test windows, every setting the sweep
tried with its own verdict, and how the verdict was reached (the walk-forward
periods and the overfitting test's distribution). The Results list shows every
test with its verdict, filterable.

The cost: a sweep takes roughly twice as long, because every combination is run
once more over the whole span. On one core that is still seconds.

## Status — Weeks 5–6, a score instead of a word, and glass

**The verdict is now a 0–100 score.** Three words hid too much: on real data
almost everything was OVERFIT, so SPY 1h Bollinger + RSI (beat holding over a
flat stretch) looked the same as NVDA Bollinger + RSI (lost 80% while holding
made 41x). The three checks each already produce a probability, so the score
is their product:

    score = 100 x deflated Sharpe x (1 - PBO) x (walk-forward wins + 1) / (folds + 2)

| Factor | What it is | On the page |
|---|---|---|
| Deflated Sharpe | P(this setting's true Sharpe > 0), after N settings tried | "Survives 81 settings tried" |
| 1 - PBO | P(the training winner does not land in the bottom half out of sample) | "Holds up on unseen data" |
| Walk-forward | Laplace's rule of succession over the 5 folds: P(the next period is up) | "Real-time picks made money, 5 of 5" |

High only when all three agree; any one at zero vetoes it. The three are not
independent, so the score is a confidence score built from probabilities, not
itself a probability, and the page always shows the three factors beside it. A
check that could not run counts as no evidence (1/2), never as a pass. The
score does not say whether the strategy beat holding -- that is a different
question, so every card and page shows return against buy-and-hold next to it.

**What counts as high comes from calibration, not intuition.** On random walks
with the real series' timestamps and volatility (50 walks x the six library
strategies per market), the scores luck alone produces:

| | p75 | p95 | scores >= 30 | scores >= 12 |
|---|---|---|---|---|
| SPY 1d | 11 | 29 | 15/300 (5.0%) | 73/300 (24%) |
| BTC/USD 4h | 3 | 16 | 4/300 (1.3%) | 23/300 (8%) |

So `high` starts at 30 (luck reaches it at most 5% of the time on either
market) and `mid` at 12 (at most 25%). On the stored tests the live EMA-cross +
ADX strategy on BTC 4h scores 57 (high), SPY 1h Bollinger + RSI 16 (mid), and the
rest 0-8.

```bash
python -m btb.engine verdict   --strategy ema_cross_adx --symbol BTC/USD --timeframe 4h
python -m btb.engine calibrate --symbol SPY --timeframe 1d --asset-class equity --walks 50
python -m btb.engine rescore   --dry-run     # re-score stored verdicts from their stored numbers
```

`rules.py` holds the formula and the bands; `VERSION` is 2. Old verdicts were
re-scored in place from the numbers they already held -- nothing re-ran.

**Results is a grid of cards.** Each test is one card: the strategy, its market
and timeframe, the score as a ring, the test-window return against holding, both
curves over the test window (holding in blue), and the settings it ran with. A
filters panel on the left searches, sorts (score, return, vs holding, newest),
and narrows by minimum score, beat-holding, market and timeframe. A test's page
keeps its layout; the verdict word and its three check columns became the score
ring, the reason, and the three factors, and the settings table gained a Score
column and a "score at least" slider.

**The whole site is glass.** Clear glass on a static space backdrop, faintly
tinted pink to yellow. The no-blur rule became a budget (see `web/PLAN.md`,
"Glass"): cards and panels over the static backdrop are glass without blur;
real blur only on the nav, the market list over the candles, menus and
tooltips, and never inside a scrolling area. Scrolling 104 cards held 60 fps (240 frames in 4
s, p95 17 ms) in headless Chrome.

**The holdout is everything after the test window.** It used to be a locked
final year nothing could see. Now it follows the test-end handle: end test
earlier and the holdout grows, later and it shrinks (it always keeps at least
100 bars). Every backtest runs the chosen settings over it, so a test's page
has a Holdout tab -- how those settings did on data after everything they
were picked on -- and the equity curve runs on into it. The holdout runs to the
newest bar: when a test is opened and its holdout is missing (it ran before
this change) or behind the market, an `extend` backtest reruns just the
holdout on the newest bars and writes it back onto the test, leaving its
train/test numbers and score as they were. Paper trading
(weeks 9-12) is the forward test from live. Nothing that picks or scores
settings reads the holdout: the sweep ranks on test, and the score is always
computed on the default windows. One caveat when test is ended *earlier* than
the default: the stretch between the new test end and the default holdout was
not used to pick the settings, but the score's checks did see it.

## Status — Week 7, sweeps on short timeframes

**"Still seconds" held only on 4h.** The first 15m grid (`panic_fade` on BTC/USD,
500 combinations) took 454 s on the server: 15m has 16× the bars, and the
profile put the time somewhere unexpected. 54% went to the ATR behind ATR stops
and targets, which bypassed the evaluator's memo and was recomputed per run;
18% to formatting ~4,000 trades per run that a sweep never stores; 22% to the
fill loop. The ATR is now memoised like any indicator, sweep and verdict runs
skip the trade dicts, and the fill loop is compiled with numba. The same grid
takes 35 s on one server core.

**Same results, bit for bit.** The compiled loop follows the old one block for
block, without `fastmath`, so every operation is the same IEEE double operation
Python does, in the same order. Without numba installed the same source runs as
plain Python. Old engine against new over 455 configurations (every library
strategy, both fill modes, both stop fills, 1× and 5× leverage, train, test and
the verdict's return matrix) differ in 0, on the laptop and on the ARM server.

## Status — Weeks 7–8, strategies in Pine, written by your own AI

**A strategy is a TradingView Pine Script v6 file now.** The JSON spec could
only say stateless things -- four boolean rules, one fixed stop -- and most
real strategies need state: a regime flag set by one event and cleared by
another, a trailing stop, bars since entry, scaling in. Pine is also what the
people this is for already write. Scripts run through
[PineForge](https://github.com/pineforge-4pass): its transpiler turns Pine into
C++, `g++` compiles that once per script against its engine, and a small C shim
of ours (`btb/pine/native/btb_shim.cpp`) drives the result from Python. The
verdict engine, the score and Results did not change.

```bash
python -m btb.engine check  my_strategy.pine          # errors with line:col, inputs, grid, N, costs
python -m btb.engine run     --file my_strategy.pine --symbol BTC/USD --timeframe 4h
python -m btb.engine verdict --strategy ema_cross_adx --csv tests/fixtures/btcusd_4h.csv.gz --timeframe 4h
python -m btb.engine reference                         # the Pine contract a model is shown
python -m btb.engine worker --kinds check               # the checker: answers check jobs only
```

Pine runs in the worker image (`backend/Dockerfile`, on PineForge 1.6.0); a
machine without it still runs everything else, and the Pine tests skip.

| Module | Does |
|---|---|
| `pine/check.py` | Source -> errors and warnings with line and column, the inputs, the grid and N, the costs, the other markets it reads (resolved against the universe and fed to the engine). Adds the warning TradingView gives and PineForge does not: a `ta.*` call that runs on some bars only. The one judge: the web app, the MCP server and the worker all call it. |
| `pine/pragmas.py` | `// @grid name = 10, 20, 40` (or `1 .. 5 step 0.5`) and `// @constraint a < b`, resolved against the inputs. Pure Python. No `@grid` lines means the script runs once and is not scored. |
| `pine/native.py`, `native/btb_shim.cpp` | Compile once per C++ (cached by hash); run on our bars with inputs, cost overrides, a trade start and the symbol's tick, returning per-bar equity and trades. |
| `pine/sandbox.py` | Native code runs only in forked children with memory and CPU limits and a wall-clock timeout; a crash or runaway loop fails the job, not the worker. |
| `pine/program.py` | Engine output -> the same `Fills` the JSON engine produced, so metrics, curves, the sweep table and the verdict read it unchanged. The cost policy. One run per sweep combination. |
| `pine/library/*.pine` | The six library strategies, ported. Each grid is the JSON one exactly, so N is unchanged. |
| `pine/REFERENCE.md` | The contract: pragmas, costs, data, patterns that work, a complete example. Served to models by the MCP server and the copy-prompt from this one file. |

**Parity is still the gate.** Each ported library strategy, with the three
semantics the engines are meant to differ on set to the old engine's (below),
trades exactly as the JSON engine did -- every entry and exit on the same bar at
the same price -- across the default settings and a spread of the grid, from
2016 on BTC 4h (`tests/test_pine.py`). One setting of one strategy is the
exception, and is finding 9.

**Costs: the script's own if it sets any, the market's if it sets none.**
Pine charges slippage as a fixed number of ticks, which cannot say "5 bps" on a
series that went from $10 to $100,000, so unset defaults are charged as
commission (0.10% + 5 bps = 0.15% a side for crypto). A script that sets both
to zero is run as written and flagged on its card.

**One run per combination, two processes.** A scored test used to run every
combination twice, once for train and test and once over the whole span for the
score. Pine is slower per run than the vectorised engine, so each combination
now runs once over train + test; the train and test numbers are cut from that
run, and the score's daily returns come from the same equity curve. The worker
gets the box's second core for sweeps. Measured on the ARM box, one core:
the 81-setting BTC 4h grid in 5.8 s; the 500-setting 15m grid in 290 s, which
the two changes bring to about 2.4 minutes.

**Calibration holds.** On 50 random walks per market with the ported library:
SPY 1d p95 29 and 15/300 (5.0%) at or above 30; BTC 4h p95 17 and 7/300 (2.3%).
The bands stay where they were.

**Your own AI, over MCP.** `/api/mcp` is a Model Context Protocol server with
six tools -- `get_reference`, `list_markets`, `check_pine`, `run_test`,
`get_test`, `list_tests` -- plus a `write_strategy` prompt. Claude and ChatGPT
connectors sign in with OAuth 2.1 (dynamic client registration, PKCE required,
rotating refresh tokens); Claude Code can use a personal access token. Every
call is bound to the token's user. We run no model: the user's own does the
writing, the site does the checking and the testing. A copy-prompt built from
the same reference serves a chatbot with no tools. Details in `web/PLAN.md`.

### What the port found

Each was isolated by reading PineForge's values out of a run (a probe script
that places orders sized by the indicator), not inferred from the trade lists.

1. **EMA warm-up.** The JSON engine hid an EMA until it had n values;
   PineForge shows it from bar 0, seeded with the first close, and warns that
   this is its approximation ("PineForge may return a finite value before
   TradingView does"). TradingView is on the JSON engine's side here. Early
   entries move; nothing after the first few hundred bars does.
2. **ADX across bars with no range.** BTC 4h has 980 bars where high, low and
   the previous close are equal, almost all before 2016. Pine's `ta.dmi` carries
   the last DI forward (`fixnan`) and keeps smoothing; the JSON engine skipped
   the bar. ADX differs by up to ~8 points there, then converges.
3. **Higher timeframes, one bar sooner.** On the last base bar of a 12h period,
   `request.security` with lookahead off already shows the completed 12h value;
   the JSON engine showed it one bar later. Neither looks ahead.
4. **Sizing.** TradingView sizes a 100%-of-equity order so the commission fits:
   positions are 0.1% smaller and final equity about 0.3% lower.
5. **Margin calls.** Pine's default 100% margin partially liquidates a losing
   short and keeps trading; the JSON engine ended the run at zero equity.
6. **Order prices round to the tick.** A stop at 3.20306 fills at 3.20.
7. **Exits are sized when placed.** A stop placed before its entry fills covers
   the planned position, not the filled one; a margin call in between leaves
   part of the position unprotected. The reference tells models to re-issue
   `strategy.exit` every bar.
8. **Stops gapped through fill at the open** -- the one rule both engines
   already shared, which is why `stop_fill="gap"` was the setting that matched.
9. **The JSON engine's Supertrend was not Pine's.** It decided a flip against
   the previous bar's band; Pine compares the close with the current, ratcheted
   band. On BTC 4h that changes one flip in about 470 trades for one setting.
   The "Pine-exact named indicators" claim of Weeks 2–3 was wrong for this one.
10. **A ruined account stays ruined.** With one run per combination, an account
    lost during train starts the test window at zero, where a separate test run
    had started fresh and hidden it. The first calibration run divided by that
    zero; the test window is now reported as ruined.

2 and 3 split the two Pine runtimes tried (PineTS agreed with the JSON engine
on both); only TradingView can say which is right, and PineForge's published
TradingView probes are the evidence so far. Both matter only in the first bars
of a series and on illiquid stretches.

**The JSON spec is retired, not deleted.** Tests made with it keep their pages,
marked read-only; they cannot be re-run. `btb/engine/spec.py` and
`btb/engine/library/*.json` stay as the oracle the Pine library is tested
against (`--legacy` on the CLI runs them).

**Licence.** PineForge's engine is Apache-2.0; its transpiler is under the
PineForge Source License, free for non-commercial use and personal trading.
Operating it for other people's trading may need the licensor's permission, so
accounts stay invitation-only (there is no sign-up) until that is settled. The
runtime sits behind one adapter; PineTS (AGPL) is the drop-in alternative.

## Next

Weeks 9–12: paper trading -- the forward test from live data -- and the site
going public behind Caddy.
