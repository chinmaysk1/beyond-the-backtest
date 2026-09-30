"""Daily returns for every combination in a grid: the matrix the checks read.

Every check in the verdict works on the same object: R[T, N], one column per
grid combination, one row per calendar day of the train + test span. Daily,
whatever the bar size, so a 4h crypto grid and a 1d equity grid are judged on
the same clock -- and so a Sharpe means the same thing in both.

Each combination is run ONCE over train + test as one continuous window. The
sweep's separate train and test runs cannot be stitched instead: a position
open at the boundary is closed at the end of train and never reopened, which
is a trade the strategy would not have made.

The holdout is never part of the span.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..engine import backtest as B
from ..engine import spec as S
from ..engine import windows as W
from ..engine.fills import Costs

DAY = 86400


@dataclass
class Matrix:
    R: np.ndarray              # [T, N] daily simple returns
    days: np.ndarray           # [T] day index (unix day) of each row
    combos: list[dict]         # the N parameter sets, column order
    trades: np.ndarray         # [N] completed trades over the span
    bench: np.ndarray          # [T] buy-and-hold daily returns, same days


def daily(values: np.ndarray, ts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Last value of each calendar day -> (days, simple returns between them).

    The first day has no previous close inside the window, so it is dropped:
    T is one less than the number of days touched. A value of zero (a ruined
    account) returns zero afterwards rather than a division by zero.
    """
    values = np.asarray(values, dtype=float)
    ts = np.asarray(ts, dtype="int64")
    ok = ~np.isnan(values)
    values, ts = values[ok], ts[ok]
    day = ts // DAY
    last = np.r_[day[1:] != day[:-1], True]
    days, close = day[last], values[last]
    prev = close[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(prev > 0, close[1:] / prev - 1.0, 0.0)
    return days[1:], r


def build(spec: dict, series: B.Series, costs: Costs, *, split: W.Split | None = None,
          fill: str = "close", combos: list[dict] | None = None) -> Matrix:
    """Run every combination over train + test and collect its daily returns."""
    split = split or W.split(series.ts)
    span = split.full()
    if combos is None:
        combos, _ = S.grid(spec)
    if not combos:
        raise ValueError("the grid is empty")
    f = series.frame
    i0, i1 = B.index_range(f.ts, (span.start, span.end))
    ts = f.ts[i0:i1 + 1]
    days, bench = daily(f.close[i0:i1 + 1], ts)

    cols, trades = [], []
    for p in combos:
        r = B.run(spec, p, series, window=(span.start, span.end), costs=costs,
                  fill=fill, with_curve=False)
        d, col = daily(r.fills.equity, ts)
        if len(d) != len(days):           # equity is filled every bar; guard anyway
            col = _align(d, col, days)
        cols.append(col)
        trades.append(r.metrics["trades"])
    return Matrix(R=np.column_stack(cols), days=days, combos=list(combos),
                  trades=np.asarray(trades), bench=bench)


def _align(d: np.ndarray, col: np.ndarray, days: np.ndarray) -> np.ndarray:
    out = np.zeros(len(days))
    idx = np.searchsorted(days, d)
    keep = (idx < len(days)) & (days[np.minimum(idx, len(days) - 1)] == d)
    out[idx[keep]] = col[keep]
    return out
