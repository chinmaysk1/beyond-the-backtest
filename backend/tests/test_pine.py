"""Pine through PineForge: checking, the library, parity with the JSON oracle,
the sandbox, and a sweep end to end.

Needs the worker image (the transpiler, g++ and libpineforge.a). Elsewhere
every test here is skipped, not failed:

    docker build -t btb-backend backend
    docker run --rm --entrypoint python -v "$PWD/backend:/app" btb-backend -m pytest tests -q

The parity gate: every library strategy, ported to Pine, must trade exactly as
the retired JSON engine did -- every entry and exit on the same bar at the
same price -- once the three places the engines are *meant* to differ are set
to the JSON engine's behaviour (see `_emulated`). Equity is not compared:
TradingView sizes a 100%-of-equity order so the commission fits, which the
JSON engine did not, and that changes quantities but never which bar trades.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("pineforge_codegen")
from btb.pine import native as N  # noqa: E402

if not N.available():
    pytest.skip("PineForge engine or the shim is not installed here", allow_module_level=True)

from btb.engine import backtest as B  # noqa: E402
from btb.engine import library as L  # noqa: E402
from btb.engine import windows as W  # noqa: E402
from btb.engine.fills import Costs  # noqa: E402
from btb.pine import library as PL  # noqa: E402
from btb.pine import pragmas as PR  # noqa: E402
from btb.pine import program as P  # noqa: E402
from btb.pine import reference  # noqa: E402
from btb.pine import sandbox as SB  # noqa: E402
from btb.pine.check import check  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "btcusd_4h.csv.gz"
# Parity starts in 2016. Before it, BTC 4h has long runs of bars with no range
# at all, where PineForge's ta.dmi carries DI forward (fixnan, as Pine's source
# does) and the JSON engine skipped the bar; and its EMAs are seeded from the
# first bar rather than hidden until warm. Both are documented differences.
START = 1451606400
FINE = {"mintick": 1e-8, "type": "crypto", "timezone": "UTC", "session": "24x7"}


@pytest.fixture(scope="module")
def btc():
    df = pd.read_csv(FIXTURE)
    ts = df["time"].to_numpy("int64")
    c = {k: df[k].to_numpy("float64") for k in ("open", "high", "low", "close", "volume")}
    feed = N.bars(ts, c["open"], c["high"], c["low"], c["close"], c["volume"])
    series = B.Series.from_frame(df.rename(columns={"time": "ts"}), "4h", symbol="BTC/USD")
    return ts, feed, series


# -- checking ----------------------------------------------------------------------------

def test_errors_carry_line_and_column():
    r = check('//@version=6\nstrategy("x")\nx = ta.sma(close)\n')
    assert not r.ok
    assert r.errors[0].line == 3 and "length" in r.errors[0].message


def test_only_v6():
    r = check('//@version=5\nstrategy("x")\n')
    assert not r.ok and "v6 only" in r.errors[0].message


def test_other_markets_resolve_to_the_universe_or_are_refused():
    src = ('//@version=6\nstrategy("x")\n'
           'e = request.security("{sym}", "{tf}", close)\n'
           'if close > e\n    strategy.entry("L", strategy.long)\n')
    r = check(src.format(sym="BITSTAMP:ETHUSD", tf="D"))
    assert r.ok, [str(m) for m in r.errors]
    assert r.feeds == [{"key": "BITSTAMP:ETHUSD", "pine_tf": "1D", "symbol": "ETH/USD",
                        "timeframe": "1d", "asset_class": "crypto"}]
    bad = check(src.format(sym="NYSE:NOTREAL", tf="D"))
    assert not bad.ok and any("'NYSE:NOTREAL' is not one of the markets" in m.message for m in bad.errors)
    week = check(src.format(sym="AMEX:SPY", tf="W"))
    assert not week.ok and any("is not stored" in m.message for m in week.errors)


def test_another_markets_bars_reach_the_script(btc):
    """BTC 4h fed in as if it were another market: a script reading it through
    request.security sees the chart's own closes, bar for bar."""
    ts, feed, series = btc
    src = ('//@version=6\nstrategy("feed", process_orders_on_close=true, margin_long=0)\n'
           'other = request.security("BITSTAMP:BTCUSD", "240", close)\n'
           'if bar_index > 100 and close == other and strategy.position_size == 0\n'
           '    strategy.entry("L", strategy.long)\n'
           'if strategy.position_size > 0\n    strategy.close("L")\n')
    r = check(src)
    assert r.ok, [str(m) for m in r.errors]
    prog = P.Program(r)
    feeds = [P.feed_of(f, series) for f in r.feeds]
    with SB.Runner(prog.so(), feed, 1, {"ts": ts, "overrides": {}, "feeds": feeds}) as run:
        (_eq, trades), = run.map(P.task_backtest, [{"i0": 0, "i1": len(ts) - 1, "inputs": {}}])
    # An entry on (nearly) every other bar from 101 on: the closes matched.
    assert len(trades) > (len(ts) - 101) / 2 * 0.9


