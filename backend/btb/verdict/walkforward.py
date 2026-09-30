"""Walk-forward: would picking settings this way have worked in real time?

Anchored. The span is cut into folds + 1 equal parts; before each fold, the
combination with the best compounded return on everything so far is picked,
and its returns on the fold -- data it was not picked on -- are recorded.
Stitched together, the folds are one honest out-of-sample equity curve.

No new backtests: every fold is a slice of the returns matrix, whose columns
were each run once over the whole span. A fold never reads a day after it.
"""
from __future__ import annotations

import numpy as np


def run(R: np.ndarray, bench: np.ndarray | None = None, folds: int = 5) -> dict | None:
    t, n = R.shape
    if t < (folds + 1) * 20:
        return None
    edges = np.linspace(0, t, folds + 2).astype(int)
    picks, rets, bh = [], [], []
    for i in range(1, folds + 1):
        past = R[:edges[i]]
        pick = int(np.prod(1 + past, axis=0).argmax())
        seg = slice(edges[i], edges[i + 1])
        picks.append(pick)
        rets.append(float(np.prod(1 + R[seg, pick]) - 1))
        if bench is not None:
            bh.append(float(np.prod(1 + bench[seg]) - 1))
    total = float(np.prod([1 + r for r in rets]) - 1)
    return {
        "oos_pct": round(total * 100, 2),
        "bh_pct": round((float(np.prod([1 + r for r in bh])) - 1) * 100, 2) if bh else None,
        "folds": folds,
        "folds_positive": int(sum(r > 0 for r in rets)),
        "fold_pct": [round(r * 100, 2) for r in rets],
        "picks": picks,
        "pick_changed": int(sum(a != b for a, b in zip(picks, picks[1:]))),
        "start_day": int(edges[1]),
    }
