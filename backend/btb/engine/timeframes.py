"""Higher-timeframe series without lookahead.

A strategy on 4h bars that filters on a 12h ADX needs, at every 4h bar, the
12h value *that was known at that moment*. The 12h bar containing the current
4h bar has not closed yet, so its value is off limits; the one before it is
the newest legitimate reading. That is TradingView's
`request.security(..., lookahead=barmerge.lookahead_off)`, and skipping the
shift is the single easiest way to fake a good result.

Two decisions, both inherited from bugs the earlier prototype actually had:

* **Bars are grouped by wall-clock bucket, not by array index.** Grouping every
  3 bars from wherever the array happens to start makes the 12h boundaries
  depend on the first bar loaded. Feed the same history with one extra bar of
  lead-in and every HTF bar regroups, changing the filter and therefore the
  trades. Buckets are aligned to the epoch (to Monday for weekly), which is
  also what TradingView does.
* **A leading partial bucket is dropped**, so every HTF bar is built from a
  complete set of base bars and the series does not depend on where the
  window starts.
"""
from __future__ import annotations

import numpy as np

from ..data.bars import tf_seconds

WEEK = 7 * 86400
# The epoch began on a Thursday. Weekly buckets are shifted to start on Monday
# 00:00 UTC, matching every charting platform's weekly bar.
_WEEK_OFFSET = 4 * 86400


def bucket_ids(ts: np.ndarray, seconds: int) -> np.ndarray:
    ts = np.asarray(ts, dtype="int64")
    if seconds % WEEK == 0:
        return (ts - _WEEK_OFFSET) // seconds
    return ts // seconds


def resolve_seconds(base_tf: str, *, tf: str | None = None, mult: int | None = None) -> int:
    """Target bar length in seconds, from an absolute `tf` or a relative `mult`."""
    base = tf_seconds(base_tf)
    if tf is not None:
        target = tf_seconds(tf)
    elif mult is not None:
        target = base * int(mult)
    else:
        raise ValueError("need tf or mult")
    if target <= base:
        raise ValueError(f"higher timeframe must be longer than {base_tf}; got {target}s")
    if target % base:
        raise ValueError(f"higher timeframe {target}s is not a whole multiple of {base_tf}")
    return target


class Resampled:
    """One higher-timeframe view of a base series, plus the map back onto it."""

    def __init__(self, frame: dict, seconds: int):
        ts = np.asarray(frame["ts"], dtype="int64")
        ids = bucket_ids(ts, seconds)
        n = len(ts)

        # Drop the leading bucket if the series starts part-way through it.
        start = 0
        if n and (ts[0] - (_WEEK_OFFSET if seconds % WEEK == 0 else 0)) % seconds != 0:
            while start < n and ids[start] == ids[0]:
                start += 1

        # Boundaries of each bucket among the retained bars.
        sub = ids[start:]
        if len(sub):
            cut = np.flatnonzero(np.diff(sub)) + 1
            first = np.concatenate([[0], cut]) + start
            last = np.concatenate([cut, [len(sub)]]) + start - 1
        else:
            first = last = np.array([], dtype="int64")

        o, h, l, c, v = (np.asarray(frame[k], dtype="float64")
                         for k in ("open", "high", "low", "close", "volume"))
        self.frame = {
            "ts": ts[first] if len(first) else np.array([], dtype="int64"),
            "open": o[first],
            "high": np.maximum.reduceat(h, first) if len(first) else np.array([]),
            "low": np.minimum.reduceat(l, first) if len(first) else np.array([]),
            "close": c[last],
            "volume": np.add.reduceat(v, first) if len(first) else np.array([]),
        }
        # Index of the base bar at which each HTF bar closes.
        self.close_at = last
        self.base_len = n

        # For each base bar, the newest HTF bar that closed strictly BEFORE it.
        # A base bar is never shown the HTF bar it belongs to.
        pos = np.searchsorted(self.close_at, np.arange(n), side="left") - 1
        self._pick = pos

    def to_base(self, values: np.ndarray) -> np.ndarray:
        values = np.asarray(values, dtype="float64")
        out = np.full(self.base_len, np.nan)
        ok = self._pick >= 0
        out[ok] = values[self._pick[ok]]
        return out
