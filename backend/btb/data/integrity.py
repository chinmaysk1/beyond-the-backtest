"""Audit a bar series before anything is allowed to backtest on it.

The premise: a data feed does not fail loudly. It returns plausible-looking
wrong numbers -- a truncated span under a full-span label, a double-adjusted
price, a bar the venue quietly resampled -- and every one of those is
indistinguishable from correct data until something downstream is already built
on it. The only defence is a suite of checks that runs every time and is
recorded alongside the data it passed or failed.

Each check returns a `Finding`. Severity is either ERROR (the series is not fit
to backtest on) or WARN (a real property of the market that must be visible in
any result derived from this data, e.g. a weekend gap or a thin sample).

Deliberately NOT done here: filling gaps. A forward-filled bar is an invented
price, and an invented price is indistinguishable from a real one two layers
later. Gaps are reported, not repaired.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .bars import COLUMNS, iso, tf_seconds

ERROR = "ERROR"
WARN = "WARN"
# Not a problem -- a property of the data worth stating where it would
# otherwise look like one.
INFO = "INFO"

# A series short enough that any statistic drawn from it deserves a sample-size
# caveat. Usually this is the venue's own retention limit rather than anything
# about the market -- and because the request succeeds either way, the caveat
# has to travel with the numbers or it will not be there when they are read.
THIN_BARS = 2000

# A session gap longer than this many business days is a data hole, not a
# holiday. 4 covers Thanksgiving week and the 2012 Sandy closure; 9/11 (7) and
# anything longer is genuinely worth a look.
MAX_SESSION_GAP_DAYS = 4

# How many distinct intraday offsets a session-anchored series may show before
# it stops looking like a market and starts looking like a resampling bug. Two
# DST regimes plus a short closing bar is 3; more than a few is not explicable.
MAX_SESSION_PHASES = 4

DAY = 86400

# How large a single-bar move has to be before it is worth examining.
#
# Two thresholds, because the question differs by market. For an instrument
# with corporate actions the check is really "did a split happen that the venue
# did not tell us about", and the most common split of all -- 2:1 -- moves the
# price exactly 50%. A strictly-greater-than-50% test misses every one of them,
# which is the majority of splits. 35% catches 2:1 and 3:2 while still being far
# outside what a large-cap equity does on its own.
#
# Crypto has no corporate actions, so the threshold is only asking "is this a
# bad print". 50% daily moves are ordinary there -- XRP moved 180% in a day in
# April 2017 -- so a lower bar would fire constantly and mean nothing.
JUMP_CORPORATE = 0.35
JUMP_VOLATILE = 0.5


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str
    message: str
    count: int = 0

    def __str__(self) -> str:
        return f"[{self.severity}] {self.check}: {self.message}"


@dataclass
class Report:
    findings: list[Finding]
    stats: dict

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARN]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def notes(self) -> list:
        return [f for f in self.findings if f.severity == INFO]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "findings": [asdict(f) for f in self.findings],
            "stats": self.stats,
        }

    def render(self) -> str:
        head = "PASS" if self.ok else "FAIL"
        lines = [f"{head}  {self.stats.get('rows', 0)} bars  "
                 f"{self.stats.get('first', '-')} .. {self.stats.get('last', '-')}"]
        lines += ["  " + str(f) for f in self.findings]
        if not self.findings:
            lines.append("  (no findings)")
        return "\n".join(lines)


def check(df: pd.DataFrame, timeframe: str, *, expect_regular: bool = True,
          events: dict | None = None, corporate_actions: bool = True) -> Report:
    """Run every check against a canonical frame.

    `expect_regular=False` for instruments that legitimately do not trade around
    the clock (equities: nights, weekends, holidays). Grid-alignment and
    ordering are still enforced; only the missing-bar check relaxes to a warning
    about session structure rather than an error.
    """
    out: list[Finding] = []
    step = tf_seconds(timeframe)

    missing_cols = [c for c in COLUMNS if c not in df.columns]
    if missing_cols:
        return Report([Finding("schema", ERROR, f"missing columns {missing_cols}")], {"rows": len(df)})

    if df.empty:
        return Report([Finding("empty", ERROR, "no bars")], {"rows": 0})

    ts = df["ts"].to_numpy(dtype="int64")
    o, h, l, c = (df[k].to_numpy(dtype="float64") for k in ("open", "high", "low", "close"))
    v = df["volume"].to_numpy(dtype="float64")

    # --- ordering and uniqueness -------------------------------------------
    d = np.diff(ts)
    if len(d) and (d <= 0).any():
        n = int((d <= 0).sum())
        out.append(Finding("monotonic", ERROR, f"{n} timestamps out of order or duplicated", n))

    # --- grid alignment ------------------------------------------------------
    # A 24/7 market has no session, so its bars are anchored to the epoch and
    # every open must land on a multiple of the timeframe. A bar off that grid
    # means the source silently resampled, or the timeframe label is wrong.
    #
    # An exchange-traded instrument is anchored to the SESSION OPEN instead, and
    # that is a different grid: SPY hourly bars begin at 13:30 UTC, so all 3,473
    # of them are "misaligned" against the epoch while being perfectly regular
    # against the market. The anchor also moves twice a year with daylight
    # saving. So the check there is that the phase is CONSISTENT, not that it is
    # zero -- a couple of distinct phases is DST; many is a source resampling.
    if step < 604800:  # weekly bars are anchored to a weekday, not the epoch
        off = ts % step
        if expect_regular:
            bad = int((off != 0).sum())
            if bad:
                out.append(Finding("alignment", ERROR,
                                   f"{bad} bars not aligned to the {timeframe} grid "
                                   f"(e.g. {iso(int(ts[np.argmax(off != 0)]))})", bad))
        elif step < 86400:
            phases = np.unique(off)
            if len(phases) > MAX_SESSION_PHASES:
                out.append(Finding(
                    "alignment", ERROR,
                    f"{len(phases)} distinct intraday offsets; a session-anchored "
                    f"series should show one per DST regime, not this many", len(phases)))
            elif len(phases) > 1:
                mins = ", ".join(f"{int(p) // 60}m" for p in phases)
                out.append(Finding("alignment", WARN,
                                   f"bars sit at {len(phases)} offsets past the hour "
                                   f"({mins}) -- session anchor plus daylight saving, "
                                   f"and a short closing bar", len(phases)))

    # --- gaps ----------------------------------------------------------------
    # Two different questions, because two different kinds of instrument.
    #
    # A 24/7 market has no legitimate gaps: a missing bar is missing data, and
    # that is an ERROR. An exchange-traded instrument is closed most of the
    # time, so counting clock gaps reports every weekend as data loss -- on SPY
    # that came to 3,817 "missing bars" in a series with nothing wrong with it,
    # which is precisely the kind of noise that trains people to ignore
    # warnings. For those, gaps are counted in BUSINESS DAYS, and only a run
    # longer than a long weekend is worth surfacing.
    if len(d):
        # A sub-timeframe interval is corruption on a 24/7 grid. On a session
        # grid it is the closing bar: the US cash session is 6.5 hours, so its
        # last hourly bar is 30 minutes long. Real, and not an error.
        short = int((d < step).sum())
        if short:
            out.append(Finding(
                "substep", ERROR if expect_regular else WARN,
                f"{short} intervals shorter than one bar"
                + ("" if expect_regular else " -- expected at a session close"), short))

        if expect_regular:
            gaps = np.flatnonzero(d > step)
            missing = int(((d[gaps] // step) - 1).sum()) if len(gaps) else 0
            if missing:
                worst = int(gaps[np.argmax(d[gaps])])
                out.append(Finding(
                    "gaps", ERROR,
                    f"{missing} bars missing across {len(gaps)} gaps; largest "
                    f"{int(d[worst] // step)} bars at {iso(int(ts[worst]))}", missing))
        elif step >= 86400:
            days = ts.astype("datetime64[s]").astype("datetime64[D]")
            bd = np.busday_count(days[:-1], days[1:])      # 1 across a normal session step
            holes = np.flatnonzero(bd > MAX_SESSION_GAP_DAYS)
            if len(holes):
                worst = int(holes[np.argmax(bd[holes])])
                out.append(Finding(
                    "gaps", WARN,
                    f"{len(holes)} gaps longer than {MAX_SESSION_GAP_DAYS} business days; "
                    f"largest {int(bd[worst])} at {iso(int(ts[worst]))} -- market closures "
                    f"are expected, a long run is not", len(holes)))
            closed = int(bd[bd > 1].sum() - (bd > 1).sum()) if len(bd) else 0
            if closed:
                out.append(Finding("sessions", WARN,
                                   f"{closed} business days with no bar -- market holidays "
                                   f"and halts, not missing data", closed))

    # --- OHLC sanity ---------------------------------------------------------
    bad_hl = int((h < l).sum())
    if bad_hl:
        out.append(Finding("ohlc", ERROR, f"{bad_hl} bars with high < low", bad_hl))
    outside = int(((o > h) | (o < l) | (c > h) | (c < l)).sum())
    if outside:
        out.append(Finding("ohlc", ERROR, f"{outside} bars with open/close outside [low, high]", outside))
    nonpos = int((~np.isfinite(c) | (c <= 0)).sum())
    if nonpos:
        out.append(Finding("prices", ERROR, f"{nonpos} bars with non-finite or non-positive close", nonpos))

    # --- flat / dead bars ----------------------------------------------------
    # Early crypto history is full of these: 2011 BTC bars where the price never
    # moved and nothing traded (888 of them, 2.7%, in the 4h series). They are
    # not corrupt, but a warmup window sitting inside them produces indicator
    # values that mean nothing.
    flat = int(((h == l) & (v == 0)).sum())
    if flat:
        pct = 100.0 * flat / len(df)
        out.append(Finding("flat", WARN,
                           f"{flat} bars ({pct:.1f}%) have zero range and zero volume", flat))

    neg_v = int((v < 0).sum())
    if neg_v:
        out.append(Finding("volume", ERROR, f"{neg_v} bars with negative volume", neg_v))

    # --- price discontinuities ----------------------------------------------
    # A >50% move between consecutive closes on a daily-or-slower series is,
    # overwhelmingly, a corporate action rather than a real move.
    #
    # Since `bars` deliberately holds RAW prints, those discontinuities are the
    # correct contents of the table -- AAPL's five splits really are five step
    # changes in what one share cost. Flagging them as suspicious would fire on
    # healthy data for almost every equity, which is how a warning channel gets
    # trained into being ignored.
    #
    # So each jump is matched against the event stream. One the events explain
    # is reported as accounted for; only an UNEXPLAINED jump is a warning. That
    # version of the check is the one worth having, because it is what catches
    # a split the venue never told us about.
    threshold = JUMP_CORPORATE if corporate_actions else JUMP_VOLATILE
    with np.errstate(divide="ignore", invalid="ignore"):
        ret = np.abs(np.diff(c) / c[:-1])
    jump_idx = np.flatnonzero(np.nan_to_num(ret) >= threshold)
    if len(jump_idx) and step >= 86400:
        explained = _explained_by_events(ts, jump_idx, events, step)
        unexplained = [i for i in jump_idx if i not in explained]

        if not corporate_actions:
            # Spot crypto has no splits or dividends, so "unexplained by a
            # corporate action" is true of every jump by construction -- and a
            # warning that fires on all twelve symbols is one nobody will read.
            # XRP really did move 180% in a day in April 2017. State the
            # observation instead of implying a cause that cannot exist.
            worst = max(jump_idx, key=lambda i: ret[i])
            out.append(Finding(
                "jumps", INFO,
                f"{len(jump_idx)} single-bar moves over {threshold:.0%} (largest "
                f"{100 * ret[worst]:.0f}% at {iso(int(ts[worst]))}) -- normal in "
                f"this market, but confirm none is a bad print", len(jump_idx)))
        elif unexplained:
            # Report the largest UNEXPLAINED jump, not the largest jump. Naming
            # a jump the events already account for points the reader at data
            # that is fine and hides the one that is not.
            worst = max(unexplained, key=lambda i: ret[i])
            out.append(Finding(
                "jumps", WARN,
                f"{len(unexplained)} of {len(jump_idx)} single-bar moves over "
                f"{threshold:.0%} are NOT explained by a known corporate action (largest "
                f"{100 * ret[worst]:.0f}% at {iso(int(ts[worst]))})",
                len(unexplained)))
        elif len(explained):
            out.append(Finding(
                "jumps", INFO,
                f"{len(explained)} single-bar moves over {threshold:.0%}, every one matching "
                f"a known corporate action -- expected, bars are stored raw",
                len(explained)))

    if len(df) < THIN_BARS:
        out.append(Finding("sample", WARN,
                           f"only {len(df)} bars; results from this series carry a "
                           f"sample-size caveat (< {THIN_BARS})", len(df)))

    stats = {
        "rows": int(len(df)),
        "first": iso(int(ts[0])),
        "last": iso(int(ts[-1])),
        "first_ts": int(ts[0]),
        "last_ts": int(ts[-1]),
        "expected_bars": int((ts[-1] - ts[0]) // step + 1),
        "coverage_pct": round(100.0 * len(df) / max(1, (ts[-1] - ts[0]) // step + 1), 2),
    }
    return Report(out, stats)


def _explained_by_events(ts, jump_idx, events: dict | None, step: int) -> set:
    """Which of these jumps sit exactly across a known corporate action.

    A jump between bar i and bar i+1 is explained if some event falls between
    them. The comparison uses the same day-granularity rule as the adjustment
    code: a corporate action is stamped at the session open while daily bars are
    stored snapped to UTC midnight, so comparing raw timestamps would place
    every ex-date on the wrong side of its own bar.
    """
    if not events:
        return set()
    stamps = [int(e["ts"]) for e in events.get("splits", [])]
    if not stamps:
        return set()

    def key(x):
        return x // DAY if step >= DAY else x

    boundaries = {key(t) for t in stamps}
    out = set()
    for i in jump_idx:
        lo, hi = key(int(ts[i])), key(int(ts[i + 1]))
        if any(lo < b <= hi for b in boundaries):
            out.add(int(i))
    return out
