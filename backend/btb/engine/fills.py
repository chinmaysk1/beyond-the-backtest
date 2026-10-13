"""Signals -> trades. The only part of the engine that walks bar by bar.

Everything upstream is vectorised; this cannot be, because whether a stop is
live on bar i depends on whether a trade opened on bar i-3. The loop is kept
deliberately small, and every rule in it closes a specific way a backtest can
come out better than the same strategy would trade.

Order of events on each bar, the same order the reference engine uses:

    1. stop, then take-profit, checked against the bar's range
    2. time exit (max_bars) and signal exit
    3. entry, if flat -- including straight after an exit on the same bar,
       which is how a cross reverses a position

Rules:

* **Fill timing.** `fill="close"` (default) fills a signal on bar i at bar i's
  close, as TradingView's process_orders_on_close. `fill="open"` fills it at
  bar i+1's open instead, which is more realistic and slightly worse.
* **Commission is charged on both legs**, as a % of each leg's notional.
* **Slippage** moves every fill against the trader by `slippage_bps`,
  stops included.
* **A stop the bar gaps through fills at the open, not at the stop.** The
  earlier prototype assumed an exact fill at the stop, which is the best
  possible outcome of the worst possible bar. `stop_fill="exact"` keeps that
  behaviour for reconciling against engines that model it that way.
* **Stop before take-profit** when a single bar touches both: the order inside
  a bar is unknowable from OHLC, so the worse one is assumed.
* **Liquidation.** If the bar's worst price would take the account's
  mark-to-market equity to zero, the position is closed there and the run ends
  as ruin. Without this, an unstopped 100%-equity short through a rally goes
  deeply negative mid-trade and then "recovers" -- a path no broker allows.
  Equity is floored at zero: losses past the account are reported as -100%.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import numpy as np

from .spec import Signals


@dataclass(frozen=True)
class Costs:
    commission_pct: float = 0.10       # per leg, % of notional
    slippage_bps: float = 5.0          # per fill, against the trader

    def as_dict(self) -> dict:
        return {"commission_pct": self.commission_pct, "slippage_bps": self.slippage_bps}


# Defaults per asset class. Crypto: a typical taker fee. Equities: commission
# free at retail brokers, but the spread is still paid.
DEFAULT_COSTS = {
    "crypto": Costs(0.10, 5.0),
    "equity": Costs(0.0, 2.0),
}


def default_costs(asset_class: str | None) -> Costs:
    return DEFAULT_COSTS.get(asset_class or "crypto", DEFAULT_COSTS["crypto"])


@dataclass
class Trade:
    side: int                  # +1 long, -1 short
    entry_i: int
    entry_ts: int
    entry_px: float            # fill price, after slippage
    qty: float
    equity_at_entry: float
    exit_i: int = -1
    exit_ts: int = 0
    exit_px: float = 0.0
    reason: str = ""           # signal | stop | take_profit | time | end | liquidated
    pnl: float = 0.0
    fees: float = 0.0          # commission, both legs
    slip: float = 0.0          # slippage cost, both legs

    def as_dict(self) -> dict:
        # numpy's round (scale, rint, unscale) and Python's (correctly rounded)
        # can differ in the last kept digit. Prices used to arrive as numpy
        # scalars, so numpy's is pinned: stored trades stay byte-identical.
        r = lambda v, nd: round(np.float64(v), nd)      # noqa: E731
        return {
            "side": "long" if self.side > 0 else "short",
            "entry_ts": self.entry_ts, "exit_ts": self.exit_ts,
            "entry_px": r(self.entry_px, 8), "exit_px": r(self.exit_px, 8),
            "bars": self.exit_i - self.entry_i,
            "reason": self.reason,
            "pnl": r(self.pnl, 6),
            "pnl_pct": r(self.pnl / self.equity_at_entry * 100, 4)
                       if self.equity_at_entry else 0.0,
        }


# Columns of the closed-trade record the loop writes, one row per trade.
SIDE, ENTRY_I, EXIT_I, ENTRY_PX, EXIT_PX, QTY, EQ0, PNL, FEES, SLIP, REASON = range(11)
REASONS = ("signal", "stop", "take_profit", "time", "end", "liquidated")


@dataclass
class Fills:
    rec: np.ndarray            # [trades, 11] closed trades, columns above
    ts: np.ndarray             # the series' timestamps, for the trades' ts fields
    equity: np.ndarray         # mark-to-market, one value per bar in the window
    in_pos: np.ndarray         # bool per bar in the window
    i0: int
    i1: int
    final: float
    ruin: bool
    open_trade: Trade | None = None

    # A grid run only reads columns; Trade objects and the event list are built
    # when something asks for them, which a sweep never does.
    @cached_property
    def trades(self) -> list[Trade]:
        ts = self.ts
        out = []
        for side, ei, xi, epx, xpx, qty, eq0, pnl, fees, slip, why in self.rec.tolist():
            ei, xi = int(ei), int(xi)
            out.append(Trade(int(side), ei, int(ts[ei]), epx, qty, eq0, xi, int(ts[xi]),
                             xpx, REASONS[int(why)], pnl, fees, slip))
        return out

    @cached_property
    def events(self) -> list[tuple]:
        """("entry"|"exit", bar, side, fill price), in order. Positions never
        overlap, so this is each trade's entry then exit, then any open entry."""
        out = []
        for t in self.trades:
            out.append(("entry", t.entry_i, t.side, t.entry_px))
            out.append(("exit", t.exit_i, t.side, t.exit_px))
        if self.open_trade is not None:
            t = self.open_trade
            out.append(("entry", t.entry_i, t.side, t.entry_px))
        return out

    def column(self, k: int) -> list:
        return self.rec[:, k].tolist()


