"""The strategy spec: a JSON document, never code.

A strategy is data. The engine interprets it; nothing in it is ever executed.
That is what makes it safe to accept a spec from a user -- or from an AI model
that translated a plain-English description -- on a public app.

The language is deliberately small and COMPOSABLE. Every argument that takes a
series accepts any other node, so an indicator the catalog does not name can
be written out of ones it does. That is the target an AI model writes to: it
decomposes an unfamiliar indicator into these primitives, and `validate()`
either accepts the result or names the exact path that is wrong, so the model
can correct itself.

Shape
-----
    {
      "name": "ema_cross",                       machine name, [a-z0-9_]+
      "title": "EMA cross",                      what a user sees
      "description": "one line",
      "style": "trend",                          trend | breakout | mean-reversion | other
      "timeframes": ["4h", "1d"],                suggested, not enforced
      "indicators": {"fast": <value>, ...},      named nodes, reusable by name
      "entry_long":  <condition>,   "exit_long":  <condition>,
      "entry_short": <condition>,   "exit_short": <condition>,
      "allow": "both" | "long" | "short",        default: whichever entries exist
      "stop":        {"kind": "pct", "value": 4} | {"kind": "atr", "mult": 2, "len": 14},
      "take_profit": same shape as stop,
      "max_bars": 50,                            time exit
      "defaults": {"fast": 20, ...},             every $param must have one
      "grid": {"fast": [10, 20, 30], ...},       what a sweep tries
      "labels": {"fast": "Fast EMA length"},     optional, for the UI
      "constraints": ["fast < slow"]
    }

Values
------
    3.5                     a number
    "$fast"                 a parameter
    "close"                 a price source: open high low close volume hl2 hlc3 ohlc4
    "fast"                  a named indicator from "indicators"
    {"fn": "ema", "src": "close", "len": "$fast"}
    {"fn": "adx", "len": 9, "tf_mult": 3}      computed on a 3x timeframe, no lookahead
    {"fn": "rsi", "len": 14, "tf": "1d"}       ... or on an absolute one

Conditions
----------
    {"gt": [a, b]}  lt gte lte eq             comparisons; NaN is always false
    {"between": [x, lo, hi]}
    {"cross_above": [a, b]}  cross_below
    {"rising": [x, n]}  falling               x beyond every value of the last n bars
    {"all": [c, ...]}  {"any": [c, ...]}  {"not": c}
    true / false

Lengths (`len` and friends) must come out as a positive whole number. A
computed length is floored, so `{"fn": "div", "a": "$n", "b": 2}` on n=9 is 4;
`round` rounds halves up. Those are the rules that make a decomposed Hull MA
agree with the built-in one exactly.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from itertools import product
from typing import Any

import numpy as np

from . import indicators as I
from .timeframes import Resampled, resolve_seconds

SOURCES = ("open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4")
STYLES = ("trend", "breakout", "mean-reversion", "other")
RULES = ("entry_long", "exit_long", "entry_short", "exit_short")
MAX_LEN = 5000
# Keys of a function node that are options, not values. Never resolved as
# names: {"fn": "bbands", "out": "upper"} must not be read as a reference to an
# indicator that happens to be called "upper".
OPTION_KEYS = ("fn", "out", "tf", "tf_mult")


class SpecError(ValueError):
    """A spec failed validation. `errors` is a list of 'path: message'."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors[:5]) + (" ..." if len(errors) > 5 else ""))


# -- the catalog ------------------------------------------------------------------
#
# Argument kinds:
#   S  a series (any value node; scalars broadcast)
#   L  a length: must evaluate to a positive whole number
#   N  a scalar number
# Each entry: (args {name: (kind, default)}, outputs or None, one-line doc).
# A default of REQUIRED means the argument must be given.

REQUIRED = object()


def _a(**kw):
    return kw


