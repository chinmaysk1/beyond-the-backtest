"""Frozen reference engine for the parity test. DO NOT "improve" this file.

This is the pure-Python EMA-cross + dual-ADX loop from the project's earlier
prototype, kept verbatim. That loop was reconciled bar by bar against
TradingView's Strategy Tester and drives the live trader, so it is the closest
thing this project has to ground truth. The vectorised engine in btb.engine
must reproduce its trades exactly (tests/test_engine_parity.py).

Its known simplifications are deliberate here and handled by options in the
real engine: stops fill exactly at the stop even on a gap (stop_fill="exact"),
an open position at the end is left out of the stats (close_open=False), and
there is no slippage.
"""
from __future__ import annotations

import math
from typing import NamedTuple


def ema(src, n):
    k = 2.0 / (n + 1.0)
    out = [None] * len(src)
    run = None
    for i, v in enumerate(src):
        run = v if run is None else (v - run) * k + run
        if i >= n - 1:
            out[i] = run
    return out


def rma(src, n):
    """Wilder smoothing, as Pine's ta.rma. Seeded with a simple mean."""
    out = [None] * len(src)
    run = None
    for i, v in enumerate(src):
        if v is None:
            continue
        if run is None:
            if i + 1 >= n:
                seed = [x for x in src[i - n + 1:i + 1] if x is not None]
                if len(seed) == n:
                    run = sum(seed) / n
                    out[i] = run
            continue
        run = (run * (n - 1) + v) / n
        out[i] = run
    return out


def dmi(h, l, c, n):
    """Returns ADX, matching ta.dmi(n, n)."""
    N = len(c)
    tr, pdm, ndm = [None] * N, [None] * N, [None] * N
    for i in range(1, N):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr = rma(tr, n)
    sp, sm = rma(pdm, n), rma(ndm, n)
    dx = [None] * N
    for i in range(N):
        if atr[i] and sp[i] is not None and sm[i] is not None and atr[i] != 0:
            pdi = 100 * sp[i] / atr[i]
            ndi = 100 * sm[i] / atr[i]
            s = pdi + ndi
            dx[i] = 100 * abs(pdi - ndi) / s if s else 0.0
    return rma(dx, n)


def htf_adx(ts, o, h, l, c, mult, n):
    """ADX on a higher timeframe, mapped back onto the base bars.

    `mult` is how many base bars make one higher-timeframe bar. The value of the
    HTF bar containing base bar i is only known once that HTF bar CLOSES, so each
    base bar sees the previous completed HTF bar -- this is what lookahead_off
    means, and skipping it is the single easiest way to fake a good result.

    Bars are grouped by WALL-CLOCK time, not by array index. Grouping by index
    (range(0, len(c), mult)) makes the 12h boundaries depend on where the array
    happens to start: feed the same bars with one extra bar of lead-in and every
    HTF bar regroups, changing ADX and therefore which entries pass the filter.
    That never showed up in backtests because data/btcusd_4h.csv begins exactly
    on a 12h boundary, so index-grouping and time-grouping coincided. It breaks
    live, where the window slides forward every cycle and would be correctly
    phased only one cycle in three -- measured at 9.1% of decisions changing.
    Time-grouping also matches request.security(), which aligns to real time.

    A leading partial group is skipped so every HTF bar is built from a complete
    set of base bars, which is what makes the series independent of the window.
    """
    if len(c) < 2:
        return [None] * len(c)
    step = ts[1] - ts[0]                    # base bar length, seconds
    bucket = step * mult

    first = 0
    while first < len(ts) and ts[first] % bucket != 0:
        first += 1
    if first >= len(ts):
        return [None] * len(c)

    gh, gl, gc, gidx = [], [], [], []
    for i in range(first, len(c), mult):
        j = min(i + mult, len(c))
        gh.append(max(h[i:j])); gl.append(min(l[i:j])); gc.append(c[j - 1])
        gidx.append(j - 1)                  # base index where this HTF bar closes
    a = dmi(gh, gl, gc, n)
    out = [None] * len(c)
    for k, close_at in enumerate(gidx):
        val = a[k]
        stop = gidx[k + 1] if k + 1 < len(gidx) else len(c) - 1
        for i in range(close_at + 1, min(stop, len(c) - 1) + 1):
            out[i] = val
    return out


class Event(NamedTuple):
    """One decision the rules produce. `kind` is "exit" or "entry"."""
    kind: str
    i: int                  # bar index
    ts: int                 # bar timestamp, seconds
    price: float            # fill price under process_orders_on_close semantics
    direction: int          # +1 long, -1 short; for an exit, the side being closed


