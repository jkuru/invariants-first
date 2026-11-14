"""Regression tests for fix round 34, the CEL engine's part (an external review of the cost model and
of the engines' parity; the pydantic computed-field item is in invariants-py's
tests/test_fix_round34_review.py, since this package's tests do not install pydantic).

3a. inv.unique_by compared every key with every earlier one (O(N^2) calls of _eq), on both backends,
    and none of it was charged: 4,000 distinct ints took 2.4 s inside the subset's budget. Keys are
    now hashed by their CEL value (linear), and the values read whole for that are charged (3c).
3b. A map comprehension (all, exists, exists_one, map, filter, inv.unique_by) and size() read a map's
    keys as Python keys on the subset, so a map holding a key CEL cannot hold (a double, null, a
    tuple, bytes, a date, an int past int64) was iterated there and a rule_error on cel-expr
    ("unsupported map key type float"). The keys are now read as CEL keys on the subset too.
3c. Reading a list, map or record whole (==, !=, in, a record as a map, an Enum-keyed map) was free
    on the subset however big the value: `xs.all(x, x != ys)` over 3,000 x and a 20,000-row ys took
    86 s inside the budget. Each value read whole now costs one unit per VALUES_PER_COST values.
    The string functions under 4,096 characters were not a gap: their node units already pay for
    them at under CHARS_PER_COST characters a unit (pinned below).
"""
from __future__ import annotations

import enum
import math
import time

import pytest

import invariants_cel as cel
from invariants_cel import available_backends, get_backend
from invariants_cel.errors import ALLOW_NON_FINITE, CelError
from invariants_cel.subset import cost, equality, evaluator, parse
from cel_support import outcome

BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]


def _run(backend: str, expr: str, env: dict) -> tuple[str, str | None]:
    return outcome(get_backend(backend).compile(expr), env)


# ---- 3a. inv.unique_by: linear, by CEL equality ------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_unique_by_does_not_compare_every_pair(backend, monkeypatch):
    calls = [0]
    real = equality._eq

    def counting(*a, **k):
        calls[0] += 1
        return real(*a, **k)

    monkeypatch.setattr(equality, "_eq", counting)
    monkeypatch.setattr(evaluator, "_eq", counting)
    monkeypatch.setattr(cel.subset, "_eq", counting)
    assert _run(backend, "inv.unique_by(xs, x, x)", {"xs": list(range(400))}) == ("violation", None)
    assert calls[0] < 400, f"{calls[0]} equality calls for 400 keys"


@pytest.mark.parametrize("backend", BACKENDS)
def test_unique_by_of_4000_ints_is_fast(backend):
    t = time.perf_counter()
    assert _run(backend, "inv.unique_by(xs, x, x)", {"xs": list(range(4000))}) == ("violation", None)
    assert time.perf_counter() - t < 1.5          # 2.4 s before the fix on a fast machine


UNIQUE_ROWS = [
    ("[1, 2, 3]", True), ("[1, 1.0]", False), ("[1, true]", True), ("[0, false]", True),
    ('["a", "a"]', False), ('["1", 1]', True), ("[[1], [1.0]]", False), ("[[1], [true]]", True),
    ("[[1, [2]], [1, [2.0]]]", False), ("[[1, 2], [2, 1]]", True), ('[{"a": 1}, {"a": 1.0}]', False),
    ('[{"a": 1, "b": 2}, {"b": 2, "a": 1}]', False), ('[{"a": 1}, {"a": true}]', True),
    ('[{1: "x"}, {1.0: "x"}]', None), ('[{1: "x"}, {true: "x"}]', True), ("[null, null]", False),
    ("[null, 0]", True), ("[[], {}]", True), ("[[], []]", False), ("[{}, {}]", False),
    ('[duration("1s"), duration("1000ms")]', False), ('[timestamp("2024-01-01T00:00:00Z"), 0]', True),
    ("[]", True), ("[1]", True),
]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("xs,unique", UNIQUE_ROWS)
def test_unique_by_keeps_cel_equality(backend, xs, unique):
    got = _run(backend, f"inv.unique_by({xs}, x, x)", {})
    if unique is None:                      # a double map literal key is an error on both
        assert got[0] == "rule_error"
    else:
        assert got == ("violation" if unique else "ok", None), xs


@pytest.mark.parametrize("backend", BACKENDS)
def test_unique_by_errors_where_a_comparison_would_have_met_them(backend):
    nan = float("nan")
    assert _run(backend, "inv.unique_by(xs, x, x)", {"xs": [1, nan]})[0] == "rule_error"
    assert _run(backend, "inv.unique_by(xs, x, [x])", {"xs": [1, nan]})[0] == "rule_error"
    assert _run(backend, "inv.unique_by(xs, x, x)", {"xs": [[1], [nan]]})[0] == "rule_error"
    if backend == "subset":                 # the subset reads lazily: a decided result stands
        assert _run(backend, "inv.unique_by(xs, x, x)", {"xs": [[1], [1], [nan]]}) == ("ok", None)
        assert _run(backend, "inv.unique_by(xs, x, x)", {"xs": [[nan]]}) == ("violation", None)
        assert _run(backend, "inv.unique_by(xs, x, x)", {"xs": [[nan], [1]]})[0] == "rule_error"


@pytest.mark.parametrize("backend", BACKENDS)
def test_unique_by_with_non_finite_numbers_allowed(backend):
    tok = ALLOW_NON_FINITE.set(True)
    try:
        nan, inf = float("nan"), math.inf
        assert _run(backend, "inv.unique_by(xs, x, x)", {"xs": [nan, nan]}) == ("violation", None)
        assert _run(backend, "inv.unique_by(xs, x, [x])", {"xs": [nan, nan]}) == ("violation", None)
        assert _run(backend, "inv.unique_by(xs, x, x)", {"xs": [inf, inf]}) == ("ok", None)
        assert _run(backend, "inv.unique_by(xs, x, x)", {"xs": [inf, -inf, 0.0, -0.0]}) == ("ok", None)
    finally:
        ALLOW_NON_FINITE.reset(tok)


