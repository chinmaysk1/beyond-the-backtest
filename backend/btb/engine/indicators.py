"""Indicator maths. Every function takes and returns float64 numpy arrays.

Missing values are NaN, never zero. A warm-up bar that read as 0.0 would pass a
`gt` test against a negative threshold and open a trade on a value that does
not exist yet.

Three conventions carried over from the earlier prototype, because that engine
was reconciled bar by bar against TradingView and every one of these was a
real disagreement before it was fixed:

* **Wilder smoothing (`rma`) is not a moving average.** ATR, RSI and DMI all
  use it. It is seeded with the simple mean of the first `n` values, then
  recurses `(prev * (n-1) + x) / n`. Using `sma` or `ema` instead compounds
  into a visibly different ADX.
* **The recursive filters run as plain Python loops over floats, in exactly
  the order the reference does the arithmetic.** A vectorised IIR (e.g. an
  lfilter) is faster but rounds differently, and at a threshold comparison a
  1e-15 difference is enough to move a trade by a bar. Parity is checked to be
  exact (tests/test_engine_parity.py), so this is not negotiable.
* **A NaN inside a series is skipped, not propagated.** The filter keeps its
  state and emits NaN for that bar. That is what the reference does with a
  `None`, and it matters on early crypto history, where whole weeks have zero
  range and DMI divides by a zero ATR.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

NAN = float("nan")


def _arr(x) -> np.ndarray:
    return np.asarray(x, dtype="float64")


def _isnan(v: float) -> bool:
    return v != v


# -- averages -----------------------------------------------------------------

def sma(src, n: int) -> np.ndarray:
    s = pd.Series(_arr(src))
    return s.rolling(n, min_periods=n).mean().to_numpy()


def ema(src, n: int) -> np.ndarray:
    """Exponential average, alpha = 2/(n+1), seeded with the first value.

    Output starts at the n-th valid value, matching the reference engine. The
    seed differs from a seeded-with-SMA EMA only during the first few multiples
    of n bars, which is why every signal is also gated on warm-up.
    """
    x = _arr(src)
    out = np.full(len(x), NAN)
    k = 2.0 / (n + 1.0)
    run = None
    seen = 0
    for i in range(len(x)):
        v = float(x[i])
        if _isnan(v):
            continue
        run = v if run is None else (v - run) * k + run
        seen += 1
        if seen >= n:
            out[i] = run
    return out


def rma(src, n: int) -> np.ndarray:
    """Wilder smoothing, as Pine's ta.rma. Seeded with a simple mean.

    Seeds on the first bar whose trailing window of n values contains no NaN,
    exactly as the reference does, then recurses over every non-NaN value.
    """
    x = _arr(src)
    out = np.full(len(x), NAN)
    run = None
    for i in range(len(x)):
        v = float(x[i])
        if _isnan(v):
            continue
        if run is None:
            if i + 1 >= n:
                window = x[i - n + 1:i + 1]
                if not np.isnan(window).any():
                    run = float(sum(float(w) for w in window)) / n
                    out[i] = run
            continue
        run = (run * (n - 1) + v) / n
        out[i] = run
    return out


def wma(src, n: int) -> np.ndarray:
    """Linearly weighted average, weights 1..n (newest heaviest)."""
    x = _arr(src)
    out = np.full(len(x), NAN)
    if n <= 0 or len(x) < n:
        return out
    w = np.arange(1, n + 1, dtype="float64")
    denom = w.sum()
    # A sliding dot product. NaN anywhere in the window yields NaN, which is
    # the right answer for a warm-up window.
    win = np.lib.stride_tricks.sliding_window_view(x, n)
    out[n - 1:] = win @ w / denom
    return out


def hma(src, n: int) -> np.ndarray:
    """Hull MA: wma(2*wma(n/2) - wma(n), round(sqrt(n))).

    Lengths follow the spec's coercion rules (floor for n/2, half-up rounding
    for the square root) so that this and the same formula written out as
    spec primitives give identical numbers.
    """
    half = max(1, int(math.floor(n / 2)))
    root = max(1, round_half_up(math.sqrt(n)))
    return wma(2.0 * wma(src, half) - wma(src, n), root)


def vwma(src, volume, n: int) -> np.ndarray:
    num = sma(_arr(src) * _arr(volume), n)
    den = sma(volume, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(den > 0, num / den, NAN)


def round_half_up(v: float) -> int:
    """Pine's math.round: halves go away from zero. Python's round() is banker's."""
    return int(math.floor(v + 0.5)) if v >= 0 else -int(math.floor(-v + 0.5))


# -- series operations ----------------------------------------------------------

def shift(src, n: int) -> np.ndarray:
    """Value n bars ago (src[n] in Pine). Negative n is refused: that is the future."""
    if n < 0:
        raise ValueError("shift must be >= 0; a negative shift reads the future")
    x = _arr(src)
    if n == 0:
        return x.copy()
    out = np.full(len(x), NAN)
    out[n:] = x[:-n]
    return out