def walk(ts, o, h, l, c, *, fast=20, slow=62, adx_len=9, adx_min=23.0,
         adx_min_ltf=28.0, htf_mult=3, stop_pct=4.1,
         start_ts=None, end_ts=None, allow="both"):
    """Yield the strategy's decisions, bar by bar. Pure signal logic, no equity.

    This is the single copy of the rules. `run()` below consumes it and adds
    position sizing and P&L; the live system consumes the same generator and
    keeps only the final position, so a live signal cannot drift from a
    backtested one without this function changing underneath both.

    Deliberately excluded: anything involving equity. Position size cannot
    affect which bar a signal fires on, so the rules do not need to know it --
    and keeping equity out is what makes the live replay cheap and identical.

    `allow` restricts which side may be ENTERED: "both" (default, and what the
    live config trades), "long" or "short". It gates entries only -- an open
    position still exits on its stop or the opposite cross, exactly as before,
    so switching it never strands a position. Default "both" leaves every
    existing caller, including live/signal.py, bit-for-bit unchanged.
    """
    N = len(c)
    ef, es = ema(c, fast), ema(c, slow)
    a_ltf = dmi(h, l, c, adx_len)
    a_htf = htf_adx(ts, o, h, l, c, htf_mult, adx_len)

    pos = 0
    entry = 0.0

    for i in range(1, N):
        if ef[i] is None or es[i] is None or ef[i-1] is None or es[i-1] is None: continue
        if start_ts and ts[i] < start_ts: continue
        if end_ts and ts[i] > end_ts: break
        xup = ef[i] > es[i] and ef[i - 1] <= es[i - 1]
        xdn = ef[i] < es[i] and ef[i - 1] >= es[i - 1]

        # hard stop, checked intrabar against the bar's extreme. Modelled as an
        # exact fill at the stop price: a bar that gaps straight through it costs
        # more than this in reality, and nothing here can see that.
        if pos > 0 and stop_pct > 0 and l[i] <= entry * (1 - stop_pct / 100):
            yield Event("exit", i, ts[i], entry * (1 - stop_pct / 100), pos); pos = 0
        elif pos < 0 and stop_pct > 0 and h[i] >= entry * (1 + stop_pct / 100):
            yield Event("exit", i, ts[i], entry * (1 + stop_pct / 100), pos); pos = 0

        if pos > 0 and xdn:
            yield Event("exit", i, ts[i], c[i], pos); pos = 0
        elif pos < 0 and xup:
            yield Event("exit", i, ts[i], c[i], pos); pos = 0

        # The ADX gate is an ENTRY filter only. Exits above are ungated, which is
        # why some closes go flat rather than reversing: the position is out but
        # the filter blocks the other side.
        ok = ((a_htf[i] is None or a_htf[i] >= adx_min) and
              (a_ltf[i] is None or a_ltf[i] >= adx_min_ltf))
        if pos == 0 and ok and (xup or xdn):
            side = 1 if xup else -1
            if (allow == "long" and side < 0) or (allow == "short" and side > 0):
                continue
            pos = side
            entry = c[i]
            yield Event("entry", i, ts[i], c[i], pos)


def position_after(events) -> tuple[int, float, int | None]:
    """Collapse an event stream to (direction, entry_price, entry_bar_ts).

    The live system's "intent": what the rules say the position should be right
    now, derived from bars alone with no stored state. Path-dependent -- a
    position persists until a cross or a stop closes it -- so this must be fed
    the longest history available, not just the indicator warmup.
    """
    pos, entry, at = 0, 0.0, None
    for ev in events:
        if ev.kind == "exit":
            pos, entry, at = 0, 0.0, None
        else:
            pos, entry, at = ev.direction, ev.price, ev.ts
    return pos, entry, at


def run(ts, o, h, l, c, *, fast=20, slow=62, adx_len=9, adx_min=23.0,
        adx_min_ltf=28.0, htf_mult=3, stop_pct=4.1, commission=0.10,
        start_ts=None, end_ts=None, capital=10000.0, leverage=1.0):
    """Backtest the events from walk(), adding sizing and P&L.

    `leverage` multiplies position size only. It cannot change which bar a
    signal fires on, so the trade list is identical at every leverage and only
    the equity path scales.
    """
    eq = capital
    pos = 0
    entry = 0.0
    qty = 0.0
    trades, gp, gl_ = [], 0.0, 0.0
    peak, maxdd = capital, 0.0
    fee = commission / 100.0
    ruin = False

    for ev in walk(ts, o, h, l, c, fast=fast, slow=slow, adx_len=adx_len,
                   adx_min=adx_min, adx_min_ltf=adx_min_ltf, htf_mult=htf_mult,
                   stop_pct=stop_pct, start_ts=start_ts, end_ts=end_ts):
        if ev.kind == "exit":
            px = ev.price
            pnl = (px - entry) * qty * (1 if pos > 0 else -1)
            pnl -= abs(qty) * (entry + px) * fee          # both legs
            eq += pnl
            trades.append(pnl)
            if pnl > 0: gp += pnl
            else: gl_ += -pnl
            pos = 0; qty = 0.0
            peak = max(peak, eq)
            if peak > 0: maxdd = max(maxdd, (peak - eq) / peak)
            # Ruin. The original loop kept iterating but could never trade again
            # (its entry test required eq > 0 and no position was open), so
            # stopping here is equivalent and says so out loud.
            if eq <= 0:
                ruin = True
                break
        else:
            if eq <= 0:
                ruin = True
                break
            pos = ev.direction
            entry = ev.price
            qty = leverage * eq / entry

    n = len(trades)
    return {
        "trades": n,
        "net_pct": (eq / capital - 1) * 100,
        "win_pct": (sum(1 for t in trades if t > 0) / n * 100) if n else 0,
        "pf": (gp / gl_) if gl_ else None,
        "maxdd_pct": maxdd * 100,
        "final": eq,
        "ruin": ruin,
    }
