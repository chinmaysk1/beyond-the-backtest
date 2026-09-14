"""US equities and ETFs from Yahoo's chart endpoint, plus corporate actions.

Why this source: it is keyless, it serves full daily history back to listing, it
carries the split and dividend event stream, and it works from this machine.
Stooq (the usual keyless alternative) returns an interstitial instead of CSV
here, and Alpaca -- which the proposal names for paper trading -- needs an
account key that does not exist yet. When that key exists, Alpaca becomes a
second `Source` implementation and nothing above this file changes.

**What Yahoo actually returns, measured rather than assumed.** The payload has
three parts and they are adjusted differently, which is the trap:

    indicators.quote        OHLCV -- ALREADY SPLIT-ADJUSTED, not dividend-adjusted
    indicators.adjclose     close, split AND dividend adjusted
    events                  the split and dividend stream, still delivered in full

The first line is the one that costs you. The event stream arrives alongside
data that has already had the splits applied, so the obvious implementation --
take `quote`, apply `events` -- adjusts every split twice. Measured on QQQ: the
2000 2:1 split halved all 22 years before it a second time, leaving a 108%
one-day "gain" on 2000-03-21 where nothing happened. The `close/adjclose` ratio
is constant at 1.187 straight across that split date, which is the proof: the
only difference between the two series is dividends.

**This source therefore returns RAW PRINTS.** It un-applies the feed's split
adjustment on the way out, so what gets stored is what actually traded. Every
adjusted view is computed at read time from `btb.data.adjust`, against the
events table. That is what keeps stored rows permanent: a split next year
changes nothing on disk.

The un-adjustment is checked on every daily fetch against Yahoo's own
`adjclose` -- two independent routes to the same number, compared, and the worst
disagreement recorded in the series manifest as `adjustment_max_dev`. Measured
at 0.05% over 27 years of QQQ, which is what agreement looks like.

Intraday is available but shallow: roughly two years of hourly, 60 days of
30m/15m/5m, a week of 1m. Exceeding the limit is not a soft truncation -- it is
an HTTP 422 -- so `_raw` clamps the request to what will be granted and records
the cap in the manifest. `integrity.check` then flags the result as a thin
sample. Deep intraday history is a real gap in this source and, if the project
needs it, a reason to pay for a feed.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

from ...config import HTTP_TIMEOUT, HTTP_UA
from ..adjust import adjust, unadjust_splits
from ..bars import empty_frame, normalise, tf_seconds

BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"

# btb timeframe -> Yahoo interval. Yahoo has no 4h; asking for one must fail
# loudly rather than be silently served something else.
INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
             "1h": "1h", "1d": "1d", "1w": "1wk"}

# Yahoo's own depth limits per interval, in days. Requesting more succeeds and
# returns less; recorded here so the caller can be told what it actually got.
MAX_DAYS = {"1m": 7, "5m": 58, "15m": 58, "30m": 58, "1h": 730, "1d": None, "1w": None}

class EquitySource:
    name = "yahoo"
    regular = False          # nights, weekends and market holidays are not gaps
    corporate_actions = True

    def __init__(self):
        # No adjustment mode. This source returns raw prints, always; every
        # adjusted view is derived at read time from the events table by
        # btb.data.adjust. A mode argument here would only be a way to store
        # the wrong thing.
        self._meta: dict = {}
        self._events_cache: dict = {}
        self.max_dev = None        # total-return check vs Yahoo adjclose
        self.clamped_days = None   # intraday depth actually granted

    # -- transport -----------------------------------------------------------

    def _get(self, symbol: str, params: dict) -> dict:
        url = BASE + urllib.parse.quote(symbol) + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": HTTP_UA})
        try:
            raw = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"yahoo {e.code} for {symbol}: {e.reason}") from e
        payload = json.loads(raw)
        chart = payload.get("chart") or {}
        if chart.get("error"):
            raise RuntimeError(f"yahoo error for {symbol}: {chart.get('error')}")
        results = chart.get("result") or []
        if not results:
            raise RuntimeError(f"yahoo returned no result for {symbol}")
        return results[0]

    def _raw(self, symbol: str, timeframe: str, since, until) -> dict:
        if timeframe not in INTERVALS:
            raise ValueError(
                f"yahoo does not serve {timeframe}; available: {sorted(INTERVALS)}. "
                f"(Aggregate from a finer timeframe rather than mislabelling one.)")

        end = int(until) if until is not None else int(time.time())
        start = int(since) if since is not None else 0

        # Intraday has a hard depth limit, and exceeding it is NOT a soft
        # truncation -- asking for 1h since the epoch returns HTTP 422, not a
        # short series. Clamp to what the endpoint will grant and record how
        # much was actually asked for, so a thin intraday series is visibly
        # thin rather than mysteriously absent.
        cap = MAX_DAYS.get(timeframe)
        if cap is not None:
            floor = end - cap * 86400
            self.clamped_days = cap
            if start < floor:
                start = floor

        params = {
            "interval": INTERVALS[timeframe],
            "events": "div,split",
            "includeAdjustedClose": "true",
            "period1": start,
            "period2": end,
        }
        return self._get(symbol, params)

    # -- Source protocol ------------------------------------------------------

    def fetch(self, symbol: str, timeframe: str, *, since=None, until=None) -> pd.DataFrame:
        r = self._raw(symbol, timeframe, since, until)
        self._meta[symbol] = r.get("meta", {})

        stamps = r.get("timestamp") or []
        quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
        ev = _events_from(r)
        self._events_cache[symbol] = ev
        if not stamps:
            return empty_frame()

        adjclose = ((r.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []

        rows, adj = [], []
        for i, t in enumerate(stamps):
            vals = []
            for k in ("open", "high", "low", "close"):
                col = quote.get(k) or []
                vals.append(col[i] if i < len(col) else None)
            vol_col = quote.get("volume") or []
            v = vol_col[i] if i < len(vol_col) else 0
            if any(x is None for x in vals):
                continue          # Yahoo pads halted / no-trade slots with nulls
            rows.append((int(t), float(vals[0]), float(vals[1]), float(vals[2]),
                         float(vals[3]), float(v or 0)))
            a = adjclose[i] if i < len(adjclose) else None
            adj.append(float(a) if a is not None else float("nan"))

        df = normalise(rows)
        if df.empty:
            return df
        # normalise() may sort or drop; re-attach adjclose by timestamp so the
        # verification below can never compare mismatched rows.
        ref = pd.Series(adj, index=[r0[0] for r0 in rows])
        ref = ref[~ref.index.duplicated(keep="last")]

        # Daily bars are stamped at the session open in exchange-local time
        # (13:30 or 14:30 UTC depending on daylight saving). Snap to the UTC day
        # so the series lands on the 1d grid the integrity check enforces, and
        # so a DST change cannot make two bars land on one calendar day.
        raw_ts = df["ts"].to_numpy().copy()
        if timeframe in ("1d", "1w"):
            step = tf_seconds("1d")
            snapped = (df["ts"] // step) * step
            keep = ~snapped.duplicated(keep="last")
            df = df[keep].copy()
            df["ts"] = snapped[keep]
            raw_ts = raw_ts[keep.to_numpy()]
            df = df.reset_index(drop=True)

        # Un-apply the feed's split adjustment, so what we store is what traded.
        # The ex-date boundary is handled inside adjust.py: daily bars compare at
        # day granularity, so a midnight-snapped timestamp is not placed before
        # its own session-open event.
        raw = unadjust_splits(df, ev, timeframe)

        # The cheapest correctness test in the project, run on every daily
        # fetch: re-derive the fully adjusted close from our raw prints and
        # compare it against Yahoo's independently computed adjclose. Two routes
        # to one number. Any disagreement means one of them is wrong, and the
        # size of it says how badly.
        if timeframe in ("1d", "1w") and len(ref):
            check = adjust(raw, ev, "total", timeframe)
            # Keyed on the PRE-SNAP timestamps: `ref` was built from Yahoo's own
            # session-open stamps, so looking it up with midnight-snapped ones
            # matches nothing, yields an empty comparison, and reports perfect
            # agreement. A verification that cannot fail is not a verification.
            theirs = ref.reindex(pd.Index(raw_ts)).to_numpy(dtype="float64")
            self.max_dev = _max_rel_dev(check["close"].to_numpy(dtype="float64"), theirs)
        return raw

    def events(self, symbol: str) -> dict:
        cached = self._events_cache.get(symbol)
        if cached is not None:
            return cached
        return _events_from(self._raw(symbol, "1d", None, None))

    def describe(self, symbol: str) -> dict:
        m = self._meta.get(symbol, {})
        return {
            "venue": "yahoo",
            "venue_timezone": m.get("exchangeTimezoneName", "America/New_York"),
            "exchange": m.get("fullExchangeName") or m.get("exchangeName"),
            "currency": m.get("currency"),
            "instrument_type": m.get("instrumentType", "EQUITY"),
            "stored_adjustment": "none",         # raw prints, as traded
            "feed_adjustment": "split",          # what the payload arrives as
            "adjustment_max_dev": self.max_dev,  # our total view vs Yahoo adjclose
            "intraday_cap_days": self.clamped_days,
        }


# -- corporate actions --------------------------------------------------------

def _events_from(result: dict) -> dict:
    ev = result.get("events") or {}
    splits = [
        {"ts": int(s["date"]), "ratio": float(s["numerator"]) / float(s["denominator"]),
         "label": s.get("splitRatio")}
        for s in (ev.get("splits") or {}).values()
    ]
    divs = [
        {"ts": int(d["date"]), "amount": float(d["amount"])}
        for d in (ev.get("dividends") or {}).values()
    ]
    return {"splits": sorted(splits, key=lambda x: x["ts"]),
            "dividends": sorted(divs, key=lambda x: x["ts"])}


def _max_rel_dev(ours, theirs) -> float | None:
    """Worst relative disagreement between two computations of the same series.

    Returned rather than asserted, and written into the series manifest, so the
    agreement is auditable later instead of merely claimed now.
    """
    if ours is None or theirs is None or len(ours) != len(theirs):
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        dev = np.abs(ours - theirs) / np.where(theirs > 0, theirs, np.nan)
    dev = dev[np.isfinite(dev)]
    return float(np.max(dev)) if len(dev) else None