def test_costs_unset_set_and_zero():
    base = '//@version=6\nstrategy("x"{})\nif close > open\n    strategy.entry("L", strategy.long)\n'
    assert not check(base.format("")).costs.set
    c = check(base.format(", commission_type=strategy.commission.percent, commission_value=0.2")).costs
    assert c.set and c.commission_type == "percent" and c.commission_value == 0.2 and not c.zero
    z = check(base.format(", commission_value=0, slippage=0"))
    assert z.costs.zero and any("both 0" in m.message for m in z.warnings)


def test_inputs_get_their_variable_names():
    r = check('//@version=6\nstrategy("x")\nlen = input.int(14, "Length")\n'
              'src_mult = input.float(2.0, "Width", minval=1)\n'
              'if ta.crossover(close, ta.sma(close, len))\n    strategy.entry("L", strategy.long)\n')
    assert [(i.name, i.title, i.type) for i in r.inputs] == [
        ("len", "Length", "int"), ("src_mult", "Width", "float")]
    assert r.inputs[1].min == 1.0


def test_the_reference_example_checks():
    text = reference.text()
    example = text.split("## 6. A complete example")[1].split("```pine")[1].split("```")[0]
    r = check(example)
    assert r.ok, [str(m) for m in r.errors]
    assert r.grid.n == 18          # 3 x 2 x 3; fast < slow removes nothing here


# -- the library --------------------------------------------------------------------------

def test_library_checks_and_keeps_its_grids():
    """Every port sweeps exactly the settings its JSON original did, so N --
    and with it the deflated Sharpe -- is unchanged by the port."""
    from btb.engine import spec as S
    for name, r in PL.load_all().items():
        combos, raw = S.grid(L.get(name))
        assert r.grid.n == len(combos), name
        assert r.grid.raw == raw, name
        assert not r.costs.set, f"{name} must leave costs to the platform"
        assert r.title == L.get(name)["title"], name


def _emulated(src: str) -> str:
    """The JSON engine's semantics, in Pine: no margin calls (it liquidated at
    zero instead), and a higher timeframe seen one bar later (it showed each
    base bar the HTF bar that closed strictly before it). Tick rounding is the
    third, set through syminfo."""
    s = src.replace("process_orders_on_close=true)",
                    "process_orders_on_close=true, margin_long=0, margin_short=0)")
    return s.replace("f_adx(), lookahead=barmerge.lookahead_off)",
                     "f_adx()[1], lookahead=barmerge.lookahead_on)")


# The JSON engine's supertrend decided a flip against the PREVIOUS bar's band;
# Pine compares the close with the current, ratcheted band. On this fixture
# that changes one flip in ~470 trades for this setting, at bar 26652.
KNOWN = {("supertrend_trend", '{"atr_len": 20, "mult": 2, "trend_len": 100}')}


