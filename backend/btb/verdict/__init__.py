"""The verdict engine: a 0-100 score for a sweep, and why.

    from btb import verdict
    v = verdict.evaluate(spec, series, costs, sweep_result)
    v["score"], v["factors"], v["reason"], v["checks"], v["rows"]

Three independent checks over one returns matrix (returns.py):

    walkforward.py   would picking settings this way have worked in real time?
    cscv.py          how often does the in-sample winner fall below median OOS?
    dsr.py           is this Sharpe better than the best of N skill-less tries?

rules.py turns them into the score. Pure numpy: the worker calls this at the
end of a sweep job, the CLI calls it on a CSV, and neither touches the
database from here.

Always scored on the DEFAULT windows. A user can drag the train/test windows
to explore, but a score that moved with them could be dragged until it
looked good.
"""
from __future__ import annotations

import json
import time

import numpy as np

from ..engine import backtest as B
from ..engine import windows as W
from ..engine.fills import Costs
from . import cscv, dsr, returns, rules, walkforward


def evaluate(spec: dict, series: B.Series, costs: Costs, sweep: dict | None = None, *,
             fill: str = "close", split: W.Split | None = None) -> dict:
    """Score a grid. `sweep` is sweep_series' result; every test-ranked row
    gets its own score, and its #1 row is the headline."""
    t0 = time.perf_counter()
    split = split or W.split(series.ts)
    mx = returns.build(spec, series, costs, split=split, fill=fill)
    n = len(mx.combos)
    years = len(mx.days) / 365.25

    wf = walkforward.run(mx.R, mx.bench)
    if wf:                                  # which settings each fold traded, for the test page
        wf["pick_params"] = [mx.combos[j] for j in wf["picks"]]
    pb = cscv.pbo(mx.R)
    grid = rules.grid_checks(wf, pb, years=years, thin=split.thin_sample)
    srs = dsr.sharpes(mx.R)
    var = float(srs.var(ddof=1)) if n > 1 else 0.0

    col = {json.dumps(p, sort_keys=True): i for i, p in enumerate(mx.combos)}
    ranked = (sweep or {}).get("rows") or (sweep or {}).get("top") or []
    if ranked:
        order = [(r["test_rank"], col[json.dumps(r["params"], sort_keys=True)]) for r in ranked]
    else:                                   # no sweep given: rank by full-span Sharpe
        order = [(i + 1, int(j)) for i, j in enumerate(np.argsort(-srs))]

    rows = {}
    for rank, j in order:
        checks = {**grid, **rules.row_checks(
            dsr.deflated(mx.R[:, j], n_trials=n, trial_var=var), int(mx.trades[j]))}
        rows[str(rank)] = row_entry(checks, mx.combos[j])
        if rank == order[0][0]:
            head = checks

    sc = rules.score(head)
    return {
        "score": sc, "band": rules.band(sc), "factors": rules.factors(head),
        "reason": rules.reason(head), "checks": head,
        "rows": rows, "combos": n, "days": int(len(mx.days)),
        "pbo_logits": pb["logits"] if pb else None,
        "windows": split.as_dict(), "version": rules.VERSION,
        "seconds": round(time.perf_counter() - t0, 2),
    }


def row_entry(checks: dict, params: dict) -> dict:
    sc = rules.score(checks)
    return {"score": sc, "band": rules.band(sc), "reason": rules.reason(checks),
            "dsr": checks["dsr"]["value"], "params": params}


def rescore(v: dict) -> dict:
    """A stored verdict, re-scored from its stored numbers under the current
    rules -- no re-run. The grid checks are shared; each row brings its own
    deflated Sharpe. Rows never stored their trade count, so each keeps the
    headline's trade check, which only picks the sentence, never the score."""
    head = v["checks"]
    grid = {k: head[k] for k in ("walk_forward", "pbo", "sample")}
    trials = head["dsr"].get("trials")
    rows = {}
    for rank, r in v.get("rows", {}).items():
        checks = {**grid, **rules.row_checks({"value": r.get("dsr"), "trials": trials},
                                             int(head["trades"].get("count", 0)))}
        rows[rank] = row_entry(checks, r["params"])
    sc = rules.score(head)
    out = {k: x for k, x in v.items() if k != "label"}
    out.update(score=sc, band=rules.band(sc), factors=rules.factors(head),
               reason=rules.reason(head), rows=rows, version=rules.VERSION)
    return out
