"""Postgres access. The only module that knows SQL.

Replaces the Parquet-file store. The move was not about size -- the whole
dataset is a couple of gigabytes -- but about three things files cannot do:

  * **Concurrent writers.** A background refresher writing while a sweep reads
    is two processes. Files are the wrong primitive for that; `INSERT ... ON
    CONFLICT` is exactly the right one.
  * **Appends without rewriting.** Adding one bar to a Parquet file rewrites
    all 33,000. Adding one row to a table adds one row.
  * **Questions.** "Which series are stale?" was a directory walk over JSON
    sidecars. It is now a single query.

Connection settings come from the environment, with a local default that
matches the SSH tunnel used in development:

    BTB_DB_HOST  (default 127.0.0.1)
    BTB_DB_PORT  (default 55432 -- the tunnel; 5432 when running on the server)
    BTB_DB_NAME / BTB_DB_USER / BTB_DB_PASSWORD

The database is never published to a public interface. In development it is
reached through `ssh -L 55432:127.0.0.1:5432`; in production the app and worker
containers reach it over the Docker network. There is no configuration in which
it listens on 0.0.0.0.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import psycopg
from psycopg.rows import dict_row

from .data.bars import COLUMNS, DTYPES, empty_frame

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def dsn() -> str:
    host = os.environ.get("BTB_DB_HOST", "127.0.0.1")
    port = os.environ.get("BTB_DB_PORT", "55432")
    name = os.environ.get("BTB_DB_NAME", "btb")
    user = os.environ.get("BTB_DB_USER", "btb")
    pw = os.environ.get("BTB_DB_PASSWORD") or _password_from_env_file()
    return f"host={host} port={port} dbname={name} user={user} password={pw}"


def _password_from_env_file() -> str:
    """Read .env.local rather than requiring it to be exported every shell.

    Deliberately not a general dotenv loader: one key, no side effects on the
    process environment, and the file is gitignored.
    """
    f = Path(__file__).resolve().parents[1] / ".env.local"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.startswith("PGPASSWORD="):
                return line.split("=", 1)[1].strip()
    return ""


@contextmanager
def connect(**kw):
    with psycopg.connect(dsn(), **kw) as conn:
        yield conn


def init_schema() -> None:
    with connect() as conn:
        conn.execute(SCHEMA_FILE.read_text())
        conn.commit()


# -- bars ---------------------------------------------------------------------

def write_bars(conn, df: pd.DataFrame, source: str, symbol: str, timeframe: str,
               *, partial_last: bool = False) -> int:
    """Upsert bars. Returns the number of rows sent.

    `partial_last` marks the newest bar as still forming. Only that one bar is
    ever expected to change on a later write, and marking it is what lets a
    research read exclude it without having to reason about wall-clock time.

    ON CONFLICT DO UPDATE rather than DO NOTHING: a venue correcting a bad print
    should land, not be silently discarded. In practice it fires on the forming
    bar and almost nowhere else.
    """
    if df.empty:
        return 0

    rows = []
    last = len(df) - 1
    for i, r in enumerate(df.itertuples(index=False)):
        rows.append((source, symbol, timeframe, int(r.ts), float(r.open),
                     float(r.high), float(r.low), float(r.close),
                     float(r.volume), bool(partial_last and i == last)))

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO bars (source, symbol, timeframe, ts, open, high, low,
                              close, volume, partial)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (source, symbol, timeframe, ts) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high,
                low = EXCLUDED.low,   close = EXCLUDED.close,
                volume = EXCLUDED.volume, partial = EXCLUDED.partial
            """, rows)
    return len(rows)


def read_bars(conn, source: str, symbol: str, timeframe: str, *,
              since=None, until=None, as_of=None,
              include_partial: bool = False) -> pd.DataFrame:
    """Read one series as a canonical frame.

    `as_of` is the reproducibility bound. Immutable rows are not enough on their
    own -- the same query next year returns more bars, because more bars exist.
    A run records its as_of and re-reading with it returns the same series.

    `include_partial` defaults to False. A forming bar's close is the current
    price, not a close; letting one into a backtest gives the strategy a look at
    an unfinished period. Live views pass True deliberately.
    """
    sql = ["SELECT ts, open, high, low, close, volume FROM bars",
           "WHERE source=%s AND symbol=%s AND timeframe=%s"]
    args: list = [source, symbol, timeframe]
    if not include_partial:
        sql.append("AND partial = false")
    if since is not None:
        sql.append("AND ts >= %s"); args.append(int(since))
    bound = as_of if as_of is not None else until
    if bound is not None:
        sql.append("AND ts <= %s"); args.append(int(bound))
    sql.append("ORDER BY ts")

    with conn.cursor() as cur:
        cur.execute(" ".join(sql), args)
        rows = cur.fetchall()
    if not rows:
        return empty_frame()
    return pd.DataFrame(rows, columns=COLUMNS).astype(DTYPES)


