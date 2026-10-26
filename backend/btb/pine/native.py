"""Transpiled C++ -> a loaded strategy that runs on our bars.

Two steps, both cached:

* `compile(cpp)` builds a shared library with g++ against PineForge's static
  engine, once per distinct C++ (its sha256 names the file). Compiling takes
  4-7 seconds; running the result takes milliseconds, which is what makes a
  sweep affordable: one compile, N runs.
* `Engine(so)` loads that library through `libbtb_shim.so` (native/btb_shim.cpp)
  and runs it with inputs, cost overrides and a trade start, returning the
  per-bar equity and the trades as numpy arrays.

Flags are PineForge's own. `-ffp-contract=off` is not optional: the engine is
built with it, and a strategy compiled without it can fuse multiply-adds
differently and drift in the last bit -- which is how bit-identical x86 and
ARM results were obtained.

Run user-derived native code only inside `sandbox` children, never in the
worker process itself.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np

PREFIX = Path(os.environ.get("PINEFORGE_PREFIX", "/opt/pineforge"))
SHIM = Path(os.environ.get("BTB_PINE_SHIM", "/opt/btb/libbtb_shim.so"))
CACHE = Path(os.environ.get("BTB_PINE_CACHE", os.path.join(tempfile.gettempdir(), "btb-pine")))
EIGEN = os.environ.get("BTB_EIGEN_INCLUDE", "/usr/include/eigen3")
COMPILE_TIMEOUT = 120

# pf_bar_t: five doubles then an int64 millisecond open time, 48 bytes, no padding.
BAR = np.dtype([("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
                ("volume", "<f8"), ("timestamp", "<i8")])
TRADE_WIDTH = 10   # see btb_trades() in btb_shim.cpp
IS_LONG, ENTRY_BAR, EXIT_BAR, ENTRY_PX, EXIT_PX, QTY, PNL, COMMISSION, OPEN_AT_END, ENTRY_MS = \
    range(TRADE_WIDTH)


class NativeError(RuntimeError):
    pass


def available() -> bool:
    return SHIM.exists() and (PREFIX / "lib" / "libpineforge.a").exists()


def engine_version() -> str:
    v = PREFIX / "VERSION"
    if v.exists():
        return v.read_text().strip()
    return os.environ.get("PINEFORGE_ENGINE_VERSION", "unknown")


def compile(cpp: str) -> Path:
    """The shared library for this C++, building it if it is not cached."""
    key = hashlib.sha256((engine_version() + "\0" + cpp).encode("utf-8")).hexdigest()[:32]
    CACHE.mkdir(parents=True, exist_ok=True)
    so = CACHE / f"{key}.so"
    if so.exists():
        return so
    with tempfile.TemporaryDirectory(dir=CACHE) as tmp:
        src = Path(tmp) / "strategy.cpp"
        src.write_text(cpp, encoding="utf-8")
        out = Path(tmp) / "strategy.so"
        cmd = ["g++", "-std=c++17", "-O2", "-ffp-contract=off", "-fPIC", "-shared",
               f"-I{PREFIX / 'include'}", f"-I{EIGEN}", str(src),
               "-Wl,--whole-archive", str(PREFIX / "lib" / "libpineforge.a"),
               "-Wl,--no-whole-archive", "-o", str(out)]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=COMPILE_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise NativeError(f"compiling the strategy took over {COMPILE_TIMEOUT}s") from None
        if p.returncode != 0:
            # A transpiled script that does not compile is PineForge's bug, not
            # the author's: say so, and keep the compiler's first lines.
            raise NativeError("the transpiled strategy did not compile (a PineForge "
                              "limitation): " + "\n".join(p.stderr.splitlines()[:5]))
        os.replace(out, so)       # atomic: two workers compiling the same script both win
    return so


def bars(ts: np.ndarray, o, h, l, c, v, n: int | None = None) -> np.ndarray:
    """Our arrays (ts in unix SECONDS) as pf_bar_t[], optionally only the first n."""
    n = len(ts) if n is None else n
    out = np.empty(n, dtype=BAR)
    out["open"], out["high"], out["low"] = o[:n], h[:n], l[:n]
    out["close"], out["volume"] = c[:n], v[:n]
    out["timestamp"] = np.asarray(ts[:n], dtype="int64") * 1000
    return out


class Engine:
    """One loaded strategy. Not thread-safe; one per process."""

    _lib = None

    def __init__(self, so: Path):
        lib = Engine._load_shim()
        err = ctypes.create_string_buffer(1024)
        self._h = lib.btb_open(str(so).encode(), err, len(err))
        if not self._h:
            raise NativeError(err.value.decode(errors="replace"))
        self._lib = lib

    @classmethod
    def _load_shim(cls):
        if cls._lib is None:
            if not SHIM.exists():
                raise NativeError(f"{SHIM} is missing; Pine runs in the worker image")
            lib = ctypes.CDLL(str(SHIM))
            P, I, D = ctypes.c_void_p, ctypes.c_int, ctypes.c_double
            S, SS = ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p)
            lib.btb_open.restype = P
            lib.btb_open.argtypes = [S, S, I]
            lib.btb_close.argtypes = [P]
            lib.btb_run.restype = I
            lib.btb_run.argtypes = [P, P, I, I, SS, SS, I, SS, SS, ctypes.c_longlong,
                                    S, S, S, D, ctypes.POINTER(D), ctypes.POINTER(D), S, I]
            lib.btb_trades.restype = I
            lib.btb_trades.argtypes = [P, ctypes.POINTER(D), I]
            lib.btb_add_feed.restype = I
            lib.btb_add_feed.argtypes = [P, S, S, S, S, P, P, I]
            cls._lib = lib
        return cls._lib

    def run(self, feed: np.ndarray, *, inputs: dict[str, str] | None = None,
            overrides: dict[str, str] | None = None,
            trade_start_ms: int = 0,
            syminfo: dict | None = None) -> tuple[np.ndarray, float, np.ndarray]:
        """(equity per fed bar, final equity, trades [k, TRADE_WIDTH]).

        `syminfo`: {"type", "timezone", "session", "mintick"}, any subset."""
        si = syminfo or {}
        feed = np.ascontiguousarray(feed, dtype=BAR)
        n = len(feed)
        ni, ik, iv = _strings(inputs)
        no, ok, ov = _strings(overrides)
        equity = np.empty(n, dtype="float64")
        final = ctypes.c_double(0.0)
        err = ctypes.create_string_buffer(2048)
        k = self._lib.btb_run(
            self._h, feed.ctypes.data_as(ctypes.c_void_p), n,
            ni, ik, iv, no, ok, ov, int(trade_start_ms),
            str(si.get("type") or "").encode(), str(si.get("timezone") or "").encode(),
            str(si.get("session") or "").encode(), float(si.get("mintick") or 0.0),
            equity.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), ctypes.byref(final),
            err, len(err))
        if k < 0:
            raise NativeError(err.value.decode(errors="replace") or "the strategy failed")
        trades = np.empty((k, TRADE_WIDTH), dtype="float64")
        if k:
            self._lib.btb_trades(self._h, trades.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), k)
        return equity, final.value, trades

    def add_feed(self, key: str, timeframe: str, feed: np.ndarray, close_ms: np.ndarray,
                 *, sym_type: str = "", mintick: float | None = None) -> None:
        """Another symbol's bars, for every later run's request.security on `key`."""
        feed = np.ascontiguousarray(feed, dtype=BAR)
        close_ms = np.ascontiguousarray(close_ms, dtype="int64")
        self._lib.btb_add_feed(self._h, key.encode(), timeframe.encode(), sym_type.encode(),
                               (repr(float(mintick)) if mintick else "").encode(),
                               feed.ctypes.data_as(ctypes.c_void_p),
                               close_ms.ctypes.data_as(ctypes.c_void_p), len(feed))

    def close(self):
        if self._h:
            self._lib.btb_close(self._h)
            self._h = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _strings(d: dict[str, str] | None):
    """(count, keys, values) as char*[] for the shim. Never a zero-length
    array: ctypes cannot pass one, so an empty dict gets one unused slot."""
    keys = list((d or {}).keys())
    arr = ctypes.c_char_p * max(len(keys), 1)
    return (len(keys), arr(*[s.encode() for s in keys]),
            arr(*[str(d[s]).encode() for s in keys]))
