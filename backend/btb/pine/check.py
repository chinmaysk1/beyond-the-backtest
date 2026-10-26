"""Pine source -> everything a strategy is checked and run by.

`check(source)` is the one place a script is judged before anything runs. The
web app's check button, the MCP `check_pine` tool and the worker all call it,
so they cannot disagree about whether a script is acceptable:

    r = check(source)
    r.ok, r.errors, r.warnings          line-numbered, for a person or a model
    r.inputs, r.grid                    what the sweep will try, and N
    r.costs                             what the script charges, or that it is unset
    r.cpp                               the transpiled strategy, for runtime.compile

Transpiling is PineForge's (pineforge_codegen). Its errors already name the
line, the column and TradingView's signature; this module adds the rules that
are ours -- the pragmas, the cost policy, and what the platform can feed.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from . import pragmas as PR
from . import symbols as SY

MAX_SOURCE_BYTES = 200_000
# Commission and slippage keys whose presence in strategy() means the author
# chose the costs. Absent, the platform's asset-class defaults apply.
COST_KEYS = ("commission_type", "commission_value", "slippage")
_VERSION = re.compile(r"^\s*//\s*@version\s*=\s*(\d+)", re.M)


class Unavailable(RuntimeError):
    """The transpiler is not installed here. The worker image has it; a
    laptop without it can still run everything that does not compile Pine."""


@dataclass
class Message:
    line: int
    col: int
    message: str
    code: str = ""

    def as_dict(self) -> dict:
        return {"line": self.line, "col": self.col, "message": self.message,
                **({"code": self.code} if self.code else {})}

    def __str__(self) -> str:
        return f"{self.line}:{self.col}: {self.message}" if self.line else self.message


@dataclass
class Costs:
    """What strategy() says about costs. `set` False means none of the cost
    keys appear, so the asset-class defaults are charged instead."""

    set: bool
    commission_type: str | None = None
    commission_value: float | None = None
    slippage_ticks: float | None = None

    @property
    def zero(self) -> bool:
        return self.set and not (self.commission_value or 0) and not (self.slippage_ticks or 0)

    def as_dict(self) -> dict:
        return {"set": self.set, "commission_type": self.commission_type,
                "commission_value": self.commission_value,
                "slippage_ticks": self.slippage_ticks, "zero": self.zero}


@dataclass
class CheckResult:
    source: str
    sha: str
    ok: bool = False
    title: str = ""
    version: int | None = None
    errors: list[Message] = field(default_factory=list)
    warnings: list[Message] = field(default_factory=list)
    inputs: list[PR.Input] = field(default_factory=list)
    grid: PR.Grid = field(default_factory=PR.Grid)
    costs: Costs = field(default_factory=lambda: Costs(False))
    params: dict = field(default_factory=dict)       # literal strategy() kwargs
    requests: list[dict] = field(default_factory=list)
    # Other markets the script reads: {"key", "pine_tf", "symbol", "timeframe",
    # "asset_class"}. The worker loads each and installs it as a symbol feed.
    feeds: list[dict] = field(default_factory=list)
    cpp: str = ""

    def as_dict(self) -> dict:
        """The JSON the web app and the MCP tool return. No C++."""
        return {
            "ok": self.ok, "sha": self.sha, "title": self.title, "version": self.version,
            "errors": [m.as_dict() for m in self.errors],
            "warnings": [m.as_dict() for m in self.warnings],
            "inputs": [{"name": i.name, "title": i.title, "type": i.type, "default": i.default,
                        "min": i.min, "max": i.max, "step": i.step} for i in self.inputs],
            "grid": {"values": self.grid.values,
                     "constraints": [" ".join(c) for c in self.grid.constraints],
                     "combinations": self.grid.n, "raw": self.grid.raw},
            "scored": self.grid.scored and not self.grid.errors,
            "costs": self.costs.as_dict(),
            "feeds": [{"key": f["key"], "symbol": f["symbol"], "timeframe": f["timeframe"]}
                      for f in self.feeds],
        }


def source_sha(source: str) -> str:
    """sha256 of the source with line endings and trailing space normalised,
    so a paste from Windows and the same paste from a Mac are one strategy."""
    norm = "\n".join(line.rstrip() for line in source.replace("\r\n", "\n").split("\n")).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _codegen():
    try:
        import pineforge_codegen as pc
        from pineforge_codegen.errors import CompileError
        from pineforge_codegen.lexer import Lexer
        from pineforge_codegen.parser import Parser
        from pineforge_codegen.ast_nodes import VarDecl
        from pineforge_codegen.pine_spelling import global_input_calls
    except ImportError as e:                                 # pragma: no cover
        raise Unavailable("pineforge_codegen is not installed; Pine runs in the "
                          "worker image") from e
    return pc, CompileError, Lexer, Parser, VarDecl, global_input_calls


def check(source: str) -> CheckResult:
    res = CheckResult(source=source, sha=source_sha(source))
    if not isinstance(source, str) or not source.strip():
        res.errors.append(Message(0, 0, "the script is empty"))
        return res
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        res.errors.append(Message(0, 0, f"the script is over {MAX_SOURCE_BYTES // 1000} KB"))
        return res
    m = _VERSION.search(source)
    res.version = int(m.group(1)) if m else None
    if res.version != 6:
        found = f"//@version={res.version}" if res.version else "no //@version line"
        res.errors.append(Message(1, 1, f"Pine v6 only (found {found}). TradingView's Pine "
                                        "editor converts a script in one click: "
                                        "'Convert code to v6'."))
        return res

    pc, CompileError, Lexer, Parser, VarDecl, global_input_calls = _codegen()
    try:
        full = pc.transpile_full(source, filename="strategy.pine")
    except CompileError as e:
        for d in e.diagnostics:
            loc = d.location
            target = res.errors if getattr(d.level, "value", "") == "error" else res.warnings
            target.append(Message(loc.line if loc else 0, loc.col if loc else 0,
                                  d.message + (f" ({d.hint})" if d.hint else ""), _code(d)))
        if not res.errors:
            res.errors.append(Message(0, 0, str(e)))
        return res
    except Exception as e:                                   # a transpiler bug, not the user's
        res.errors.append(Message(0, 0, f"the script could not be transpiled: {e}"))
        return res

    for d in full["diagnostics"]:
        loc = d.location
        res.warnings.append(Message(loc.line if loc else 0, loc.col if loc else 0,
                                    d.message, _code(d)))

    ast = Parser(Lexer(source, filename="strategy.pine").tokenize(), source=source,
                 filename="strategy.pine").parse()
    names, kinds = _input_names(ast, VarDecl, global_input_calls)
    res.warnings.extend(_conditional_ta(ast))
    manifest = full["inputs"]
    for k, entry in enumerate(manifest):
        res.inputs.append(PR.Input(
            name=names[k] if k < len(names) else None, title=entry.get("title", ""),
            # PineForge reports input.time as an int of milliseconds; it is a
            # date, shown as one and never swept.
            type="time" if k < len(kinds) and kinds[k] == "time" else entry.get("type", ""),
            default=entry.get("default"),
            min=_num(entry.get("min")), max=_num(entry.get("max")), step=_num(entry.get("step"))))

    res.params = dict(full.get("strategyParams") or {})
    res.title = str(res.params.get("title") or _title(source) or "")
    res.costs = _costs(res.params)
    if res.costs.zero:
        res.warnings.append(Message(0, 0, "commission and slippage are both 0: real fills cost "
                                          "more than this, so the result is optimistic"))

    res.requests = list(full.get("requests") or [])
    for r in res.requests:
        line = int(r.get("line") or 0)
        sym, tf = r.get("symbol") or {}, r.get("timeframe") or {}
        key = sym.get("value") if sym.get("kind") == "literal" else (
            sym.get("default") if sym.get("kind") == "input" else None)
        ptf = tf.get("value") if tf.get("kind") == "literal" else (
            tf.get("default") if tf.get("kind") == "input" else None)
        if not isinstance(key, str) or not isinstance(ptf, str):
            res.errors.append(Message(line, 0, "request.security on another symbol needs a literal "
                                               "symbol and timeframe (or an input's default)"))
            continue
        found, ours = SY.resolve(key), SY.timeframe(ptf)
        if found is None:
            res.errors.append(Message(line, 0, f"'{key}' is not one of the markets here; "
                                               "list_markets shows which can be requested"))
            continue
        if ours is None:
            res.errors.append(Message(line, 0, f"timeframe '{ptf}' for {found[0]} is not stored; "
                                               "use 5, 15, 30, 60, 240 or D"))
            continue
        if not any(f["key"] == key and f["pine_tf"] == ptf for f in res.feeds):
            res.feeds.append({"key": key, "pine_tf": ptf, "symbol": found[0], "timeframe": ours,
                              "asset_class": found[1]})

    res.grid = PR.build(source, res.inputs)
    for p in res.grid.errors:
        res.errors.append(Message(p.line, 0, p.message))
    if not res.grid.scored and not res.grid.errors:
        res.warnings.append(Message(0, 0, "no // @grid lines: this runs once on its default "
                                          "inputs and is not scored"))

    res.cpp = full["cpp"]
    res.ok = not res.errors
    return res


def _conditional_ta(ast) -> list[Message]:
    """`ta.*` calls that run on some bars only: inside an if/else, a loop, a
    switch case, a ternary branch, or the right side of `and` / `or`. Their
    history then skips the bars that did not reach them, which silently
    changes what they compute -- TradingView warns about this; PineForge does
    not. Function bodies are skipped: they run wherever they are called."""
    import dataclasses
    from pineforge_codegen import ast_nodes as A

    found: dict[int, Message] = {}

    def is_ta(node) -> str | None:
        if isinstance(node, A.FuncCall) and isinstance(node.callee, A.MemberAccess) \
                and isinstance(node.callee.object, A.Identifier) and node.callee.object.name == "ta":
            return f"ta.{node.callee.member}"
        return None

    def visit(node, conditional: bool):
        if isinstance(node, list):
            for item in node:
                visit(item, conditional)
            return
        if not isinstance(node, A.ASTNode) or isinstance(node, (A.FuncDef, A.MethodDef)):
            return
        name = is_ta(node)
        if name and conditional and node.loc and node.loc.line not in found:
            found[node.loc.line] = Message(
                node.loc.line, node.loc.col,
                f"{name} runs only on some bars here, so its history skips bars; compute it "
                "once at the top level and use the result inside the condition")
        if isinstance(node, A.IfStmt):
            visit(node.condition, conditional)
            visit(node.body, True)
            visit(node.else_body, True)
            return
        if isinstance(node, (A.ForStmt, A.ForInStmt, A.WhileStmt)):
            for f in dataclasses.fields(node):
                visit(getattr(node, f.name), True if f.name == "body" else conditional)
            return
        if isinstance(node, A.SwitchStmt):
            visit(node.expr, conditional)
            for case_expr, body in node.cases:
                visit(case_expr, True)
                visit(body, True)
            visit(node.default_body, True)
            return
        if isinstance(node, A.Ternary):
            visit(node.condition, conditional)
            visit(node.true_val, True)
            visit(node.false_val, True)
            return
        if isinstance(node, A.BinOp) and node.op in ("and", "or"):
            visit(node.left, conditional)
            visit(node.right, True)
            return
        for f in dataclasses.fields(node):
            if f.name not in ("loc", "annotations"):
                value = getattr(node, f.name)
                visit(list(value.values()) if isinstance(value, dict) else value, conditional)

    visit(ast.body, False)
    return [found[k] for k in sorted(found)]


def _input_names(ast, VarDecl, global_input_calls) -> tuple[list[str | None], list[str]]:
    """The variable each global input call is assigned to, and which input
    function it is (`int`, `time`, ...), in the manifest's order. The manifest
    walks the same statements the same way."""
    from pineforge_codegen.ast_nodes import Identifier, MemberAccess
    names, kinds = [], []
    for stmt in ast.body:
        for node in global_input_calls(stmt):
            names.append(stmt.name if isinstance(stmt, VarDecl) else None)
            c = node.callee
            kinds.append(c.member if isinstance(c, MemberAccess) and isinstance(c.object, Identifier)
                         and c.object.name == "input" else "input")
    return names, kinds


def _costs(params: dict) -> Costs:
    if not any(k in params for k in COST_KEYS):
        return Costs(False)
    ctype = params.get("commission_type")
    ctype = str(ctype).rsplit(".", 1)[-1] if ctype is not None else "percent"
    return Costs(True, ctype, _num(params.get("commission_value")) or 0.0,
                 _num(params.get("slippage")) or 0.0)


def _title(source: str) -> str | None:
    m = re.search(r'strategy\(\s*(?:title\s*=\s*)?"([^"]*)"', source)
    return m.group(1) if m else None


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _code(d) -> str:
    try:
        return d.code
    except Exception:
        return ""
