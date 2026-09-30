"""The rule table: check results -> ROBUST / WEAK / OVERFIT, and why.

Thresholds are data, in one dict, so calibration changes numbers rather than
code. Each check reports pass / warn / fail / na with its own sentence:

    any fail            -> OVERFIT
    any warn or na      -> WEAK      (thin or missing evidence is never ROBUST)
    otherwise           -> ROBUST

Walk-forward, PBO and the sample judge the grid, so every row shares them; the
deflated Sharpe and the trade count are each row's own. A row's label is taken
over both, so no row can be ROBUST when picking from its grid is overfit -- a
lone good-looking setting among many is exactly the lucky winner these checks
exist to catch.

Bump VERSION whenever a threshold changes: a stored verdict keeps the version
it was scored under.
"""
from __future__ import annotations

VERSION = 1

T = {
    "pbo_fail": 0.5,        # winner below the OOS median at least half the time
    "pbo_warn": 0.2,
    "dsr_fail": 0.5,
    "dsr_pass": 0.95,
    "wf_min_positive": 3,   # of 5 folds
    "min_trades": 30,
}

ORDER = ["OVERFIT", "WEAK", "ROBUST"]
# Most serious first: the reason shown is the first non-passing check here.
SERIOUS = [("pbo", "fail"), ("walk_forward", "fail"), ("dsr", "fail"),
           ("sample", "warn"), ("trades", "warn"), ("pbo", "warn"), ("dsr", "warn"),
           ("walk_forward", "warn"), ("pbo", "na"), ("walk_forward", "na"), ("dsr", "na")]


def grid_checks(wf: dict | None, pb: dict | None, *, years: float, thin: bool) -> dict:
    out = {}
    if wf is None:
        out["walk_forward"] = {"status": "na"}
    else:
        s = ("fail" if wf["oos_pct"] <= 0 else
             "warn" if wf["folds_positive"] < T["wf_min_positive"] else "pass")
        out["walk_forward"] = {"status": s, **wf}
    if pb is None:
        out["pbo"] = {"status": "na"}
    else:
        v = pb["value"]
        s = "fail" if v >= T["pbo_fail"] else "warn" if v >= T["pbo_warn"] else "pass"
        out["pbo"] = {"status": s, **{k: x for k, x in pb.items() if k != "logits"}}
    out["sample"] = {"status": "warn" if thin else "pass", "years": round(years, 2)}
    return out


def row_checks(ds: dict, trades: int) -> dict:
    v = ds["value"]
    s = ("na" if v is None else "fail" if v < T["dsr_fail"] else
         "warn" if v < T["dsr_pass"] else "pass")
    return {"dsr": {"status": s, **ds},
            "trades": {"status": "warn" if trades < T["min_trades"] else "pass",
                       "count": int(trades)}}


def label(checks: dict) -> str:
    st = {c["status"] for c in checks.values()}
    return "OVERFIT" if "fail" in st else "WEAK" if st & {"warn", "na"} else "ROBUST"


def reason(checks: dict) -> str:
    for name, status in SERIOUS:
        c = checks.get(name)
        if c and c["status"] == status:
            return _say(name, status, c)
    n = checks["dsr"].get("trials")
    return ("The edge held up in every check: walk-forward, the overfitting test, "
            f"and after discounting for {n} settings tried.")


def _say(name: str, status: str, c: dict) -> str:
    if status == "na":
        return "Too little data to run every check, so this can't be called robust."
    if name == "pbo":
        p = round(c["value"] * 100)
        if status == "fail":
            return (f"The setting that won in training finished in the bottom half "
                    f"on unseen data {p}% of the time.")
        return (f"The winning setting mostly held up on unseen data, but not reliably: "
                f"a {p}% chance the pick is overfit.")
    if name == "walk_forward":
        if status == "fail":
            return (f"Picking settings this way in real time would have returned "
                    f"{c['oos_pct']:+.1f}%.")
        return (f"Only {c['folds_positive']} of {c['folds']} walk-forward periods "
                f"made money.")
    if name == "dsr":
        if status == "fail":
            return (f"After {c['trials']} settings tried, a Sharpe this high is about "
                    f"what luck alone would produce.")
        return (f"Suggestive but not convincing after {c['trials']} settings tried "
                f"(deflated Sharpe {c['value']:.2f}).")
    if name == "sample":
        return "Less than two years of data: too little to judge."
    if name == "trades":
        return f"Only {c['count']} trades: too few to judge."
    return ""