CATALOG: dict[str, tuple[dict, tuple | None, str]] = {
    # arithmetic -- scalars or series
    "add": (_a(a=("S", REQUIRED), b=("S", REQUIRED)), None, "a + b"),
    "sub": (_a(a=("S", REQUIRED), b=("S", REQUIRED)), None, "a - b"),
    "mul": (_a(a=("S", REQUIRED), b=("S", REQUIRED)), None, "a * b"),
    "div": (_a(a=("S", REQUIRED), b=("S", REQUIRED)), None, "a / b (NaN where b is 0)"),
    "min": (_a(a=("S", REQUIRED), b=("S", REQUIRED)), None, "element-wise minimum"),
    "max": (_a(a=("S", REQUIRED), b=("S", REQUIRED)), None, "element-wise maximum"),
    "pow": (_a(a=("S", REQUIRED), b=("S", REQUIRED)), None, "a ** b"),
    "neg": (_a(x=("S", REQUIRED)), None, "-x"),
    "abs": (_a(x=("S", REQUIRED)), None, "|x|"),
    "sqrt": (_a(x=("S", REQUIRED)), None, "square root"),
    "log": (_a(x=("S", REQUIRED)), None, "natural log"),
    "round": (_a(x=("S", REQUIRED)), None, "round, halves away from zero"),
    # series operations
    "shift": (_a(src=("S", "close"), n=("L", 1)), None, "value n bars ago"),
    "change": (_a(src=("S", "close"), n=("L", 1)), None, "src - src n bars ago"),
    "roc": (_a(src=("S", "close"), len=("L", REQUIRED)), None, "% change over len bars"),
    "highest": (_a(src=("S", "high"), len=("L", REQUIRED)), None, "highest value over len bars, incl. current"),
    "lowest": (_a(src=("S", "low"), len=("L", REQUIRED)), None, "lowest value over len bars, incl. current"),
    "sum": (_a(src=("S", REQUIRED), len=("L", REQUIRED)), None, "rolling sum"),
    "stdev": (_a(src=("S", "close"), len=("L", REQUIRED)), None, "population standard deviation"),
    "barssince": (_a(cond=("C", REQUIRED)), None, "bars since a condition was last true"),
    # averages
    "sma": (_a(src=("S", "close"), len=("L", REQUIRED)), None, "simple moving average"),
    "ema": (_a(src=("S", "close"), len=("L", REQUIRED)), None, "exponential moving average"),
    "wma": (_a(src=("S", "close"), len=("L", REQUIRED)), None, "linearly weighted moving average"),
    "rma": (_a(src=("S", "close"), len=("L", REQUIRED)), None, "Wilder smoothing (as ta.rma)"),
    "hma": (_a(src=("S", "close"), len=("L", REQUIRED)), None, "Hull moving average"),
    "vwma": (_a(src=("S", "close"), len=("L", REQUIRED)), None, "volume-weighted moving average"),
    # named indicators -- Pine-exact, because rebuilding them from parts gets
    # the seeding and smoothing subtly wrong
    "atr": (_a(len=("L", 14)), None, "average true range (Wilder)"),
    "tr": (_a(), None, "true range"),
    "rsi": (_a(src=("S", "close"), len=("L", 14)), None, "Wilder RSI, 0-100"),
    "adx": (_a(len=("L", 14)), None, "average directional index (as ta.dmi)"),
    "dmi": (_a(len=("L", 14)), ("plus", "minus", "adx"), "+DI, -DI, ADX"),
    "macd": (_a(src=("S", "close"), fast=("L", 12), slow=("L", 26), signal=("L", 9)),
             ("macd", "signal", "hist"), "MACD line, signal line, histogram"),
    "bbands": (_a(src=("S", "close"), len=("L", 20), mult=("N", 2.0)),
               ("upper", "mid", "lower"), "Bollinger bands (population stdev)"),
    "stoch": (_a(len=("L", 14), smooth_k=("L", 1), d=("L", 3)), ("k", "d"), "stochastic %K / %D"),
    "cci": (_a(src=("S", "hlc3"), len=("L", 20)), None, "commodity channel index"),
    "mfi": (_a(len=("L", 14)), None, "money flow index, 0-100"),
    "williams_r": (_a(len=("L", 14)), None, "Williams %R, -100..0"),
    "obv": (_a(), None, "on-balance volume"),
    "vwap": (_a(), None, "VWAP anchored to each UTC day"),
    "donchian": (_a(len=("L", 20)), ("upper", "lower", "mid"), "Donchian channel"),
    "keltner": (_a(len=("L", 20), mult=("N", 2.0), atr_len=("L", 10)),
                ("upper", "mid", "lower"), "Keltner channel (EMA +/- ATR)"),
    "supertrend": (_a(atr_len=("L", 10), mult=("N", 3.0)), ("line", "dir"),
                   "Supertrend; dir is +1 up, -1 down"),
    "psar": (_a(start=("N", 0.02), inc=("N", 0.02), max=("N", 0.2)), None, "parabolic SAR"),
}

COMPARE = ("gt", "lt", "gte", "lte", "eq")
CROSS = ("cross_above", "cross_below")
TREND = ("rising", "falling")
LOGIC = ("all", "any", "not")
CONDITIONS = COMPARE + CROSS + TREND + LOGIC + ("between",)

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CONSTRAINT = re.compile(r"^\s*([A-Za-z_]\w*|-?\d+(?:\.\d+)?)\s*(<=|>=|==|!=|<|>)\s*"
                         r"([A-Za-z_]\w*|-?\d+(?:\.\d+)?)\s*$")


# -- identity -----------------------------------------------------------------------

def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def spec_sha(spec: dict) -> str:
    """sha256 of the canonical JSON. Ties a result to the exact spec that made it."""
    return hashlib.sha256(canonical(spec).encode()).hexdigest()