_NO_DIST = np.zeros(1)


def _exit_args(spec: dict | None) -> tuple[int, float, np.ndarray]:
    """A stop / target dict as the loop takes it: (kind, value, dist), kind
    0 none, 1 pct, 2 atr."""
    if spec is None:
        return 0, 0.0, _NO_DIST
    if spec["kind"] == "pct":
        return 1, float(spec["value"]), _NO_DIST
    return 2, 0.0, np.ascontiguousarray(spec["dist"], dtype="float64")


def simulate(ts, o, h, l, c, sig: Signals, *, i0: int, i1: int,
             capital: float = 10_000.0, costs: Costs = Costs(),
             size_pct: float = 100.0, leverage: float = 1.0,
             fill: str = "close", stop_fill: str = "gap",
             close_open: bool = True, liquidate: bool = True) -> Fills:
    """Run the position loop over bars i0..i1 inclusive.

    Indicators were computed over the whole history, so i0 can sit after the
    warm-up; no position exists before i0 and none is carried in.

    `close_open` marks any position still open at i1 to the last close as a
    completed trade ("end"). The reference engine leaves it out of the stats,
    which silently drops the most recent -- often the largest -- open loss.

    `liquidate=False` lets mark-to-market equity go below zero mid-trade, as
    the reference engine does (it only checks ruin at exits). Only for
    reconciling against it.
    """
    if fill not in ("close", "open"):
        raise ValueError("fill must be 'close' or 'open'")
    fee = costs.commission_pct / 100.0
    s = costs.slippage_bps / 10_000.0
    frac = size_pct / 100.0 * leverage
    sk, sv, sd = _exit_args(sig.stop)
    tk, tv, td = _exit_args(sig.take_profit)
    f64 = lambda a: np.ascontiguousarray(a, dtype="float64")      # noqa: E731
    b = lambda a: np.ascontiguousarray(a, dtype=np.bool_)          # noqa: E731

    rec, curve, inpos, eq, ruin, op = _walk(
        f64(o), f64(h), f64(l), f64(c),
        b(sig.entry_long), b(sig.exit_long), b(sig.entry_short), b(sig.exit_short),
        int(i0), int(i1), sk, sv, sd, tk, tv, td,
        -1 if sig.max_bars is None else int(sig.max_bars),
        float(capital), float(fee), float(s), float(frac),
        fill == "open", stop_fill == "gap", bool(close_open), bool(liquidate))

    open_trade = None
    if op[0] != 0:
        ei = int(op[1])
        open_trade = Trade(int(op[0]), ei, int(ts[ei]), float(op[2]), float(op[3]),
                           float(op[4]))
        open_trade.slip = float(op[5])

    # After ruin the account is flat at whatever it ended on.
    if ruin:
        last = np.flatnonzero(~np.isnan(curve))
        fill_from = (last[-1] + 1) if len(last) else 0
        curve[fill_from:] = eq
    return Fills(rec, ts, curve, inpos, i0, i1, float(eq), bool(ruin), open_trade)