def test_unique_matches_pairwise_equality_on_mixed_values():
    import itertools
    pool = [0, 1, 1.0, 2, True, False, "1", "a", None, [], {}, [1], [1.0], [True], {"a": 1}, {"a": 1.0},
            {1: 2}, {True: 2}, [[1]], [[1.0]], -0.0, 0.0]
    for xs in itertools.combinations(pool, 3):
        pairwise = not any(equality._eq(a, b) for a, b in itertools.combinations(xs, 2))
        assert equality.unique(list(xs)) is pairwise, xs


# ---- 3b. a map's keys are read as CEL keys under the macros and size() --------------------------------

class Color(enum.Enum):
    RED = "red"


BAD_KEYS = [1.5, None, (1, 2), b"x", 2**70, float("nan")]
KEY_EXPRS = ["m.exists(k, true)", "m.all(k, false)", "m.exists_one(k, true)", "size(m.map(k, 1)) == 1",
             "size(m.filter(k, true)) == 1", "inv.unique_by(m, k, 1)", "size(m) == 1"]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("key", BAD_KEYS, ids=repr)
@pytest.mark.parametrize("expr", KEY_EXPRS)
def test_a_key_cel_cannot_hold_is_a_rule_error(backend, key, expr):
    assert _run(backend, expr, {"m": {key: 1, "ok": 2}})[0] == "rule_error"


@pytest.mark.parametrize("backend", BACKENDS)
def test_map_comprehensions_read_only_the_keys(backend):
    env = {"m": {Color.RED: float("nan"), "b": 1}, "n": {"a": float("nan")}}
    assert _run(backend, 'm.exists(k, k == "red")', env) == ("violation", None)
    assert _run(backend, "size(m) == 2", env) == ("violation", None)
    assert _run(backend, "n.all(k, k != '')", env) == ("violation", None)
    assert _run(backend, "m2.exists(k, true)", {"m2": {Color.RED: 1, "red": 2}})[0] == "rule_error"


def test_map_keys_convert_as_the_engines_do():
    assert equality.cel_keys({Color.RED: 1, 3: 2, True: 0}) == ["red", 3, True]
    with pytest.raises(CelError, match="unsupported map key type float"):
        equality.cel_keys({1.5: 1})
    with pytest.raises(CelError, match="repeated map key"):
        equality.cel_keys({Color.RED: 1, "red": 2})


# ---- 3c. values read whole are charged; string functions were already bounded -------------------------

ROWS = [[i, [i]] for i in range(2000)]


def _spent(expr: str, env: dict, budget: int = cost.DEFAULT_BUDGET) -> int:
    b = [budget]
    evaluator.evaluate(parse(expr), env, b)
    return budget - b[0]


def test_a_value_read_whole_is_charged():
    env = {"ys": ROWS, "zs": [list(r) for r in ROWS]}
    values = 2 * (1 + 2000 * 4)             # each side: the list, and per row a list, an int, a list, an int
    assert _spent("ys == zs", env) >= values // cost.VALUES_PER_COST
    assert _spent("[1] == [1]", {}) < 10    # small values stay cheap
    assert _spent("1 in ys", {"ys": list(range(10_000))}) >= 10_000 // cost.VALUES_PER_COST


def test_a_map_read_whole_counts_its_keys_and_values():
    m = {f"k{i}": i for i in range(2000)}
    values = 2 * (1 + 2 * 2000)             # each side: the map, its 2,000 keys and 2,000 values
    assert _spent("m == n", {"m": m, "n": dict(m)}) >= values // cost.VALUES_PER_COST


def test_unique_by_charges_the_keys_it_reads_whole():
    xs = [[i] * 2000 for i in range(4)]
    assert _spent("inv.unique_by(xs, x, x)", {"xs": xs}) >= 4 * 2001 // cost.VALUES_PER_COST


def test_comparing_against_a_big_list_in_a_loop_runs_out_of_budget():
    t = time.perf_counter()
    got = outcome(get_backend("subset").compile("xs.all(x, x != ys)"), {"xs": [[0]] * 300, "ys": ROWS})
    assert got[0] == "rule_error" and "cost limit exceeded" in (got[1] or "")
    assert time.perf_counter() - t < 1.0


def test_membership_and_records_in_a_loop_are_charged():
    import dataclasses

    @dataclasses.dataclass
    class Rec:
        rows: list

    env = {"xs": list(range(300)), "ys": ROWS, "r": Rec(ROWS)}
    for expr in ("xs.all(x, [x] in ys || true)", "xs.all(x, size(r) == 1)", 'xs.all(x, "rows" in r)'):
        assert outcome(get_backend("subset").compile(expr), env)[0] == "rule_error", expr


def test_string_functions_never_scan_more_than_chars_per_cost_a_unit():
    """Pinned: a call under CHARS_PER_COST characters costs no extra unit, but its own nodes (the
    call, its receiver, its argument) cost three, so no loop scans more than CHARS_PER_COST
    characters per unit spent."""
    for n in (1, 100, 2047, 4094, 4095, 4096, 8191, 20_000):
        s = "a" * n
        for fn, scanned in (("contains", n + 1), ("startsWith", 1), ("endsWith", 1)):
            spent = _spent(f"xs.all(x, s.{fn}(x))", {"xs": ["a"] * 100, "s": s}, 10**9)
            assert spent * cost.CHARS_PER_COST >= 100 * scanned, (fn, n, spent)