# -- validation -----------------------------------------------------------------------

def validate(spec: Any) -> list[str]:
    """Every problem with a spec, as 'path: message'. Empty means valid.

    Collects rather than stopping at the first error, so a model rewriting a
    spec gets the whole list in one round trip.
    """
    errs: list[str] = []
    if not isinstance(spec, dict):
        return ["$: spec must be a JSON object"]

    name = spec.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        errs.append("name: required, lowercase letters/digits/underscore, starting with a letter")
    if "title" in spec and not isinstance(spec["title"], str):
        errs.append("title: must be a string")
    if spec.get("style", "other") not in STYLES:
        errs.append(f"style: must be one of {list(STYLES)}")

    defaults = spec.get("defaults", {})
    if not isinstance(defaults, dict):
        errs.append("defaults: must be an object")
        defaults = {}
    for k, v in defaults.items():
        if not _num(v):
            errs.append(f"defaults.{k}: must be a number")

    inds = spec.get("indicators", {})
    if not isinstance(inds, dict):
        errs.append("indicators: must be an object")
        inds = {}
    for k in inds:
        if not _NAME.match(str(k)):
            errs.append(f"indicators.{k}: invalid name")
        elif k in SOURCES:
            errs.append(f"indicators.{k}: shadows a price source")

    used: set[str] = set()
    ctx = _VCtx(set(inds), defaults, used, errs)
    for k, node in inds.items():
        ctx.value(node, f"indicators.{k}")
    _check_cycles(inds, errs)

    rules = [r for r in RULES if r in spec]
    if not any(r.startswith("entry_") for r in rules):
        errs.append("entry_long / entry_short: at least one entry rule is required")
    for r in rules:
        ctx.condition(spec[r], r)
    allow = spec.get("allow")
    if allow is not None and allow not in ("both", "long", "short"):
        errs.append("allow: must be 'both', 'long' or 'short'")
    for side in ("long", "short"):
        if f"entry_{side}" in spec and f"exit_{side}" not in spec \
                and "stop" not in spec and "take_profit" not in spec and "max_bars" not in spec:
            errs.append(f"exit_{side}: an entry needs some way out -- an exit rule, a stop, "
                        "a take-profit or max_bars")

    for key in ("stop", "take_profit"):
        if key in spec:
            _check_exit(spec[key], key, ctx)
    if "max_bars" in spec:
        ctx.scalar(spec["max_bars"], "max_bars")

    grid = spec.get("grid", {})
    if not isinstance(grid, dict):
        errs.append("grid: must be an object")
        grid = {}
    for k, vals in grid.items():
        if k not in defaults:
            errs.append(f"grid.{k}: not a parameter (add it to defaults)")
        if not isinstance(vals, list) or not vals or not all(_num(v) for v in vals):
            errs.append(f"grid.{k}: must be a non-empty list of numbers")
        elif len(vals) > 25:
            errs.append(f"grid.{k}: at most 25 values -- a sweep is coarse on purpose")

    for i, c in enumerate(spec.get("constraints", []) or []):
        m = _CONSTRAINT.match(str(c))
        if not m:
            errs.append(f"constraints[{i}]: expected 'a < b' with <, <=, >, >=, ==, !=")
            continue
        for side in (m.group(1), m.group(3)):
            if not _is_number_literal(side) and side not in defaults:
                errs.append(f"constraints[{i}]: unknown parameter {side!r}")

    for p in sorted(used - set(defaults)):
        errs.append(f"defaults.{p}: parameter ${p} is used but has no default")
    return errs


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_number_literal(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


class _VCtx:
    def __init__(self, names, defaults, used, errs):
        self.names, self.defaults, self.used, self.errs = names, defaults, used, errs

    def value(self, node, path):
        if _num(node):
            return
        if isinstance(node, str):
            if node.startswith("$"):
                self.used.add(node[1:])
            elif node not in SOURCES and node not in self.names:
                self.errs.append(f"{path}: unknown name {node!r} -- not a price source "
                                 f"{list(SOURCES)} or a named indicator")
            return
        if not isinstance(node, dict) or "fn" not in node:
            self.errs.append(f"{path}: expected a number, '$param', a name, or {{\"fn\": ...}}")
            return
        fn = node["fn"]
        if fn not in CATALOG:
            self.errs.append(f"{path}.fn: unknown function {fn!r}")
            return
        args, outs, _ = CATALOG[fn]
        extra = set(node) - set(args) - {"fn", "out", "tf", "tf_mult"}
        for k in sorted(extra):
            self.errs.append(f"{path}.{k}: {fn} takes no argument {k!r} "
                             f"(takes {sorted(args) or 'none'})")
        for a, (kind, default) in args.items():
            if a not in node:
                if default is REQUIRED:
                    self.errs.append(f"{path}.{a}: required by {fn}")
                continue
            if kind == "C":
                self.condition(node[a], f"{path}.{a}")
            elif kind in ("L", "N"):
                self.scalar(node[a], f"{path}.{a}")
            else:
                self.value(node[a], f"{path}.{a}")
        if outs:
            if node.get("out", outs[0]) not in outs:
                self.errs.append(f"{path}.out: {fn} outputs {list(outs)}")
        elif "out" in node:
            self.errs.append(f"{path}.out: {fn} has a single output")
        if "tf" in node and "tf_mult" in node:
            self.errs.append(f"{path}: give tf or tf_mult, not both")
        if "tf_mult" in node and not (isinstance(node["tf_mult"], int) and node["tf_mult"] >= 2):
            self.errs.append(f"{path}.tf_mult: must be a whole number >= 2")

    def scalar(self, node, path):
        """A value that must be the same on every bar -- a length or a multiplier."""
        if _num(node):
            return
        if isinstance(node, str) and node.startswith("$"):
            self.used.add(node[1:])
            return
        if isinstance(node, dict) and node.get("fn") in ("add", "sub", "mul", "div", "min",
                                                          "max", "pow", "neg", "abs", "sqrt",
                                                          "log", "round"):
            for k, v in node.items():
                if k != "fn":
                    self.scalar(v, f"{path}.{k}")
            return
        self.errs.append(f"{path}: must be a number, a $param, or arithmetic on those")

    def condition(self, node, path):
        if isinstance(node, bool):
            return
        if not isinstance(node, dict) or len(node) != 1:
            self.errs.append(f"{path}: a condition is one-key object like {{\"gt\": [a, b]}}")
            return
        (op, args), = node.items()
        if op not in CONDITIONS:
            self.errs.append(f"{path}: unknown condition {op!r}; one of {list(CONDITIONS)}")
            return
        if op == "not":
            self.condition(args, f"{path}.not")
            return
        if not isinstance(args, list):
            self.errs.append(f"{path}.{op}: expects a list")
            return
        if op in ("all", "any"):
            if not args:
                self.errs.append(f"{path}.{op}: empty list")
            for i, c in enumerate(args):
                self.condition(c, f"{path}.{op}[{i}]")
            return
        want = 3 if op == "between" else 2
        if len(args) != want:
            self.errs.append(f"{path}.{op}: expects {want} arguments, got {len(args)}")
            return
        if op in TREND:
            self.value(args[0], f"{path}.{op}[0]")
            self.scalar(args[1], f"{path}.{op}[1]")
            return
        for i, a in enumerate(args):
            self.value(a, f"{path}.{op}[{i}]")


def _check_exit(node, path, ctx):
    if not isinstance(node, dict) or node.get("kind") not in ("pct", "atr"):
        ctx.errs.append(f"{path}: expected {{\"kind\": \"pct\", \"value\": ...}} "
                        "or {\"kind\": \"atr\", \"mult\": ..., \"len\": ...}")
        return
    if node["kind"] == "pct":
        if "value" not in node:
            ctx.errs.append(f"{path}.value: required")
        else:
            ctx.scalar(node["value"], f"{path}.value")
    else:
        if "mult" not in node:
            ctx.errs.append(f"{path}.mult: required")
        else:
            ctx.scalar(node["mult"], f"{path}.mult")
        ctx.scalar(node.get("len", 14), f"{path}.len")


def _refs(node, names, out):
    if isinstance(node, str) and node in names:
        out.add(node)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k not in OPTION_KEYS:
                _refs(v, names, out)
    elif isinstance(node, list):
        for v in node:
            _refs(v, names, out)


def _check_cycles(inds, errs):
    names = set(inds)
    deps = {}
    for k, v in inds.items():
        s: set = set()
        _refs(v, names, s)
        deps[k] = s
    state: dict[str, int] = {}

    def visit(k, stack):
        if state.get(k) == 2:
            return
        if state.get(k) == 1:
            errs.append(f"indicators.{k}: circular reference via {' -> '.join(stack + [k])}")
            return
        state[k] = 1
        for d in deps[k]:
            visit(d, stack + [k])
        state[k] = 2

    for k in inds:
        visit(k, [])


def load(spec: dict) -> dict:
    """Validate, raising SpecError. Returns the spec unchanged."""
    errs = validate(spec)
    if errs:
        raise SpecError(errs)
    return spec


# -- parameters ---------------------------------------------------------------------

def check_constraints(spec: dict, params: dict) -> bool:
    for c in spec.get("constraints", []) or []:
        m = _CONSTRAINT.match(str(c))
        a, op, b = m.group(1), m.group(2), m.group(3)
        x = float(a) if _is_number_literal(a) else float(params[a])
        y = float(b) if _is_number_literal(b) else float(params[b])
        ok = {"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y,
              "==": x == y, "!=": x != y}[op]
        if not ok:
            return False
    return True


def resolve_params(spec: dict, overrides: dict | None = None) -> dict:
    params = dict(spec.get("defaults", {}))
    for k, v in (overrides or {}).items():
        if k not in params:
            raise SpecError([f"params.{k}: not a parameter of {spec['name']}"])
        if not _num(v):
            raise SpecError([f"params.{k}: must be a number"])
        params[k] = v
    if not check_constraints(spec, params):
        raise SpecError([f"params: violate constraints {spec.get('constraints')}"])
    return params


def grid(spec: dict) -> tuple[list[dict], int]:
    """Every combination a sweep runs, and the raw grid size before constraints.

    Parameters not in the grid stay at their defaults. The count that matters
    for multiple-testing correction is the number actually RUN, which is the
    length of the list.
    """
    g = spec.get("grid", {}) or {}
    keys = sorted(g)
    base = dict(spec.get("defaults", {}))
    combos, raw = [], 0
    for vals in product(*(g[k] for k in keys)):
        raw += 1
        p = dict(base)
        p.update(zip(keys, vals))
        if check_constraints(spec, p):
            combos.append(p)
    return combos, raw


def bind(node, params: dict, names: dict | None = None):
    """Substitute every $param with its value and inline every named indicator.

    The result mentions only numbers, price sources and functions, so two nodes
    with the same JSON are the same computation. That is what makes the
    evaluator's memo safe: without inlining, {"fn": "ema", "src": "fast"} would
    look identical across combinations in which "fast" means different things.
    """
    if isinstance(node, str):
        if node.startswith("$"):
            return params[node[1:]]
        if names and node in names:
            return bind(names[node], params, names)
        return node
    if isinstance(node, dict):
        return {k: (v if k in OPTION_KEYS else bind(v, params, names)) for k, v in node.items()}
    if isinstance(node, list):
        return [bind(v, params, names) for v in node]
    return node


# -- evaluation -----------------------------------------------------------------------

@dataclass
class Frame:
    """The bars a spec is evaluated on, as float arrays, plus the timeframe."""

    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    timeframe: str
    cache: dict = field(default_factory=dict)
    htf: dict = field(default_factory=dict)

    @staticmethod
    def of(df, timeframe: str) -> "Frame":
        return Frame(df["ts"].to_numpy("int64"), df["open"].to_numpy("float64"),
                     df["high"].to_numpy("float64"), df["low"].to_numpy("float64"),
                     df["close"].to_numpy("float64"), df["volume"].to_numpy("float64"),
                     timeframe)

    def __len__(self):
        return len(self.ts)

    def source(self, name: str) -> np.ndarray:
        if name == "hl2":
            return (self.high + self.low) / 2.0
        if name == "hlc3":
            return (self.high + self.low + self.close) / 3.0
        if name == "ohlc4":
            return (self.open + self.high + self.low + self.close) / 4.0
        return getattr(self, name)


def _length(v) -> int:
    if isinstance(v, np.ndarray):
        raise SpecError(["a length evaluated to a series; lengths must be constant"])
    n = int(math.floor(float(v) + 1e-9))
    if n < 1 or n > MAX_LEN:
        raise SpecError([f"length {v!r} out of range 1..{MAX_LEN}"])
    return n


class Evaluator:
    """Evaluates bound nodes on a Frame, memoising every node by its JSON.

    The memo is what makes a sweep cheap: 81 combinations of an EMA-cross grid
    contain only a handful of distinct EMAs, and each is computed once per
    series, not once per combination.
    """

    def __init__(self, frame: Frame):
        self.f = frame

    def value(self, node):
        if isinstance(node, bool):
            raise SpecError(["a condition was used where a value was expected"])
        if isinstance(node, (int, float)):
            return float(node)
        if isinstance(node, str):
            if node in SOURCES:
                return self.f.source(node)
            raise SpecError([f"unbound name {node!r}"])
        key = canonical(node)
        hit = self.f.cache.get(key)
        if hit is not None:
            return hit
        if "tf" in node or "tf_mult" in node:
            out = self._higher(node)
        else:
            out = self._call(node)
        self.f.cache[key] = out
        return out

    def _higher(self, node):
        secs = resolve_seconds(self.f.timeframe, tf=node.get("tf"), mult=node.get("tf_mult"))
        rs = self.f.htf.get(secs)
        if rs is None:
            base = {"ts": self.f.ts, "open": self.f.open, "high": self.f.high,
                    "low": self.f.low, "close": self.f.close, "volume": self.f.volume}
            r = Resampled(base, secs)
            hf = Frame(r.frame["ts"], r.frame["open"], r.frame["high"], r.frame["low"],
                       r.frame["close"], r.frame["volume"], f"{secs // 60}m")
            rs = (r, hf)
            self.f.htf[secs] = rs
        r, hf = rs
        inner = {k: v for k, v in node.items() if k not in ("tf", "tf_mult")}
        # Named indicators were inlined by bind(), so everything inside a
        # higher-timeframe node is evaluated on the higher timeframe.
        vals = Evaluator(hf).value(inner)
        if not isinstance(vals, np.ndarray):
            return vals
        return r.to_base(vals)

    def cond(self, node) -> np.ndarray:
        n = len(self.f)
        if isinstance(node, bool):
            return np.full(n, node)
        (op, args), = node.items()
        if op == "all":
            out = np.ones(n, dtype=bool)
            for c in args:
                out &= self.cond(c)
            return out
        if op == "any":
            out = np.zeros(n, dtype=bool)
            for c in args:
                out |= self.cond(c)
            return out
        if op == "not":
            return ~self.cond(args)
        if op in TREND:
            x = self._series(self.value(args[0]))
            k = _length(self.value(args[1]))
            prev = I.shift(x, 1)
            if op == "rising":
                ref = I.highest(prev, k)
                return _nan_false(x > ref, x, ref)
            ref = I.lowest(prev, k)
            return _nan_false(x < ref, x, ref)
        if op == "between":
            x, lo, hi = (self._series(self.value(a)) for a in args)
            return _nan_false((x >= lo) & (x <= hi), x, lo, hi)
        a = self._series(self.value(args[0]))
        b = self._series(self.value(args[1]))
        with np.errstate(invalid="ignore"):
            if op == "gt":
                r = a > b
            elif op == "lt":
                r = a < b
            elif op == "gte":
                r = a >= b
            elif op == "lte":
                r = a <= b
            elif op == "eq":
                r = a == b
            elif op == "cross_above":
                pa, pb = I.shift(a, 1), I.shift(b, 1)
                return _nan_false((a > b) & (pa <= pb), a, b, pa, pb)
            else:
                pa, pb = I.shift(a, 1), I.shift(b, 1)
                return _nan_false((a < b) & (pa >= pb), a, b, pa, pb)
        return _nan_false(r, a, b)

    def _series(self, v) -> np.ndarray:
        if isinstance(v, np.ndarray):
            return v
        return np.full(len(self.f), float(v))

    def _call(self, node):
        fn = node["fn"]
        args, outs, _ = CATALOG[fn]
        f = self.f
        got = {}
        for a, (kind, default) in args.items():
            raw = node.get(a, default)
            if kind == "C":
                got[a] = self.cond(raw)
            elif kind == "L":
                got[a] = _length(self.value(raw))
            elif kind == "N":
                v = self.value(raw)
                if isinstance(v, np.ndarray):
                    raise SpecError([f"{fn}.{a}: must be constant, got a series"])
                got[a] = float(v)
            else:
                got[a] = self.value(raw)
        out = node.get("out", outs[0] if outs else None)

        if fn in ("add", "sub", "mul", "div", "min", "max", "pow"):
            a, b = got["a"], got["b"]
            with np.errstate(divide="ignore", invalid="ignore"):
                if fn == "add":
                    return a + b
                if fn == "sub":
                    return a - b
                if fn == "mul":
                    return a * b
                if fn == "div":
                    if isinstance(b, np.ndarray):
                        return np.where(b != 0, a / np.where(b != 0, b, 1.0), np.nan)
                    return a / b if b != 0 else float("nan")
                if fn == "min":
                    return np.minimum(a, b) if isinstance(a, np.ndarray) or isinstance(b, np.ndarray) else min(a, b)
                if fn == "max":
                    return np.maximum(a, b) if isinstance(a, np.ndarray) or isinstance(b, np.ndarray) else max(a, b)
                return np.power(a, b) if isinstance(a, np.ndarray) or isinstance(b, np.ndarray) else a ** b
        if fn in ("neg", "abs", "sqrt", "log", "round"):
            x = got["x"]
            if not isinstance(x, np.ndarray):
                if fn == "round":
                    return float(I.round_half_up(x))
                if fn == "sqrt":
                    return math.sqrt(x) if x >= 0 else float("nan")
                if fn == "log":
                    return math.log(x) if x > 0 else float("nan")
                return -x if fn == "neg" else abs(x)
            with np.errstate(divide="ignore", invalid="ignore"):
                if fn == "round":
                    return np.where(x >= 0, np.floor(x + 0.5), -np.floor(-x + 0.5))
                if fn == "sqrt":
                    return np.sqrt(x)
                if fn == "log":
                    return np.where(x > 0, np.log(np.where(x > 0, x, 1.0)), np.nan)
                return -x if fn == "neg" else np.abs(x)

        s = lambda k: self._series(got[k])      # noqa: E731
        if fn == "shift":
            return I.shift(s("src"), got["n"])
        if fn == "change":
            return I.change(s("src"), got["n"])
        if fn == "roc":
            return I.roc(s("src"), got["len"])
        if fn == "highest":
            return I.highest(s("src"), got["len"])
        if fn == "lowest":
            return I.lowest(s("src"), got["len"])
        if fn == "sum":
            return I.rolling_sum(s("src"), got["len"])
        if fn == "stdev":
            return I.stdev(s("src"), got["len"])
        if fn == "barssince":
            return I.barssince(got["cond"])
        if fn in ("sma", "ema", "wma", "rma", "hma"):
            return getattr(I, fn)(s("src"), got["len"])
        if fn == "vwma":
            return I.vwma(s("src"), f.volume, got["len"])
        if fn == "atr":
            return I.atr(f.high, f.low, f.close, got["len"])
        if fn == "tr":
            return I.true_range(f.high, f.low, f.close)
        if fn == "rsi":
            return I.rsi(s("src"), got["len"])
        if fn == "adx":
            return I.dmi(f.high, f.low, f.close, got["len"])["adx"]
        if fn == "dmi":
            return I.dmi(f.high, f.low, f.close, got["len"])[out]
        if fn == "macd":
            return I.macd(s("src"), got["fast"], got["slow"], got["signal"])[out]
        if fn == "bbands":
            return I.bbands(s("src"), got["len"], got["mult"])[out]
        if fn == "stoch":
            return I.stoch(f.high, f.low, f.close, got["len"], got["smooth_k"], got["d"])[out]
        if fn == "cci":
            return I.cci(s("src"), got["len"])
        if fn == "mfi":
            return I.mfi(f.high, f.low, f.close, f.volume, got["len"])
        if fn == "williams_r":
            return I.williams_r(f.high, f.low, f.close, got["len"])
        if fn == "obv":
            return I.obv(f.close, f.volume)
        if fn == "vwap":
            return I.vwap(f.high, f.low, f.close, f.volume, f.ts)
        if fn == "donchian":
            return I.donchian(f.high, f.low, got["len"])[out]
        if fn == "keltner":
            return I.keltner(f.high, f.low, f.close, got["len"], got["mult"], got["atr_len"])[out]
        if fn == "supertrend":
            return I.supertrend(f.high, f.low, f.close, got["atr_len"], got["mult"])[out]
        if fn == "psar":
            return I.psar(f.high, f.low, got["start"], got["inc"], got["max"])
        raise SpecError([f"{fn}: not implemented"])


def _nan_false(result, *series) -> np.ndarray:
    """A comparison involving a missing value is false, never true."""
    r = np.asarray(result, dtype=bool).copy()
    for s in series:
        r &= ~np.isnan(s)
    return r


# -- signals ------------------------------------------------------------------------

@dataclass
class Signals:
    entry_long: np.ndarray
    exit_long: np.ndarray
    entry_short: np.ndarray
    exit_short: np.ndarray
    stop: dict | None           # {"kind": "pct", "value": float} or {"kind":"atr", "dist": array}
    take_profit: dict | None
    max_bars: int | None


def signals(spec: dict, params: dict, frame: Frame) -> Signals:
    """spec + params + bars -> boolean rule arrays. No position state here."""
    names = spec.get("indicators", {})
    ev = Evaluator(frame)
    b = lambda node: bind(node, params, names)      # noqa: E731
    n = len(frame)
    allow = spec.get("allow")
    if allow is None:
        has_l, has_s = "entry_long" in spec, "entry_short" in spec
        allow = "both" if has_l and has_s else ("long" if has_l else "short")

    def rule(key, enabled=True):
        if not enabled or key not in spec:
            return np.zeros(n, dtype=bool)
        return ev.cond(b(spec[key]))

    return Signals(
        entry_long=rule("entry_long", allow in ("both", "long")),
        exit_long=rule("exit_long"),
        entry_short=rule("entry_short", allow in ("both", "short")),
        exit_short=rule("exit_short"),
        stop=_exit_level(spec.get("stop"), params, ev),
        take_profit=_exit_level(spec.get("take_profit"), params, ev),
        max_bars=(_length(ev.value(b(spec["max_bars"])))
                  if "max_bars" in spec else None),
    )


def _exit_level(node, params, ev):
    if not node:
        return None
    node = bind(node, params)
    if node["kind"] == "pct":
        v = float(ev.value(node["value"]))
        return {"kind": "pct", "value": v} if v > 0 else None
    mult = float(ev.value(node["mult"]))
    if mult <= 0:
        return None
    n = _length(ev.value(node.get("len", 14)))
    return {"kind": "atr", "mult": mult,
            "dist": I.atr(ev.f.high, ev.f.low, ev.f.close, n) * mult}


# -- self-description ------------------------------------------------------------------

def catalog() -> list[dict]:
    """The function list, machine-readable. What an AI model is shown."""
    out = []
    kinds = {"S": "series", "L": "length", "N": "number", "C": "condition"}
    for fn, (args, outs, doc) in CATALOG.items():
        out.append({
            "fn": fn, "doc": doc,
            "args": {a: {"kind": kinds[k], **({} if d is REQUIRED else {"default": d})}
                     for a, (k, d) in args.items()},
            **({"outputs": list(outs)} if outs else {}),
        })
    return out


def json_schema() -> dict:
    """A JSON Schema for the spec, generated from the catalog so it cannot drift.

    Structural only: it tells a model the shape. `validate()` is still the
    authority -- it knows things a schema cannot, like which names exist.
    """
    fn_variants = []
    for fn, (args, outs, doc) in CATALOG.items():
        props: dict = {"fn": {"const": fn}}
        req = ["fn"]
        for a, (kind, default) in args.items():
            props[a] = {"$ref": "#/$defs/condition" if kind == "C"
                        else "#/$defs/scalar" if kind in ("L", "N") else "#/$defs/value"}
            if default is REQUIRED:
                req.append(a)
        if outs:
            props["out"] = {"enum": list(outs)}
        props["tf"] = {"type": "string", "pattern": "^[0-9]+[mhdw]$"}
        props["tf_mult"] = {"type": "integer", "minimum": 2}
        fn_variants.append({"type": "object", "description": doc, "properties": props,
                            "required": req, "additionalProperties": False})
    pair = {"type": "array", "items": {"$ref": "#/$defs/value"}, "minItems": 2, "maxItems": 2}
    cond_variants = [{"type": "boolean"}]
    for op in COMPARE + CROSS:
        cond_variants.append({"type": "object", "properties": {op: pair}, "required": [op],
                              "additionalProperties": False})
    cond_variants.append({"type": "object", "required": ["between"], "additionalProperties": False,
                          "properties": {"between": {"type": "array", "minItems": 3, "maxItems": 3,
                                                     "items": {"$ref": "#/$defs/value"}}}})
    for op in TREND:
        cond_variants.append({"type": "object", "required": [op], "additionalProperties": False,
                              "properties": {op: {"type": "array", "prefixItems": [
                                  {"$ref": "#/$defs/value"}, {"$ref": "#/$defs/scalar"}],
                                  "minItems": 2, "maxItems": 2}}})
    for op in ("all", "any"):
        cond_variants.append({"type": "object", "required": [op], "additionalProperties": False,
                              "properties": {op: {"type": "array", "minItems": 1,
                                                  "items": {"$ref": "#/$defs/condition"}}}})
    cond_variants.append({"type": "object", "required": ["not"], "additionalProperties": False,
                          "properties": {"not": {"$ref": "#/$defs/condition"}}})
    level = {"oneOf": [
        {"type": "object", "required": ["kind", "value"], "additionalProperties": False,
         "properties": {"kind": {"const": "pct"}, "value": {"$ref": "#/$defs/scalar"}}},
        {"type": "object", "required": ["kind", "mult"], "additionalProperties": False,
         "properties": {"kind": {"const": "atr"}, "mult": {"$ref": "#/$defs/scalar"},
                        "len": {"$ref": "#/$defs/scalar"}}},
    ]}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Beyond the Backtest strategy spec",
        "type": "object",
        "required": ["name", "defaults"],
        "properties": {
            "name": {"type": "string", "pattern": _NAME.pattern},
            "title": {"type": "string"}, "description": {"type": "string"},
            "style": {"enum": list(STYLES)},
            "timeframes": {"type": "array", "items": {"type": "string"}},
            "indicators": {"type": "object", "additionalProperties": {"$ref": "#/$defs/value"}},
            **{r: {"$ref": "#/$defs/condition"} for r in RULES},
            "allow": {"enum": ["both", "long", "short"]},
            "stop": level, "take_profit": level,
            "max_bars": {"$ref": "#/$defs/scalar"},
            "defaults": {"type": "object", "additionalProperties": {"type": "number"}},
            "grid": {"type": "object", "additionalProperties": {
                "type": "array", "items": {"type": "number"}, "minItems": 1, "maxItems": 25}},
            "labels": {"type": "object", "additionalProperties": {"type": "string"}},
            "constraints": {"type": "array", "items": {"type": "string"}},
        },
        "$defs": {
            "scalar": {"anyOf": [{"type": "number"}, {"type": "string", "pattern": r"^\$\w+$"},
                                 {"type": "object", "required": ["fn"]}]},
            "value": {"anyOf": [{"type": "number"}, {"type": "string"}, *fn_variants]},
            "condition": {"anyOf": cond_variants},
        },
    }
