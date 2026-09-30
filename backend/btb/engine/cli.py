"""`python -m btb.engine <command>` -- the engine's interface.

    library [--sync]              list the built-in strategies; --sync loads them into the DB
    validate FILE                 check a spec file; prints every error with its path
    schema | catalog              the spec's JSON Schema / function catalog (what an AI is shown)
    run    --strategy S --symbol X --timeframe T [--param k=v ...] [--csv FILE]
    sweep  --strategy S --symbol X --timeframe T [--csv FILE] [--save]
    verdict --strategy S --symbol X --timeframe T [--csv FILE]
    calibrate --timeframe T [--csv FILE | --symbol X] [--walks N] [--strategy S ...]
    enqueue backtest|sweep --strategy S --symbol X --timeframe T [--param k=v ...]
    worker [--no-refresh] [--once]
    refresh                       fetch newly closed bars for every due series, once

`run`, `sweep`, `verdict` and `calibrate` read from the database unless given
--csv, which runs fully offline on a file with columns time|ts, open, high, low,
close, volume.
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
from . import spec as S
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
    lib = L.load_all()
    for name, spec in lib.items():
        combos, raw = S.grid(spec)
        print(f"{name:20} {spec.get('style', ''):15} {len(combos):>3} combos  "
              f"{spec.get('title', '')}")
    if args.sync:
        from .. import db
        with db.connect() as conn:
            for name, sid in L.sync(conn):
                print(f"synced {name} -> {sid}")
    return 0


def cmd_validate(args) -> int:
    spec = json.loads(Path(args.file).read_text(encoding="utf-8"))
    errs = S.validate(spec)
    for e in errs:
        print(e)
    print("valid" if not errs else f"{len(errs)} error(s)")
    return 1 if errs else 0


def cmd_schema(args) -> int:
    print(json.dumps(S.json_schema(), indent=2))
    return 0


def cmd_catalog(args) -> int:
    print(json.dumps(S.catalog(), indent=2))
    return 0


def cmd_run(args) -> int:
    spec = L.get(args.strategy)
    series = _series(args)
    costs = _costs(args, series.asset_class)
    split = W.split(series.ts)
    print(f"{spec['title']} on {series.symbol} {series.timeframe}  "
          f"({len(series.ts)} bars, costs {costs.commission_pct}% + {costs.slippage_bps} bps, "
          f"adjust {series.adjust})")
    for w in (split.train, split.test, split.full()):
        t = time.perf_counter()
        r = B.run(spec, _params(args.param), series, window=(w.start, w.end), costs=costs,
                  fill=args.fill, with_curve=False)
        print(f"  {w.name:6} {pd.Timestamp(w.start, unit='s'):%Y-%m-%d}..{pd.Timestamp(w.end, unit='s'):%Y-%m-%d}"
              f"  {_fmt(r.metrics)}  ({time.perf_counter() - t:.2f}s)")
    if split.thin_sample:
        print("  thin sample: under two years of data")
    return 0


def cmd_sweep(args) -> int:
    from .jobs import sweep_series
    spec = L.get(args.strategy)
    if args.save and not args.csv:
        from .. import db
        from .jobs import sweep_job
        with db.connect() as conn:
            L.sync(conn)
            out = sweep_job(conn, {"strategy": args.strategy, "symbol": args.symbol,
                                   "timeframe": args.timeframe})
    else:
        series = _series(args)
        costs = _costs(args, series.asset_class)
        t0 = time.perf_counter()
        out = sweep_series(spec, series, costs, fill=args.fill)
        out["seconds"] = round(time.perf_counter() - t0, 2)
    print(f"{out['combos']} combos x 2 windows = {out['evaluated']} runs in {out['seconds']}s "
          f"({out.get('reused', 0)} reused); train/test rank correlation {out['rank_corr']}")
    for r in out["top"]:
        print(f"  #{r['test_rank']:<3} train #{r['train_rank']:<3} {json.dumps(r['params'])}")
        print(f"        test  {_fmt(r['test'])}")
        print(f"        train {_fmt(r['train'])}")
    return 0


def cmd_verdict(args) -> int:
    from .. import verdict as V
    from .jobs import sweep_series
    spec = L.get(args.strategy)
    series = _series(args)
    costs = _costs(args, series.asset_class)
    v = V.evaluate(spec, series, costs, sweep_series(spec, series, costs, fill=args.fill),
                   fill=args.fill)
    _print_verdict(args.strategy, v)
    for rank, r in v["rows"].items():
        print(f"  #{rank:<3} {r['label']:<8} dsr {r['dsr']}  {json.dumps(r['params'])}")
    return 0


def _print_verdict(name: str, v: dict) -> None:
    c = v["checks"]
    wf, pb = c["walk_forward"], c["pbo"]
    print(f"{name:<20} {v['label']:<8} {v['combos']:>3} settings  "
          f"pbo {pb.get('value', '-')}  "
          f"walk-forward {wf.get('oos_pct', '-')}% ({wf.get('folds_positive', '-')}/5 up, "
          f"hold {wf.get('bh_pct', '-')}%)  dsr {c['dsr']['value']}  [{v['seconds']}s]")
    print(f"{'':<20} {v['reason']}")


def cmd_calibrate(args) -> int:
    """Run the verdict on random walks, where no strategy has an edge, with the
    real series' timestamps and volatility. ROBUST here is a false positive;
    the target is at most 5% of runs."""
    import numpy as np
    from .. import verdict as V
    from .jobs import sweep_series
    real = _series(args)
    close = real.frame.close
    vol = float(np.std(np.diff(np.log(close))))
    names = args.strategy or sorted(L.load_all())
    rng = np.random.default_rng(args.seed)
    counts = {n: {"ROBUST": 0, "WEAK": 0, "OVERFIT": 0} for n in names}
    for i in range(args.walks):
        c = close[0] * np.exp(np.cumsum(rng.normal(0, vol, len(close))))
        df = pd.DataFrame({"ts": real.ts, "open": c, "high": c * (1 + vol), "low": c * (1 - vol),
                           "close": c, "volume": 1.0})
        walk = B.Series.from_frame(df, real.timeframe, asset_class=real.asset_class)
        costs = _costs(args, walk.asset_class)
        for n in names:
            spec = L.get(n)
            v = V.evaluate(spec, walk, costs, sweep_series(spec, walk, costs), fill=args.fill)
            counts[n][v["label"]] += 1
        print(f"walk {i + 1}/{args.walks}", file=sys.stderr)
    total = sum(c["ROBUST"] for c in counts.values())
    runs = args.walks * len(names)
    print(f"{'strategy':<20} robust  weak  overfit   ({args.walks} random walks, vol {vol:.4f}/bar)")
    for n, c in counts.items():
        print(f"{n:<20} {c['ROBUST']:>6} {c['WEAK']:>5} {c['OVERFIT']:>8}")
    print(f"false-positive rate: {total}/{runs} = {total / runs:.1%} (target <= 5%)")
    return 0 if total / runs <= 0.05 else 1


def cmd_enqueue(args) -> int:
    from .. import db
    payload = {"strategy": args.strategy, "symbol": args.symbol, "timeframe": args.timeframe}
    if args.param:
        payload["params"] = _params(args.param)
    with db.connect() as conn:
        L.sync(conn)
        jid = db.enqueue_job(conn, args.kind, payload)
        conn.commit()
    print(f"queued job {jid}")
    return 0


def cmd_worker(args) -> int:
    from .. import db
    from .worker import run
    with db.connect() as conn:
        L.sync(conn)
    run(refresh=not args.no_refresh, once=args.once)
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

    v = sub.add_parser("validate", help="validate a spec file")
    v.add_argument("file")
    v.set_defaults(fn=cmd_validate)
    sub.add_parser("schema", help="print the spec JSON Schema").set_defaults(fn=cmd_schema)
    sub.add_parser("catalog", help="print the function catalog").set_defaults(fn=cmd_catalog)

    def market(sp, csv=True):
        sp.add_argument("--strategy", required=True)
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

    vd = sub.add_parser("verdict", help="sweep, then ROBUST / WEAK / OVERFIT and why")
    market(vd)
    vd.set_defaults(fn=cmd_verdict)

    cb = sub.add_parser("calibrate", help="false-positive rate of the verdict on random walks")
    cb.add_argument("--strategy", action="append", help="repeatable; default every library strategy")
    cb.add_argument("--symbol")
    cb.add_argument("--timeframe", required=True)
    cb.add_argument("--commission", type=float, help="% per leg")
    cb.add_argument("--slippage", type=float, help="bps per fill")
    cb.add_argument("--fill", choices=["close", "open"], default="close")
    cb.add_argument("--csv", help="take timestamps and volatility from this file")
    cb.add_argument("--asset-class", default="crypto", choices=["crypto", "equity"])
    cb.add_argument("--walks", type=int, default=50)
    cb.add_argument("--seed", type=int, default=0)
    cb.set_defaults(fn=cmd_calibrate)

    e = sub.add_parser("enqueue", help="queue a job for the worker")
    e.add_argument("kind", choices=["backtest", "sweep"])
    market(e, csv=False)
    e.set_defaults(fn=cmd_enqueue)

    w = sub.add_parser("worker", help="claim and run jobs; refresh data when idle")
    w.add_argument("--no-refresh", action="store_true")
    w.add_argument("--once", action="store_true", help="handle one job (or one idle cycle) and exit")
    w.set_defaults(fn=cmd_worker)

    rf = sub.add_parser("refresh", help="fetch newly closed bars for due series")
    rf.add_argument("--budget", type=float, default=600, help="seconds before stopping")
    rf.set_defaults(fn=cmd_refresh)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
