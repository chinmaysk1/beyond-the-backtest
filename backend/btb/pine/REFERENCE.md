# Writing a strategy for Beyond the Backtest

Beyond the Backtest runs **TradingView Pine Script v6 strategies** on its own
audited market data and scores them for overfitting. A score of 0-100 says how
likely the strategy's edge is real rather than luck from trying many settings.
This page is the contract a script must meet. Follow it exactly.

## 1. The script

- Pine **v6** only: the first line is `//@version=6`.
- A `strategy(...)`, not an `indicator(...)`.
- Size positions in the declaration, usually
  `default_qty_type=strategy.percent_of_equity, default_qty_value=100`.
- Set `process_orders_on_close=true` unless the strategy really means to fill
  at the next bar's open.
- Everything TradingView's strategy engine does is honoured: `strategy.entry`,
  `strategy.exit` (stop, limit, trailing), `strategy.close`, `strategy.order`,
  pyramiding, `var` state, arrays, maps, matrices, user-defined types and
  methods, `request.security` on the chart's own symbol.

## 2. Inputs and the grid

Every setting worth testing is an `input.int` or `input.float`, assigned to a
variable. Then declare which values to try with `// @grid` comment lines,
naming the **variable**:

```pine
len  = input.int(20, "Breakout length", minval=5, maxval=200)
mult = input.float(3.0, "Trail (x ATR)", minval=0.5, step=0.5)
// @grid len  = 10, 20, 40              a list
// @grid mult = 1.5 .. 4 step 0.5       a range, inclusive
// @constraint len > 5                  <, <=, >, >=, ==, != between inputs or numbers
```

- At most **25 values per input** and **1000 combinations** after constraints.
- Every value must respect the input's `minval` / `maxval`, and be a whole
  number for an `input.int`.
- Coarse and broad beats fine and narrow. The score penalises every setting
  tried, so a grid of 9 well-chosen settings can score higher than one of 500.
- **No `@grid` lines** means the script runs once on its defaults and is
  **not scored**. That is allowed, but it is not a test.

Optional lines that describe the strategy in the picker:

```pine
// @description Buy pullbacks in an uptrend; exit on a close above the 5-day high.
// @style mean-reversion            trend | breakout | mean-reversion | other
// @timeframes 4h, 1d
```

## 3. Costs

- If `strategy(...)` sets **any** of `commission_type`, `commission_value` or
  `slippage`, the script's costs are charged exactly as written.
- If it sets **none**, the platform's defaults are charged: crypto 0.10%
  commission + 5 bps slippage, US stocks 0% + 2 bps, as commission per side.
- Commission and slippage both 0 is allowed but flagged: real fills cost more.

Leave costs unset unless you mean specific ones.

## 4. Data

- Markets: the platform's curated list (crypto from Bitstamp, US stocks and
  ETFs from Yahoo). Call `list_markets` for symbols and timeframes.
- `request.security` may ask for the chart's own symbol on a **higher or equal**
  timeframe. Another symbol, or a timeframe finer than the chart, is refused.
- Write the higher timeframe as a string (`"D"`, `"720"`, `"W"`), or build it
  with `timeframe.multiplier`. `timeframe.from_seconds` is not supported yet.
- Use `lookahead=barmerge.lookahead_off` (the default). Never peek at a bar
  that has not closed.

## 5. Patterns that work

- **Stops that cover the whole position, from the first bar.** Place the stop
  in the same block as the entry (priced from `close`, which is the fill with
  `process_orders_on_close`), so it is live on the very next bar. Then re-issue
  it on every bar the position is open, priced from
  `strategy.position_avg_price`: an exit placed before the entry fills is sized
  for the planned position, not the filled one.

  ```pine
  if long_signal
      strategy.entry("Long", strategy.long)
      strategy.exit("stop", "Long", stop=close * (1 - stop_pct / 100))
  if strategy.position_size > 0
      strategy.exit("stop", "Long", stop=strategy.position_avg_price * (1 - stop_pct / 100))
  ```

- **`ta.*` at the top level.** Compute indicators once per bar, outside `if`
  blocks and short-circuiting `and` / `or`, then use the results inside them.
- **Bars held:** `bar_index - strategy.opentrades.entry_bar_index(0)`.
- **Channel breakouts exclude the current bar:** `ta.highest(high, n)[1]`.
- **A position reverses** when `strategy.entry` is called against it.

## 6. A complete example

```pine
//@version=6
// @description EMA cross, only in the direction of the 200 EMA, with a 2 ATR stop.
// @style trend
// @timeframes 4h, 1d
strategy("Trend-filtered EMA cross", overlay=true,
     initial_capital=10000,
     default_qty_type=strategy.percent_of_equity, default_qty_value=100,
     process_orders_on_close=true)

fast     = input.int(20, "Fast EMA", minval=2)
slow     = input.int(50, "Slow EMA", minval=3)
stop_atr = input.float(2.0, "Stop (x ATR)", minval=0.5)
// @grid fast     = 10, 20, 30
// @grid slow     = 50, 100
// @grid stop_atr = 1.5, 2, 3
// @constraint fast < slow

f = ta.ema(close, fast)
s = ta.ema(close, slow)
trend = ta.ema(close, 200)
atr = ta.atr(14)
var float entry_atr = na

if ta.crossover(f, s) and close > trend
    strategy.entry("Long", strategy.long)
    strategy.exit("stop", "Long", stop=close - stop_atr * atr)
    entry_atr := atr
if ta.crossunder(f, s)
    strategy.close("Long")
if strategy.position_size > 0
    strategy.exit("stop", "Long", stop=strategy.position_avg_price - stop_atr * entry_atr)
```

## 7. How it is judged

Each grid combination runs once over the training and test windows. Settings
are ranked on the **test** window only. The score multiplies three checks:
whether the best Sharpe survives the number of settings tried (deflated
Sharpe), how often the training winner lands in the bottom half out of sample
(PBO), and whether picking settings this way made money period by period
(walk-forward). The result is reported beside buy-and-hold over the same
period, and a later holdout window is shown separately. Nothing that picks or
scores settings ever sees the holdout.

## 8. Using the tools

1. `get_reference` (this page) and `list_markets`.
2. Write the script. Call `check_pine` and fix every error it returns; each
   names a line and column. Repeat until it returns `ok`.
3. `run_test` with the script, a symbol and a timeframe. It returns a test id
   and a link.
4. `get_test` with the id. It waits up to 50 seconds per call; call it again
   until the status is `done`. Report the score, the three factors, the test
   return against buy-and-hold, and the link.
