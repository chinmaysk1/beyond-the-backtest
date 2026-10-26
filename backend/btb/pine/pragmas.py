"""`// @grid` and `// @constraint`: how a Pine strategy declares its sweep.

Pine has inputs but no notion of a grid, and the score needs one with an
exact size: the deflated Sharpe corrects for the N settings tried, so N must
be the number actually run, not an estimate. The grid therefore lives in
comment lines a TradingView user can leave in place -- Pine ignores them --
and that a model writing Pine can produce as easily as the code:

    len  = input.int(20, "Breakout length", minval=5, maxval=100)
    mult = input.float(3.0, "Trail (x ATR)", minval=1, step=0.5)
    // @grid len  = 10, 20, 40              a list
    // @grid mult = 1.5 .. 4 step 0.5       a range, inclusive
    // @constraint len > 5                  same operators as the old spec

The name on a pragma is the *variable* an input is assigned to, not the
input's title: the variable is what the author reads in the code, and it is
an identifier, so it cannot collide with punctuation in a title. The runtime
keys inputs by title; `Input.title` carries the mapping.

No @grid lines is not an error. It means the script runs once on its default
inputs and is not scored, because there is no grid to score.

Nothing here imports the transpiler, so the grammar and the grid arithmetic
are testable on any machine. `check.py` supplies the inputs from the real
Pine parser.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from itertools import product

MAX_VALUES = 25          # per input, as the JSON spec allowed
MAX_COMBINATIONS = 1000  # after constraints; the sweep's cost is linear in it

_PRAGMA = re.compile(r"^\s*//\s*@(grid|constraint)\b(.*)$")
_GRID = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*$")
_RANGE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*\.\.\s*(-?\d+(?:\.\d+)?)(?:\s+step\s+(\d+(?:\.\d+)?))?$")
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
_CONSTRAINT = re.compile(r"^\s*([A-Za-z_]\w*|-?\d+(?:\.\d+)?)\s*(<=|>=|==|!=|<|>)\s*"
                         r"([A-Za-z_]\w*|-?\d+(?:\.\d+)?)\s*$")
_OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b}


@dataclass
class Input:
    """One `input.*()` call, as the transpiler's manifest describes it."""

    name: str | None        # the variable it is assigned to; None for an inline input
    title: str              # the key the compiled strategy reads it by
    type: str               # int | float | bool | string | source | ...
    default: object = None
    min: float | None = None
    max: float | None = None
    step: float | None = None

    @property
    def numeric(self) -> bool:
        return self.type in ("int", "float")


@dataclass
class Problem:
    line: int               # 1-based; 0 when not tied to a line
    message: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}" if self.line else self.message


@dataclass
class Grid:
    """The sweep a script declares, resolved against its inputs."""

    values: dict[str, list[float]] = field(default_factory=dict)   # in declaration order
    constraints: list[tuple[str, str, str]] = field(default_factory=list)
    combos: list[dict] = field(default_factory=list)               # every run, by variable
    raw: int = 0                                                   # before constraints
    errors: list[Problem] = field(default_factory=list)

    @property
    def scored(self) -> bool:
        """A script with no @grid lines runs once and is not scored."""
        return bool(self.values)

    @property
    def n(self) -> int:
        return len(self.combos)


def parse(source: str) -> tuple[list[tuple[int, str, list[float]]], list[tuple[int, tuple]], list[Problem]]:
    """The pragma lines, syntax only: (line, name, values) grids and
    (line, (a, op, b)) constraints, plus the lines that are malformed."""
    grids, constraints, errors = [], [], []
    for i, line in enumerate(source.splitlines(), 1):
        m = _PRAGMA.match(line)
        if not m:
            continue
        kind, rest = m.group(1), m.group(2)
        if kind == "grid":
            g = _GRID.match(rest)
            if not g:
                errors.append(Problem(i, "expected '// @grid name = 10, 20, 30' or "
                                         "'// @grid name = 1 .. 5 step 1'"))
                continue
            vals, err = _values(g.group(2))
            if err:
                errors.append(Problem(i, f"@grid {g.group(1)}: {err}"))
                continue
            grids.append((i, g.group(1), vals))
        else:
            c = _CONSTRAINT.match(rest)
            if not c:
                errors.append(Problem(i, "expected '// @constraint a < b' with "
                                         "<, <=, >, >=, == or !="))
                continue
            constraints.append((i, (c.group(1), c.group(2), c.group(3))))
    return grids, constraints, errors


def _values(text: str) -> tuple[list[float], str | None]:
    text = text.strip()
    r = _RANGE.match(text)
    if r:
        lo, hi = float(r.group(1)), float(r.group(2))
        step = float(r.group(3)) if r.group(3) else 1.0
        if step <= 0:
            return [], "step must be positive"
        if hi < lo:
            return [], "a range runs low .. high"
        count = math.floor((hi - lo) / step + 1e-9) + 1
        if count > MAX_VALUES:
            return [], f"{count} values; at most {MAX_VALUES} -- a sweep is coarse on purpose"
        # Built by multiplication, not repeated addition, so 0.1 steps land on
        # the decimals the author wrote rather than on accumulated error.
        nd = max(_decimals(r.group(1)), _decimals(r.group(2)), _decimals(r.group(3) or "1"))
        return [_clean(round(lo + k * step, nd)) for k in range(count)], None
    parts = [p.strip() for p in text.split(",")]
    if not all(_NUMBER.match(p) for p in parts):
        return [], "values must be numbers separated by commas"
    vals = [_clean(float(p)) for p in parts]
    if len(set(vals)) != len(vals):
        return [], "a value is listed twice"
    if len(vals) > MAX_VALUES:
        return [], f"{len(vals)} values; at most {MAX_VALUES} -- a sweep is coarse on purpose"
    return vals, None


