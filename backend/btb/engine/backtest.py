"""The engine's public entry point: one spec, one parameter set, one window.

    from btb.engine import backtest
    series = backtest.Series.from_frame(df, "4h", symbol="BTC/USD")
    result = backtest.run(spec, params, series, window=(start, end))
    result.metrics["net_pct"], result.trades, result.curve

Indicators are computed over the WHOLE series up to the window's end and
trading is confined to the window. That way the first bar of a test window has
properly warmed-up indicators, exactly as it would have had in real time --
while no bar after the window is ever read.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import metrics as M
from . import spec as S
from .fills import Costs, Fills, simulate


@dataclass
class Series:
    """One market's bars, prepared once and reused across every run on it.

    The evaluator memo lives on the Frame, so a sweep of 81 combinations over
    one Series computes each distinct indicator once.
    """

    frame: S.Frame
    symbol: str = ""
    source: str = ""
    asset_class: str = "crypto"
    adjust: str = "none"
    as_of: int | None = None

    @staticmethod
    def from_frame(df: pd.DataFrame, timeframe: str, **meta) -> "Series":
        if df.empty:
            raise ValueError("no bars")
        if not df["ts"].is_monotonic_increasing:
            raise ValueError("bars must be sorted by ts")
        return Series(S.Frame.of(df.reset_index(drop=True), timeframe), **meta)

    @property
    def ts(self) -> np.ndarray:
        return self.frame.ts

    @property
    def timeframe(self) -> str:
        return self.frame.timeframe


@dataclass
class Result:
    metrics: dict
    trades: list[dict]
    curve: dict
    fills: Fills
    window: tuple[int, int]
    params: dict


def index_range(ts: np.ndarray, window: tuple[int, int] | None) -> tuple[int, int]:
    if window is None:
        return 0, len(ts) - 1
    start, end = window
    i0 = int(np.searchsorted(ts, start, side="left"))
    i1 = int(np.searchsorted(ts, end, side="right")) - 1
    if i0 > i1 or i1 < 0 or i0 >= len(ts):
        raise ValueError(f"window {window} contains no bars")
    return i0, i1


def run(spec: dict, params: dict | None, series: Series, *,
        window: tuple[int, int] | None = None, costs: Costs | None = None,
        capital: float = 10_000.0, size_pct: float = 100.0, leverage: float = 1.0,
        fill: str = "close", stop_fill: str = "gap", close_open: bool = True,
        liquidate: bool = True, with_curve: bool = True) -> Result:
    S.load(spec)
    p = S.resolve_params(spec, params)
    costs = costs or Costs()
    f = series.frame
    i0, i1 = index_range(f.ts, window)
    sig = S.signals(spec, p, f)
    fills = simulate(f.ts, f.open, f.high, f.low, f.close, sig, i0=i0, i1=i1,
                     capital=capital, costs=costs, size_pct=size_pct,
                     leverage=leverage, fill=fill, stop_fill=stop_fill,
                     close_open=close_open, liquidate=liquidate)
    m = M.compute(fills, f.ts, f.close, capital=capital, costs_dict=costs.as_dict())
    return Result(
        metrics=m,
        trades=[t.as_dict() for t in fills.trades],
        curve=M.curve(fills, f.ts, f.close, capital=capital) if with_curve else {},
        fills=fills,
        window=(int(f.ts[i0]), int(f.ts[i1])),
        params=p,
    )


def config(costs: Costs, *, fill: str = "close", stop_fill: str = "gap",
           size_pct: float = 100.0, leverage: float = 1.0, adjust: str = "none",
           capital: float = 10_000.0) -> dict:
    """Everything besides params and window that changes a result.

    Stored on every run and part of its dedupe key: two runs that differ only
    in commission are different results, not the same one twice.
    """
    return {"costs": costs.as_dict(), "fill": fill, "stop_fill": stop_fill,
            "size_pct": size_pct, "leverage": leverage, "adjust": adjust,
            "capital": capital}
