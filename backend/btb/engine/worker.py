"""The worker: claim jobs from Postgres, run them, write results back.

    python -m btb.engine worker

The web app never calls this. It inserts a `jobs` row and returns; this loop
claims it with FOR UPDATE SKIP LOCKED, so any number of workers -- the one on
the server, one on a laptop -- can drain the same queue without coordination.

When the queue is empty it refreshes stale market data (btb.engine.refresh),
which is what keeps the stored series current while the worker is up.

Resilience, in the order it matters:

* A job that raises is marked failed with its message; the loop carries on.
* A lost database connection (the server restarting, a dropped tunnel) is
  retried with backoff rather than killing the process.
* A job left 'running' by a worker that died is requeued after 30 minutes.
  Runs are deduplicated, so a requeued sweep resumes rather than repeats.
"""
from __future__ import annotations

import os
import socket
import time
import traceback

import psycopg

from .. import db
from .jobs import HANDLERS, JobError
from .spec import SpecError
from .refresh import refresh_once


def _log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def run(*, name: str | None = None, refresh: bool = True, refresh_every: float = 300,
        poll: float = 1.0, once: bool = False) -> None:
    name = name or f"{socket.gethostname()}:{os.getpid()}"
    kinds = list(HANDLERS)
    backoff = 1.0
    last_refresh = 0.0
    _log(f"worker {name} up; kinds={kinds} refresh={'every %ds' % refresh_every if refresh else 'off'}")

    while True:
        try:
            with db.connect() as conn:
                n = db.requeue_stale_jobs(conn)
                if n:
                    _log(f"requeued {n} stale job(s)")
                backoff = 1.0
                while True:
                    job = db.claim_job(conn, name, kinds)
                    if job:
                        _handle(conn, job)
                        if once:
                            return
                        continue
                    if refresh and time.monotonic() - last_refresh >= refresh_every:
                        r = refresh_once(conn, log=_log,
                                         yield_to=lambda: db.has_queued_job(conn, kinds))
                        # Cut short for a job: come back to the rest right after it.
                        cut = r["processed"] < r["due"]
                        last_refresh = 0.0 if cut else time.monotonic()
                        if r["due"]:
                            _log(f"refresh: {r}")
                    if once:
                        return
                    time.sleep(poll)
        except psycopg.OperationalError as e:
            _log(f"database unavailable ({str(e).strip()[:100]}); retrying in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except KeyboardInterrupt:
            _log("worker stopped")
            return


def _handle(conn, job: dict) -> None:
    jid, kind = job["id"], job["kind"]
    _log(f"job {jid} {kind} {job['payload']}")
    t0 = time.perf_counter()
    try:
        result = HANDLERS[kind](conn, job["payload"] or {}, job_id=jid,
                                user_id=job["user_id"])
    except (JobError, SpecError) as e:
        conn.rollback()
        db.finish_job(conn, jid, error=str(e))
        _log(f"job {jid} rejected: {e}")
        return
    except psycopg.OperationalError:
        raise                              # connection trouble: let run() reconnect
    except Exception as e:                 # noqa: BLE001
        conn.rollback()
        db.finish_job(conn, jid, error=f"internal error: {type(e).__name__}: {e}")
        _log(f"job {jid} failed:\n{traceback.format_exc()}")
        return
    db.finish_job(conn, jid, result=result)
    _log(f"job {jid} done in {time.perf_counter() - t0:.1f}s")
