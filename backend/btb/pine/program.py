"""A checked Pine strategy, run the way the rest of the engine expects.

`Program.from_source(text)` checks and transpiles once. Its runs come back as
the same `Fills` the JSON engine's position loop produced, so metrics, the
stored equity curve, the sweep table and the verdict read them unchanged.

What changed with Pine, and is decided here:

* **The script owns fills and position state.** TradingView semantics, from
  PineForge: `process_orders_on_close`, stop/limit/trailing orders,
  pyramiding, margin calls, prices rounded to the tick. The JSON engine's
  `fill` / `stop_fill` / `liquidate` options do not exist for Pine.
* **Costs: the script's if it sets any, else the asset-class defaults.**
  Pine slippage is a fixed number of ticks, which cannot express "5 bps" on a
  series that went from $10 to $100,000. Unset, the default slippage is
  therefore charged as extra commission -- commission + slippage/100 percent
  per side -- which costs the same per unit of notional, and the page says so.
* **Windows.** Indicators warm up on all history before the window; orders
  are honoured from the window's start (PineForge's trade start), no bar after
  its end is fed, and a position still open at the end is closed at the last
  close, as TradingView's report does.
* **One run per sweep combination.** Each combination runs once over train +
  test; the train and test numbers are cut from that run, and the verdict's
  daily returns come from the same equity curve. A position open at the
  train/test boundary is carried across rather than closed and reopened --
  what a real account would do.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..engine import metrics as M
from ..engine.fills import Costs, Fills, EQ0
from . import native as N
from . import pragmas as PR
from .check import CheckResult, check
from .sandbox import Runner

PROCESSES = int(os.environ.get("BTB_PINE_PROCESSES", "2"))
RUNTIME = "pineforge"


class PineError(ValueError):
    """A script that cannot run. `messages` are line-numbered, for the user."""

    def __init__(self, messages: list[str]):
        self.messages = messages
        super().__init__("; ".join(messages[:4]) + (f" (+{len(messages) - 4} more)"
                                                     if len(messages) > 4 else ""))


@dataclass
class Program:
    checked: CheckResult
    _so: Path | None = None

    @classmethod
    def from_source(cls, source: str) -> "Program":
        r = check(source)
        if not r.ok:
            raise PineError([str(m) for m in r.errors])
        return cls(r)

    # -- what it is -------------------------------------------------------------------

    @property
    def source(self) -> str:
        return self.checked.source

    @property
    def sha(self) -> str:
        return self.checked.sha

    @property
    def title(self) -> str:
        return self.checked.title

    @property
    def inputs(self) -> list[PR.Input]:
        return self.checked.inputs

    @property
    def grid(self) -> PR.Grid:
        return self.checked.grid

    @property
    def scored(self) -> bool:
        return self.grid.scored

    @property
    def feeds(self) -> list[dict]:
        """Other markets the script reads through request.security."""
        return self.checked.feeds

    def defaults(self) -> dict:
        return PR.defaults(self.inputs)

    def resolve_params(self, overrides: dict | None) -> dict:
        p = self.defaults()
        names = {i.name: i for i in self.inputs if i.name}
        for k, v in (overrides or {}).items():
            inp = names.get(k)
            if inp is None or not inp.numeric:
                raise PineError([f"params.{k}: not a numeric input of this script"])
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise PineError([f"params.{k}: must be a number"])
            if inp.type == "int" and not float(v).is_integer():
                raise PineError([f"params.{k}: {k} is an input.int"])
            p[k] = int(v) if inp.type == "int" else float(v)
        return p

    def so(self) -> Path:
        if self._so is None:
            self._so = N.compile(self.checked.cpp)
        return self._so

    # -- costs ------------------------------------------------------------------------

    def cost_overrides(self, defaults: Costs) -> tuple[dict[str, str], dict]:
        """strategy() overrides for a run, and the costs as recorded with it."""
        c = self.checked.costs
        if c.set:
            pct = c.commission_value if c.commission_type in (None, "percent") else None
            return {}, {"source": "script", "commission_type": c.commission_type or "percent",
                        "commission_value": c.commission_value, "slippage_ticks": c.slippage_ticks,
                        "commission_pct": pct, "slippage_bps": None, "zero": c.zero}
        charged = round(defaults.commission_pct + defaults.slippage_bps / 100.0, 6)
        return ({"commission_type": "percent", "commission_value": repr(charged), "slippage": "0"},
                {"source": "default", "commission_pct": defaults.commission_pct,
                 "slippage_bps": defaults.slippage_bps, "charged_pct_per_side": charged,
                 "zero": charged == 0})

    def config(self, defaults: Costs, adjust: str) -> dict:
        """Everything besides params and window that changes a result: the run's
        dedupe key, as backtest.config() is for the JSON engine."""
        _, costs = self.cost_overrides(defaults)
        return {"costs": costs, "adjust": adjust, "runtime": f"{RUNTIME} {N.engine_version()}",
                "orders_on_close": bool(self.checked.params.get("process_orders_on_close"))}


# -- turning engine output into Fills ------------------------------------------------------

def fills_from(equity: np.ndarray, trades: np.ndarray, ts: np.ndarray, i0: int, i1: int) -> tuple[Fills, float]:
    """Engine output over bars 0..i1 -> Fills for the window i0..i1, and the
    equity the window started with (its 'capital').

    Trades are the ones ENTERED in the window. A trade still open when the
    window ends is in the window's equity but not in its trade list; one
    carried in from before the window is the reverse. Every equity-based
    number (return, drawdown, Sharpe) is exact for the window either way.
    """
    eq = equity[i0:i1 + 1]
    start = float(equity[i0 - 1]) if i0 > 0 else float(equity[0])
    if trades.size:
        sel = trades[(trades[:, N.ENTRY_BAR] >= i0) & (trades[:, N.ENTRY_BAR] <= i1)]
    else:
        sel = trades
    rec = np.zeros((len(sel), 11), dtype="float64")
    in_pos = np.zeros(len(eq), dtype=bool)
    if len(sel):
        ei = sel[:, N.ENTRY_BAR].astype(int)
        xi = np.minimum(sel[:, N.EXIT_BAR].astype(int), i1)
        rec[:, 0] = np.where(sel[:, N.IS_LONG] > 0, 1.0, -1.0)
        rec[:, 1], rec[:, 2] = ei, xi
        rec[:, 3], rec[:, 4] = sel[:, N.ENTRY_PX], sel[:, N.EXIT_PX]
        rec[:, 5] = sel[:, N.QTY]
        rec[:, EQ0] = [equity[e - 1] if e > 0 else equity[0] for e in ei]
        rec[:, 7], rec[:, 8] = sel[:, N.PNL], sel[:, N.COMMISSION]
        rec[:, 10] = np.where(sel[:, N.OPEN_AT_END] > 0, 4, 0)     # REASONS: "end" / "signal"
        for e, x in zip(ei, xi):
            in_pos[max(e, i0) - i0:max(x, e) - i0] = True
    final = float(eq[-1]) if len(eq) and not np.isnan(eq[-1]) else start
    valid = eq[~np.isnan(eq)]
    ruin = bool(len(valid) and valid.min() <= 0)
    return Fills(rec=rec, ts=ts, equity=eq, in_pos=in_pos, i0=i0, i1=i1,
                 final=final, ruin=ruin), start


def ruined_metrics(f: Fills, ts: np.ndarray, close: np.ndarray, costs: dict) -> dict:
    """A window that starts with no account left. With one run per combination
    an account ruined in train is still ruined in test -- as it would be --
    where a separate test run would have started fresh and hidden it."""
    n = len(f.equity)
    return {"net_pct": -100.0, "cagr_pct": None, "max_dd_pct": 100.0, "sharpe": None,
            "sortino": None, "trades": 0, "win_pct": None, "profit_factor": None,
            "avg_trade_pct": None, "exposure_pct": 0.0,
            "bh_pct": M._r(float(close[f.i1] / close[f.i0] - 1) * 100), "fees": 0.0,
            "slippage": 0.0, "cost_pct_of_capital": None, "final_equity": 0.0, "ruin": True,
            "bars": n, "years": M._r((int(ts[f.i1]) - int(ts[f.i0])) / M.YEAR, 3), "costs": costs}


# -- tasks run inside sandbox children ---------------------------------------------------------
#
# Module-level so they pickle by reference. `shared` holds what every task of a
# batch needs (the series' ts and close, the overrides, the costs record);
# `task` holds what differs.

def task_backtest(engine, feed, shared, task):
    """One run for a backtest job's window: full Fills, for metrics, curve and trades."""
    i0, i1, inputs = task["i0"], task["i1"], task["inputs"]
    equity, _final, trades = engine.run(feed[:i1 + 1], inputs=inputs,
                                        overrides=shared["overrides"],
                                        trade_start_ms=int(shared["ts"][i0]) * 1000,
                                        syminfo=shared.get("syminfo"))
    return equity, trades


