"""The @grid / @constraint grammar and the grid it builds. Pure Python: runs
without PineForge, so it runs everywhere the rest of the suite does."""
from __future__ import annotations

import pytest

from btb.pine import pragmas as PR


def _inputs(**kw):
    out = []
    for name, (typ, default, lo, hi) in kw.items():
        out.append(PR.Input(name=name, title=name.replace("_", " ").title(), type=typ,
                            default=default, min=lo, max=hi))
    return out


INPUTS = _inputs(fast=("int", 20, 1, None), slow=("int", 50, 2, 400),
                 mult=("float", 3.0, 0.5, 10.0), on=("bool", True, None, None))


def test_lists_ranges_and_constraints():
    src = """
    // @grid fast = 10, 20, 30
    // @grid slow = 20 .. 60 step 20
    // @constraint fast < slow
    """
    g = PR.build(src, INPUTS)
    assert not g.errors
    assert g.values == {"fast": [10, 20, 30], "slow": [20, 40, 60]}
    assert g.raw == 9
    # fast < slow removes (20, 20), (30, 20)
    assert g.n == 7
    assert all(p["fast"] < p["slow"] for p in g.combos)
    # Inputs not in the grid stay at their defaults in every combination.
    assert all(p["mult"] == 3.0 for p in g.combos)


def test_decimal_ranges_land_on_the_written_decimals():
    g = PR.build("// @grid mult = 1.5 .. 3 step 0.5", INPUTS)
    assert g.values["mult"] == [1.5, 2, 2.5, 3]
    g = PR.build("// @grid mult = 0.5 .. 0.8 step 0.1", INPUTS)
    assert g.values["mult"] == [0.5, 0.6, 0.7, 0.8]


def test_no_pragmas_means_unscored_not_an_error():
    g = PR.build("len = input.int(10)", INPUTS)
    assert not g.errors and not g.scored and g.n == 0


@pytest.mark.parametrize("line, needle", [
    ("// @grid nope = 1, 2", "no input is assigned to 'nope'"),
    ("// @grid fast = 1.5, 2", "is an input.int"),
    ("// @grid slow = 1, 2", "outside the input's minval=2"),
    ("// @grid slow = 100, 500", "outside the input's maxval=400"),
    ("// @grid on = 1, 2", "only int and float inputs"),
    ("// @grid fast = 1, 1", "listed twice"),
    ("// @grid fast = a, b", "must be numbers"),
    ("// @grid fast 1 2", "expected '// @grid name"),
    ("// @grid fast = 1 .. 100 step 1", "at most 25"),
    ("// @constraint fast <> slow", "expected '// @constraint"),
    ("// @constraint fast < nope", "'nope' is not an input"),
])
def test_errors_name_the_line(line, needle):
    src = "x = 1\n" + line
    g = PR.build(src, INPUTS)
    assert g.errors, line
    assert needle in g.errors[0].message
    assert g.errors[0].line in (0, 2)


def test_constraints_that_rule_out_everything():
    g = PR.build("// @grid fast = 30, 40\n// @grid slow = 10, 20\n// @constraint fast < slow",
                 _inputs(fast=("int", 30, None, None), slow=("int", 20, None, None)))
    assert any("rule out all 4" in e.message for e in g.errors)


def test_combination_cap():
    many = _inputs(a=("int", 1, None, None), b=("int", 1, None, None), c=("int", 1, None, None))
    src = "\n".join(f"// @grid {k} = 1 .. 11 step 1" for k in "abc")   # 11**3 = 1331
    g = PR.build(src, many)
    assert any("at most 1000" in e.message for e in g.errors)


def test_runtime_inputs_are_keyed_by_title():
    p = {"fast": 10, "mult": 2.5}
    assert PR.as_runtime_inputs(p, INPUTS) == {"Fast": "10", "Mult": "2.5"}


def test_meta():
    m = PR.meta("// @description Buy dips.\n// @style trend\n// @timeframes 4h, 1d\n"
                "// @style nonsense")
    assert m == {"description": "Buy dips.", "style": "other", "timeframes": ["4h", "1d"]}