def first_bar_ts(conn, source: str, symbol: str, timeframe: str):
    """Oldest bar we hold. Needed to notice a hole at the FRONT of a series."""
    with conn.cursor() as cur:
        cur.execute("SELECT min(ts) FROM bars WHERE source=%s AND symbol=%s "
                    "AND timeframe=%s", (source, symbol, timeframe))
        (v,) = cur.fetchone()
    return int(v) if v is not None else None


def count_bars(conn, source: str, symbol: str, timeframe: str, *,
               until=None, include_partial: bool = False) -> int:
    """How many bars we hold. A COUNT, not a full read.

    Ingest reports how many bars a fetch added, which means comparing before and
    after. Doing that by re-reading the series pulls every row across the wire
    to measure its length -- on a 300,000-bar 5m series that is the single most
    expensive thing in an ingest that added four bars.
    """
    sql = ["SELECT count(*) FROM bars",
           "WHERE source=%s AND symbol=%s AND timeframe=%s"]
    args: list = [source, symbol, timeframe]
    if not include_partial:
        sql.append("AND partial = false")
    if until is not None:
        sql.append("AND ts <= %s"); args.append(int(until))
    with conn.cursor() as cur:
        cur.execute(" ".join(sql), args)
        (n,) = cur.fetchone()
    return int(n)


def last_bar_ts(conn, source: str, symbol: str, timeframe: str):
    """Newest bar we hold, forming or not.

    Incremental fetches resume from here. It must include the partial bar --
    resuming after it would leave the partial permanently unrefreshed.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT max(ts) FROM bars WHERE source=%s AND symbol=%s "
                    "AND timeframe=%s", (source, symbol, timeframe))
        (v,) = cur.fetchone()
    return int(v) if v is not None else None


# -- events -------------------------------------------------------------------

def write_events(conn, events: dict, source: str, symbol: str) -> int:
    rows = [(source, symbol, int(s["ts"]), "split", float(s["ratio"]), None,
             s.get("label")) for s in events.get("splits", [])]
    rows += [(source, symbol, int(d["ts"]), "dividend", None,
              float(d["amount"]), None) for d in events.get("dividends", [])]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO events (source, symbol, ts, kind, ratio, amount, label)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (source, symbol, ts, kind) DO UPDATE SET
                ratio = EXCLUDED.ratio, amount = EXCLUDED.amount,
                label = EXCLUDED.label
            """, rows)
    return len(rows)


def read_events(conn, source: str, symbol: str) -> dict:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT ts, kind, ratio, amount, label FROM events "
                    "WHERE source=%s AND symbol=%s ORDER BY ts", (source, symbol))
        rows = cur.fetchall()
    return {
        "splits": [{"ts": r["ts"], "ratio": r["ratio"], "label": r["label"]}
                   for r in rows if r["kind"] == "split"],
        "dividends": [{"ts": r["ts"], "amount": r["amount"]}
                      for r in rows if r["kind"] == "dividend"],
    }


# -- series manifest ----------------------------------------------------------

def upsert_series(conn, source: str, symbol: str, timeframe: str, **fields) -> None:
    cols = ["source", "symbol", "timeframe"] + list(fields)
    vals = [source, symbol, timeframe] + [
        json.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in fields)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO series ({', '.join(cols)}) "
            f"VALUES ({', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT (source, symbol, timeframe) DO UPDATE SET {sets}", vals)


