"""`python -m btb.data <command>` -- the data layer's interface.

    init                      create the schema, register the universe
    build                     ingest the whole curated universe
    fetch  --kind ... --symbol ... --timeframe ...
    verify [--symbol ...]     re-audit what is stored
    list                      inventory, from the series manifest table
    stale                     which series need a refresh (one query, no network)
    show   --symbol ... --timeframe ... [--adjust split|total|none]
    events --symbol ...       corporate actions held for a symbol

Exit codes are meaningful: non-zero if anything failed its integrity check, so
`build` and `verify` can gate a scripted refresh or CI.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import pandas as pd

from .. import db
from . import adjust as ADJ
from . import bars as B
from . import integrity
from .ingest import HISTORY_YEARS, ingest_series, load_universe, register_universe


def _date(s):
    """YYYY-MM-DD or a bare epoch -> UTC epoch seconds."""
    if not s:
        return None
    s = str(s)
    return int(s) if s.isdigit() else int(pd.Timestamp(s, tz="UTC").timestamp())


# -- commands -----------------------------------------------------------------

def cmd_init(args) -> int:
    db.init_schema()
    with db.connect() as conn:
        n = register_universe(conn)
    print(f"schema applied; {n} symbols registered in the universe")
    return 0


def cmd_build(args) -> int:
    plan = load_universe()
    if args.asset_class:
        plan = [e for e in plan if e["asset_class"] == args.asset_class]
    if args.symbol:
        want = {s.upper() for s in args.symbol}
        plan = [e for e in plan if e["symbol"].upper() in want]
    if args.timeframes:
        plan = [e for e in plan if e["timeframe"] in args.timeframes]

    # Coarse timeframes first. They are fast and carry full history, so a run
    # that gets interrupted has still delivered the most useful series, rather
    # than a third of the 5m backfill and nothing else.
    order = {tf: i for i, tf in enumerate(["1d", "4h", "1h", "30m", "15m", "5m", "1m"])}
    plan.sort(key=lambda e: (order.get(e["timeframe"], 99), e["symbol"]))

    print(f"{len(plan)} series to ingest\n", flush=True)
    failures = done = 0
    t0 = time.perf_counter()
    with db.connect() as conn:
        register_universe(conn)
        for i, e in enumerate(plan, 1):
            kw = {k: e[k] for k in ("venue",) if e.get(k)}
            try:
                r = ingest_series(conn, e["kind"], e["symbol"], e["timeframe"],
                                  since=_date(e.get("since")),
                                  full_refresh=args.refresh,
                                  asset_class=e["asset_class"], **kw)
            except Exception as ex:                    # noqa: BLE001
                print(f"[{i:>3}/{len(plan)}] ERR  {e['symbol']:10} {e['timeframe']:4} "
                      f"{type(ex).__name__}: {str(ex)[:80]}", flush=True)
                failures += 1
                continue
            done += 1
            print(f"[{i:>3}/{len(plan)}] {r.line()}", flush=True)
            for f in r.report.findings:
                print(f"           {f}", flush=True)
            if not r.ok:
                failures += 1

    mins = (time.perf_counter() - t0) / 60
    print(f"\n{done} series ingested, {failures} failed, {mins:.1f} min")
    return 1 if failures else 0


def cmd_fetch(args) -> int:
    kw = {"venue": args.venue} if args.venue else {}
    with db.connect() as conn:
        r = ingest_series(conn, args.kind, args.symbol, args.timeframe,
                          since=_date(args.since), until=_date(args.until),
                          full_refresh=args.refresh, **kw)
    print(r.line())
    print(r.report.render())
    return 0 if r.ok else 1


def cmd_verify(args) -> int:
    failures = 0
    with db.connect() as conn:
        inv = db.inventory(conn, args.symbol)
        if not inv:
            print("nothing stored")
            return 0
        for m in inv:
            df = db.read_bars(conn, m["source"], m["symbol"], m["timeframe"])
            ev = db.read_events(conn, m["source"], m["symbol"])
            equity = m["asset_class"] == "equity"
            rep = integrity.check(df, m["timeframe"], expect_regular=not equity,
                                  events=ev, corporate_actions=equity)
            print(f"{'PASS' if rep.ok else 'FAIL':4}  {m['symbol']:10} "
                  f"{m['timeframe']:4} {rep.stats.get('rows', 0):>9,} bars")
            for f in rep.findings:
                print(f"        {f}")
            # Persist the fresh report. The audit logic improves over time --
            # the weekend check, the event-aware jump check -- and a manifest
            # still carrying last month's verdict describes a series nobody has
            # actually re-examined. Re-verifying costs one read; re-fetching
            # eighty million bars to update a JSON blob does not.
            if not args.dry_run:
                db.upsert_series(conn, m["source"], m["symbol"], m["timeframe"],
                                 integrity=rep.to_dict(),
                                 checked_at=__import__("datetime").datetime.now(
                                     __import__("datetime").timezone.utc))
                conn.commit()
            if not rep.ok:
                failures += 1
    return 1 if failures else 0


def cmd_list(args) -> int:
    with db.connect() as conn:
        inv = db.inventory(conn, args.symbol)
    if not inv:
        print("nothing stored")
        return 0
    print(f"{'SYMBOL':10} {'TF':4} {'CLASS':7} {'ROWS':>9}  {'FIRST':11} "
          f"{'LAST':11} {'TIER':>5}  FETCHED")
    total = 0
    for m in inv:
        total += m["row_count"] or 0
        tier = HISTORY_YEARS.get(m["timeframe"])
        ok = "" if (m.get("integrity") or {}).get("ok", True) else "  <FAIL>"
        print(f"{m['symbol']:10} {m['timeframe']:4} {str(m['asset_class'])[:7]:7} "
              f"{m['row_count'] or 0:>9,}  {B.iso(m['first_ts'])[:11]:11} "
              f"{B.iso(m['last_ts'])[:11]:11} {str(tier or 'all'):>5}  "
              f"{str(m['fetched_at'])[:16]}{ok}")
    print(f"\n{len(inv)} series, {total:,} bars")
    return 0


def cmd_stale(args) -> int:
    with db.connect() as conn:
        rows = db.stale_series(conn, grace=args.grace)
    if not rows:
        print("everything current")
        return 0
    print(f"{'SYMBOL':10} {'TF':4} {'AGE':>10}  LAST COMPLETE BAR")
    for r in rows:
        age = r["age"] or 0
        human = f"{age/3600:.1f}h" if age < 86400 else f"{age/86400:.1f}d"
        print(f"{r['symbol']:10} {r['timeframe']:4} {human:>10}  {B.iso(r['last_ts'])}")
    return 0


def cmd_show(args) -> int:
    with db.connect() as conn:
        inv = db.inventory(conn, args.symbol)
        row = next((m for m in inv if m["timeframe"] == args.timeframe), None)
        if row is None:
            print(f"no stored series for {args.symbol} {args.timeframe}")
            return 1
        df = db.read_bars(conn, row["source"], args.symbol, args.timeframe,
                          as_of=_date(args.as_of),
                          include_partial=args.include_partial)
        ev = db.read_events(conn, row["source"], args.symbol)

    print(json.dumps({k: str(row[k]) for k in
                      ("source", "symbol", "timeframe", "asset_class",
                       "row_count", "feed_adjustment", "fetched_at")}, indent=2))
    if args.adjust != "none":
        df = ADJ.adjust(df, ev, args.adjust, args.timeframe)
    print(f"view: {args.adjust}   ({len(ev['splits'])} splits, "
          f"{len(ev['dividends'])} dividends on file)"
          + ("   [forming bar included]" if args.include_partial else ""))

    view = df.copy()
    view.insert(1, "utc", view["ts"].map(B.iso))
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(view.head(args.n).to_string(index=False))
        print("   ...")
        print(view.tail(args.n).to_string(index=False))
    return 0


def cmd_events(args) -> int:
    with db.connect() as conn:
        inv = db.inventory(conn, args.symbol)
        if not inv:
            print(f"{args.symbol} not stored")
            return 1
        ev = db.read_events(conn, inv[0]["source"], args.symbol)
    print(f"{args.symbol}: {len(ev['splits'])} splits, {len(ev['dividends'])} dividends")
    for s in ev["splits"]:
        print(f"  split    {B.iso(s['ts'])}  {s.get('label') or s['ratio']}")
    for d in ev["dividends"][-args.n:]:
        print(f"  dividend {B.iso(d['ts'])}  {d['amount']}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m btb.data",
                                description="Beyond the Backtest -- data layer")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create schema, register universe").set_defaults(fn=cmd_init)

    b = sub.add_parser("build", help="ingest the curated universe")
    b.add_argument("--asset-class", choices=["crypto", "equity"])
    b.add_argument("--symbol", nargs="*")
    b.add_argument("--timeframes", nargs="*")
    b.add_argument("--refresh", action="store_true",
                   help="re-fetch the whole tier instead of only new bars")
    b.set_defaults(fn=cmd_build)

    f = sub.add_parser("fetch", help="ingest one series")
    f.add_argument("--kind", required=True, choices=["crypto", "equity", "csv"])
    f.add_argument("--symbol", required=True)
    f.add_argument("--timeframe", required=True)
    f.add_argument("--venue")
    f.add_argument("--since")
    f.add_argument("--until")
    f.add_argument("--refresh", action="store_true")
    f.set_defaults(fn=cmd_fetch)

    v = sub.add_parser("verify", help="re-audit stored series")
    v.add_argument("--symbol")
    v.add_argument("--dry-run", action="store_true",
                   help="report without updating the stored manifest")
    v.set_defaults(fn=cmd_verify)

    l = sub.add_parser("list", help="inventory")
    l.add_argument("--symbol")
    l.set_defaults(fn=cmd_list)

    st = sub.add_parser("stale", help="series needing a refresh")
    st.add_argument("--grace", type=float, default=1.5,
                    help="bar-periods of tolerance before calling it stale")
    st.set_defaults(fn=cmd_stale)

    s = sub.add_parser("show", help="head/tail of a stored series")
    s.add_argument("--symbol", required=True)
    s.add_argument("--timeframe", required=True)
    s.add_argument("--adjust", choices=["none", "split", "total"], default="split")
    s.add_argument("--as-of", help="reproducibility bound (YYYY-MM-DD or epoch)")
    s.add_argument("--include-partial", action="store_true",
                   help="include the forming bar (never do this for a backtest)")
    s.add_argument("-n", type=int, default=5)
    s.set_defaults(fn=cmd_show)

    e = sub.add_parser("events", help="corporate actions")
    e.add_argument("--symbol", required=True)
    e.add_argument("-n", type=int, default=10)
    e.set_defaults(fn=cmd_events)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