# -- the loop ------------------------------------------------------------------------
#
# Compiled with numba when it is installed (it is in requirements.txt); without
# it the same source runs as plain Python, only slower. numba runs without
# fastmath, so every operation is the same IEEE double operation Python does, in
# the same order, and results are bit-identical either way --
# tests/test_engine_parity.py holds it to that.

try:
    from numba import njit
except ImportError:                                    # pragma: no cover
    def njit(**_):
        return lambda fn: fn


@njit(cache=True)
def _level(kind, value, dist, entry_px, entry_i, direction):
    """Price of a stop (direction against the trade) or target (with it).

    The percentage form is computed as entry * (1 +/- pct/100), not
    entry +/- entry*pct/100: the two round differently, and the reference
    engine uses the first. A one-ulp difference at the level is enough to
    flip whether a bar's low touched it.
    """
    if kind == 1:
        return entry_px * (1 + direction * value / 100)
    d = dist[entry_i]
    if d != d:
        # No ATR yet at entry: the level is unreachable, in whichever
        # direction it points.
        return np.inf if direction > 0 else -np.inf
    return entry_px + direction * d


@njit(cache=True)
def _entry(raw_px, side, s, frac, eq):
    """Fill price, quantity and slippage cost of an entry at raw_px."""
    px = raw_px * (1 + s * side)
    qty = frac * eq / px
    return px, qty, qty * raw_px * s


@njit(cache=True)
def _push(rec, nt, side, ei, xi, epx, raw_px, qty, eq0, slip, reason, s, fee):
    """Close the position at raw_px into row nt of rec. Returns (rec, pnl);
    rec is a new, larger array when it was full."""
    px = raw_px * (1 - s * side)
    fees = qty * (epx + px) * fee
    pnl = (px - epx) * qty * side - fees
    if nt == rec.shape[0]:
        grown = np.empty((rec.shape[0] * 2, 11))
        grown[:nt] = rec[:nt]
        rec = grown
    rec[nt, 0] = side
    rec[nt, 1] = ei
    rec[nt, 2] = xi
    rec[nt, 3] = epx
    rec[nt, 4] = px
    rec[nt, 5] = qty
    rec[nt, 6] = eq0
    rec[nt, 7] = pnl
    rec[nt, 8] = fees
    rec[nt, 9] = slip + qty * raw_px * s
    rec[nt, 10] = reason
    return rec, pnl


