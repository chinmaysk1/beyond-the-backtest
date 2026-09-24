"""Keep stored series current: fetch whatever has closed since the last read.

Runs inside the worker whenever it has no job, so the database stays fresh as
long as the worker is up. Nothing here is new ingest logic -- it decides WHAT
is due and hands each one to `ingest_series`, the single ingest path, so every
refreshed bar goes through the same audit as a backfill.

When a series is due:

    A series whose newest complete bar opened at T has its next bar complete at
    T + 2 * step. Before then there is nothing new to fetch; after it, there is.

Throttling. Never more than once per 5 minutes. And once a check made well
AFTER the next bar was due came back with nothing -- a closed market, equities
overnight or at the weekend -- back off to once per bar period. A check made
just BEFORE the bar closed does not count: backing off then would leave a 4h
series up to four hours stale, which is the bug this replaced.
"""
from __future__ import annotations

import time

from ..data.bars import tf_seconds
from ..data.ingest import ingest_series, load_universe

MIN_RECHECK = 300
# How long after a bar is due a venue may take to publish it before an empty
# answer is read as "market closed".
PUBLISH_GRACE = 600
_ORDER = {tf: i for i, tf in enumerate(["1d", "4h", "1h", "30m", "15m", "5m", "1m"])}


def due(candidates: list[dict], plan: list[dict]) -> list[dict]:
    """Universe entries whose stored series has a newly completed bar."""
    stored = {(c["symbol"], c["timeframe"]): c for c in candidates}
    out = []
    for e in plan:
        step = tf_seconds(e["timeframe"])
        c = stored.get((e["symbol"], e["timeframe"]))
        if c is None or c["last_ts"] is None:
            continue                      # never backfilled: that is `build`'s job
        age, since = c["age"], c["since_check"]
        if age < 2 * step:
            continue                      # the next bar has not closed yet
        if since is not None:
            overdue_for = age - 2 * step  # seconds since the next bar was due
            asked_after_due = since < overdue_for - PUBLISH_GRACE
            if since < MIN_RECHECK or (asked_after_due and since < step):
                continue
        out.append(e)
    out.sort(key=lambda e: (_ORDER.get(e["timeframe"], 99), e["symbol"]))
    return out


def refresh_once(conn, *, budget_s: float = 120.0, log=print, yield_to=None) -> dict:
    """Refresh every due series, stopping after `budget_s`, or as soon as
    `yield_to()` says a job is waiting -- a user who clicked Run should not sit
    behind a data catch-up. Whatever was skipped is still due next cycle."""
    from .. import db

    todo = due(db.refresh_candidates(conn), load_universe())
    t0 = time.perf_counter()
    done = failed = added = processed = 0
    for e in todo:
        if time.perf_counter() - t0 > budget_s or (yield_to and yield_to()):
            break
        processed += 1
        kw = {k: e[k] for k in ("venue",) if e.get(k)}
        try:
            r = ingest_series(conn, e["kind"], e["symbol"], e["timeframe"],
                              since=_epoch(e.get("since")), asset_class=e["asset_class"],
                              **kw)
        except Exception as ex:                                   # noqa: BLE001
            conn.rollback()
            failed += 1
            log(f"refresh ERR {e['symbol']} {e['timeframe']}: {type(ex).__name__}: {str(ex)[:120]}")
            continue
        done += 1
        added += r.added
        if not r.ok:
            failed += 1
        log(f"refresh {r.line()}")
    return {"due": len(todo), "processed": processed, "done": done, "failed": failed,
            "added": added,
            "seconds": round(time.perf_counter() - t0, 1)}


def _epoch(v):
    """universe.json pins `since` as YYYY-MM-DD or epoch seconds."""
    if v in (None, ""):
        return None
    s = str(v)
    if s.isdigit():
        return int(s)
    import pandas as pd
    return int(pd.Timestamp(s, tz="UTC").timestamp())
