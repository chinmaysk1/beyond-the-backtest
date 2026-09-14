"""Tests for the data layer.

Deliberately regression tests, not coverage. Every one pins a bug that actually
occurred while building this, because those are the ones that will happen again:

    test_short_page_is_not_end_of_data           ETH truncated to its first 42 bars
    test_probe_reaches_back_past_the_cap         twelve 4h series stored EMPTY
    test_probe_does_not_end_on_a_future_window   that bug's actual cause
    test_empty_series_is_a_failure               ...and those empties said PASS
    test_feed_split_adjustment_is_undone         QQQ halved for 22 years
    test_event_on_ex_date_boundary               ex-date bar adjusted by mistake
    test_session_phase_is_not_misalignment       SPY 1h: 3,473 bars called corrupt
    test_weekend_is_not_a_gap                    SPY: 3,817 "missing" bars
    test_split_jump_is_explained                 AAPL's real splits called bad data
    test_crypto_jumps_are_not_corporate_actions  noise on every crypto symbol

Nothing here touches the network or the database. Sources are exercised through
fakes shaped like the real venues.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from btb.data import bars as B
from btb.data import integrity
from btb.data.adjust import adjust, unadjust_splits
from btb.data.sources.crypto import CryptoSource


# -- schema and timeframe -----------------------------------------------------

def test_tf_seconds_known_and_unknown():
    assert B.tf_seconds("4h") == 14400
    assert B.tf_seconds("1d") == 86400
    for bad in ("4hours", "h4", "0m", "", "1y"):
        with pytest.raises(ValueError):
            B.tf_seconds(bad)


def test_normalise_converts_ms_once_and_sorts():
    df = B.normalise([(2000, 1, 1, 1, 1, 0), (1000, 2, 2, 2, 2, 0)], ms=True)
    assert list(df["ts"]) == [1, 2]
    assert df["ts"].dtype == np.dtype("int64")


def test_merge_prefers_the_newer_read_of_the_same_bar():
    """The last stored bar is usually one that was still forming.

    If a re-fetch could not overwrite it, every series would carry a permanently
    half-formed final bar -- and it looks exactly like a complete one, so no
    check downstream could catch it.
    """
    old = B.normalise([(0, 1, 1, 1, 1, 5), (3600, 2, 2, 2, 2, 1)])     # partial
    new = B.normalise([(3600, 2, 9, 1, 8, 50)])                        # settled
    merged = B.merge(old, new)
    assert len(merged) == 2
    assert float(merged.loc[merged["ts"] == 3600, "close"].iloc[0]) == 8.0


# -- pagination and the listing-date probe ------------------------------------

class FakeVenue:
    """A venue serving the window [since, since + limit*step], like Bitstamp.

    Reproduces the two behaviours that broke real fetches: a window entirely
    before the listing date comes back EMPTY, and one straddling it comes back
    only partly filled.
    """

    def __init__(self, inception_ms, step_ms, now_ms, limit=1000):
        self.inception = inception_ms
        self.step = step_ms
        self.now = now_ms
        self.limit = limit
        self.timeframes = {"4h": "4h", "1d": "1d"}
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls += 1
        lo = max(since or 0, self.inception)
        hi = min((since or 0) + self.limit * self.step, self.now)
        return [[t, 1.0, 1.0, 1.0, 1.0, 1.0]
                for t in range(lo, hi, self.step)][:self.limit]


def _source(inception_ms, step_ms, now_ms):
    src = CryptoSource.__new__(CryptoSource)      # no ccxt, no network
    src.venue, src.name = "fake", "ccxt-fake"
    src.inception_ms, src.clamped = None, False
    src.client = FakeVenue(inception_ms, step_ms, now_ms)
    return src


def test_short_page_is_not_end_of_data():
    """A partly-filled page means the window straddled the listing date.

    Treating it as a terminator truncated ETH to 42 bars out of 19,892 -- 1% of
    the real history, in a series that looked completely plausible.
    """
    step = 14400 * 1000
    now = 1_760_000_000_000
    inception = now - 5000 * step
    src = _source(inception, step, now)
    df = src.fetch("ETH/USD", "4h", since=(inception - 3000 * step) // 1000,
                   until=now // 1000)
    assert len(df) > 4000, f"truncated to {len(df)} bars"


def test_probe_reaches_back_past_the_cap():
    """Finding a 2011 listing date on a 4h grid.

    One page of 4h bars is 166 days, so a linear probe needs ~90 steps to reach
    2011 from the epoch. Capped at 40, it gave up and returned nothing -- and
    twelve 4h crypto series were stored empty.
    """
    step = 14400 * 1000
    now = 1_760_000_000_000
    inception = 1_313_000_000_000          # 2011, as BTC on Bitstamp really is
    src = _source(inception, step, now)
    df = src.fetch("BTC/USD", "4h", since=0, until=now // 1000)
    assert not df.empty
    assert int(df["ts"].iloc[0]) * 1000 <= inception + 1000 * step


def test_probe_does_not_end_on_a_future_window():
    """The actual cause: the last doubling step landed on `now`.

    A window starting at "now" lies entirely in the future and returns empty, so
    the search concluded "no data" one step past a market with fifteen years of
    it. The probe must end on a window covering real past time.
    """
    step = 14400 * 1000
    now = 1_760_000_000_000
    src = _source(1_313_000_000_000, step, now)
    page, cursor = src._find_start("BTC/USD", "4h", 0, now)
    assert page, "probe gave up on a symbol with fifteen years of history"
    assert cursor < now


def test_probe_gives_up_on_a_symbol_with_no_history():
    """The search must still terminate when there genuinely is nothing."""
    step = 86400 * 1000
    now = 1_760_000_000_000
    src = _source(now + 10 * step, step, now)     # listed in the future
    page, cursor = src._find_start("NOPE/USD", "1d", 0, now)
    assert page is None and cursor is None


# -- corporate actions --------------------------------------------------------

DAY = 86400
EX_DATE = 1_000_000_000                        # a session open, mid-day UTC
EX_DAY = (EX_DATE // DAY) * DAY
SPLIT = {"splits": [{"ts": EX_DATE, "ratio": 2.0, "label": "2:1"}], "dividends": []}


def _daily(closes, end_day=EX_DAY + DAY):
    start = end_day - (len(closes) - 1) * DAY
    return B.normalise([(start + i * DAY, c, c, c, c, 100.0)
                        for i, c in enumerate(closes)])


def test_feed_split_adjustment_is_undone():
    """Yahoo's OHLC arrives ALREADY split-adjusted.

    Applying the event stream on top halved 22 years of QQQ and invented a 108%
    one-day gain at the split date. Storage must un-apply it instead, so what
    lands in `bars` is what actually traded.
    """
    df = _daily([50.0, 50.0, 50.0])            # as delivered, split-adjusted
    raw = unadjust_splits(df, SPLIT, "1d")
    # Bars: ex-date minus one, the ex-date itself, ex-date plus one. Only the
    # first predates the split, so only it is restored to its pre-split price.
    assert raw["close"].tolist() == [100.0, 50.0, 50.0]
    assert raw["volume"].tolist() == [50.0, 100.0, 100.0]


def test_round_trip_raw_and_back():
    """unadjust then adjust must return the original, or storage loses data."""
    df = _daily([50.0, 51.0, 52.0, 53.0])
    back = adjust(unadjust_splits(df, SPLIT, "1d"), SPLIT, "split", "1d")
    assert np.allclose(back["close"].to_numpy(), df["close"].to_numpy())
    assert np.allclose(back["volume"].to_numpy(), df["volume"].to_numpy())


def test_event_on_ex_date_boundary():
    """The ex-date bar is already post-split and must NOT be adjusted.

    Corporate actions are stamped at the session open; daily bars are stored
    snapped to UTC midnight. Comparing them directly puts the ex-date bar before
    its own event and adjusts one bar too many. The day-granularity comparison
    in `_prior_mask` is what prevents it.
    """
    df = B.normalise([(EX_DAY - DAY, 10, 10, 10, 10, 1),
                      (EX_DAY,       10, 10, 10, 10, 1),
                      (EX_DAY + DAY, 10, 10, 10, 10, 1)])
    raw = unadjust_splits(df, SPLIT, "1d")
    assert raw["close"].tolist() == [20.0, 10.0, 10.0]   # only the prior bar


def test_dividend_uses_split_adjusted_basis():
    """Yahoo quotes dividends already split-adjusted, so order matters.

    Applying a split-adjusted dividend against a raw close subtracts a nonsense
    fraction. Splits first, then dividends against the split-adjusted close.
    """
    df = _daily([100.0, 100.0, 100.0])
    ev = {"splits": [], "dividends": [{"ts": EX_DATE, "amount": 10.0}]}
    out = adjust(df, ev, "total", "1d")
    assert np.allclose(out["close"].to_numpy(), [90.0, 100.0, 100.0])


def test_absurd_dividend_cannot_invert_prices():
    df = _daily([5.0, 5.0, 5.0])
    ev = {"splits": [], "dividends": [{"ts": EX_DATE, "amount": 500.0}]}
    assert (adjust(df, ev, "total", "1d")["close"] > 0).all()


def test_adjust_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        adjust(_daily([1.0, 1.0]), SPLIT, "adjusted", "1d")


# -- integrity ----------------------------------------------------------------

def _regular(n=10, step=3600, start=0):
    return B.normalise([(start + i * step, 1, 1, 1, 1, 1) for i in range(n)])


def test_empty_series_is_a_failure():
    """An empty fetch is a failure, not a series with no findings.

    Short-circuiting this is how twelve empty 4h crypto series were written and
    reported as PASS -- the exact silent-success failure this layer exists to
    prevent.
    """
    rep = integrity.check(B.empty_frame(), "4h")
    assert not rep.ok
    assert any(f.check == "empty" for f in rep.errors)


def test_clean_series_has_no_errors():
    assert integrity.check(_regular(), "1h").ok


def test_missing_bar_is_an_error_on_a_247_market():
    df = _regular(10).drop(index=5).reset_index(drop=True)
    rep = integrity.check(df, "1h")
    assert not rep.ok and any(f.check == "gaps" for f in rep.errors)


def test_weekend_is_not_a_gap_on_an_exchange_traded_instrument():
    """Counting clock gaps reported 3,817 "missing bars" in a clean SPY series.

    A warning that fires on healthy data is worse than no warning, because it
    teaches everyone to ignore the channel.
    """
    days = pd.bdate_range("2020-01-01", periods=60, tz="UTC")
    df = B.normalise([(int(d.timestamp()), 1, 1, 1, 1, 1) for d in days])
    rep = integrity.check(df, "1d", expect_regular=False)
    assert rep.ok and not [f for f in rep.findings if f.check == "gaps"]


def test_long_closure_is_still_reported():
    days = list(pd.bdate_range("2020-01-01", periods=20, tz="UTC"))
    df = B.normalise([(int(d.timestamp()), 1, 1, 1, 1, 1)
                      for d in days[:5] + days[15:]])
    rep = integrity.check(df, "1d", expect_regular=False)
    assert any(f.check == "gaps" for f in rep.warnings)


def test_session_phase_is_not_misalignment():
    """SPY hourly opens at 13:30 UTC. All 3,473 bars were flagged as corrupt."""
    start = 13 * 3600 + 1800
    df = B.normalise([(start + i * 3600, 1, 1, 1, 1, 1) for i in range(7)])
    assert integrity.check(df, "1h", expect_regular=False).ok
    assert not integrity.check(df, "1h", expect_regular=True).ok


def test_scrambled_intraday_offsets_are_still_an_error():
    df = B.normalise([(i * 3600 + (i * 137) % 3600, 1, 1, 1, 1, 1) for i in range(50)])
    rep = integrity.check(df, "1h", expect_regular=False)
    assert any(f.check == "alignment" for f in rep.errors)


def test_split_jump_is_explained():
    """AAPL's five splits are real step-changes in a raw series.

    Flagging them as suspicious fires on healthy data for nearly every equity.
    A jump the event stream accounts for is reported as accounted for.
    """
    four_to_one = {"splits": [{"ts": EX_DATE, "ratio": 4.0, "label": "4:1"}],
                   "dividends": []}
    df = B.normalise([(EX_DAY - 2 * DAY, 100, 100, 100, 100, 1),
                      (EX_DAY - DAY,     100, 100, 100, 100, 1),
                      (EX_DAY,            25,  25,  25,  25, 1),
                      (EX_DAY + DAY,      25,  25,  25,  25, 1)])
    rep = integrity.check(df, "1d", expect_regular=False, events=four_to_one)
    jumps = [f for f in rep.findings if f.check == "jumps"]
    assert jumps and jumps[0].severity == integrity.INFO


def test_unexplained_jump_is_still_a_warning():
    """A real crash and an unrecorded split look alike; both deserve the flag."""
    df = B.normalise([(EX_DAY - 2 * DAY, 100, 100, 100, 100, 1),
                      (EX_DAY - DAY,     100, 100, 100, 100, 1),
                      (EX_DAY,            20,  20,  20,  20, 1)])
    rep = integrity.check(df, "1d", expect_regular=False,
                          events={"splits": [], "dividends": []})
    assert any(f.check == "jumps" and f.severity == integrity.WARN
               for f in rep.findings)


def test_crypto_jumps_are_not_corporate_actions():
    """Spot crypto has no splits, so every jump is "unexplained" by definition.

    XRP really did move 180% in a day in April 2017. Reporting that as a missing
    corporate action would fire on all twelve crypto symbols.
    """
    df = B.normalise([(0, 1, 1, 1, 1, 1), (DAY, 1, 1, 1, 1, 1),
                      (2 * DAY, 3, 3, 3, 3, 1)])
    rep = integrity.check(df, "1d", corporate_actions=False)
    jumps = [f for f in rep.findings if f.check == "jumps"]
    assert jumps and jumps[0].severity == integrity.INFO
    assert "corporate action" not in jumps[0].message


def test_impossible_bars_are_errors():
    df = B.normalise([(0, 1, 0.5, 2.0, 1, 1), (3600, 1, 2, 0.5, 9.0, -5)])
    checks = {f.check for f in integrity.check(df, "1h").errors}
    assert {"ohlc", "volume"} <= checks


def test_info_findings_do_not_fail_a_series():
    """INFO is a note, not a problem. It must never gate a series."""
    f = integrity.Finding("x", integrity.INFO, "note")
    assert integrity.Report([f], {"rows": 1}).ok
