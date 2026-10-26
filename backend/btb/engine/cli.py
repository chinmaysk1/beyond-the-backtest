"""`python -m btb.engine <command>` -- the engine's interface.

    library [--sync]              list the built-in strategies; --sync loads them into the DB
    check FILE.pine               check a script: errors and warnings with line numbers, the
                                  inputs, the grid and N, the costs
    reference                     the Pine contract a model is shown (REFERENCE.md)
    run    (--strategy S | --file F) --symbol X --timeframe T [--param k=v ...] [--csv FILE]
    sweep  (--strategy S | --file F) --symbol X --timeframe T [--csv FILE] [--save]
    verdict (--strategy S | --file F) --symbol X --timeframe T [--csv FILE]
    calibrate --timeframe T [--csv FILE | --symbol X] [--walks N] [--strategy S ...]
    rescore [--dry-run] [--force]  re-score stored verdicts from their stored numbers
    enqueue backtest|sweep --strategy S --symbol X --timeframe T [--param k=v ...]
    worker [--no-refresh] [--once] [--kinds check]
    refresh                       fetch newly closed bars for every due series, once

Strategies are Pine. `--strategy` names a library script (btb/pine/library);
`--file` runs any .pine file. `--legacy` runs the retired JSON library instead
(btb/engine/library), which is now only the oracle the Pine ports are tested
against.

`run`, `sweep`, `verdict` and `calibrate` read from the database unless given
--csv, which runs fully offline on a file with columns time|ts, open, high, low,
close, volume. Pine needs the worker image (PineForge); --legacy runs anywhere.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

from . import backtest as B
from . import library as L
from . import windows as W
from .fills import Costs, default_costs


def _params(pairs) -> dict:
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        if not _:
            raise SystemExit(f"--param expects k=v, got {p!r}")
        out[k] = float(v) if "." in v else int(v)
    return out


def _strategy(args):
    """("pine", Program) or ("json", spec) for the command's strategy."""
    if getattr(args, "legacy", False):
        return "json", L.get(args.strategy)
    from ..pine import library as PL
    from ..pine.program import PineError, Program
    if getattr(args, "file", None):
        src = Path(args.file).read_text(encoding="utf-8")
    else:
        lib = PL.sources()
        if args.strategy not in lib:
            raise SystemExit(f"no library strategy {args.strategy!r}; have {sorted(lib)}")
        src = lib[args.strategy]
    try:
        return "pine", Program.from_source(src)
    except PineError as e:
        raise SystemExit("\n".join(e.messages)) from None


def _name(kind, strat) -> str:
    return strat.title if kind == "pine" else strat.get("title", strat["name"])


def _costs(args, asset_class) -> Costs:
    base = default_costs(asset_class)
    return Costs(base.commission_pct if args.commission is None else args.commission,
                 base.slippage_bps if args.slippage is None else args.slippage)


def _series(args) -> B.Series:
    if args.csv:
        df = pd.read_csv(args.csv).rename(columns={"time": "ts"})
        return B.Series.from_frame(df[["ts", "open", "high", "low", "close", "volume"]],
                                   args.timeframe, symbol=args.symbol or Path(args.csv).stem,
                                   asset_class=args.asset_class, as_of=int(df["ts"].iloc[-1]))
    from .. import db
    from .jobs import load_series
    with db.connect() as conn:
        return load_series(conn, args.symbol, args.timeframe)


def _fmt(m: dict) -> str:
    def f(k, fmt="{:+.1f}%"):
        v = m.get(k)
        return "-" if v is None else fmt.format(v)
    return (f"net {f('net_pct')}  b&h {f('bh_pct')}  dd {f('max_dd_pct', '{:.1f}%')}  "
            f"sharpe {f('sharpe', '{:.2f}')}  trades {m['trades']}  "
            f"win {f('win_pct', '{:.0f}%')}" + ("  RUIN" if m.get("ruin") else ""))


# -- commands -------------------------------------------------------------------------

def cmd_library(args) -> int:
    from ..pine import library as PL
    for name, r in PL.load_all().items():
        style = PL.spec_of(name, r)["style"]
        print(f"{name:20} {style:15} {r.grid.n:>4} combos  {r.title}")
    if args.sync:
        from .. import db
        with db.connect() as conn:
            for name, sid in PL.sync(conn):
                print(f"synced {name} -> {sid}")
    return 0