def task_sweep(engine, feed, shared, task):
    """One combination over train + test: the train and test metric summaries
    cut from it, and its daily returns over the span for the verdict."""
    from ..verdict.returns import daily
    ts, close = shared["ts"], shared["close"]
    a0, a1 = shared["span"]
    equity, _final, trades = engine.run(feed[:a1 + 1], inputs=task["inputs"],
                                        overrides=shared["overrides"],
                                        trade_start_ms=int(ts[a0]) * 1000,
                                        syminfo=shared.get("syminfo"))
    out = {}
    for name, (i0, i1) in shared["windows"].items():
        f, cap = fills_from(equity, trades, ts, i0, i1)
        out[name] = (M.compute(f, ts, close, capital=cap, costs_dict=shared["costs"]) if cap > 0
                     else ruined_metrics(f, ts, close, shared["costs"]))
    f, _cap = fills_from(equity, trades, ts, a0, a1)
    _days, col = daily(f.equity, ts[a0:a1 + 1])
    out["daily"] = col
    out["trades_span"] = int(len(f.rec))
    return out


def runtime_inputs(program: Program, params: dict) -> dict[str, str]:
    return PR.as_runtime_inputs(params, program.inputs)


def params_key(params: dict) -> str:
    return json.dumps(params, sort_keys=True)


