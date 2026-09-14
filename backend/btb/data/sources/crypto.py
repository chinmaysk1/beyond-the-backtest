"""Crypto OHLCV through ccxt.

**Venue choice, measured from this machine 2026-09 rather than assumed.** Four
findings, three of them the same shape: the call succeeds, and returns less than
you asked for, under a label that does not say so.

    binance, bybit    HTTP 451 / 403 -- geo-blocked, unusable here.
    kraken            works, but caps OHLC at ~720 candles and silently ignores
                      `since`. Asking for 2023 onward returns the last 720 bars
                      stamped 2024-09-23 onward. No error.
    okx, kucoin,      return an EMPTY list for a historical `since`, which is
    bitget, mexc      indistinguishable from "this symbol has no data".
    bitstamp          honours `since` and pages forward correctly, back to the
                      symbol's listing date.

So the default is **bitstamp** for research history. That is not necessarily the
venue the strategy would eventually be filled on, and the difference is a real
modelling issue rather than a detail: a strategy validated on one book and traded
on another is being scored on a market it does not trade in. Weeks 3-5 owe a
like-for-like comparison of any two candidate feeds over their overlapping
window. The venue is written into every manifest so that comparison stays
possible after the fact.

The two silent-failure modes above are handled explicitly rather than tolerated:

* A venue that clamps `since` makes no forward progress between pages. The
  `last_ts` guard breaks the loop instead of re-fetching the same page forever,
  and `venue_clamped_since` goes into the manifest.
* A venue that returns empty for a pre-listing `since` is probed forward in
  coarse steps until data appears, and the discovered inception is recorded --
  so "nothing before 2017" reads as a fact about ETH rather than as a failure.
"""
from __future__ import annotations

import time

import pandas as pd

from ..bars import empty_frame, merge, normalise, tf_seconds

DEFAULT_VENUE = "bitstamp"
EXECUTION_VENUE = "kraken"     # what live/ trades on; see the module docstring

PAGE_LIMIT = 1000
MAX_PAGES = 400                # ~400k bars; a stop, not a target
MAX_PROBES = 40                # listing-date search: 40 x PAGE_LIMIT bars forward
EMPTY_PAGES_STOP = 3           # consecutive empty windows that mean "end of history"