@njit(cache=True)
def _walk(o, h, l, c, el, xl, es, xs, i0, i1, sk, sv, sd, tk, tv, td, max_bars,
          capital, fee, s, frac, fill_open, stop_gap, close_open, liquidate):
    # Reasons, as REASONS indexes.
    SIGNAL, STOP, TAKE_PROFIT, TIME, END, LIQUIDATED = 0, 1, 2, 3, 4, 5
    n = i1 - i0 + 1
    rec = np.empty((max(16, n // 16), 11))
    nt = 0
    curve = np.full(n, np.nan)
    inpos = np.zeros(n, dtype=np.bool_)
    eq = capital
    ruin = False
    # The open position: has, side, entry bar, entry px, qty, equity at entry,
    # slippage so far, and its fixed stop / target levels.
    has = False
    p_side = 0
    p_ei = 0
    p_px = 0.0
    p_qty = 0.0
    p_eq0 = 0.0
    p_slip = 0.0
    stop_lv = 0.0
    tp_lv = 0.0
    # fill="open": orders queued on the previous bar -- (exit?, entry side or 0)
    pending = False
    pend_exit = False
    pend_side = 0

    for i in range(i0, i1 + 1):
        # -- orders queued on the previous bar fill at this bar's open --------
        if pending:
            want_exit, want_side = pend_exit, pend_side
            pending = False
            if want_exit and has:
                rec, pnl = _push(rec, nt, p_side, p_ei, i, p_px, o[i], p_qty, p_eq0,
                                 p_slip, SIGNAL, s, fee)
                nt += 1
                eq = max(eq + pnl, 0.0) if liquidate else eq + pnl
                has = False
                if eq <= 0:
                    ruin = True
                    break
            if want_side != 0 and not has:
                if eq <= 0:
                    ruin = True
                    break
                p_px, p_qty, p_slip = _entry(o[i], want_side, s, frac, eq)
                has, p_side, p_ei, p_eq0 = True, want_side, i, eq
                if sk != 0:
                    stop_lv = _level(sk, sv, sd, p_px, i, -want_side)
                if tk != 0:
                    tp_lv = _level(tk, tv, td, p_px, i, want_side)

        # -- protective exits, against this bar's range ----------------------
        if has and p_ei < i:
            side = p_side
            hit = -1
            hit_px = 0.0
            if sk != 0:
                level = stop_lv
                if (side > 0 and l[i] <= level) or (side < 0 and h[i] >= level):
                    gapped = (side > 0 and o[i] <= level) or (side < 0 and o[i] >= level)
                    hit = STOP
                    hit_px = o[i] if (gapped and stop_gap) else level
            if hit < 0 and tk != 0:
                level = tp_lv
                if (side > 0 and h[i] >= level) or (side < 0 and l[i] <= level):
                    hit = TAKE_PROFIT
                    hit_px = level
            if hit >= 0:
                rec, pnl = _push(rec, nt, p_side, p_ei, i, p_px, hit_px, p_qty, p_eq0,
                                 p_slip, hit, s, fee)
                nt += 1
                eq = max(eq + pnl, 0.0) if liquidate else eq + pnl
                has = False
                if eq <= 0:
                    ruin = True
                    break

        # -- liquidation: the bar's worst price wipes out the account ---------
        if liquidate and has and p_ei < i:
            worst = l[i] if p_side > 0 else h[i]
            mtm_worst = (eq + (worst - p_px) * p_qty * p_side
                         - p_qty * (p_px + worst) * fee)
            if mtm_worst <= 0:
                sd_, q = p_side, p_qty
                px = (sd_ * q * p_px + q * p_px * fee - eq) / (q * (sd_ - fee))
                if (sd_ > 0 and o[i] <= px) or (sd_ < 0 and o[i] >= px):
                    px = o[i]                     # gapped through it
                rec, pnl = _push(rec, nt, p_side, p_ei, i, p_px, px, p_qty, p_eq0,
                                 p_slip, LIQUIDATED, s, fee)
                nt += 1
                eq = max(eq + pnl, 0.0)
                has = False
                ruin = True
                break

        # -- time and signal exits ---------------------------------------------
        if has:
            timed = max_bars >= 0 and i - p_ei >= max_bars
            signal = (p_side > 0 and xl[i]) or (p_side < 0 and xs[i])
            if timed or signal:
                if not fill_open or timed:
                    rec, pnl = _push(rec, nt, p_side, p_ei, i, p_px, c[i], p_qty, p_eq0,
                                     p_slip, TIME if timed else SIGNAL, s, fee)
                    nt += 1
                    eq = max(eq + pnl, 0.0) if liquidate else eq + pnl
                    has = False
                    if eq <= 0:
                        ruin = True
                        break
                else:
                    pending, pend_exit, pend_side = True, True, 0

        # -- entries -------------------------------------------------------------
        # No entry on the final bar when it would be force-closed at once: that
        # is a zero-length trade that only pays fees.
        flat = (not has) or (pending and pend_exit)
        if flat and not (close_open and i == i1):
            side = 1 if el[i] else (-1 if es[i] else 0)
            if side != 0:
                if not fill_open:
                    if eq <= 0:
                        ruin = True
                        break
                    p_px, p_qty, p_slip = _entry(c[i], side, s, frac, eq)
                    has, p_side, p_ei, p_eq0 = True, side, i, eq
                    if sk != 0:
                        stop_lv = _level(sk, sv, sd, p_px, i, -side)
                    if tk != 0:
                        tp_lv = _level(tk, tv, td, p_px, i, side)
                elif i < i1:
                    pend_exit = pend_exit if pending else False
                    pending, pend_side = True, side

        # -- mark to market -------------------------------------------------------
        k = i - i0
        if has:
            curve[k] = eq + (c[i] - p_px) * p_qty * p_side \
                       - p_qty * p_px * fee
            inpos[k] = True
        else:
            curve[k] = eq

    op = np.zeros(6)
    if has and not ruin:
        if close_open:
            rec, pnl = _push(rec, nt, p_side, p_ei, i1, p_px, c[i1], p_qty, p_eq0,
                             p_slip, END, s, fee)
            nt += 1
            eq = max(eq + pnl, 0.0) if liquidate else eq + pnl
            curve[n - 1] = eq
        else:
            op[0] = p_side
            op[1] = p_ei
            op[2] = p_px
            op[3] = p_qty
            op[4] = p_eq0
            op[5] = p_slip
    return rec[:nt].copy(), curve, inpos, eq, ruin, op
