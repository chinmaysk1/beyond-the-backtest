"""What the worker does with a job: load the series, run, record.

Two job kinds, both payloads validated here rather than trusted from the web
app -- the app checks what it can, but this is the side that spends the CPU:

    backtest  {"strategy", "symbol", "timeframe", "params"?, "costs"?, "fill"?}
              One parameter set, run on train, test and full (train+test).
              The full run keeps its trades and equity curve for the UI.

    sweep     {"strategy", "symbol", "timeframe", "costs"?, "fill"?}
              Every grid combination on train and on test. Ranked on TEST.

Neither touches the holdout.
"""
from __future__ import annotations

import json
import time
from typing import Callable

from .. import db
from ..data import adjust as ADJ
from . import backtest as B
from . import spec as S
from . import windows as W
from .fills import Costs, default_costs

MAX_COMMISSION_PCT = 2.0
# A grid past this is a search, not a test -- and on one shared core, a way to
# occupy the worker for an hour. The web app refuses the same bound up front.
MAX_COMBOS = 500
MAX_SLIPPAGE_BPS = 200.0
SUMMARY_KEYS = ("net_pct", "cagr_pct", "max_dd_pct", "sharpe", "trades", "win_pct",
                "profit_factor", "bh_pct", "ruin")


class JobError(ValueError):
    """A job that cannot run as asked. The message is shown to the user."""


# -- inputs --------------------------------------------------------------------------

def load_series(conn, symbol: str, timeframe: str, adjust: str | None = None) -> B.Series:
    """Read one stored series, bounded at its newest complete bar.

    `as_of` is that bar's timestamp. Re-reading with it later returns the same
    bars, which is what makes a stored run reproducible after more data lands.
    """
    uni = db.universe_entry(conn, symbol)
    if not uni:
        raise JobError(f"{symbol} is not in the universe")
    if timeframe not in (uni["timeframes"] or []):
        raise JobError(f"{symbol} has no {timeframe} series")
    meta = db.series_meta(conn, symbol, timeframe)
    if not meta or not meta["last_ts"]:
        raise JobError(f"no stored bars for {symbol} {timeframe}")
    as_of = int(meta["last_ts"])
    df = db.read_bars(conn, meta["source"], symbol, timeframe, as_of=as_of)
    if len(df) < 200:
        raise JobError(f"only {len(df)} bars for {symbol} {timeframe}; too few to test on")

    asset_class = uni["asset_class"]
    mode = adjust or ("split" if asset_class == "equity" else "none")
    if mode not in ADJ.MODES:
        raise JobError(f"unknown adjustment {mode!r}")
    if mode != "none":
        df = ADJ.adjust(df, db.read_events(conn, meta["source"], symbol), mode, timeframe)
    return B.Series.from_frame(df, timeframe, symbol=symbol, source=meta["source"],
                               asset_class=asset_class, adjust=mode, as_of=as_of)


def parse_costs(raw: dict | None, asset_class: str) -> Costs:
    base = default_costs(asset_class)
    if not raw:
        return base
    try:
        c = float(raw.get("commission_pct", base.commission_pct))
        s = float(raw.get("slippage_bps", base.slippage_bps))
    except (TypeError, ValueError):
        raise JobError("costs must be numbers") from None
    if not (0 <= c <= MAX_COMMISSION_PCT) or not (0 <= s <= MAX_SLIPPAGE_BPS):
        raise JobError(f"commission must be 0-{MAX_COMMISSION_PCT}% and slippage "
                       f"0-{MAX_SLIPPAGE_BPS} bps")
    return Costs(c, s)