def inventory(conn, symbol: str | None = None) -> list[dict]:
    sql = ("SELECT source, symbol, timeframe, asset_class, first_ts, last_ts, "
           "row_count, fetched_at, checked_at, feed_adjustment, integrity "
           "FROM series")
    args: list = []
    if symbol:
        sql += " WHERE symbol = %s"
        args.append(symbol)
    sql += " ORDER BY asset_class, symbol, row_count DESC"
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def stale_series(conn, grace: float = 1.5) -> list[dict]:
    """Series whose newest complete bar is older than `grace` bar-periods.

    One query replaces opening every manifest on disk. This is what the
    background refresher polls, and what the web app checks before serving --
    it costs a single index scan and no network call.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT s.source, s.symbol, s.timeframe, s.last_ts,
                   EXTRACT(EPOCH FROM now())::bigint - s.last_ts AS age
            FROM series s
            ORDER BY age DESC
            """)
        rows = cur.fetchall()
    from .data.bars import tf_seconds
    out = []
    for r in rows:
        if r["last_ts"] is None:
            out.append(r); continue
        if r["age"] > grace * tf_seconds(r["timeframe"]):
            out.append(r)
    return out


# -- engine: strategies ---------------------------------------------------------

def upsert_strategy(conn, strategy_id: str, name: str, spec: dict, sha: str,
                    user_id: str | None = None) -> None:
    """Insert a strategy version. Ids derive from (name, sha), so loading the
    same spec twice is a no-op and an edited spec becomes a new row -- old runs
    stay attached to the exact spec that produced them."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO strategies (id, user_id, name, spec, spec_sha) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (strategy_id, user_id, name, json.dumps(spec), sha))


def latest_strategy(conn, name: str) -> dict | None:
    """The newest library version of a strategy (user_id IS NULL)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id::text AS id, name, spec, spec_sha FROM strategies "
                    "WHERE name = %s AND user_id IS NULL "
                    "ORDER BY created_at DESC LIMIT 1", (name,))
        return cur.fetchone()


def strategy_by_id(conn, strategy_id: str) -> dict | None:
    """One strategy version by id -- how a user's own (custom) spec is found."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id::text AS id, user_id::text AS user_id, name, spec, spec_sha "
                    "FROM strategies WHERE id = %s", (strategy_id,))
        return cur.fetchone()


def universe_entry(conn, symbol: str) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT symbol, source, asset_class, timeframes, active "
                    "FROM universe WHERE symbol = %s", (symbol,))
        return cur.fetchone()


def series_meta(conn, symbol: str, timeframe: str) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT source, symbol, timeframe, asset_class, first_ts, last_ts, "
                    "row_count, checked_at FROM series WHERE symbol=%s AND timeframe=%s "
                    "ORDER BY row_count DESC LIMIT 1", (symbol, timeframe))
        return cur.fetchone()


# -- engine: runs -----------------------------------------------------------------

_RUN_KEY = """
    strategy_id=%s AND symbol=%s AND timeframe=%s AND split=%s
    AND window_start=%s AND window_end=%s
    AND md5(params::text) = md5(%s::jsonb::text)
    AND md5(config::text) = md5(%s::jsonb::text)
"""


def insert_run(conn, *, job_id, strategy_id, symbol, timeframe, params, window_start,
               window_end, as_of, split, metrics, config, trades=None, equity=None
               ) -> tuple[int, bool]:
    """Record one evaluated configuration. Returns (run id, newly inserted).

    ON CONFLICT DO NOTHING against `runs_dedupe` is what lets an interrupted
    sweep resume: work already recorded is recognised instead of redone. The
    key includes `config` (costs, fill model, adjustment), because two runs
    differing only in commission are different results.
    """
    p, c = json.dumps(params, sort_keys=True), json.dumps(config, sort_keys=True)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO runs (job_id, strategy_id, symbol, timeframe, params,
                              window_start, window_end, as_of, split, metrics,
                              config, trades, equity)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (job_id, strategy_id, symbol, timeframe, p, int(window_start),
             int(window_end), int(as_of), split, json.dumps(metrics), c,
             json.dumps(trades) if trades is not None else None,
             json.dumps(equity) if equity is not None else None))
        row = cur.fetchone()
        if row:
            return int(row[0]), True
        cur.execute("SELECT id FROM runs WHERE" + _RUN_KEY,
                    (strategy_id, symbol, timeframe, split, int(window_start),
                     int(window_end), p, c))
        return int(cur.fetchone()[0]), False


def insert_runs(conn, rows: list[dict]) -> None:
    """Batch form of insert_run for sweeps: one round trip for many rows.

    Same dedupe semantics (ON CONFLICT DO NOTHING). Sweep rows carry no trades
    or curve, and their ids are not needed, so nothing is returned.
    """
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO runs (job_id, strategy_id, symbol, timeframe, params,
                              window_start, window_end, as_of, split, metrics, config)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
            """,
            [(r["job_id"], r["strategy_id"], r["symbol"], r["timeframe"],
              json.dumps(r["params"], sort_keys=True), int(r["window_start"]),
              int(r["window_end"]), int(r["as_of"]), r["split"], json.dumps(r["metrics"]),
              json.dumps(r["config"], sort_keys=True)) for r in rows])