@pytest.mark.parametrize("name", sorted(PL.sources()))
def test_library_matches_the_json_engine_trade_for_trade(btc, name):
    import json
    ts, feed, series = btc
    r = check(_emulated(PL.sources()[name]))
    assert r.ok, [str(m) for m in r.errors]
    prog = P.Program(r)
    overrides, _ = prog.cost_overrides(Costs(0.10, 5.0))       # charged as 0.15% a side
    i0, i1 = int(np.searchsorted(ts, START)), len(ts) - 1
    combos = [PR.defaults(r.inputs)] + r.grid.combos[::max(1, len(r.grid.combos) // 4)]
    shared = {"ts": ts, "overrides": overrides, "syminfo": FINE}
    with SB.Runner(prog.so(), feed, 2, shared) as run:
        outs = run.map(P.task_backtest, [{"i0": i0, "i1": i1, "inputs": PR.as_runtime_inputs(p, r.inputs)}
                                         for p in combos])
    spec = L.get(name)
    for p, (equity, trades) in zip(combos, outs):
        f, _ = P.fills_from(equity, trades, ts, i0, i1)
        o = B.run(spec, {k: v for k, v in p.items() if k in spec["defaults"]}, series,
                  window=(START, int(ts[-1])), costs=Costs(0.15, 0.0), stop_fill="gap",
                  close_open=True, liquidate=False)
        pine = [(int(x[1]), int(x[2]), int(x[0]), x[3], x[4]) for x in f.rec]
        json_ = [(t.entry_i, t.exit_i, t.side, t.entry_px, t.exit_px) for t in o.fills.trades]
        same = len(pine) == len(json_) and all(
            a[:3] == b[:3] and a[3] == pytest.approx(b[3], rel=1e-9)
            and a[4] == pytest.approx(b[4], rel=1e-9) for a, b in zip(pine, json_))
        if (name, json.dumps(p, sort_keys=True)) in KNOWN:
            assert not same, f"{name} {p} now matches; remove it from KNOWN"
            continue
        assert same, f"{name} {p}: {len(pine)} Pine trades vs {len(json_)} JSON"


# -- running -------------------------------------------------------------------------------

def test_a_sliced_window_is_the_fresh_run_until_its_last_bar(btc):
    """Train numbers cut from one train+test run equal a fresh train run's,
    except that the fresh run closes any open position on its last bar."""
    ts, feed, series = btc
    prog = P.Program.from_source(PL.sources()["ema_cross"])
    split = W.split(series.ts)
    a0, a1 = B.index_range(ts, (split.full().start, split.full().end))
    t0, t1 = B.index_range(ts, (split.train.start, split.train.end))
    ov, _ = prog.cost_overrides(Costs(0.10, 5.0))
    with SB.Runner(prog.so(), feed, 1, {"ts": ts, "overrides": ov}) as run:
        (eq_full, tr_full), (eq_train, _tr) = run.map(
            P.task_backtest, [{"i0": a0, "i1": a1, "inputs": {}}, {"i0": t0, "i1": t1, "inputs": {}}])
    np.testing.assert_allclose(eq_full[t0:t1], eq_train[t0:t1], rtol=0, atol=1e-9)


def test_runaway_script_is_stopped(btc, monkeypatch):
    ts, feed, _ = btc
    src = ('//@version=6\nstrategy("spin")\nvar float x = 0.0\n'
           'for i = 0 to 100000000\n    x += math.sin(i)\n'
           'if x > 1e300\n    strategy.entry("L", strategy.long)\n')
    r = check(src)
    if not r.ok:
        pytest.skip("PineForge refuses an unbounded loop at check time: " + str(r.errors[0]))
    prog = P.Program(r)
    with pytest.raises(SB.SandboxError):
        with SB.Runner(prog.so(), feed, 1, {"ts": ts, "overrides": {}}) as run:
            run.map(P.task_backtest, [{"i0": 0, "i1": len(ts) - 1, "inputs": {}}], timeout=5)


def test_sweep_and_verdict_end_to_end(btc):
    from btb import verdict as V
    from btb.engine.jobs import pine_sweep_series
    _ts, _feed, series = btc
    prog = P.Program.from_source(PL.sources()["ema_cross"])
    out = pine_sweep_series(prog, series, Costs(0.10, 5.0))
    assert out["combos"] == prog.grid.n == len(out["rows"])
    assert sorted(r["test_rank"] for r in out["rows"]) == list(range(1, prog.grid.n + 1))
    mx = out.pop("matrix")
    assert mx.R.shape[1] == prog.grid.n
    v = V.evaluate(prog, series, Costs(0.10, 5.0), out, matrix=mx)
    assert 0 <= v["score"] <= 100 and v["combos"] == prog.grid.n


def test_ta_inside_a_condition_is_warned_about():
    r = check('//@version=6\nstrategy("x")\nif close > open\n    s = ta.sma(close, 10)\n'
              '    if close > s\n        strategy.entry("L", strategy.long)\n'
              'c = close > open and ta.change(close) > 0\nif c\n    strategy.close("L")\n')
    assert r.ok
    lines = sorted(m.line for m in r.warnings if "runs only on some bars" in m.message)
    assert lines == [4, 7]


def test_the_library_has_no_conditional_ta():
    for name, r in PL.load_all().items():
        assert not [m for m in r.warnings if "runs only on some bars" in m.message], name


def test_time_inputs_are_dates_not_numbers():
    r = check('//@version=6\nstrategy("x")\n'
              'startDate = input.time(timestamp("2000-01-01T00:00"), "startDate")\n'
              'len = input.int(14, "len")\n'
              '// @grid startDate = 1, 2\n'
              'if time >= startDate and close > ta.sma(close, len)\n    strategy.entry("L", strategy.long)\n')
    kinds = {i.name: i.type for i in r.inputs}
    assert kinds == {"startDate": "time", "len": "int"}
    assert any("only int and float inputs can be swept" in m.message for m in r.errors)