def _strategy(conn, payload: dict, user_id: str | None = None) -> dict:
    """The spec a job runs: a library strategy by name, or a user's own by id.

    A custom spec arrives from the web app, which checks what it can in the
    browser. This is where it is actually validated -- the engine is the only
    authority on the language -- and where ownership is enforced: a job may
    run the library or its own user's strategies, nobody else's.
    """
    sid = payload.get("strategy_id")
    if sid:
        row = db.strategy_by_id(conn, str(sid))
        if not row or (row["user_id"] is not None and row["user_id"] != user_id):
            raise JobError("unknown strategy")
    else:
        name = payload.get("strategy")
        row = db.latest_strategy(conn, str(name or ""))
        if not row:
            raise JobError(f"unknown strategy {name!r}")
    errs = S.validate(row["spec"])
    if errs:
        raise JobError("the strategy has problems: " + "; ".join(errs[:4])
                       + (f" (+{len(errs) - 4} more)" if len(errs) > 4 else ""))
    return row


def _fill(payload: dict) -> str:
    fill = payload.get("fill", "close")
    if fill not in ("close", "open"):
        raise JobError("fill must be 'close' or 'open'")
    return fill


def summary(m: dict) -> dict:
    return {k: m.get(k) for k in SUMMARY_KEYS}


# -- backtest --------------------------------------------------------------------------

def backtest_job(conn, payload: dict, *, job_id: int | None = None,
                 user_id: str | None = None) -> dict:
    t0 = time.perf_counter()
    strat = _strategy(conn, payload, user_id)
    spec = strat["spec"]
    try:
        params = S.resolve_params(spec, payload.get("params") or {})
    except S.SpecError as e:
        raise JobError(str(e)) from None

    series = load_series(conn, payload.get("symbol"), payload.get("timeframe"),
                         payload.get("adjust"))
    costs = parse_costs(payload.get("costs"), series.asset_class)
    fill = _fill(payload)
    cfg = B.config(costs, fill=fill, adjust=series.adjust)
    split = W.split(series.ts)

    runs, metrics = {}, {}
    for w in (split.train, split.test, split.full()):
        full = w.name == "full"
        r = B.run(spec, params, series, window=(w.start, w.end), costs=costs,
                  fill=fill, with_curve=full)
        rid, _ = db.insert_run(
            conn, job_id=job_id, strategy_id=strat["id"], symbol=series.symbol,
            timeframe=series.timeframe, params=r.params, window_start=r.window[0],
            window_end=r.window[1], as_of=series.as_of, split=w.name,
            metrics=r.metrics, config=cfg,
            trades=r.trades if full else None, equity=r.curve if full else None)
        runs[w.name] = rid
        metrics[w.name] = r.metrics
    conn.commit()
    return {
        "strategy": strat["name"], "spec_sha": strat["spec_sha"],
        "symbol": series.symbol, "timeframe": series.timeframe,
        "as_of": series.as_of, "adjust": series.adjust,
        "params": params, "config": cfg, "windows": split.as_dict(),
        "runs": runs, "metrics": metrics,
        "seconds": round(time.perf_counter() - t0, 2),
    }


# -- sweep ----------------------------------------------------------------------------

