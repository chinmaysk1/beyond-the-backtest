"""The canonical bar schema, and the timeframe arithmetic everything else uses.

One schema, enforced at every source boundary, so that a Kraken candle and a
Yahoo candle are indistinguishable downstream:

    ts      int64   UTC epoch **seconds**, the bar's OPEN time
    open    float64
    high    float64
    low     float64
    close   float64
    volume  float64

Three decisions worth stating, because each one is a silent-wrong-number risk:

* **Open time, not close time.** ccxt yields open time; Yahoo yields open time.
  Mixing conventions shifts a whole series by one bar, which looks like a
  strategy that can see the future.
* **Seconds, not milliseconds.** Exchange APIs are split on this (ccxt yields
  milliseconds, most CSV exports yield seconds), so one of the two has to be
  converted. Doing it at the source boundary and nowhere else means no function
  downstream ever has to ask which unit it was handed.
* **UTC always.** The exchange's local timezone is recorded in the manifest as
  metadata; it is never applied to `ts`. Session-local questions ("was this bar
  in the US cash session?") are answered by converting at the point of use.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

COLUMNS = ["ts", "open", "high", "low", "close", "volume"]
DTYPES = {
    "ts": "int64",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "float64",
}

# Timeframe string -> seconds. Deliberately a closed set: an unrecognised
# timeframe should fail loudly rather than be guessed at.
_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]


def tf_seconds(timeframe: str) -> int:
    """'4h' -> 14400. Raises on anything not of the form <int><m|h|d|w>."""
    tf = timeframe.strip().lower()
    if len(tf) < 2 or tf[-1] not in _UNITS or not tf[:-1].isdigit():
        raise ValueError(f"unrecognised timeframe {timeframe!r}; expected e.g. {TIMEFRAMES}")
    n = int(tf[:-1])
    if n <= 0:
        raise ValueError(f"timeframe must be positive: {timeframe!r}")
    return n * _UNITS[tf[-1]]


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=DTYPES[c]) for c in COLUMNS})


def normalise(rows, *, ms: bool = False) -> pd.DataFrame:
    """Turn any source's rows into the canonical frame.

    `rows` is an iterable of (ts, open, high, low, close, volume). Set ms=True
    if the source's timestamps are milliseconds (ccxt) -- that conversion
    happens here and only here.

    Sorts by ts and drops exact-duplicate timestamps, keeping the LAST one: when
    a paginated fetch overlaps its previous page, the later page is the fresher
    read of a bar that may still have been forming.
    """
    df = pd.DataFrame(list(rows), columns=COLUMNS)
    if df.empty:
        return empty_frame()
    if ms:
        df["ts"] = (df["ts"].astype("int64") // 1000)
    df = df.astype(DTYPES)
    df = df.sort_values("ts", kind="mergesort")
    df = df.drop_duplicates(subset="ts", keep="last")
    return df.reset_index(drop=True)


def merge(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Fold a fresh fetch into a cached frame. New bars win on collision.

    The last bar of a cached series is frequently a *partial* bar that was still
    forming when it was written. Letting the new read overwrite it is the whole
    reason this is not a plain append.
    """
    if old.empty:
        return new.reset_index(drop=True)
    if new.empty:
        return old.reset_index(drop=True)
    both = pd.concat([old, new], ignore_index=True)
    both = both.sort_values("ts", kind="mergesort")
    both = both.drop_duplicates(subset="ts", keep="last")
    return both.reset_index(drop=True).astype(DTYPES)


@dataclass(frozen=True)
class Span:
    """What a frame actually covers, as opposed to what was requested."""

    rows: int
    first_ts: int | None
    last_ts: int | None

    @staticmethod
    def of(df: pd.DataFrame) -> "Span":
        if df.empty:
            return Span(0, None, None)
        return Span(len(df), int(df["ts"].iloc[0]), int(df["ts"].iloc[-1]))

    def __str__(self) -> str:
        if self.rows == 0:
            return "empty"
        return f"{self.rows} bars {iso(self.first_ts)} .. {iso(self.last_ts)}"


def iso(ts: int | None) -> str:
    if ts is None:
        return "-"
    return pd.Timestamp(ts, unit="s", tz="UTC").strftime("%Y-%m-%d %H:%M")
