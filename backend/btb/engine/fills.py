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

from dataclasses import dataclass, field

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
        return {
            "side": "long" if self.side > 0 else "short",
            "entry_ts": self.entry_ts, "exit_ts": self.exit_ts,
            "entry_px": round(self.entry_px, 8), "exit_px": round(self.exit_px, 8),
            "bars": self.exit_i - self.entry_i,
            "reason": self.reason,
            "pnl": round(self.pnl, 6),
            "pnl_pct": round(self.pnl / self.equity_at_entry * 100, 4)
                       if self.equity_at_entry else 0.0,
        }


@dataclass
class Fills:
    trades: list[Trade]
    equity: np.ndarray         # mark-to-market, one value per bar in the window
    in_pos: np.ndarray         # bool per bar in the window
    i0: int
    i1: int
    final: float
    ruin: bool
    open_trade: Trade | None = None
    events: list = field(default_factory=list)


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

    eq = capital
    pos: Trade | None = None
    trades: list[Trade] = []
    n = i1 - i0 + 1
    curve = np.full(n, np.nan)
    inpos = np.zeros(n, dtype=bool)
    ruin = False
    pending: tuple | None = None       # fill="open": (exit?, entry side or 0)
    events: list = []

    def open_(i, side, raw_px):
        nonlocal pos
        px = raw_px * (1 + s * side)
        qty = frac * eq / px
        pos = Trade(side, i, int(ts[i]), px, qty, eq)
        pos.slip = qty * raw_px * s
        events.append(("entry", i, side, px))

    def close_(i, raw_px, reason):
        nonlocal pos, eq
        t = pos
        px = raw_px * (1 - s * t.side)
        fees = t.qty * (t.entry_px + px) * fee
        pnl = (px - t.entry_px) * t.qty * t.side - fees
        t.exit_i, t.exit_ts, t.exit_px, t.reason = i, int(ts[i]), px, reason
        t.fees, t.slip, t.pnl = fees, t.slip + t.qty * raw_px * s, pnl
        eq = max(eq + pnl, 0.0) if liquidate else eq + pnl
        trades.append(t)
        events.append(("exit", i, t.side, px))
        pos = None

    for i in range(i0, i1 + 1):
        # -- orders queued on the previous bar fill at this bar's open --------
        if pending is not None:
            want_exit, want_side = pending
            pending = None
            if want_exit and pos is not None:
                close_(i, o[i], "signal")
                if eq <= 0:
                    ruin = True
                    break
            if want_side and pos is None:
                if eq <= 0:
                    ruin = True
                    break
                open_(i, want_side, o[i])

        # -- protective exits, against this bar's range ----------------------
        if pos is not None and pos.entry_i < i:
            side = pos.side
            hit = None
            if sig.stop is not None:
                level = _level(sig.stop, pos, -side)
                if (side > 0 and l[i] <= level) or (side < 0 and h[i] >= level):
                    gapped = (side > 0 and o[i] <= level) or (side < 0 and o[i] >= level)
                    hit = ("stop", o[i] if (gapped and stop_fill == "gap") else level)
            if hit is None and sig.take_profit is not None:
                level = _level(sig.take_profit, pos, side)
                if (side > 0 and h[i] >= level) or (side < 0 and l[i] <= level):
                    hit = ("take_profit", level)
            if hit is not None:
                close_(i, hit[1], hit[0])
                if eq <= 0:
                    ruin = True
                    break

        # -- liquidation: the bar's worst price wipes out the account ---------
        if liquidate and pos is not None and pos.entry_i < i:
            worst = l[i] if pos.side > 0 else h[i]
            mtm_worst = (eq + (worst - pos.entry_px) * pos.qty * pos.side
                         - pos.qty * (pos.entry_px + worst) * fee)
            if mtm_worst <= 0:
                sd, q = pos.side, pos.qty
                px = (sd * q * pos.entry_px + q * pos.entry_px * fee - eq) / (q * (sd - fee))
                if (sd > 0 and o[i] <= px) or (sd < 0 and o[i] >= px):
                    px = o[i]                     # gapped through it
                close_(i, px, "liquidated")
                ruin = True
                break

        # -- time and signal exits ---------------------------------------------
        if pos is not None:
            timed = sig.max_bars is not None and i - pos.entry_i >= sig.max_bars
            signal = (pos.side > 0 and sig.exit_long[i]) or (pos.side < 0 and sig.exit_short[i])
            if timed or signal:
                if fill == "close" or timed:
                    close_(i, c[i], "time" if timed else "signal")
                    if eq <= 0:
                        ruin = True
                        break
                else:
                    pending = (True, 0)

        # -- entries -------------------------------------------------------------
        # No entry on the final bar when it would be force-closed at once: that
        # is a zero-length trade that only pays fees.
        flat = pos is None or (pending is not None and pending[0])
        if flat and not (close_open and i == i1):
            side = 1 if sig.entry_long[i] else (-1 if sig.entry_short[i] else 0)
            if side:
                if fill == "close":
                    if eq <= 0:
                        ruin = True
                        break
                    open_(i, side, c[i])
                elif i < i1:
                    pending = (pending[0] if pending else False, side)

        # -- mark to market -------------------------------------------------------
        k = i - i0
        if pos is not None:
            curve[k] = eq + (c[i] - pos.entry_px) * pos.qty * pos.side \
                       - pos.qty * pos.entry_px * fee
            inpos[k] = True
        else:
            curve[k] = eq

    open_trade = None
    if pos is not None and not ruin:
        if close_open:
            close_(i1, c[i1], "end")
            curve[n - 1] = eq
        else:
            open_trade = pos

    # After ruin the account is flat at whatever it ended on.
    if ruin:
        last = np.flatnonzero(~np.isnan(curve))
        fill_from = (last[-1] + 1) if len(last) else 0
        curve[fill_from:] = eq
    return Fills(trades, curve, inpos, i0, i1, eq, ruin, open_trade, events)


def _level(spec: dict, t: Trade, direction: int) -> float:
    """Price of a stop (direction against the trade) or target (with it).

    The percentage form is computed as entry * (1 +/- pct/100), not
    entry +/- entry*pct/100: the two round differently, and the reference
    engine uses the first. A one-ulp difference at the level is enough to
    flip whether a bar's low touched it.
    """
    if spec["kind"] == "pct":
        return t.entry_px * (1 + direction * spec["value"] / 100)
    d = spec["dist"][t.entry_i]
    if d != d:
        # No ATR yet at entry: the level is unreachable, in whichever
        # direction it points.
        return float("inf") if direction > 0 else float("-inf")
    return t.entry_px + direction * float(d)
