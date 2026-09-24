"""The parity gate: the engine must reproduce the reference engine EXACTLY.

`reference_engine.py` is the pure-Python EMA-cross + dual-ADX loop from the
earlier prototype, which was reconciled bar by bar against TradingView and
drives the live trader. The vectorised engine, running the same strategy as a
JSON spec, must produce the same entries and exits on the same bars at the
same prices -- not approximately, exactly.

Run this first after any change to indicators, timeframes, signals or fills.
A failure here means some other test's green is not worth trusting.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import reference_engine as R
from btb.engine import backtest as B
from btb.engine import library as L
from btb.engine.fills import Costs

FIXTURE = Path(__file__).parent / "fixtures" / "btcusd_4h.csv.gz"


@pytest.fixture(scope="module")
def btc():
    df = pd.read_csv(FIXTURE).rename(columns={"time": "ts"})
    cols = [df[k].tolist() for k in ("ts", "open", "high", "low", "close")]
    return df, cols


def _reference_settings():
    """The engine options that model what the reference does.

    Each one is a known simplification in the reference, which the real
    engine does differently by default -- see fills.py.
    """
    return dict(costs=Costs(commission_pct=0.10, slippage_bps=0.0),
                stop_fill="exact", close_open=False, liquidate=False)


@pytest.mark.parametrize("params", [
    {},                                                       # the live defaults
    {"fast": 10, "slow": 50, "adx_min": 18, "stop_pct": 0},   # no stop
    {"fast": 30, "slow": 100, "adx_min": 28, "stop_pct": 5},
])
def test_trades_match_reference_exactly(btc, params):
    df, (ts, o, h, l, c) = btc
    spec = L.get("ema_cross_adx")
    p = {**spec["defaults"], **params}
    ref_events = list(R.walk(ts, o, h, l, c, fast=p["fast"], slow=p["slow"],
                             adx_len=p["adx_len"], adx_min=p["adx_min"],
                             adx_min_ltf=p["adx_min_ltf"], htf_mult=3,
                             stop_pct=p["stop_pct"]))
    ref = R.run(ts, o, h, l, c, fast=p["fast"], slow=p["slow"], adx_len=p["adx_len"],
                adx_min=p["adx_min"], adx_min_ltf=p["adx_min_ltf"], htf_mult=3,
                stop_pct=p["stop_pct"], commission=0.10)

    r = B.run(spec, params, B.Series.from_frame(df, "4h"), **_reference_settings())
    mine = r.fills.events

    # walk() yields every signal regardless of equity; run() stops consuming
    # them at ruin (the no-stop case shorts BTC's 2013 rally and goes under).
    # Compare against what run() actually traded.
    assert r.fills.ruin == ref["ruin"]
    if ref["ruin"]:
        ref_events = ref_events[:len(mine)]
    assert len(mine) == len(ref_events)
    for a, b in zip(ref_events, mine):
        # kind, bar, side and fill price: all four, exactly.
        assert (a.kind, a.i, a.direction, a.price) == b, f"first divergence at bar {a.i}"
    assert r.metrics["trades"] == ref["trades"]
    assert r.fills.final == pytest.approx(ref["final"], rel=1e-12)
    assert r.metrics["win_pct"] == pytest.approx(ref["win_pct"], abs=1e-4)
    if ref["pf"] is not None:
        assert r.metrics["profit_factor"] == pytest.approx(ref["pf"], abs=1e-3)


def test_gap_aware_stops_are_never_better_than_exact(btc):
    """The one intended difference from the reference: a stop the bar gaps
    through fills at the open. That can only ever cost money."""
    df, _ = btc
    spec = L.get("ema_cross_adx")
    series = B.Series.from_frame(df, "4h")
    exact = B.run(spec, None, series, costs=Costs(0.1, 0), stop_fill="exact", close_open=False)
    gap = B.run(spec, None, series, costs=Costs(0.1, 0), stop_fill="gap", close_open=False)
    assert gap.fills.final <= exact.fills.final


def test_library_spec_matches_reference_defaults():
    """If someone edits the library spec's defaults, the parity above silently
    starts testing something other than the live configuration."""
    d = json.loads((L.LIB_DIR / "ema_cross_adx.json").read_text())["defaults"]
    assert d == {"fast": 20, "slow": 62, "adx_len": 9, "adx_min": 23,
                 "adx_min_ltf": 28, "stop_pct": 4.1}
