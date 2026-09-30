"""Deflated Sharpe ratio: a Sharpe discounted for how many settings were tried.

Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio". Try N settings
with no skill between them and the best one still shows a positive Sharpe,
growing with N. The deflated Sharpe is the probability that the true Sharpe
beats that expected-best-of-N benchmark, given the sample's length, skew and
fat tails -- so it reads as "the chance this edge is real", between 0 and 1.

Everything here is per period (daily), never annualised: the formulas assume
the Sharpe and T are measured on the same clock.

N is the raw number of grid combinations. Neighbouring settings are close to
duplicates, so this overstates the independent tries and errs harsh -- a
deliberate choice: it is exact, and it is what the screen can say ("after 81
settings tried").
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

EULER = 0.5772156649015329
_N = NormalDist()


def sharpe(r: np.ndarray) -> float:
    r = np.asarray(r, dtype=float)
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    return float(r.mean() / sd) if sd > 0 else 0.0


def sharpes(R: np.ndarray) -> np.ndarray:
    """Per-column Sharpe; a column that never moved (never traded) is 0."""
    sd = R.std(axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(sd > 0, R.mean(axis=0) / sd, 0.0)


def expected_max(n_trials: int, var: float) -> float:
    """Expected maximum Sharpe of `n_trials` skill-less trials whose Sharpes
    have variance `var` (the False Strategy Theorem)."""
    if n_trials < 2 or var <= 0:
        return 0.0
    z = _N.inv_cdf
    return math.sqrt(var) * ((1 - EULER) * z(1 - 1 / n_trials)
                             + EULER * z(1 - 1 / (n_trials * math.e)))


def psr(sr: float, benchmark: float, t: int, skew: float, kurt: float) -> float:
    """Probabilistic Sharpe: P(true Sharpe > benchmark). `kurt` is plain
    (not excess) kurtosis -- 3 for a normal distribution."""
    if t < 3:
        return float("nan")
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if denom <= 0:
        return float("nan")
    return _N.cdf((sr - benchmark) * math.sqrt(t - 1) / math.sqrt(denom))


def moments(r: np.ndarray) -> tuple[float, float]:
    """Sample skew and plain kurtosis; (0, 3) for a flat series."""
    r = np.asarray(r, dtype=float)
    sd = r.std()
    if sd == 0:
        return 0.0, 3.0
    z = (r - r.mean()) / sd
    return float((z ** 3).mean()), float((z ** 4).mean())


def deflated(r: np.ndarray, *, n_trials: int, trial_var: float) -> dict:
    """The deflated Sharpe of one return series, against `n_trials` tries."""
    sr = sharpe(r)
    sk, ku = moments(r)
    sr0 = expected_max(n_trials, trial_var)
    return {"value": _round(psr(sr, sr0, len(r), sk, ku)), "sharpe": round(sr, 5),
            "sr0": round(sr0, 5), "trials": int(n_trials), "t": int(len(r)),
            "skew": round(sk, 3), "kurtosis": round(ku, 3)}


def _round(v: float) -> float | None:
    return None if v != v else round(v, 4)