def cmd_check(args) -> int:
    from ..pine.check import check
    r = check(Path(args.file).read_text(encoding="utf-8"))
    for m in r.errors:
        print(f"error   {m}")
    for m in r.warnings:
        print(f"warning {m}")
    if r.ok:
        for i in r.inputs:
            swept = r.grid.values.get(i.name)
            print(f"  input {i.name or '-':16} {i.type:6} default {i.default!s:8}"
                  + (f" swept {swept}" if swept else ""))
        print(f"{r.title}: ok, {r.grid.n} combination(s)"
              + ("" if r.grid.scored else " (no @grid: runs once, unscored)")
              + f"; costs {'from the script' if r.costs.set else 'asset-class defaults'}")
    return 0 if r.ok else 1


def cmd_reference(args) -> int:
    from ..pine import reference
    print(reference.text())
    return 0


def _pine_costs(prog, costs: Costs) -> str:
    _, c = prog.cost_overrides(costs)
    if c["source"] == "script":
        return f"script's costs: {c['commission_value']} {c['commission_type']} + {c['slippage_ticks']} ticks"
    return f"default costs {costs.commission_pct}% + {costs.slippage_bps} bps, charged as {c['charged_pct_per_side']}% a side"


def cmd_run(args) -> int:
    kind, strat = _strategy(args)
    series = _series(args)
    costs = _costs(args, series.asset_class)
    split = W.split(series.ts)
    wins = (split.train, split.test, split.full())
    desc = (_pine_costs(strat, costs) if kind == "pine"
            else f"costs {costs.commission_pct}% + {costs.slippage_bps} bps")
    print(f"{_name(kind, strat)} on {series.symbol} {series.timeframe}  "
          f"({len(series.ts)} bars, {desc}, adjust {series.adjust})")
    t = time.perf_counter()
    if kind == "pine":
        from ..pine.program import run_windows
        results = run_windows(strat, series, costs, strat.resolve_params(_params(args.param)),
                              [(w.start, w.end) for w in wins], [False] * len(wins))
    else:
        results = [B.run(strat, _params(args.param), series, window=(w.start, w.end),
                         costs=costs, fill=args.fill, with_curve=False) for w in wins]
    for w, r in zip(wins, results):
        print(f"  {w.name:6} {pd.Timestamp(w.start, unit='s'):%Y-%m-%d}..{pd.Timestamp(w.end, unit='s'):%Y-%m-%d}"
              f"  {_fmt(r.metrics)}")
    print(f"  ({time.perf_counter() - t:.2f}s)")
    if split.thin_sample:
        print("  thin sample: under two years of data")
    return 0


def _sweep(kind, strat, series, costs, fill):
    from .jobs import pine_sweep_series, sweep_series
    if kind == "pine":
        out = pine_sweep_series(strat, series, costs)
        out.pop("_window_bounds", None)
        for r in out["rows"]:
            r.pop("_metrics", None)
        return out
    return sweep_series(strat, series, costs, fill=fill)


def cmd_sweep(args) -> int:
    kind, strat = _strategy(args)
    if args.save and not args.csv:
        if kind != "pine" or args.file:
            raise SystemExit("--save runs a library strategy through the worker's job")
        from .. import db
        from ..pine import library as PL
        from .jobs import sweep_job
        with db.connect() as conn:
            PL.sync(conn)
            out = sweep_job(conn, {"strategy": args.strategy, "symbol": args.symbol,
                                   "timeframe": args.timeframe})
    else:
        series = _series(args)
        costs = _costs(args, series.asset_class)
        t0 = time.perf_counter()
        out = _sweep(kind, strat, series, costs, args.fill)
        out.pop("matrix", None)
        out["seconds"] = round(time.perf_counter() - t0, 2)
    per = out.get("runs_per_combination", 2)
    print(f"{out['combos']} combos x {per} run(s) = {out['combos'] * per} runs in {out['seconds']}s "
          f"({out.get('reused', 0)} reused); train/test rank correlation {out['rank_corr']}")
    for r in out["top"]:
        print(f"  #{r['test_rank']:<3} train #{r['train_rank']:<3} {json.dumps(r['params'])}")
        print(f"        test  {_fmt(r['test'])}")
        print(f"        train {_fmt(r['train'])}")
    return 0


def cmd_verdict(args) -> int:
    from .. import verdict as V
    kind, strat = _strategy(args)
    series = _series(args)
    costs = _costs(args, series.asset_class)
    sw = _sweep(kind, strat, series, costs, args.fill)
    v = V.evaluate(strat, series, costs, sw, fill=args.fill, matrix=sw.pop("matrix", None))
    _print_verdict(args.strategy or args.file, v)
    for rank, r in v["rows"].items():
        print(f"  #{rank:<3} score {r['score']:>3}  dsr {r['dsr']}  {json.dumps(r['params'])}")
    return 0


