"""Where compiled Pine runs: child processes, never the worker itself.

A strategy is transpiled from text anyone can paste, compiled to native code
and executed. The transpiler emits only engine calls -- Pine has no file,
network or process access to emit -- but a native crash or an endless loop
must still not take the worker with it. So every run happens in a forked
child that:

* holds the compiled strategy and the bars, loaded once (fork shares the
  parent's arrays without copying or pickling them);
* has an address-space limit and a CPU-time limit (`RLIMIT_AS`, `RLIMIT_CPU`):
  the kernel kills a runaway child, not us;
* is reaped by a wall-clock timeout on the whole batch.

`Runner(engine_so, feed, processes)` is one pool for one strategy on one
series. `map(fn, tasks)` runs `fn(engine, feed, task)` for every task across the
children and returns the results in task order -- so a sweep's ranking never
depends on which child finished first.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import resource
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import numpy as np

MEM_MB = int(os.environ.get("BTB_PINE_MEM_MB", "3072"))
CPU_SECONDS = int(os.environ.get("BTB_PINE_CPU_SECONDS", "1800"))
WALL_SECONDS = int(os.environ.get("BTB_PINE_WALL_SECONDS", "2400"))


class SandboxError(RuntimeError):
    pass


# Set in each child by _init; the parent never loads a strategy.
_ENGINE = None
_FEED = None
_SHARED = None


def _init(so: str, feed: np.ndarray, shared: dict):
    global _ENGINE, _FEED, _SHARED
    try:
        resource.setrlimit(resource.RLIMIT_AS, (MEM_MB << 20, MEM_MB << 20))
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    except (ValueError, OSError):            # pragma: no cover - limits above the hard cap
        pass
    os.environ.pop("BTB_DB_PASSWORD", None)  # nothing a strategy runs can use it
    from .native import Engine
    _ENGINE = Engine(Path(so))
    for f in shared.get("feeds", ()):
        _ENGINE.add_feed(f["key"], f["timeframe"], f["bars"], f["close_ms"],
                         sym_type=f.get("type", ""), mintick=f.get("mintick"))
    _FEED = feed
    _SHARED = shared


def _call(job):
    fn, task = job
    return fn(_ENGINE, _FEED, _SHARED, task)


class Runner:
    def __init__(self, so: Path, feed: np.ndarray, processes: int = 1, shared: dict | None = None):
        self.processes = max(1, int(processes))
        # concurrent.futures rather than multiprocessing.Pool: when a child
        # dies (segfault, memory or CPU limit) Pool silently replaces it and
        # the lost task waits for the timeout; an executor fails at once.
        self._pool = ProcessPoolExecutor(self.processes, mp_context=mp.get_context("fork"),
                                         initializer=_init,
                                         initargs=(str(so), feed, shared or {}))

    def map(self, fn, tasks: list, timeout: float | None = None) -> list:
        if not tasks:
            return []
        chunk = max(1, len(tasks) // (self.processes * 8))
        try:
            return list(self._pool.map(_call, [(fn, t) for t in tasks],
                                       timeout=timeout or WALL_SECONDS, chunksize=chunk))
        except FuturesTimeout:
            self.close(kill=True)
            raise SandboxError(f"the strategy ran for over {timeout or WALL_SECONDS}s and "
                               "was stopped") from None
        except BrokenProcessPool:
            self.close(kill=True)
            raise SandboxError("the strategy crashed or hit its memory or CPU limit") from None
        except Exception as e:
            self.close(kill=True)
            raise SandboxError(f"the strategy failed while running: {e}") from None

    def close(self, kill: bool = False):
        if self._pool is None:
            return
        if kill:
            for p in list(getattr(self._pool, "_processes", {}).values()):
                p.kill()
        self._pool.shutdown(wait=not kill, cancel_futures=kill)
        self._pool = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close(kill=exc[0] is not None)
