"""What the worker does with a job: load the series, run, record.

Three job kinds, every payload validated here rather than trusted from the web
app -- the app checks what it can, but this is the side that spends the CPU:

    backtest  {"strategy", "symbol", "timeframe", "params"?, "costs"?, "fill"?,
               "windows"?}
              One parameter set, run on train, test and full (train+test).
              The full run keeps its trades and equity curve for the UI.

    sweep     {"strategy", "symbol", "timeframe", "costs"?, "fill"?, "windows"?}
              Every grid combination on train and on test. Ranked on TEST.
              Ends with the verdict (btb.verdict), always on the default windows.

    check     {"source"}
              A Pine script checked (btb.pine.check): errors, warnings, inputs,
              grid, costs. Nothing runs. The web app's check button and the MCP
              check_pine tool are answered from this.

`windows` moves train and test: {"train": [start, end], "test": [start, end]}
in unix seconds (see windows.custom). Omitted, the default split is used.

Neither touches the holdout, and no `windows` can make them.

Strategies are Pine (btb.pine). A strategy row in the retired JSON format can
still be opened -- its stored results are untouched -- but not run again.
"""
from __future__ import annotations

import json
import time
from typing import Callable

from psycopg.types.json import Jsonb

from .. import db
from .. import verdict as V
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


# Checked and transpiled scripts, by source sha. Compiled libraries are cached
# on disk by native.compile; this saves re-transpiling on every job.
_PROGRAMS: dict = {}


def program_for(source: str, sha: str | None = None):
    from ..pine.program import PineError, Program
    key = sha or source
    prog = _PROGRAMS.get(key)
    if prog is None:
        try:
            prog = Program.from_source(source)
        except PineError as e:
            raise JobError("the strategy has problems: " + str(e)) from None
        if len(_PROGRAMS) > 64:
            _PROGRAMS.clear()
        _PROGRAMS[key] = prog
    return prog