def _print_verdict(name: str, v: dict) -> None:
    c, f = v["checks"], v["factors"]
    wf, pb = c["walk_forward"], c["pbo"]
    print(f"{name:<20} score {v['score']:>3} = {f['dsr']:.2f} x {f['pbo']:.2f} x "
          f"{f['walk_forward']:.2f}  ({v['combos']} settings; pbo {pb.get('value', '-')}, "
          f"walk-forward {wf.get('oos_pct', '-')}% with {wf.get('folds_positive', '-')}/5 up "
          f"vs hold {wf.get('bh_pct', '-')}%)  [{v['seconds']}s]")
    print(f"{'':<20} {v['reason']}")


def cmd_calibrate(args) -> int:
    """Score random walks, where no strategy has an edge, with the real
    series' timestamps and volatility. The distribution of headline scores is
    what luck alone produces: its 95th percentile is where `high` should start
    and its 75th where `mid` should. A walk scoring `high` under the current
    bands is a false positive; the target is at most 5% of runs."""
    import numpy as np
    from .. import verdict as V
    from ..verdict import rules
    real = _series(args)
    close = real.frame.close
    vol = float(np.std(np.diff(np.log(close))))
    if args.legacy:
        names = args.strategy or sorted(L.load_all())
        strategies = {n: ("json", L.get(n)) for n in names}
    else:
        from ..pine import library as PL
        from ..pine.program import Program
        lib = PL.sources()
        names = args.strategy or sorted(lib)
        strategies = {n: ("pine", Program.from_source(lib[n])) for n in names}
    rng = np.random.default_rng(args.seed)
    scores = {n: [] for n in names}
    for i in range(args.walks):
        c = close[0] * np.exp(np.cumsum(rng.normal(0, vol, len(close))))
        df = pd.DataFrame({"ts": real.ts, "open": c, "high": c * (1 + vol), "low": c * (1 - vol),
                           "close": c, "volume": 1.0})
        walk = B.Series.from_frame(df, real.timeframe, asset_class=real.asset_class)
        costs = _costs(args, walk.asset_class)
        for n in names:
            kind, strat = strategies[n]
            sw = _sweep(kind, strat, walk, costs, args.fill)
            v = V.evaluate(strat, walk, costs, sw, fill=args.fill, matrix=sw.pop("matrix", None))
            scores[n].append(v["score"])
        print(f"walk {i + 1}/{args.walks}", file=sys.stderr)
    hi = rules.BANDS["high"]
    print(f"{'strategy':<20} median  p75  p95  max  >={hi}   ({args.walks} random walks, vol {vol:.4f}/bar)")
    for n, xs in scores.items():
        q = np.percentile(xs, [50, 75, 95])
        print(f"{n:<20} {q[0]:>6.0f} {q[1]:>4.0f} {q[2]:>4.0f} {max(xs):>4} {sum(x >= hi for x in xs):>5}")
    every = [x for xs in scores.values() for x in xs]
    q = np.percentile(every, [75, 95])
    fp = sum(x >= hi for x in every)
    print(f"all runs: p75 {q[0]:.0f} (mid should start here), p95 {q[1]:.0f} (high should start here)")
    print(f"false-positive rate at high >= {hi}: {fp}/{len(every)} = {fp / len(every):.1%} (target <= 5%)")
    if args.out:
        Path(args.out).write_text(json.dumps({"timeframe": real.timeframe, "vol": vol,
                                              "walks": args.walks, "scores": scores}))
    return 0 if fp / len(every) <= 0.05 else 1


def cmd_rescore(args) -> int:
    """Re-score every stored verdict under the current rules, from the numbers
    it already holds. Nothing is re-run; verdicts already at this version are
    left alone unless --force."""
    from psycopg.types.json import Jsonb
    from .. import db
    from .. import verdict as V
    from ..verdict import rules
    n = 0
    with db.connect() as conn:
        rows = conn.execute("SELECT id, result->'verdict' FROM jobs "
                            "WHERE kind = 'sweep' AND result ? 'verdict'").fetchall()
        for jid, v in rows:
            if not v or (v.get("version") == rules.VERSION and not args.force):
                continue
            new = V.rescore(v)
            if not args.dry_run:
                conn.execute("UPDATE jobs SET result = jsonb_set(result, '{verdict}', %s) "
                             "WHERE id = %s", (Jsonb(new), jid))
            print(f"job {jid}: {v.get('label', '-')} -> score {new['score']}")
            n += 1
        if not args.dry_run:
            conn.commit()
    print(f"{'would rescore' if args.dry_run else 'rescored'} {n} of {len(rows)} stored verdicts")
    return 0


def cmd_enqueue(args) -> int:
    from .. import db
    payload = {"strategy": args.strategy, "symbol": args.symbol, "timeframe": args.timeframe}
    if args.param:
        payload["params"] = _params(args.param)
    from ..pine import library as PL
    with db.connect() as conn:
        PL.sync(conn)
        jid = db.enqueue_job(conn, args.kind, payload)
        conn.commit()
    print(f"queued job {jid}")
    return 0