# -- what a job calls --------------------------------------------------------------------

# Every series we hold is quoted to the cent. The session and timezone drive
# Pine's time functions (dayofweek, time(...) session filters).
SYMINFO = {
    "crypto": {"type": "crypto", "timezone": "UTC", "session": "24x7", "mintick": 0.01},
    "equity": {"type": "stock", "timezone": "America/New_York", "session": "0930-1600",
               "mintick": 0.01},
}


def _feed(series) -> np.ndarray:
    f = series.frame
    cached = f.cache.get("pine_feed")
    if cached is None:
        cached = N.bars(f.ts, f.open, f.high, f.low, f.close, f.volume)
        f.cache["pine_feed"] = cached
    return cached


def feed_of(feed: dict, series) -> dict:
    """Another market's series as a PineForge symbol feed: its bars, and when
    each one closed -- its period after its open, or the next bar's open if
    that comes first (a session that ended early)."""
    from .symbols import TF_SECONDS
    f = series.frame
    bars = _feed(series)
    opens = bars["timestamp"]
    close = opens + TF_SECONDS[feed["timeframe"]] * 1000
    if len(opens) > 1:
        close[:-1] = np.minimum(close[:-1], opens[1:])
    si = SYMINFO.get(series.asset_class, SYMINFO["crypto"])
    return {"key": feed["key"], "timeframe": feed["pine_tf"], "bars": bars, "close_ms": close,
            "type": si["type"], "mintick": si["mintick"], "n": len(f.ts)}