def _decimals(s: str) -> int:
    return len(s.split(".")[1]) if "." in s else 0


def _clean(v: float) -> float | int:
    return int(v) if float(v).is_integer() else v


def build(source: str, inputs: list[Input]) -> Grid:
    """Resolve the pragmas against the script's inputs into the runs a sweep
    makes. Every problem is collected, with its line, so one round trip shows
    a model everything to fix."""
    grids, cons, errors = parse(source)
    by_name = {i.name: i for i in inputs if i.name}
    g = Grid(errors=list(errors))

    for line, name, vals in grids:
        inp = by_name.get(name)
        if inp is None:
            known = ", ".join(sorted(by_name)) or "none"
            g.errors.append(Problem(line, f"@grid {name}: no input is assigned to '{name}' "
                                          f"(inputs: {known})"))
            continue
        if name in g.values:
            g.errors.append(Problem(line, f"@grid {name}: declared twice"))
            continue
        if not inp.numeric:
            g.errors.append(Problem(line, f"@grid {name}: only int and float inputs can be "
                                          f"swept; '{name}' is {inp.type}"))
            continue
        bad = [v for v in vals if inp.type == "int" and not float(v).is_integer()]
        if bad:
            g.errors.append(Problem(line, f"@grid {name}: {name} is an input.int, so "
                                          f"{bad[0]} is not allowed"))
            continue
        low = [v for v in vals if inp.min is not None and v < inp.min]
        high = [v for v in vals if inp.max is not None and v > inp.max]
        if low or high:
            v = (low or high)[0]
            bound = f"minval={_clean(inp.min)}" if low else f"maxval={_clean(inp.max)}"
            g.errors.append(Problem(line, f"@grid {name}: {v} is outside the input's {bound}"))
            continue
        g.values[name] = vals

    for line, (a, op, b) in cons:
        unknown = [s for s in (a, b) if not _NUMBER.match(s) and s not in by_name]
        if unknown:
            g.errors.append(Problem(line, f"@constraint: '{unknown[0]}' is not an input"))
            continue
        g.constraints.append((a, op, b))

    if g.errors or not g.values:
        return g

    defaults = {i.name: _clean(float(i.default)) for i in inputs
                if i.name and i.numeric and isinstance(i.default, (int, float))
                and not isinstance(i.default, bool)}
    keys = list(g.values)
    for combo in product(*(g.values[k] for k in keys)):
        g.raw += 1
        p = dict(defaults)
        p.update(zip(keys, combo))
        if satisfies(g.constraints, p):
            g.combos.append(p)
    if not g.combos:
        g.errors.append(Problem(0, f"the constraints rule out all {g.raw} combinations"))
    elif len(g.combos) > MAX_COMBINATIONS:
        g.errors.append(Problem(0, f"{len(g.combos)} combinations; at most {MAX_COMBINATIONS}. "
                                   "Fewer values per input, or a constraint, brings it down."))
    return g


STYLES = ("trend", "breakout", "mean-reversion", "other")
_META = re.compile(r"^\s*//\s*@(description|style|timeframes)\b\s*(.*?)\s*$")


def meta(source: str) -> dict:
    """`// @description`, `// @style` and `// @timeframes`: how a script
    describes itself to the strategy picker. `@description` is Pine's own
    annotation; the other two are ours. All optional."""
    out: dict = {}
    for line in source.splitlines():
        m = _META.match(line)
        if not m or not m.group(2):
            continue
        key, val = m.group(1), m.group(2)
        if key == "timeframes":
            out[key] = [t.strip() for t in val.split(",") if t.strip()]
        elif key == "style":
            out[key] = val if val in STYLES else "other"
        else:
            out.setdefault(key, val)
    return out


def satisfies(constraints: list[tuple[str, str, str]], params: dict) -> bool:
    for a, op, b in constraints:
        x = float(a) if _NUMBER.match(a) else params.get(a)
        y = float(b) if _NUMBER.match(b) else params.get(b)
        if x is None or y is None or not _OPS[op](float(x), float(y)):
            return False
    return True


def defaults(inputs: list[Input]) -> dict:
    """The parameters of an unscored run: every named numeric input at its default."""
    return {i.name: _clean(float(i.default)) for i in inputs
            if i.name and i.numeric and isinstance(i.default, (int, float))
            and not isinstance(i.default, bool)}


def as_runtime_inputs(params: dict, inputs: list[Input]) -> dict[str, str]:
    """Variable-keyed parameters -> the title-keyed strings the runtime takes."""
    by_name = {i.name: i for i in inputs if i.name}
    out = {}
    for k, v in params.items():
        inp = by_name[k]
        out[inp.title] = str(int(v)) if inp.type == "int" else repr(float(v))
    return out
