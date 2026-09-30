"""Probability of backtest overfitting, by combinatorially symmetric CV.

Bailey, Borwein, Lopez de Prado & Zhu (2016). Cut the span into S blocks and
take every way of choosing half of them as "in sample" (the rest out of
sample): C(16, 8) = 12,870 splits. In each, find the combination with the best
in-sample Sharpe and see where it ranks out of sample. PBO is the share of
splits in which that winner lands in the bottom half.

It scores the SELECTION PROCEDURE -- "pick the best of this grid" -- not any
one setting, so every row of a grid shares it. It is the rigorous version of
the sweep's single-split train/test rank correlation.

Block sums are computed once; any split's Sharpe is then a sum of block sums,
so all 12,870 splits are three matrix products.
"""
from __future__ import annotations

from itertools import combinations

import numpy as np

MIN_BLOCK_DAYS = 20


def blocks_for(t: int) -> int | None:
    """16 blocks with ~3 years of days, 8 with less, none under 160 days."""
    if t >= 16 * 63:
        return 16
    if t >= 8 * MIN_BLOCK_DAYS:
        return 8
    return None


def pbo(R: np.ndarray, blocks: int | None = None) -> dict | None:
    """PBO over the columns of R [T, N]. None when there is too little data
    or fewer than two combinations to choose between."""
    t, n = R.shape
    s = blocks or blocks_for(t)
    if s is None or n < 2 or s % 2 or not R.any():
        return None                          # nothing to choose, or nothing ever traded
    size = t // s
    R = R[t - size * s:]                    # drop the oldest remainder
    B = R.reshape(s, size, n)
    bsum, bsq = B.sum(axis=1), (B * B).sum(axis=1)      # [S, N]

    C = np.array([[i in c for i in range(s)] for c in combinations(range(s), s // 2)],
                 dtype=float)                           # [K, S]
    m = size * (s // 2)
    is_sr = _sharpe(C @ bsum, C @ bsq, m)
    oos_sr = _sharpe((1 - C) @ bsum, (1 - C) @ bsq, m)   # [K, N]

    best = is_sr.argmax(axis=1)
    k = np.arange(len(C))
    v = oos_sr[k, best][:, None]
    # Relative rank of the IS winner out of sample, ties split evenly.
    below = (oos_sr < v).sum(axis=1)
    ties = (oos_sr == v).sum(axis=1) - 1
    w = (below + 0.5 * ties + 1) / (n + 1)
    logit = np.log(w / (1 - w))
    return {"value": round(float((logit <= 0).mean()), 4), "blocks": s,
            "splits": int(len(C)), "block_days": int(size),
            # Coarse histogram of the logits, for the Sweeps tab.
            "logits": np.histogram(logit, bins=20, range=(-4, 4))[0].tolist()}


def _sharpe(total, sq, m):
    mean = total / m
    var = (sq - m * mean * mean) / (m - 1)
    sd = np.sqrt(np.maximum(var, 0))
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(sd > 1e-12, mean / sd, 0.0)
