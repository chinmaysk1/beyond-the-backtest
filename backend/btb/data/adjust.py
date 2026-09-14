"""Corporate-action adjustment, applied when data is READ, never when stored.

`bars` holds what actually printed. This module turns that into whichever view
a caller needs. Doing it here rather than at ingest is what makes the stored
rows permanent facts: when a symbol splits next year, nothing on disk changes
meaning, because nothing on disk was ever adjusted.

Three views:

    "none"    raw prints -- what traded, at the price it traded.
    "split"   split-adjusted. The default for backtesting.
    "total"   splits and dividends, i.e. total return. For benchmarks.

Split-adjusted is the backtest default because a split changes what a price
means while a dividend does not. A 2:1 split makes last year's 108 dollars and
today's 54 the same thing. Re-basing every past price for a dividend, by
contrast, invents prices at which nothing ever traded -- correct for measuring
total return, wrong for simulating trades.

Whichever view produced a number must be recorded with it. A strategy compared
across two adjustment modes is being compared across two different assets.

**Order matters.** Splits are applied before dividends, because Yahoo reports
dividend amounts already split-adjusted (AAPL's 2012 dividend is quoted as
0.0946, the $2.65 actually paid divided by the 28x of later splits). Applying a
split-adjusted dividend against a raw close would subtract a nonsense fraction.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .bars import tf_seconds

MODES = ("none", "split", "total")
DAY = 86400


def _prior_mask(bar_ts: np.ndarray, event_ts: int, step: int) -> np.ndarray:
    """Which bars fall strictly before a corporate action.

    The subtlety that has bitten twice: an ex-date is stamped at the SESSION
    OPEN (13:30 or 14:30 UTC), while daily bars are stored snapped to UTC
    midnight. Comparing those directly puts the ex-date bar at 00:00 before its
    own 13:30 event, and adjusts one bar too many -- the ex-date bar is already
    trading post-split.

    So daily-or-slower series compare at DAY granularity; intraday series
    compare raw timestamps, where the session-open stamp already separates the
    ex-date's bars correctly.
    """
    if step >= DAY:
        return (bar_ts // DAY) < (event_ts // DAY)
    return bar_ts < event_ts


def adjust(df: pd.DataFrame, events: dict, mode: str, timeframe: str) -> pd.DataFrame:
    """Convert RAW bars to the requested view.

    Back-adjustment convention: the most recent bar keeps its actual price and
    history is scaled to meet it. That is the right way round for a backtest --
    today's price is the one a user recognises, and forward-adjusting would
    change today's number every time a future event occurred.

    Per event at time t, every bar strictly before t is scaled:

        split of ratio R (7:1 -> R=7):     prices x 1/R, volume x R
        dividend of D on a close of C:     prices x (1 - D/C)

    Factors accumulate multiplicatively, so successive splits and a long
    dividend history compound correctly.
    """
    if mode not in MODES:
        raise ValueError(f"unknown adjustment mode {mode!r}; expected {MODES}")
    if df.empty or mode == "none":
        return df

    step = tf_seconds(timeframe)
    out = df.copy()
    ts = out["ts"].to_numpy(dtype="int64")
    pf = np.ones(len(out), dtype="float64")
    vf = np.ones(len(out), dtype="float64")

    # --- splits, newest first so the factors compound ------------------------
    for s in sorted(events.get("splits", []), key=lambda x: x["ts"], reverse=True):
        ratio = float(s.get("ratio") or 0)
        if ratio <= 0:
            continue
        prior = _prior_mask(ts, int(s["ts"]), step)
        if not prior.any():
            continue
        pf[prior] *= 1.0 / ratio
        vf[prior] *= ratio

    if mode == "total":
        # Dividends are computed against SPLIT-ADJUSTED closes, since that is
        # the basis the venue quotes the amounts on.
        split_close = out["close"].to_numpy(dtype="float64") * pf
        for d in sorted(events.get("dividends", []), key=lambda x: x["ts"], reverse=True):
            amt = float(d.get("amount") or 0)
            if amt <= 0:
                continue
            t = int(d["ts"])
            prior = _prior_mask(ts, t, step)
            on_after = np.flatnonzero(~prior)
            if not prior.any() or len(on_after) == 0:
                continue                    # event outside the window we hold
            c = float(split_close[on_after[0]])
            # A dividend at or above the close would drive the factor to zero or
            # negative. That is bad event data, not a real corporate action, and
            # it must never be allowed to invert a price series.
            if c <= 0 or amt >= c:
                continue
            pf[prior] *= (1.0 - amt / c)

    for col in ("open", "high", "low", "close"):
        out[col] = out[col].to_numpy(dtype="float64") * pf
    out["volume"] = out["volume"].to_numpy(dtype="float64") * vf
    return out


def unadjust_splits(df: pd.DataFrame, events: dict, timeframe: str) -> pd.DataFrame:
    """Recover raw prints from a feed that ships split-adjusted prices.

    Yahoo's OHLC arrives already split-adjusted while still carrying the event
    stream, so storing it verbatim would store an adjusted price under a column
    that promises a raw one -- and the error is invisible until the next split
    makes those numbers mean something new.

    This is the inverse of `adjust(..., "split")`, applied once at ingest.
    """
    if df.empty:
        return df

    step = tf_seconds(timeframe)
    out = df.copy()
    ts = out["ts"].to_numpy(dtype="int64")
    pf = np.ones(len(out), dtype="float64")
    vf = np.ones(len(out), dtype="float64")

    for s in events.get("splits", []):
        ratio = float(s.get("ratio") or 0)
        if ratio <= 0:
            continue
        prior = _prior_mask(ts, int(s["ts"]), step)
        if not prior.any():
            continue
        pf[prior] *= ratio
        vf[prior] *= 1.0 / ratio

    for col in ("open", "high", "low", "close"):
        out[col] = out[col].to_numpy(dtype="float64") * pf
    out["volume"] = out["volume"].to_numpy(dtype="float64") * vf
    return out