class CryptoSource:
    regular = True
    corporate_actions = False   # spot crypto has no splits or dividends

    def __init__(self, venue: str = DEFAULT_VENUE):
        import ccxt  # imported here so the equities path has no ccxt dependency

        if not hasattr(ccxt, venue):
            raise ValueError(f"unknown ccxt venue {venue!r}")
        self.venue = venue
        self.name = f"ccxt-{venue}"
        self.client = getattr(ccxt, venue)({"enableRateLimit": True, "timeout": 30_000})
        self.inception_ms = None
        self.clamped = False

    # -- helpers --------------------------------------------------------------

    def _page(self, symbol, timeframe, since_ms):
        return self.client.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=PAGE_LIMIT)

    def _find_start(self, symbol, timeframe, since_ms, stop_ms):
        """Find the earliest window the venue will actually serve data for.

        A venue returns an empty list for a `since` before the symbol was
        listed, which is indistinguishable from "no such market". Stepping
        forward one page at a time to locate the listing date works for daily
        bars and fails badly for anything finer: on 4h one page is 166 days, so
        reaching 2011 from the epoch needs about ninety steps. Capped at forty,
        the search gave up and returned nothing -- and twelve 4h crypto series
        were stored empty, each reported as PASS.

        So: double the step until data appears, then bisect back for the
        earliest window that still yields some. Doubling covers forty years in
        roughly eight requests; the bisection costs about ten more and is what
        stops the doubling from overshooting the real listing date and silently
        truncating the front of the series.

        Returns (first_page, cursor_ms), or (None, None) if the symbol has no
        history anywhere in the requested span.
        """
        step_ms = tf_seconds(timeframe) * 1000
        page_ms = PAGE_LIMIT * step_ms

        page = self._page(symbol, timeframe, since_ms)
        if page:
            return page, since_ms

        # Phase 1 -- exponential search for any window that returns data.
        #
        # The probe is capped at `stop_ms - page_ms`, never at `stop_ms`. A
        # window starting exactly at "now" lies entirely in the future and comes
        # back empty, so clamping the final doubling step to stop_ms made the
        # search conclude "no data" one step past a market with fifteen years of
        # it. Ending on a window that covers real past time is the difference
        # between finding BTC in 2011 and storing an empty table.
        cap = max(since_ms, stop_ms - page_ms)
        empty_at, span, found_at = since_ms, page_ms, None
        for _ in range(MAX_PROBES):
            cursor = min(empty_at + span, cap)
            page = self._page(symbol, timeframe, cursor)
            if page:
                found_at = cursor
                break
            if cursor >= cap:
                return None, None          # genuinely nothing in the whole span
            empty_at = cursor
            span *= 2
        if found_at is None:
            return None, None

        # Phase 2 -- bisect (empty_at, found_at] for the earliest window with
        # data, so the doubling above cannot cost us real history.
        lo, hi, best = empty_at, found_at, page
        while hi - lo > page_ms:
            mid = lo + (hi - lo) // 2
            probe = self._page(symbol, timeframe, mid)
            if probe:
                hi, best = mid, probe
            else:
                lo = mid
        return best, hi

    # -- Source protocol ------------------------------------------------------

    def fetch(self, symbol: str, timeframe: str, *, since=None, until=None) -> pd.DataFrame:
        if timeframe not in self.client.timeframes:
            raise ValueError(
                f"{self.venue} does not serve {timeframe}; it offers "
                f"{sorted(self.client.timeframes)}")

        step_ms = tf_seconds(timeframe) * 1000
        start_ms = (since if since is not None else 0) * 1000
        stop_ms = (until * 1000) if until is not None else int(time.time() * 1000)

        page, cursor = self._find_start(symbol, timeframe, start_ms, stop_ms)
        if page is None:
            self.inception_ms = None
            return empty_frame()

        self.inception_ms = int(page[0][0])
        # A venue ignoring `since` announces itself here: it was asked for deep
        # history and answered with bars far newer than the request.
        self.clamped = bool(start_ms and
                            self.inception_ms > start_ms + 30 * PAGE_LIMIT * step_ms)

        out = empty_frame()
        last_ts = None
        empties = 0

        for _ in range(MAX_PAGES):
            if page:
                empties = 0
                df = normalise(page, ms=True)
                df = df[df["ts"] <= stop_ms // 1000]
                out = merge(out, df)

                newest = int(page[-1][0])
                if last_ts is not None and newest <= last_ts:
                    break                   # no forward progress: venue clamped
                last_ts = newest
                if newest >= stop_ms:
                    break
                cursor = newest + step_ms
            else:
                # A short or empty page is NOT the end of the data. bitstamp
                # serves the window [since, since + limit*step], so a window that
                # straddles the listing date comes back partly filled, and a
                # window entirely before it comes back empty. Treating either as
                # a terminator truncated ETH to its first 42 bars. Step the
                # window forward instead, and stop only after several
                # consecutive empties -- a genuine end of history.
                empties += 1
                if empties >= EMPTY_PAGES_STOP:
                    break
                cursor += PAGE_LIMIT * step_ms

            if cursor > stop_ms:
                break
            page = self._page(symbol, timeframe, cursor)

        if since is not None:
            out = out[out["ts"] >= since]
        return out.reset_index(drop=True)

    def events(self, symbol: str) -> dict:
        return {"dividends": [], "splits": []}   # spot crypto has neither

    def describe(self, symbol: str) -> dict:
        import ccxt
        return {
            "venue": self.venue,
            "venue_timezone": "UTC",
            "instrument_type": "CRYPTO",
            "adjustment": "none",
            "ccxt_version": ccxt.__version__,
            "inception_ts": (self.inception_ms // 1000) if self.inception_ms else None,
            "venue_clamped_since": self.clamped,
        }
