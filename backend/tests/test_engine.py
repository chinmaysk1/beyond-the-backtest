"""Tests for the backtest engine, beyond parity.

Each pins a way a backtest can report a number the strategy would not have
made, or a bug that actually occurred while building this:

    test_no_lookahead_*                   future bars never change past signals
    test_htf_value_is_previous_bucket     the 12h filter never sees its own bar
    test_htf_grouping_ignores_lead_in     buckets by wall clock, not array index
    test_more_cost_never_helps            costs are applied, and in one direction
    test_stop_gap_fills_at_open           a gap through the stop is not a stop fill
    test_unstopped_short_is_liquidated    -184% "returns" before this existed
    test_memo_is_keyed_on_inlined_nodes   "fast" meant different things per combo
    test_out_key_is_not_a_reference       bbands out:"upper" read as a cycle
    test_partial_bar_is_never_read        the forming bar stays out of research
    test_sweep_resumes_without_rerunning  recorded work is recognised
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from btb.engine import backtest as B
from btb.engine import indicators as I
from btb.engine import library as L
from btb.engine import refresh as RF
from btb.engine import spec as S
from btb.engine import windows as W
from btb.engine.fills import Costs
from btb.engine.timeframes import Resampled

FIXTURE = Path(__file__).parent / "fixtures" / "btcusd_4h.csv.gz"
H4 = 14400


@pytest.fixture(scope="module")
def btc():
    df = pd.read_csv(FIXTURE).rename(columns={"time": "ts"})
    return df.iloc[-6000:].reset_index(drop=True)     # recent history is plenty


def bars(closes, *, step=H4, start=0, spread=0.0, opens=None):
    c = np.asarray(closes, dtype=float)
    o = np.asarray(opens, dtype=float) if opens is not None else np.r_[c[0], c[:-1]]
    return pd.DataFrame({"ts": start + np.arange(len(c)) * step, "open": o,
                         "high": np.maximum(o, c) * (1 + spread),
                         "low": np.minimum(o, c) * (1 - spread),
                         "close": c, "volume": np.ones(len(c))})


# -- indicators ------------------------------------------------------------------------

def test_basic_averages_by_hand():
    x = [1, 2, 3, 4, 5]
    assert np.isnan(I.sma(x, 3)[1]) and I.sma(x, 3)[4] == 4
    assert I.wma(x, 3)[4] == pytest.approx((3 * 1 + 4 * 2 + 5 * 3) / 6)
    # rma seeds with the mean of the first n, then (prev*(n-1) + x)/n
    assert I.rma(x, 3)[2] == 2 and I.rma(x, 3)[3] == pytest.approx((2 * 2 + 4) / 3)
    # ema seeds with the first value and reports from the n-th
    e = I.ema(x, 3)
    assert np.isnan(e[1]) and e[2] == pytest.approx(1 + (2 - 1) * .5 + (3 - 1.5) * .5)


def test_rsi_extremes():
    up = np.arange(1, 40, dtype=float)
    assert I.rsi(up, 14)[-1] == 100
    assert I.rsi(up[::-1], 14)[-1] == 0


def test_shift_refuses_the_future():
    with pytest.raises(ValueError):
        I.shift([1, 2, 3], -1)


def test_hma_equals_its_decomposition(btc):
    """The built-in and the AI-style rewrite from primitives must agree, or a
    model that decomposes an indicator changes the strategy by doing so."""
    f = S.Frame.of(btc, "4h")
    ev = S.Evaluator(f)
    for n in (9, 16, 21):
        built = ev.value({"fn": "hma", "src": "close", "len": n})
        tree = {"fn": "wma", "len": {"fn": "round", "x": {"fn": "sqrt", "x": n}},
                "src": {"fn": "sub",
                        "a": {"fn": "mul", "a": 2,
                              "b": {"fn": "wma", "src": "close",
                                    "len": {"fn": "div", "a": n, "b": 2}}},
                        "b": {"fn": "wma", "src": "close", "len": n}}}
        np.testing.assert_array_equal(built, ev.value(tree))


# -- lookahead -------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(L.load_all()))
def test_no_lookahead_in_any_library_strategy(btc, name):
    """Truncating the future must not change any signal in the past.

    Cut points are chosen mid-bucket for the 12h filter, where an HTF value
    computed from an unfinished bucket would show up as a difference.
    """
    spec = L.get(name)
    p = S.resolve_params(spec)
    full = S.signals(spec, p, S.Frame.of(btc, "4h"))
    for cut in (3001, 4002, 5000):
        part = S.signals(spec, p, S.Frame.of(btc.iloc[:cut], "4h"))
        for rule in ("entry_long", "exit_long", "entry_short", "exit_short"):
            np.testing.assert_array_equal(getattr(full, rule)[:cut], getattr(part, rule),
                                          err_msg=f"{name}.{rule} changed at cut {cut}")


def test_htf_value_is_previous_bucket():
    """A 4h bar sees the 12h bar that CLOSED before it, never its own."""
    df = bars(np.arange(1, 13, dtype=float))                 # 12 x 4h = 4 x 12h
    rs = Resampled({k: df[k].to_numpy() for k in df}, 3 * H4)
    base = rs.to_base(rs.frame["close"])
    # buckets close at bars 2, 5, 8, 11 (closes 3, 6, 9, 12)
    assert np.isnan(base[:3]).all()
    assert list(base[3:6]) == [3, 3, 3] and list(base[6:9]) == [6, 6, 6]
    assert list(base[9:12]) == [9, 9, 9]


def test_htf_grouping_ignores_lead_in():
    """One extra bar of lead-in must not regroup every 12h bucket."""
    df = bars(np.arange(1, 40, dtype=float), start=0)
    a = Resampled({k: df[k].to_numpy() for k in df}, 3 * H4)
    b = Resampled({k: df.iloc[1:][k].to_numpy() for k in df}, 3 * H4)
    # b drops the partial leading bucket; after that the buckets are identical
    assert list(a.frame["ts"][1:]) == list(b.frame["ts"])
    assert list(a.frame["close"][1:]) == list(b.frame["close"])


# -- fills and costs ------------------------------------------------------------------------

def test_more_cost_never_helps(btc):
    spec = L.get("ema_cross_adx")
    s = B.Series.from_frame(btc, "4h")
    nets = [B.run(spec, None, s, costs=Costs(c, sl)).metrics["net_pct"]
            for c, sl in ((0, 0), (0.05, 0), (0.1, 5), (0.2, 20))]
    assert nets == sorted(nets, reverse=True)
    zero = B.run(spec, None, s, costs=Costs(0, 0)).metrics
    assert zero["fees"] == 0 and zero["slippage"] == 0


SIMPLE_LONG = {
    "name": "t", "indicators": {},
    "entry_long": {"gt": ["close", "$trigger"]},
    "exit_long": {"lt": ["close", 0]},
    "stop": {"kind": "pct", "value": "$stop"},
    "defaults": {"trigger": 100, "stop": 10},
}


def test_stop_gap_fills_at_open():
    # enter at 101 (bar 1); bar 3 opens at 80, far through the 90.9 stop
    df = bars([99, 101, 100, 85], opens=[99, 99, 101, 80])
    s = B.Series.from_frame(df, "4h")
    gap = B.run(SIMPLE_LONG, None, s, costs=Costs(0, 0))
    exact = B.run(SIMPLE_LONG, None, s, costs=Costs(0, 0), stop_fill="exact")
    assert gap.trades[0]["reason"] == "stop" and gap.trades[0]["exit_px"] == 80
    assert exact.trades[0]["exit_px"] == pytest.approx(101 * 0.9)


def test_fill_at_next_open():
    df = bars([99, 101, 103, 104, 105], opens=[99, 99, 102, 103.5, 104.5])
    s = B.Series.from_frame(df, "4h")
    t = B.run(SIMPLE_LONG, None, s, costs=Costs(0, 0), fill="open").trades[0]
    assert t["entry_ts"] == 2 * H4 and t["entry_px"] == 102     # bar 2's open, not bar 1's close


def test_slippage_moves_fills_against_the_trader():
    df = bars([99, 101, 102, 103])
    t = B.run(SIMPLE_LONG, None, B.Series.from_frame(df, "4h"),
              costs=Costs(0, 100)).trades[0]
    assert t["entry_px"] == pytest.approx(101 * 1.01)
    assert t["exit_px"] == pytest.approx(103 * 0.99) and t["reason"] == "end"


def test_unstopped_short_is_liquidated():
    spec = {"name": "t", "entry_short": {"gt": ["close", 0]},
            "exit_short": {"lt": ["close", 0]}, "defaults": {}}
    df = bars([100, 100, 150, 210, 300, 250], spread=0.01)
    r = B.run(spec, None, B.Series.from_frame(df, "4h"), costs=Costs(0, 0))
    assert r.metrics["ruin"] and r.metrics["net_pct"] == -100
    assert r.trades[-1]["reason"] == "liquidated"
    assert min(r.curve["equity"]) >= 0


def test_open_position_is_closed_at_the_end():
    df = bars([99, 101, 102, 110])
    r = B.run(SIMPLE_LONG, None, B.Series.from_frame(df, "4h"), costs=Costs(0, 0))
    assert r.trades[-1]["reason"] == "end" and r.metrics["net_pct"] > 0


# -- the spec language ----------------------------------------------------------------------

def test_every_library_spec_is_valid_and_named_for_its_file():
    lib = L.load_all()
    assert set(lib) == {"ema_cross", "ema_cross_adx", "donchian_breakout",
                        "bollinger_rsi", "rsi_recovery", "supertrend_trend"}


def test_validation_names_the_exact_path():
    bad = {"name": "x", "indicators": {"a": {"fn": "ema", "src": "clsoe", "len": "$n"}},
           "entry_long": {"all": [{"gt": ["a", 1]}, {"crossover": ["a", 1]}]},
           "exit_long": {"lt": ["a", {"fn": "nope"}]}, "defaults": {}}
    errs = S.validate(bad)
    assert any(e.startswith("indicators.a.src: unknown name 'clsoe'") for e in errs)
    assert any(e.startswith("entry_long.all[1]: unknown condition") for e in errs)
    assert any(e.startswith("exit_long.lt[1].fn: unknown function") for e in errs)
    assert "defaults.n: parameter $n is used but has no default" in errs


def test_cycles_are_rejected():
    errs = S.validate({"name": "x", "indicators": {"a": {"fn": "sma", "src": "b", "len": 3},
                                                   "b": {"fn": "sma", "src": "a", "len": 3}},
                       "entry_long": {"gt": ["a", 1]}, "exit_long": True, "defaults": {}})
    assert any("circular" in e for e in errs)


def test_out_key_is_not_a_reference():
    spec = L.get("bollinger_rsi")         # has indicators named upper/mid/lower
    assert S.validate(spec) == []
    f = S.Frame.of(bars(np.linspace(100, 120, 60)), "4h")
    upper = S.Evaluator(f).value(S.bind(spec["indicators"]["upper"], spec["defaults"],
                                        spec["indicators"]))
    lower = S.Evaluator(f).value(S.bind(spec["indicators"]["lower"], spec["defaults"],
                                        spec["indicators"]))
    assert np.nanmin(upper - lower) >= 0


def test_memo_is_keyed_on_inlined_nodes(btc):
    """One Frame is shared by every combination in a sweep. A cache keyed on
    {"src": "fast"} would hand combination 2 the EMA from combination 1."""
    spec = L.get("ema_cross")
    shared = B.Series.from_frame(btc, "4h")
    for combo in S.grid(spec)[0]:
        fresh = B.Series.from_frame(btc, "4h")
        a = B.run(spec, combo, shared, with_curve=False).metrics
        b = B.run(spec, combo, fresh, with_curve=False).metrics
        assert a == b, combo


def test_spec_sha_ignores_key_order():
    a = {"name": "x", "defaults": {"a": 1, "b": 2}}
    b = {"defaults": {"b": 2, "a": 1}, "name": "x"}
    assert S.spec_sha(a) == S.spec_sha(b)


def test_grid_applies_constraints():
    combos, raw = S.grid(L.get("donchian_breakout"))
    assert raw == 9 and len(combos) == 7                 # exit_len < len drops 2
    assert all(c["exit_len"] < c["len"] for c in combos)


def test_schema_describes_every_function():
    schema = S.json_schema()
    fns = {v["properties"]["fn"]["const"] for v in schema["$defs"]["value"]["anyOf"]
           if isinstance(v, dict) and "properties" in v}
    assert fns == set(S.CATALOG)
    json.dumps(schema)                                   # serialisable


# -- windows -----------------------------------------------------------------------------------

def test_windows_are_ordered_disjoint_and_on_bars(btc):
    sp = W.split(btc["ts"].to_numpy())
    ts = set(btc["ts"])
    for w in (sp.train, sp.test, sp.holdout):
        assert w.start in ts and w.end in ts and w.start <= w.end
    assert sp.train.end < sp.test.start and sp.test.end < sp.holdout.start
    assert sp.holdout.end == btc["ts"].iloc[-1]


def test_short_series_is_thin_and_keeps_a_train_window():
    df = bars(np.linspace(1, 2, 400), step=86400)
    sp = W.split(df["ts"].to_numpy())
    assert sp.thin_sample
    assert sp.holdout.end - sp.holdout.start <= 0.26 * (df["ts"].iloc[-1] - df["ts"].iloc[0])


# -- data access -------------------------------------------------------------------------------

def test_partial_bar_is_never_read(monkeypatch):
    from btb import db
    from btb.engine import jobs
    seen = {}
    monkeypatch.setattr(db, "universe_entry", lambda c, s: {
        "symbol": s, "asset_class": "crypto", "timeframes": ["4h"]})
    monkeypatch.setattr(db, "series_meta", lambda c, s, t: {"source": "bitstamp",
                                                            "last_ts": 999 * H4})

    def read_bars(conn, source, symbol, tf, **kw):
        seen.update(kw)
        return bars(np.linspace(1, 2, 300))
    monkeypatch.setattr(db, "read_bars", read_bars)
    s = jobs.load_series(None, "BTC/USD", "4h")
    assert seen.get("include_partial", False) is False
    assert seen["as_of"] == 999 * H4 and s.as_of == 999 * H4


def test_sweep_resumes_without_rerunning(btc):
    from btb.engine.jobs import sweep_series
    spec = L.get("ema_cross")
    s = B.Series.from_frame(btc, "4h")
    stored = {}
    first = sweep_series(spec, s, Costs(), record=lambda n, p, r: stored.__setitem__(
        (n, json.dumps(p, sort_keys=True)), r.metrics))
    assert len(stored) == first["evaluated"] and first["reused"] == 0

    recorded = []
    again = sweep_series(spec, s, Costs(),
                         done=lambda n, p: stored.get((n, json.dumps(p, sort_keys=True))),
                         record=lambda *a: recorded.append(a))
    assert recorded == [] and again["reused"] == again["evaluated"]
    assert again["top"] == first["top"]


def test_sweep_ranks_on_test_not_train(btc):
    from btb.engine.jobs import sweep_series
    out = sweep_series(L.get("ema_cross"), B.Series.from_frame(btc, "4h"), Costs())
    tests = [r["test"]["net_pct"] for r in out["top"] if not r["test"]["ruin"]]
    assert tests == sorted(tests, reverse=True)
    assert out["ranked_on"] == "test"


# -- refresher ---------------------------------------------------------------------------------

def test_refresh_due_only_when_a_new_bar_has_closed():
    plan = [{"symbol": "BTC/USD", "timeframe": "4h"}, {"symbol": "SPY", "timeframe": "1d"},
            {"symbol": "ETH/USD", "timeframe": "1h"}, {"symbol": "NEW", "timeframe": "1d"}]
    cands = [
        {"symbol": "BTC/USD", "timeframe": "4h", "last_ts": 1, "age": 2 * H4 + 5, "since_check": 9999},
        {"symbol": "SPY", "timeframe": "1d", "last_ts": 1, "age": 3 * 86400, "since_check": 600},
        {"symbol": "ETH/USD", "timeframe": "1h", "last_ts": 1, "age": 3000, "since_check": None},
    ]
    due = [e["symbol"] for e in RF.due(cands, plan)]
    # BTC: a 4h bar closed and not asked recently. SPY: stale but asked 10 min
    # ago (market closed) -- throttled. ETH: no new bar yet. NEW: never
    # backfilled, which is build's job, not the refresher's.
    assert due == ["BTC/USD"]


def test_custom_strategy_is_validated_and_owned(monkeypatch):
    """A pasted spec is checked by the engine, and only its owner may run it."""
    from btb import db
    from btb.engine import jobs
    good = L.get("ema_cross")
    rows = {"mine": {"id": "mine", "user_id": "u1", "name": "x", "spec": good, "spec_sha": "s"},
            "bad": {"id": "bad", "user_id": "u1", "name": "x", "spec_sha": "s",
                    "spec": {**good, "entry_long": {"crossover": ["fast", "slow"]}}}}
    monkeypatch.setattr(db, "strategy_by_id", lambda c, i: rows.get(i))
    assert jobs._strategy(None, {"strategy_id": "mine"}, "u1")["id"] == "mine"
    with pytest.raises(jobs.JobError, match="unknown strategy"):
        jobs._strategy(None, {"strategy_id": "mine"}, "someone-else")
    with pytest.raises(jobs.JobError, match="entry_long: unknown condition 'crossover'"):
        jobs._strategy(None, {"strategy_id": "bad"}, "u1")