def sweep_series(spec: dict, series: B.Series, costs: Costs, *, fill: str = "close",
                 split: W.Split | None = None,
                 record: Callable[[str, dict, B.Result], None] | None = None,
                 done: Callable[[str, dict], dict | None] | None = None,
                 progress: Callable[[int, int], None] | None = None) -> dict:
    """Run every grid combination on train and test, and rank on test.

    `done(split_name, params)` returns stored metrics for a run that already
    exists (resume); `record(...)` stores a new one. Both are optional, so the
    same function serves the worker and an offline CSV sweep.
    """
    combos, raw = S.grid(spec)
    if not combos:
        raise JobError("the grid is empty after constraints")
    if len(combos) > MAX_COMBOS:
        raise JobError(f"the grid has {len(combos)} combinations; at most {MAX_COMBOS}")
    split = split or W.split(series.ts)
    rows = []
    total = len(combos) * 2
    k = reused = 0
    for p in combos:
        row = {"params": p}
        for w in (split.train, split.test):
            m = done(w.name, p) if done else None
            if m is None:
                r = B.run(spec, p, series, window=(w.start, w.end), costs=costs,
                          fill=fill, with_curve=False)
                m = r.metrics
                if record:
                    record(w.name, p, r)
            else:
                reused += 1
            row[w.name] = summary(m)
            k += 1
            if progress:
                progress(k, total)
        rows.append(row)

    def key(split_name):
        # Ruined runs sink to the bottom whatever their return says.
        return lambda r: (not r[split_name]["ruin"], r[split_name]["net_pct"] or -1e18)

    for rank, r in enumerate(sorted(rows, key=key("train"), reverse=True), 1):
        r["train_rank"] = rank
    ranked = sorted(rows, key=key("test"), reverse=True)
    for rank, r in enumerate(ranked, 1):
        r["test_rank"] = rank
    return {
        "combos": len(combos), "raw_grid": raw, "evaluated": total, "reused": reused,
        "ranked_on": "test", "windows": split.as_dict(),
        "top": ranked[:10],
        # [train_rank, test_rank] for every combination, for the rank scatter.
        "ranks": [[r["train_rank"], r["test_rank"]] for r in ranked],
        # Rank agreement across the whole grid. Near +1: settings that did well
        # on train also did well on test. Near 0 or below: the train ranking
        # carried no information -- the pattern the verdict engine will score.
        "rank_corr": _spearman([r["train_rank"] for r in rows],
                               [r["test_rank"] for r in rows]),
    }


def sweep_job(conn, payload: dict, *, job_id: int | None = None,
              user_id: str | None = None) -> dict:
    t0 = time.perf_counter()
    strat = _strategy(conn, payload, user_id)
    spec = strat["spec"]
    series = load_series(conn, payload.get("symbol"), payload.get("timeframe"),
                         payload.get("adjust"))
    costs = parse_costs(payload.get("costs"), series.asset_class)
    fill = _fill(payload)
    cfg = B.config(costs, fill=fill, adjust=series.adjust)
    split = W.split(series.ts)
    # split() snaps boundaries to real bars, so these are exactly the windows a
    # run records -- which is what lets a restarted sweep recognise its work.
    key = dict(strategy_id=strat["id"], symbol=series.symbol, timeframe=series.timeframe)
    existing = db.runs_for_windows(conn, config=cfg, windows={
        w.name: (w.start, w.end) for w in (split.train, split.test)}, **key)
    pending: list[dict] = []
    last = [time.perf_counter()]

    def done(name, p):
        return existing.get((name, json.dumps(p, sort_keys=True)))

    def record(name, p, r):
        pending.append(dict(job_id=job_id, params=r.params, window_start=r.window[0],
                            window_end=r.window[1], as_of=series.as_of, split=name,
                            metrics=r.metrics, config=cfg, **key))

    def flush():
        db.insert_runs(conn, pending)
        pending.clear()
        conn.commit()                  # recorded runs survive a crash

    def progress(k, total):
        # Batched about once a second: often enough that a killed worker loses
        # at most a second of work, rarely enough that the round trips to the
        # database do not dominate a sweep that computes in milliseconds.
        now = time.perf_counter()
        if now - last[0] > 1.0 or k == total:
            flush()
            if job_id is not None:
                db.job_progress(conn, job_id, k / total)
            last[0] = now

    out = sweep_series(spec, series, costs, fill=fill, split=split, record=record,
                       done=done, progress=progress)
    conn.commit()
    out.update({"strategy": strat["name"], "spec_sha": strat["spec_sha"],
                "symbol": series.symbol, "timeframe": series.timeframe,
                "as_of": series.as_of, "adjust": series.adjust, "config": cfg,
                "seconds": round(time.perf_counter() - t0, 2)})
    return out


def _spearman(a: list[int], b: list[int]) -> float | None:
    n = len(a)
    if n < 3:
        return None
    d2 = sum((x - y) ** 2 for x, y in zip(a, b))
    return round(1 - 6 * d2 / (n * (n * n - 1)), 4)


HANDLERS = {"backtest": backtest_job, "sweep": sweep_job}