def runs_for_windows(conn, *, strategy_id, symbol, timeframe, config,
                     windows: dict[str, tuple[int, int]]) -> dict:
    """Every recorded run for these windows and config, keyed (split, params).

    One query instead of one lookup per combination -- the difference between
    a sweep that resumes instantly and one that spends most of its time on
    network round trips.
    """
    out: dict = {}
    with conn.cursor(row_factory=dict_row) as cur:
        for split, (start, end) in windows.items():
            cur.execute(
                """
                SELECT params, metrics FROM runs
                WHERE strategy_id=%s AND symbol=%s AND timeframe=%s AND split=%s
                  AND window_start=%s AND window_end=%s
                  AND md5(config::text) = md5(%s::jsonb::text)
                """,
                (strategy_id, symbol, timeframe, split, int(start), int(end),
                 json.dumps(config, sort_keys=True)))
            for r in cur.fetchall():
                out[(split, json.dumps(r["params"], sort_keys=True))] = r["metrics"]
    return out


def find_run(conn, *, strategy_id, symbol, timeframe, split, window_start,
             window_end, params, config) -> dict | None:
    """A previously recorded run with exactly this key, if any."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id, metrics FROM runs WHERE" + _RUN_KEY,
                    (strategy_id, symbol, timeframe, split, int(window_start),
                     int(window_end), json.dumps(params, sort_keys=True),
                     json.dumps(config, sort_keys=True)))
        return cur.fetchone()


# -- engine: jobs ------------------------------------------------------------------

def enqueue_job(conn, kind: str, payload: dict, *, user_id=None, priority: int = 100) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO jobs (user_id, kind, payload, priority) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (user_id, kind, json.dumps(payload), priority))
        return int(cur.fetchone()[0])


def claim_job(conn, worker: str, kinds: list[str]) -> dict | None:
    """Take the next queued job, atomically.

    FOR UPDATE SKIP LOCKED means two workers polling at once each get a
    different job, or nothing -- never the same one. Committed before
    returning, so the claim is visible to everyone while the job runs.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            UPDATE jobs SET status='running', claimed_by=%s, claimed_at=now(),
                            progress=0
            WHERE id = (SELECT id FROM jobs
                        WHERE status='queued' AND kind = ANY(%s)
                        ORDER BY priority, id
                        FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING id, user_id::text AS user_id, kind, payload
            """, (worker, kinds))
        row = cur.fetchone()
    conn.commit()
    return row


def has_queued_job(conn, kinds: list[str]) -> bool:
    """One index probe (jobs_queue is partial on status='queued')."""
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM jobs WHERE status='queued' AND kind = ANY(%s))",
                    (kinds,))
        (v,) = cur.fetchone()
    conn.commit()
    return bool(v)


def job_progress(conn, job_id: int, progress: float) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET progress=%s WHERE id=%s", (float(progress), job_id))
    conn.commit()


def finish_job(conn, job_id: int, *, result: dict | None = None,
               error: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status=%s, finished_at=now(), result=%s, error=%s, "
            "progress = CASE WHEN %s THEN 1 ELSE progress END WHERE id=%s",
            ("failed" if error else "done",
             json.dumps(result) if result is not None else None,
             error, error is None, job_id))
    conn.commit()


def requeue_stale_jobs(conn, older_than_s: int = 1800) -> int:
    """Jobs left 'running' by a worker that died go back in the queue.

    Safe because runs are deduplicated: a half-finished sweep that restarts
    skips everything it already recorded.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status='queued', claimed_by=NULL, claimed_at=NULL "
            "WHERE status='running' AND claimed_at < now() - make_interval(secs => %s)",
            (older_than_s,))
        n = cur.rowcount
    conn.commit()
    return n


# -- refresher ---------------------------------------------------------------------

def refresh_candidates(conn) -> list[dict]:
    """Every stored series, with how stale it is and when it was last asked for."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT source, symbol, timeframe, asset_class, last_ts,
                   EXTRACT(EPOCH FROM now())::bigint - last_ts AS age,
                   EXTRACT(EPOCH FROM now() - checked_at)::bigint AS since_check
            FROM series
            """)
        return cur.fetchall()