def _shared(program: Program, series, cost_defaults: Costs, feeds: list[dict] | None = None) -> dict:
    overrides, costs = program.cost_overrides(cost_defaults)
    return {"ts": series.frame.ts, "close": series.frame.close, "overrides": overrides,
            "costs": costs, "syminfo": SYMINFO.get(series.asset_class, SYMINFO["crypto"]),
            "feeds": feeds or []}


def run_windows(program: Program, series, cost_defaults: Costs, params: dict,
                windows: list[tuple[int, int]], curves: list[bool], feeds: list[dict] | None = None):
    """One parameter set over several windows (a backtest job's train, test,
    full, holdout and continuous runs), each a fresh run. Returns
    backtest.Result objects, so the job records them exactly as before."""
    from ..engine import backtest as B
    f = series.frame
    shared = _shared(program, series, cost_defaults, feeds)
    idx = [B.index_range(f.ts, w) for w in windows]
    inputs = runtime_inputs(program, params)
    with Runner(program.so(), _feed(series), 1, shared) as run:
        outs = run.map(task_backtest, [{"i0": a, "i1": b, "inputs": inputs} for a, b in idx])
    results = []
    for (i0, i1), (equity, trades), drawn in zip(idx, outs, curves):
        fl, cap = fills_from(equity, trades, f.ts, i0, i1)
        results.append(B.Result(
            metrics=M.compute(fl, f.ts, f.close, capital=cap, costs_dict=shared["costs"]),
            trades=[t.as_dict() for t in fl.trades] if drawn else [],
            curve=M.curve(fl, f.ts, f.close, capital=cap) if drawn else {},
            fills=fl, window=(int(f.ts[i0]), int(f.ts[i1])), params=dict(params)))
    return results


def sweep(program: Program, series, cost_defaults: Costs, split, *,
          progress=None, processes: int | None = None, feeds: list[dict] | None = None) -> dict:
    """Every grid combination, once each over train + test, across the sandbox
    children. Returns, in grid order: params, the train and test metrics cut
    from each run, and the verdict's returns matrix built from the same runs."""
    from ..engine import backtest as B
    from ..verdict.returns import Matrix, daily
    f = series.frame
    combos = program.grid.combos
    span = split.full()
    a0, a1 = B.index_range(f.ts, (span.start, span.end))
    shared = _shared(program, series, cost_defaults, feeds)
    shared["span"] = (a0, a1)
    shared["windows"] = {w.name: B.index_range(f.ts, (w.start, w.end))
                         for w in (split.train, split.test)}
    tasks = [{"inputs": runtime_inputs(program, p)} for p in combos]
    out: list = []
    # Batches, so the job can report progress while the children work.
    batch = max(1, min(50, len(tasks) // 10 or 1))
    with Runner(program.so(), _feed(series), processes or PROCESSES, shared) as run:
        for k in range(0, len(tasks), batch):
            out.extend(run.map(task_sweep, tasks[k:k + batch]))
            if progress:
                progress(len(out), len(tasks))
    days, bench = daily(f.close[a0:a1 + 1], f.ts[a0:a1 + 1])
    mx = Matrix(R=np.column_stack([o["daily"] for o in out]), days=days,
                combos=[dict(p) for p in combos],
                trades=np.asarray([o["trades_span"] for o in out]), bench=bench)
    rows = [{"params": dict(p), "train": o["train"], "test": o["test"]}
            for p, o in zip(combos, out)]
    windows = {name: (int(f.ts[i0]), int(f.ts[i1])) for name, (i0, i1) in shared["windows"].items()}
    return {"rows": rows, "matrix": mx, "windows": windows, "costs": shared["costs"]}