def _strategy(conn, payload: dict, user_id: str | None = None) -> dict:
    """The strategy a job runs: a library strategy by name, or a user's own by
    id, with its checked `program`.

    A script arrives from the web app or the MCP server. This is where it is
    actually checked -- the engine is the only authority on what runs -- and
    where ownership is enforced: a job may run the library or its own user's
    strategies, nobody else's.
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
    if row.get("language") != "pine":
        raise JobError("this test was made with the retired JSON strategy format; its "
                       "results stay readable, but it cannot be run again")
    row["program"] = program_for(row["source"], row["source_sha"])
    return row


def _feeds(conn, prog, series: B.Series) -> list[dict]:
    """The other markets the script reads, loaded as symbol feeds -- each bounded
    by its own newest complete bar, like the chart series."""
    from ..pine.program import feed_of
    out = []
    for f in prog.feeds:
        other = load_series(conn, f["symbol"], f["timeframe"])
        out.append(feed_of(f, other))
    return out


def _fill(payload: dict) -> str:
    fill = payload.get("fill", "close")
    if fill not in ("close", "open"):
        raise JobError("fill must be 'close' or 'open'")
    return fill


def _split(payload: dict, series: B.Series) -> W.Split:
    try:
        return W.custom(series.ts, payload.get("windows"))
    except W.WindowError as e:
        raise JobError(str(e)) from None


def _windows_out(split: W.Split, series: B.Series) -> dict:
    """The windows a job ran on, the defaults, and what may be moved: the UI
    draws all three and resets to the second."""
    base = W.split(series.ts)
    return {"windows": split.as_dict(), "default_windows": base.as_dict(),
            "movable": W.movable(series.ts, base)}


def summary(m: dict) -> dict:
    return {k: m.get(k) for k in SUMMARY_KEYS}


# -- backtest --------------------------------------------------------------------------

def backtest_job(conn, payload: dict, *, job_id: int | None = None,
                 user_id: str | None = None) -> dict:
    from ..pine.program import PineError, run_windows
    t0 = time.perf_counter()
    strat = _strategy(conn, payload, user_id)
    prog = strat["program"]
    try:
        params = prog.resolve_params(payload.get("params") or {})
    except PineError as e:
        raise JobError(str(e)) from None

    series = load_series(conn, payload.get("symbol"), payload.get("timeframe"),
                         payload.get("adjust"))
    # The script's own costs if it sets any; these defaults only if it does not.
    costs = parse_costs(payload.get("costs"), series.asset_class)
    cfg = prog.config(costs, series.adjust)
    split = _split(payload, series)

    # Train, test, both, and the holdout: each a fresh run with its own metrics.
    # Then one continuous run from train start to the newest bar, whose curve
    # and trades the page shows -- a position open at the end of test carries
    # on into the holdout, as it would have.
    #
    # `extend` names a finished backtest to bring up to date instead: same
    # settings and train/test windows (passed in `windows`), but only the
    # holdout and the continuous run are redone, on the newest bars, and the
    # results are written back onto that backtest. Its train/test numbers and
    # its score stay exactly as they were.
    extend = payload.get("extend")
    if extend is not None and (isinstance(extend, bool) or not isinstance(extend, int) or extend < 1):
        raise JobError("extend must be a job id")
    runs, metrics = {}, {}
    wins = [split.train, split.test, split.full(), split.through()]
    if split.holdout.end > split.holdout.start:
        wins.insert(3, split.holdout)
    if extend is not None:
        wins = [w for w in wins if w.name in ("holdout", "all")]
    try:
        results = run_windows(prog, series, costs, params, [(w.start, w.end) for w in wins],
                              [w.name == "all" for w in wins], feeds=_feeds(conn, prog, series))
    except Exception as e:                      # sandbox / native failures, shown to the user
        raise JobError(str(e)) from None
    for w, r in zip(wins, results):
        drawn = w.name == "all"
        rid, _ = db.insert_run(
            conn, job_id=job_id, strategy_id=strat["id"], symbol=series.symbol,
            timeframe=series.timeframe, params=r.params, window_start=r.window[0],
            window_end=r.window[1], as_of=series.as_of, split=w.name,
            metrics=r.metrics, config=cfg,
            trades=r.trades if drawn else None, equity=r.curve if drawn else None)
        runs[w.name] = rid
        if w.name != "all":
            metrics[w.name] = r.metrics
    if extend is not None:
        _extend(conn, extend, user_id, split, series, runs, metrics)
    conn.commit()
    return {
        "strategy": strat["name"], "spec_sha": strat["spec_sha"],
        "language": "pine", "source_sha": strat["source_sha"], "scored": prog.scored,
        "symbol": series.symbol, "timeframe": series.timeframe,
        "as_of": series.as_of, "adjust": series.adjust,
        "params": params, "config": cfg, **_windows_out(split, series),
        "runs": runs, "metrics": metrics,
        "seconds": round(time.perf_counter() - t0, 2),
    }


def _extend(conn, job_id: int, user_id: str | None, split: W.Split, series: B.Series,
            runs: dict, metrics: dict) -> None:
    """Write a fresh holdout onto a finished backtest of the same user, keeping
    everything else it holds. Refused unless its train/test windows are the
    ones just run, so the holdout always starts on the bar after its test."""
    row = conn.execute("SELECT result FROM jobs WHERE id = %s AND kind = 'backtest' "
                       "AND status = 'done' AND user_id IS NOT DISTINCT FROM %s",
                       (job_id, user_id)).fetchone()
    if not row or not row[0]:
        raise JobError(f"extend: no finished backtest {job_id}")
    old = row[0]
    w = old.get("windows") or {}
    if (w.get("train"), w.get("test")) != (split.train.as_dict(), split.test.as_dict()):
        raise JobError("extend: train and test must be the backtest's own windows")
    new = {**old, "as_of": series.as_of,
           "windows": {**w, "holdout": split.holdout.as_dict()},
           "runs": {**old.get("runs", {}), **runs},
           "metrics": {**old.get("metrics", {}), **metrics}}
    conn.execute("UPDATE jobs SET result = %s WHERE id = %s", (Jsonb(new), job_id))


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
                          fill=fill, with_curve=False, with_trades=False)
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

    return _ranked(rows, combos=len(combos), raw_grid=raw, evaluated=total, reused=reused,
                   windows=split.as_dict())


def _ranked(rows: list[dict], **head) -> dict:
    """Rank every row on train and on test, and lay the result out as the
    pages read it. Shared by the JSON sweep and the Pine sweep."""
    def key(split_name):
        # Ruined runs sink to the bottom whatever their return says.
        return lambda r: (not r[split_name]["ruin"], r[split_name]["net_pct"] or -1e18)

    for rank, r in enumerate(sorted(rows, key=key("train"), reverse=True), 1):
        r["train_rank"] = rank
    ranked = sorted(rows, key=key("test"), reverse=True)
    for rank, r in enumerate(ranked, 1):
        r["test_rank"] = rank
    return {
        **head, "ranked_on": "test",
        "top": ranked[:10],
        # Every combination, test-ranked, for the test page's full table.
        "rows": ranked,
        # [train_rank, test_rank] for every combination, for the rank scatter.
        "ranks": [[r["train_rank"], r["test_rank"]] for r in ranked],
        # Rank agreement across the whole grid. Near +1: settings that did well
        # on train also did well on test. Near 0 or below: the train ranking
        # carried no information -- the pattern the verdict engine will score.
        "rank_corr": _spearman([r["train_rank"] for r in rows],
                               [r["test_rank"] for r in rows]),
    }


def pine_sweep_series(prog, series: B.Series, costs: Costs, *, split: W.Split | None = None,
                      progress: Callable[[int, int], None] | None = None,
                      feeds: list[dict] | None = None) -> dict:
    """The Pine sweep: every grid combination once over train + test (see
    btb.pine.program.sweep), ranked on test. Same shape as sweep_series'
    result, plus `matrix`, the verdict's returns from the same runs, and
    `_window_bounds` / per-row `_metrics` for recording, which the caller
    removes."""
    from ..pine.program import sweep
    split = split or W.split(series.ts)
    if not prog.grid.combos:
        raise JobError("the grid is empty after constraints")
    try:
        if prog.feeds and feeds is None:
            raise JobError("this script reads other markets, which need the database")
        raw = sweep(prog, series, costs, split, progress=progress, feeds=feeds)
    except Exception as e:
        raise JobError(str(e)) from None
    rows = [{"params": r["params"], "train": summary(r["train"]), "test": summary(r["test"]),
             "_metrics": (r["train"], r["test"])} for r in raw["rows"]]
    out = _ranked(rows, combos=len(rows), raw_grid=prog.grid.raw, evaluated=len(rows),
                  reused=0, windows=split.as_dict(), runs_per_combination=1)
    out["matrix"] = raw["matrix"]
    out["_window_bounds"] = raw["windows"]
    return out


def sweep_job(conn, payload: dict, *, job_id: int | None = None,
              user_id: str | None = None) -> dict:
    t0 = time.perf_counter()
    strat = _strategy(conn, payload, user_id)
    prog = strat["program"]
    series = load_series(conn, payload.get("symbol"), payload.get("timeframe"),
                         payload.get("adjust"))
    costs = parse_costs(payload.get("costs"), series.asset_class)
    cfg = prog.config(costs, series.adjust)
    split = _split(payload, series)
    head = {"strategy": strat["name"], "spec_sha": strat["spec_sha"], "language": "pine",
            "source_sha": strat["source_sha"], "symbol": series.symbol,
            "timeframe": series.timeframe, "as_of": series.as_of, "adjust": series.adjust,
            "config": cfg, **_windows_out(split, series)}
    if not prog.scored:
        # No // @grid lines: the backtest job ran the one setting there is.
        # There is nothing to rank and nothing to score.
        return {**head, "scored": False, "combos": 1, "top": [], "rows": [], "ranks": [],
                "reason": "No // @grid lines, so only the default settings ran. Add @grid "
                          "lines to sweep and score it.",
                "seconds": round(time.perf_counter() - t0, 2)}

    key = dict(strategy_id=strat["id"], symbol=series.symbol, timeframe=series.timeframe)
    last = [time.perf_counter()]

    def progress(k, total):
        now = time.perf_counter()
        if job_id is not None and (now - last[0] > 1.0 or k == total):
            db.job_progress(conn, job_id, 0.98 * k / total)
            conn.commit()
            last[0] = now

    def record(res):
        bounds = res.pop("_window_bounds")
        db.insert_runs(conn, [
            dict(job_id=job_id, params=r["params"], window_start=bounds[name][0],
                 window_end=bounds[name][1], as_of=series.as_of, split=name,
                 metrics=m, config=cfg, **key)
            for r in res["rows"] for name, m in zip(("train", "test"), r.pop("_metrics"))])
        conn.commit()

    feeds = _feeds(conn, prog, series)
    out = pine_sweep_series(prog, series, costs, split=split, progress=progress, feeds=feeds)
    record(out)
    # The verdict is always scored on the DEFAULT windows: one that moved with
    # dragged windows could be dragged until it looked good.
    base = W.split(series.ts)
    ranked = out
    if split != base:
        ranked = pine_sweep_series(prog, series, costs, split=base, feeds=feeds)
        record(ranked)
    mx = ranked.pop("matrix")
    out.pop("matrix", None)
    out["verdict"] = V.evaluate(prog, series, costs, ranked, split=base, matrix=mx)
    out.update({**head, "scored": True, "seconds": round(time.perf_counter() - t0, 2)})
    return out


def check_job(conn, payload: dict, *, job_id: int | None = None,
              user_id: str | None = None) -> dict:
    """A script checked, plus -- when it passes -- the name and the `spec`
    description the web app stores with it, built exactly as the library's
    are, so the pages read a pasted script and a library one the same way."""
    import re
    from ..pine.check import check
    from ..pine.library import spec_of
    source = payload.get("source")
    if not isinstance(source, str):
        raise JobError("check needs a 'source'")
    r = check(source)
    out = r.as_dict()
    if r.ok:
        name = re.sub(r"[^a-z0-9]+", "_", (r.title or "").lower()).strip("_")[:64]
        name = name if re.match(r"^[a-z]", name) else f"pine_{name}" if name else "pine_strategy"
        out["name"] = name
        out["spec"] = spec_of(name, r)
    return out


def _spearman(a: list[int], b: list[int]) -> float | None:
    n = len(a)
    if n < 3:
        return None
    d2 = sum((x - y) ** 2 for x, y in zip(a, b))
    return round(1 - 6 * d2 / (n * (n * n - 1)), 4)


HANDLERS = {"backtest": backtest_job, "sweep": sweep_job, "check": check_job}
