"""The one path by which market data enters the project.

Everything -- the CLI, the backfill, the background refresher, the web app's
freshness check -- goes through `ingest_series`. One code path means exactly one
place where the audit could be skipped, which is to say nowhere.

The order is the design:

    fetch -> merge with what we hold -> audit -> write -> record the audit

Auditing after the merge catches what only the merge can produce: a page
boundary that drops a bar, or a re-fetch landing on a different grid than the
stored series. Recording the audit in the same transaction as the bars means a
bad series sits in the database with a FAIL stamped on it, rather than looking
identical to a good one.

Two things this module is careful about, both of which are invisible when wrong:

**The forming bar.** The newest bar of a live series has not finished. Its
"close" is the current price. It is written with `partial = true`, excluded from
every research read, and overwritten by the next fetch once the period elapses.
`last_ts` in the manifest tracks the newest COMPLETE bar, so staleness is
measured against real data rather than against a placeholder.

**Raw storage.** Equity feeds ship split-adjusted prices. Those are un-adjusted
at the source boundary so that what lands in `bars` is what actually traded,
and every adjusted view is derived at read time. See `btb/data/adjust.py`.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .. import db
from ..config import UNIVERSE_FILE
from . import bars as B
from . import integrity
from .sources import make_source

# History depth per timeframe, in years. `None` means everything the venue has.
#
# Tiered for a research reason before a cost one: fine-grained bars from a
# market's early years describe microstructure that no longer exists. 2.7% of
# Bitstamp's 2011-2012 4h bars have zero range and zero volume; the 5m series
# through that period is mostly emptiness. A strategy tuned on it is tuned on
# nothing. Coarse timeframes keep full history because a daily bar from 2012 is
# still a real daily bar.
HISTORY_YEARS = {"1m": 1, "5m": 3, "15m": 5, "30m": 8, "1h": None,
                 "2h": None, "4h": None, "12h": None, "1d": None, "1w": None}

YEAR = 365.25 * 86400


@dataclass
class IngestResult:
    source: str
    symbol: str
    timeframe: str
    rows: int
    added: int
    report: integrity.Report
    first_ts: int | None
    last_ts: int | None
    partial: bool
    elapsed: float

    @property
    def ok(self) -> bool:
        return self.report.ok

    def line(self) -> str:
        flag = "PASS" if self.ok else "FAIL"
        warn = len(self.report.warnings)
        span = f"{B.iso(self.first_ts)}..{B.iso(self.last_ts)}" if self.rows else "empty"
        return (f"{flag:4} {self.symbol:10} {self.timeframe:4} {self.source:14} "
                f"{self.rows:>8} bars  {span}  +{self.added}"
                + (f" {warn}w" if warn else "") + f"  {self.elapsed:.0f}s")


def window_start(timeframe: str, pinned=None) -> int | None:
    """Where history should begin for this timeframe.

    A pinned `since` always wins -- reproducibility beats the tier. Otherwise
    the tier is resolved against *now*, which means a rebuild next year fetches
    a rolling window rather than the same one. That is correct for a live store;
    results are pinned by `as_of` at read time instead.
    """
    if pinned is not None:
        return int(pinned)
    years = HISTORY_YEARS.get(timeframe)
    if years is None:
        return None
    return int(time.time() - years * YEAR)


def ingest_series(conn, kind: str, symbol: str, timeframe: str, *,
                  since=None, until=None, full_refresh: bool = False,
                  asset_class: str | None = None, **source_kw) -> IngestResult:
    """Fetch, audit and store one (symbol, timeframe) series.

    Incremental by default: it asks the source only for bars at or after the
    newest one held, minus a one-bar overlap. That overlap is not an
    optimisation -- it is how the forming bar gets replaced once it settles.
    Resuming from `last + 1` instead would freeze it half-formed forever, and a
    half-formed bar is indistinguishable from a complete one.
    """
    t0 = time.perf_counter()
    src = make_source(kind, **source_kw)
    step = B.tf_seconds(timeframe)

    floor = window_start(timeframe, since)
    held_first = db.first_bar_ts(conn, src.name, symbol, timeframe)
    held_last = db.last_bar_ts(conn, src.name, symbol, timeframe)

    start = floor
    if not full_refresh and held_last is not None:
        resume = held_last - step                    # re-read the forming bar
        start = max(resume, floor) if floor is not None else resume

    fresh = src.fetch(symbol, timeframe, since=start, until=until)

    # --- backfill the FRONT of the series ----------------------------------
    #
    # Incremental resume extends forward only, which is wrong whenever the
    # stored series starts later than it should. Measured: a one-off
    # `fetch --timeframe 1h --since 2026-08-01` on BTC created a 1,057-bar
    # series, and every later `build` resumed from its newest bar -- so the
    # fifteen years of history before it were never fetched, and the result
    # looked exactly like a market that simply has no older data.
    #
    # So if what we hold begins after the window we were asked for, fetch the
    # missing head as well. A series that is already complete skips this.
    if not full_refresh and held_first is not None:
        want_from = floor if floor is not None else 0
        if held_first > want_from + step:
            head = src.fetch(symbol, timeframe, since=want_from,
                             until=held_first + step)
            if not head.empty:
                fresh = B.merge(head, fresh)

    # A bar is still forming if its period has not elapsed. Computed against the
    # wall clock rather than trusted from the venue, because venues differ on
    # whether they return the in-progress candle at all.
    now = int(time.time())
    partial_last = bool(len(fresh) and int(fresh["ts"].iloc[-1]) + step > now)

    before = db.count_bars(conn, src.name, symbol, timeframe)
    written = db.write_bars(conn, fresh, src.name, symbol, timeframe,
                            partial_last=partial_last)

    ev = src.events(symbol)
    if ev.get("splits") or ev.get("dividends"):
        db.write_events(conn, ev, src.name, symbol)
        conn.commit()

    # Audit the STORED series, not the fetched page. The page is a fragment; the
    # series is what a backtest will actually see, and only it can reveal a gap
    # introduced at the seam between two fetches.
    #
    # No special case for an empty frame. `integrity.check` returns an ERROR for
    # one, and that is the right answer: a fetch that produced nothing is a
    # failure, not a series with no findings. Short-circuiting it is how twelve
    # empty 4h crypto series came back marked PASS -- exactly the silent-success
    # failure this layer exists to prevent.
    stored = db.read_bars(conn, src.name, symbol, timeframe, include_partial=False)
    # Events are read back from the table rather than taken from `ev`: a narrow
    # intraday fetch carries no event stream, but the splits recorded by an
    # earlier daily fetch still explain that symbol's discontinuities.
    known = db.read_events(conn, src.name, symbol)
    rep = integrity.check(stored, timeframe, expect_regular=src.regular,
                          events=known,
                          corporate_actions=getattr(src, 'corporate_actions', True))

    meta = dict(src.describe(symbol))
    meta["history_tier_years"] = HISTORY_YEARS.get(timeframe)
    meta["requested_since"] = start
    meta["expect_regular"] = src.regular

    span = B.Span.of(stored)
    db.upsert_series(
        conn, src.name, symbol, timeframe,
        asset_class=asset_class or meta.get("instrument_type", "").lower() or None,
        first_ts=span.first_ts, last_ts=span.last_ts, row_count=span.rows,
        fetched_at=datetime.now(timezone.utc) if written else None,
        checked_at=datetime.now(timezone.utc),
        venue_timezone=meta.get("venue_timezone"),
        feed_adjustment=meta.get("feed_adjustment", "none"),
        integrity=rep.to_dict(), meta=meta)
    conn.commit()

    return IngestResult(src.name, symbol, timeframe, span.rows,
                        max(0, span.rows - before),
                        rep, span.first_ts, span.last_ts, partial_last,
                        time.perf_counter() - t0)


# -- the curated universe -----------------------------------------------------

def load_universe(path: Path | None = None) -> list[dict]:
    """Flatten universe.json into one entry per (symbol, timeframe)."""
    p = path or UNIVERSE_FILE
    spec = json.loads(p.read_text())
    out = []
    for asset_class, group in spec.items():
        if asset_class.startswith("_"):
            continue
        for s in group["symbols"]:
            for tf in group["timeframes"]:
                out.append({
                    "kind": group["source"], "symbol": s["symbol"],
                    "timeframe": tf, "asset_class": asset_class,
                    "name": s.get("name"), "sector": s.get("sector"),
                    "venue": group.get("venue"), "since": s.get("since"),
                })
    return out


def register_universe(conn, path: Path | None = None) -> int:
    """Record the curated list in the database, so the app can read it."""
    p = path or UNIVERSE_FILE
    spec = json.loads(p.read_text())
    n = 0
    with conn.cursor() as cur:
        for asset_class, group in spec.items():
            if asset_class.startswith("_"):
                continue
            for s in group["symbols"]:
                cur.execute(
                    """
                    INSERT INTO universe (symbol, source, asset_class, sector,
                                          name, timeframes, since, active)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,true)
                    ON CONFLICT (symbol) DO UPDATE SET
                        source=EXCLUDED.source, asset_class=EXCLUDED.asset_class,
                        sector=EXCLUDED.sector, name=EXCLUDED.name,
                        timeframes=EXCLUDED.timeframes, active=true
                    """,
                    (s["symbol"], group["source"], asset_class, s.get("sector"),
                     s.get("name"), group["timeframes"], s.get("since")))
                n += 1
    conn.commit()
    return n
