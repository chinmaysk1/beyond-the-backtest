"""The rule table: check results -> a 0-100 score, and why.

Each check reports pass / warn / fail / na with its own sentence, and each
already yields a probability, so the score is their product:

    score = 100 x DSR x (1 - PBO) x (walk-forward wins + 1) / (folds + 2)

    DSR            P(this setting's true Sharpe > 0), after N settings tried
    1 - PBO        P(the training winner does not land in the bottom half OOS)
    walk-forward   Laplace's rule of succession over the folds: P(next period up)

High only when all three agree; any one at zero vetoes it. The three are not
independent, so the score is a confidence score built from probabilities, not
itself a probability. What counts as high comes from calibration on random
walks (BANDS), not intuition.

A check that could not run counts as no evidence either way: a rule of
succession with nothing observed is 1/2, and PBO on noise is 1/2. A missing
deflated Sharpe (no trades, no variance) is 0 -- nothing to deflate.

Walk-forward and PBO judge the grid, so every row shares them; the deflated
Sharpe is each row's own. No row can score well when picking from its grid is
overfit -- a lone good-looking setting among many is exactly the lucky winner
these checks exist to catch.

The statuses stay: they pick the reason sentence, which names the weakest
check and so explains a low score.

Bump VERSION whenever the formula or a threshold changes: a stored verdict
keeps the version it was scored under, and `python -m btb.engine rescore`
rewrites stored verdicts from their stored numbers.
"""
from __future__ import annotations

VERSION = 2

T = {
    "pbo_fail": 0.5,        # winner below the OOS median at least half the time
    "pbo_warn": 0.2,
    "dsr_fail": 0.5,
    "dsr_pass": 0.95,
    "wf_min_positive": 3,   # of 5 folds
    "min_trades": 30,
}

# Colour bands. `high` is a score at most 5% of luck-only strategies reach
# (random walks with the real series' timestamps and volatility); `mid` one at
# most 25% reach. From `python -m btb.engine calibrate`, 50 walks x the six
# library strategies on each market:
#
#               p75   p95   >= 30          >= 12
#   SPY 1d       11    29   15/300  5.0%   73/300  24%
#   BTC/USD 4h    3    16    4/300  1.3%   23/300   8%
#
# The stricter market sets each cutoff. Re-run calibrate after any change to
# the formula, the library or its grids.
BANDS = {"high": 30, "mid": 12}
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


def factors(checks: dict) -> dict:
    """The three probabilities the score multiplies, each in [0, 1]."""
    d = checks["dsr"].get("value")
    pb = checks["pbo"].get("value") if checks["pbo"]["status"] != "na" else None
    wf = checks["walk_forward"]
    wins, folds = ((wf["folds_positive"], wf["folds"]) if wf["status"] != "na"
                   else (0, 0))
    return {"dsr": round(float(d), 4) if d is not None else 0.0,
            "pbo": round(1 - float(pb), 4) if pb is not None else 0.5,
            "walk_forward": round((wins + 1) / (folds + 2), 4)}


def score(checks: dict) -> int:
    f = factors(checks)
    return int(round(100 * f["dsr"] * f["pbo"] * f["walk_forward"]))


def band(s: int) -> str:
    return "high" if s >= BANDS["high"] else "mid" if s >= BANDS["mid"] else "low"


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
