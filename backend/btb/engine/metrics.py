"""Trades and an equity curve -> the numbers a result is judged on.

Two choices worth stating:

* **Drawdown is measured on the mark-to-market curve**, bar by bar, not on
  closed trades. A position that falls 40% and recovers before its exit has a
  40% drawdown; measuring only at exits reports zero.
* **Annualisation uses the bars actually present.** 24/7 crypto has 2,190 4h
  bars a year; an equity 1h series has ~1,764 (252 sessions of 7). Counting the
  bars in the window and dividing by its length in years gets both right
  without a per-market table.
"""
from __future__ import annotations

import math

import numpy as np

from .fills import Fills

YEAR = 365.25 * 86400


def compute(f: Fills, ts: np.ndarray, close: np.ndarray, *, capital: float,
            costs_dict: dict) -> dict:
    trades = f.trades
    eq = f.equity
    n = len(eq)
    t0, t1 = int(ts[f.i0]), int(ts[f.i1])
    years = max((t1 - t0) / YEAR, 1e-9)

    net = f.final / capital - 1
    cagr = ((f.final / capital) ** (1 / years) - 1) if f.final > 0 and years > 0.05 else None

    valid = eq[~np.isnan(eq)]
    peak = np.maximum.accumulate(valid) if len(valid) else valid
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = float(np.nanmax(np.where(peak > 0, (peak - valid) / peak, 0.0))) if len(valid) else 0.0

    # Returns only while there was an account to return on: after ruin the
    # curve sits at zero and a return off zero is undefined, not -100% again.
    if len(valid) > 1:
        prev = valid[:-1]
        live = prev > 0
        rets = np.diff(valid)[live] / prev[live]
    else:
        rets = np.array([])
    bars_per_year = n / years
    sharpe = sortino = None
    if len(rets) > 2 and np.std(rets) > 0:
        sharpe = float(np.mean(rets) / np.std(rets) * math.sqrt(bars_per_year))
        down = rets[rets < 0]
        if len(down) > 1 and np.sqrt(np.mean(down ** 2)) > 0:
            sortino = float(np.mean(rets) / np.sqrt(np.mean(np.minimum(rets, 0) ** 2))
                            * math.sqrt(bars_per_year))

    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [-t.pnl for t in trades if t.pnl <= 0]
    gp, gl = sum(wins), sum(losses)
    fees = sum(t.fees for t in trades)
    slip = sum(t.slip for t in trades)
    bh = float(close[f.i1] / close[f.i0] - 1)

    return {
        "net_pct": _r(net * 100),
        "cagr_pct": _r(cagr * 100) if cagr is not None else None,
        "max_dd_pct": _r(dd * 100),
        "sharpe": _r(sharpe, 3),
        "sortino": _r(sortino, 3),
        "trades": len(trades),
        "win_pct": _r(len(wins) / len(trades) * 100) if trades else None,
        "profit_factor": _r(gp / gl, 3) if gl > 0 else None,
        "avg_trade_pct": _r(float(np.mean([t.pnl / t.equity_at_entry for t in trades])) * 100)
                         if trades else None,
        "exposure_pct": _r(float(f.in_pos.mean()) * 100) if n else 0.0,
        "bh_pct": _r(bh * 100),
        "fees": _r(fees, 2),
        "slippage": _r(slip, 2),
        "cost_pct_of_capital": _r((fees + slip) / capital * 100),
        "final_equity": _r(f.final, 2),
        "ruin": f.ruin,
        "bars": n,
        "years": _r(years, 3),
        "costs": costs_dict,
    }


def curve(f: Fills, ts: np.ndarray, close: np.ndarray, *, capital: float,
          points: int = 500) -> dict:
    """Equity and buy-and-hold, downsampled to at most `points` for storage.

    Downsampling keeps each bucket's LOWEST equity rather than an arbitrary
    sample, so the stored curve never hides a drawdown the metrics report.
    """
    eq = f.equity
    n = len(eq)
    idx = np.arange(f.i0, f.i1 + 1)
    bh = close[idx] / close[f.i0] * capital
    if n <= points:
        pick = np.arange(n)
    else:
        edges = np.linspace(0, n, points + 1).astype(int)
        pick = np.array([a + int(np.nanargmin(eq[a:b])) for a, b in zip(edges[:-1], edges[1:])
                         if b > a])
        pick[-1] = n - 1
    return {
        "ts": [int(ts[f.i0 + k]) for k in pick],
        "equity": [_r(float(eq[k]), 2) for k in pick],
        "bh": [_r(float(bh[k]), 2) for k in pick],
    }


def _r(v, nd: int = 4):
    if v is None:
        return None
    v = float(v)
    if not math.isfinite(v):
        return None
    return round(v, nd)
