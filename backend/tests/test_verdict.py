"""Tests for the verdict engine.

Each check is pinned against a case whose answer is known before it runs:

    test_dsr_matches_paper_example        Bailey & Lopez de Prado's worked example
    test_pbo_*                            noise ~0.5, a planted edge ~0, no trades -> None
    test_walk_forward_never_looks_ahead   a fold's pick ignores every later day
    test_label_*                          the rule table, including thin evidence
    test_verdict_ignores_the_holdout      rewriting the newest year changes nothing
    test_verdict_is_deterministic
    test_random_walks_are_not_robust      the false-positive guard, small version
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from btb import verdict as V
from btb.engine import backtest as B
from btb.engine import jobs as JB
from btb.engine import library as L
from btb.engine import windows as W
from btb.engine.fills import Costs
from btb.verdict import cscv, dsr, rules, walkforward

FIXTURE = Path(__file__).parent / "fixtures" / "btcusd_4h.csv.gz"
COSTS = Costs(0.1, 5.0)


@pytest.fixture(scope="module")
def btc():
    return pd.read_csv(FIXTURE).rename(columns={"time": "ts"})


def spec(name):
    return L.load_all()[name]


def test_dsr_matches_paper_example():
    # SR 2.5 annualised, 1,250 daily returns, skew -3, kurtosis 10, 100 trials
    # whose annualised Sharpes vary by 0.5: the paper reports DSR = 0.9004.
    sr, var = 2.5 / math.sqrt(250), 0.5 / 250
    sr0 = dsr.expected_max(100, var)
    assert dsr.psr(sr, sr0, 1250, -3, 10) == pytest.approx(0.9004, abs=1e-4)


def test_dsr_one_trial_is_not_deflated():
    assert dsr.expected_max(1, 0.01) == 0.0


def test_pbo_of_pure_noise_is_about_half():
    R = np.random.default_rng(1).normal(0, 0.01, (2000, 40))
    assert 0.3 < cscv.pbo(R)["value"] < 0.7


def test_pbo_with_a_planted_edge_is_near_zero():
    R = np.random.default_rng(2).normal(0, 0.01, (2000, 40))
    R[:, 7] += 0.002
    assert cscv.pbo(R)["value"] < 0.05


def test_pbo_needs_trades_and_data():
    assert cscv.pbo(np.zeros((2000, 10))) is None
    assert cscv.pbo(np.random.default_rng(3).normal(0, 0.01, (100, 10))) is None


def test_walk_forward_never_looks_ahead():
    rng = np.random.default_rng(4)
    R = rng.normal(0, 0.01, (1200, 12))
    a = walkforward.run(R)
    edge = np.linspace(0, len(R), 7).astype(int)
    # Rewrite everything from the last fold on: earlier picks cannot change.
    R2 = R.copy()
    R2[edge[5]:] = rng.normal(0, 0.05, R2[edge[5]:].shape)
    b = walkforward.run(R2)
    assert a["picks"][:5] == b["picks"][:5]


def test_walk_forward_finds_a_planted_edge():
    R = np.random.default_rng(5).normal(0, 0.01, (1200, 12))
    R[:, 3] += 0.003
    wf = walkforward.run(R)
    assert set(wf["picks"]) == {3} and wf["folds_positive"] == 5


def _checks(pbo=0.1, wf=10.0, pos=5, d=0.99, trades=100, thin=False):
    g = rules.grid_checks({"oos_pct": wf, "folds_positive": pos, "folds": 5},
                          {"value": pbo, "logits": []}, years=5, thin=thin)
    return {**g, **rules.row_checks({"value": d, "trials": 81}, trades)}


@pytest.mark.parametrize("kw,label", [
    ({}, "ROBUST"),
    ({"pbo": 0.6}, "OVERFIT"),
    ({"wf": -1.0}, "OVERFIT"),
    ({"d": 0.3}, "OVERFIT"),
    ({"pbo": 0.3}, "WEAK"),
    ({"d": 0.8}, "WEAK"),
    ({"pos": 2}, "WEAK"),
    ({"trades": 10}, "WEAK"),
    ({"thin": True}, "WEAK"),
])
def test_label_rules(kw, label):
    c = _checks(**kw)
    assert rules.label(c) == label
    assert rules.reason(c)


def test_label_missing_check_is_never_robust():
    g = rules.grid_checks(None, None, years=5, thin=False)
    c = {**g, **rules.row_checks({"value": 0.99, "trials": 8}, 100)}
    assert rules.label(c) == "WEAK"


def test_verdict_ignores_the_holdout(btc):
    s = spec("ema_cross")
    a = V.evaluate(s, B.Series.from_frame(btc, "4h"), COSTS)
    hold = W.split(btc["ts"].to_numpy()).holdout
    df = btc.copy()
    m = df["ts"] >= hold.start
    df.loc[m, ["open", "high", "low", "close"]] *= 3.0
    b = V.evaluate(s, B.Series.from_frame(df, "4h"), COSTS)
    for v in (a, b):
        v.pop("seconds")
    assert a == b


def test_verdict_is_deterministic_and_complete(btc):
    s = spec("ema_cross_adx")
    series = B.Series.from_frame(btc, "4h")
    sw = JB.sweep_series(s, series, COSTS)
    a, b = V.evaluate(s, series, COSTS, sw), V.evaluate(s, series, COSTS, sw)
    a.pop("seconds"), b.pop("seconds")
    assert a == b
    assert a["label"] in rules.ORDER and a["reason"]
    assert set(a["checks"]) == {"walk_forward", "pbo", "sample", "dsr", "trades"}
    assert a["rows"]["1"]["label"] == a["label"]
    assert json.dumps(a)                      # stored as jsonb


def test_random_walks_are_not_robust(btc):
    # The full calibration (50 walks x every strategy) is a CLI run; this is the
    # small always-on version: 5 random walks with BTC-like volatility.
    s = spec("ema_cross")
    rng = np.random.default_rng(6)
    ts = btc["ts"].to_numpy()
    for _ in range(5):
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, len(ts))))
        df = pd.DataFrame({"ts": ts, "open": c, "high": c * 1.01, "low": c * 0.99,
                           "close": c, "volume": 1.0})
        assert V.evaluate(s, B.Series.from_frame(df, "4h"), COSTS)["label"] != "ROBUST"