def change(src, n: int = 1) -> np.ndarray:
    return _arr(src) - shift(src, n)


def roc(src, n: int) -> np.ndarray:
    prev = shift(src, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(prev != 0, (_arr(src) - prev) / prev * 100.0, NAN)


def highest(src, n: int) -> np.ndarray:
    return pd.Series(_arr(src)).rolling(n, min_periods=n).max().to_numpy()


def lowest(src, n: int) -> np.ndarray:
    return pd.Series(_arr(src)).rolling(n, min_periods=n).min().to_numpy()


def rolling_sum(src, n: int) -> np.ndarray:
    return pd.Series(_arr(src)).rolling(n, min_periods=n).sum().to_numpy()


def stdev(src, n: int) -> np.ndarray:
    """Population standard deviation, as Pine's ta.stdev (biased, ddof=0)."""
    return pd.Series(_arr(src)).rolling(n, min_periods=n).std(ddof=0).to_numpy()


def barssince(cond) -> np.ndarray:
    c = np.asarray(cond, dtype=bool)
    out = np.full(len(c), NAN)
    last = -1
    for i in range(len(c)):
        if c[i]:
            last = i
        if last >= 0:
            out[i] = i - last
    return out


# -- true range family ----------------------------------------------------------

def true_range(high, low, close, *, first_bar_hl: bool = True) -> np.ndarray:
    """max(h-l, |h-prev c|, |l-prev c|).

    The first bar has no previous close. Pine's ta.atr uses h-l there
    (`first_bar_hl=True`); the reference DMI leaves it missing, and parity
    needs that too.
    """
    h, l, c = _arr(high), _arr(low), _arr(close)
    pc = shift(c, 1)
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    tr[0] = (h[0] - l[0]) if first_bar_hl else NAN
    return tr


def atr(high, low, close, n: int) -> np.ndarray:
    return rma(true_range(high, low, close), n)


def dmi(high, low, close, n: int) -> dict[str, np.ndarray]:
    """+DI, -DI and ADX, matching ta.dmi(n, n). Loop-for-loop the reference."""
    h, l, c = _arr(high), _arr(low), _arr(close)
    N = len(c)
    tr = np.full(N, NAN)
    pdm = np.full(N, NAN)
    ndm = np.full(N, NAN)
    for i in range(1, N):
        up = float(h[i]) - float(h[i - 1])
        dn = float(l[i - 1]) - float(l[i])
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(float(h[i]) - float(l[i]), abs(float(h[i]) - float(c[i - 1])),
                    abs(float(l[i]) - float(c[i - 1])))
    a = rma(tr, n)
    sp, sm = rma(pdm, n), rma(ndm, n)
    pdi = np.full(N, NAN)
    ndi = np.full(N, NAN)
    dx = np.full(N, NAN)
    for i in range(N):
        ai = float(a[i])
        if _isnan(ai) or ai == 0 or _isnan(float(sp[i])) or _isnan(float(sm[i])):
            continue
        p = 100 * float(sp[i]) / ai
        m = 100 * float(sm[i]) / ai
        pdi[i], ndi[i] = p, m
        s = p + m
        dx[i] = 100 * abs(p - m) / s if s else 0.0
    return {"plus": pdi, "minus": ndi, "adx": rma(dx, n)}


# -- oscillators ------------------------------------------------------------------

def rsi(src, n: int) -> np.ndarray:
    """Wilder RSI, as ta.rsi. All-gain windows read 100, all-loss read 0."""
    d = change(src, 1)
    up = rma(np.where(np.isnan(d), NAN, np.maximum(d, 0.0)), n)
    dn = rma(np.where(np.isnan(d), NAN, np.maximum(-d, 0.0)), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100.0 - 100.0 / (1.0 + up / dn)
    out = np.where(dn == 0, 100.0, out)
    out = np.where((up == 0) & (dn != 0), 0.0, out)
    return np.where(np.isnan(up) | np.isnan(dn), NAN, out)


def macd(src, fast: int, slow: int, signal: int) -> dict[str, np.ndarray]:
    line = ema(src, fast) - ema(src, slow)
    sig = ema(line, signal)
    return {"macd": line, "signal": sig, "hist": line - sig}


def bbands(src, n: int, mult: float) -> dict[str, np.ndarray]:
    mid = sma(src, n)
    dev = stdev(src, n) * mult
    return {"upper": mid + dev, "mid": mid, "lower": mid - dev}


def stoch(high, low, close, n: int, smooth_k: int, d: int) -> dict[str, np.ndarray]:
    hh, ll = highest(high, n), lowest(low, n)
    rng = hh - ll
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = np.where(rng > 0, 100.0 * (_arr(close) - ll) / rng, NAN)
    k = sma(raw, smooth_k) if smooth_k > 1 else raw
    return {"k": k, "d": sma(k, d)}


def cci(src, n: int) -> np.ndarray:
    x = _arr(src)
    mean = sma(x, n)
    md = pd.Series(x).rolling(n, min_periods=n).apply(
        lambda w: np.mean(np.abs(w - w.mean())), raw=True).to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(md > 0, (x - mean) / (0.015 * md), NAN)


def mfi(high, low, close, volume, n: int) -> np.ndarray:
    tp = (_arr(high) + _arr(low) + _arr(close)) / 3.0
    flow = tp * _arr(volume)
    d = change(tp, 1)
    pos = rolling_sum(np.where(d > 0, flow, 0.0), n)
    neg = rolling_sum(np.where(d < 0, flow, 0.0), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100.0 - 100.0 / (1.0 + pos / neg)
    out = np.where(neg == 0, 100.0, out)
    out[:n] = NAN                      # the first change is undefined
    return out


def williams_r(high, low, close, n: int) -> np.ndarray:
    hh, ll = highest(high, n), lowest(low, n)
    rng = hh - ll
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(rng > 0, 100.0 * (_arr(close) - hh) / rng, NAN)


def obv(close, volume) -> np.ndarray:
    d = np.sign(np.nan_to_num(change(close, 1), nan=0.0))
    return np.cumsum(d * _arr(volume))


def vwap(high, low, close, volume, ts) -> np.ndarray:
    """Anchored to each UTC day, as a session VWAP on a 24/7 market."""
    tp = (_arr(high) + _arr(low) + _arr(close)) / 3.0
    v = _arr(volume)
    day = np.asarray(ts, dtype="int64") // 86400
    df = pd.DataFrame({"pv": tp * v, "v": v, "day": day})
    g = df.groupby("day", sort=False)
    num = g["pv"].cumsum().to_numpy()
    den = g["v"].cumsum().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(den > 0, num / den, tp)


# -- channels and trailing systems --------------------------------------------------

def donchian(high, low, n: int) -> dict[str, np.ndarray]:
    up, lo = highest(high, n), lowest(low, n)
    return {"upper": up, "lower": lo, "mid": (up + lo) / 2.0}


def keltner(high, low, close, n: int, mult: float, atr_len: int) -> dict[str, np.ndarray]:
    mid = ema(close, n)
    band = atr(high, low, close, atr_len) * mult
    return {"upper": mid + band, "mid": mid, "lower": mid - band}


def supertrend(high, low, close, atr_len: int, mult: float) -> dict[str, np.ndarray]:
    """ta.supertrend. `dir` is +1 in an uptrend and -1 in a downtrend.

    Pine returns -1 for UP, which is a well-known trap; the sign here is the
    intuitive one and the spec documents it.
    """
    h, l, c = _arr(high), _arr(low), _arr(close)
    a = atr(h, l, c, atr_len)
    mid = (h + l) / 2.0
    N = len(c)
    line = np.full(N, NAN)
    dirn = np.full(N, NAN)
    up_prev = dn_prev = NAN
    d_prev = 1.0
    for i in range(N):
        if _isnan(float(a[i])):
            continue
        up = mid[i] - mult * a[i]          # support, used in an uptrend
        dn = mid[i] + mult * a[i]          # resistance, used in a downtrend
        if not _isnan(up_prev) and c[i - 1] > up_prev:
            up = max(up, up_prev)
        if not _isnan(dn_prev) and c[i - 1] < dn_prev:
            dn = min(dn, dn_prev)
        if _isnan(up_prev):
            d = 1.0
        elif d_prev < 0 and c[i] > dn_prev:
            d = 1.0
        elif d_prev > 0 and c[i] < up_prev:
            d = -1.0
        else:
            d = d_prev
        line[i] = up if d > 0 else dn
        dirn[i] = d
        up_prev, dn_prev, d_prev = up, dn, d
    return {"line": line, "dir": dirn}


def psar(high, low, start: float, inc: float, maximum: float) -> np.ndarray:
    """Wilder's parabolic SAR."""
    h, l = _arr(high), _arr(low)
    N = len(h)
    out = np.full(N, NAN)
    if N < 2:
        return out
    long = h[1] >= h[0]
    af = start
    ep = h[1] if long else l[1]
    sar = l[0] if long else h[0]
    out[1] = sar
    for i in range(2, N):
        sar = sar + af * (ep - sar)
        if long:
            sar = min(sar, l[i - 1], l[i - 2])
            if l[i] < sar:
                long, sar, ep, af = False, ep, l[i], start
            elif h[i] > ep:
                ep, af = h[i], min(af + inc, maximum)
        else:
            sar = max(sar, h[i - 1], h[i - 2])
            if h[i] > sar:
                long, sar, ep, af = True, ep, h[i], start
            elif l[i] < ep:
                ep, af = l[i], min(af + inc, maximum)
        out[i] = sar
    return out