def cmd_worker(args) -> int:
    from .. import db
    from ..pine import library as PL
    from .worker import run
    kinds = [k.strip() for k in args.kinds.split(",")] if args.kinds else None
    if not kinds or set(kinds) - {"check"}:
        with db.connect() as conn:
            PL.sync(conn)
    run(refresh=not args.no_refresh, once=args.once, kinds=kinds,
        poll=args.poll)
    return 0


def cmd_refresh(args) -> int:
    from .. import db
    from .refresh import refresh_once
    with db.connect() as conn:
        r = refresh_once(conn, budget_s=args.budget)
    print(r)
    return 1 if r["failed"] else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m btb.engine",
                                description="Beyond the Backtest -- backtest engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    lib = sub.add_parser("library", help="list built-in strategies")
    lib.add_argument("--sync", action="store_true", help="load them into the database")
    lib.set_defaults(fn=cmd_library)

    ck = sub.add_parser("check", help="check a Pine script")
    ck.add_argument("file")
    ck.set_defaults(fn=cmd_check)
    sub.add_parser("reference", help="print the Pine contract (REFERENCE.md)").set_defaults(
        fn=cmd_reference)

    def market(sp, csv=True):
        who = sp.add_mutually_exclusive_group(required=True)
        who.add_argument("--strategy", help="a library strategy")
        who.add_argument("--file", help="a .pine file")
        sp.add_argument("--legacy", action="store_true",
                        help="the retired JSON library strategy of that name")
        sp.add_argument("--symbol")
        sp.add_argument("--timeframe", required=True)
        sp.add_argument("--param", action="append", help="k=v, repeatable")
        sp.add_argument("--commission", type=float, help="% per leg")
        sp.add_argument("--slippage", type=float, help="bps per fill")
        sp.add_argument("--fill", choices=["close", "open"], default="close")
        if csv:
            sp.add_argument("--csv", help="run offline on a CSV instead of the database")
            sp.add_argument("--asset-class", default="crypto", choices=["crypto", "equity"])

    r = sub.add_parser("run", help="one backtest on train, test and full")
    market(r)
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("sweep", help="every grid combination, ranked on test")
    market(s)
    s.add_argument("--save", action="store_true", help="record runs in the database")
    s.set_defaults(fn=cmd_sweep)

    vd = sub.add_parser("verdict", help="sweep, then a 0-100 score and why")
    market(vd)
    vd.set_defaults(fn=cmd_verdict)

    cb = sub.add_parser("calibrate", help="what scores luck alone reaches, on random walks")
    cb.add_argument("--strategy", action="append", help="repeatable; default every library strategy")
    cb.add_argument("--legacy", action="store_true", help="the retired JSON library")
    cb.add_argument("--symbol")
    cb.add_argument("--timeframe", required=True)
    cb.add_argument("--commission", type=float, help="% per leg")
    cb.add_argument("--slippage", type=float, help="bps per fill")
    cb.add_argument("--fill", choices=["close", "open"], default="close")
    cb.add_argument("--csv", help="take timestamps and volatility from this file")
    cb.add_argument("--asset-class", default="crypto", choices=["crypto", "equity"])
    cb.add_argument("--walks", type=int, default=50)
    cb.add_argument("--seed", type=int, default=0)
    cb.add_argument("--out", help="write every walk's score to this JSON file")
    cb.set_defaults(fn=cmd_calibrate)

    rs = sub.add_parser("rescore", help="re-score stored verdicts under the current rules")
    rs.add_argument("--dry-run", action="store_true")
    rs.add_argument("--force", action="store_true", help="also verdicts already at this version")
    rs.set_defaults(fn=cmd_rescore)

    e = sub.add_parser("enqueue", help="queue a job for the worker")
    e.add_argument("kind", choices=["backtest", "sweep"])
    market(e, csv=False)
    e.set_defaults(fn=cmd_enqueue)

    w = sub.add_parser("worker", help="claim and run jobs; refresh data when idle")
    w.add_argument("--no-refresh", action="store_true")
    w.add_argument("--once", action="store_true", help="handle one job (or one idle cycle) and exit")
    w.add_argument("--kinds", help="comma-separated job kinds to claim (default: all)")
    w.add_argument("--poll", type=float, default=1.0, help="seconds between queue checks")
    w.set_defaults(fn=cmd_worker)

    rf = sub.add_parser("refresh", help="fetch newly closed bars for due series")
    rf.add_argument("--budget", type=float, default=600, help="seconds before stopping")
    rf.set_defaults(fn=cmd_refresh)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
