"""Regression tests for fix round 24 (runtime): each test failed before its fix.

H1. A list-like value that is not a list (a pydantic RootModel, collections.UserList, any Sequence or
    iterable class) is never read as the map of its fields: a RootModel is its .root, anything else is
    a rule_error on every backend (cel-expr made size() count its attributes), refused at load when
    annotated.
H2. cel-expr: a NUL in a value one clause reads errors that clause only, never the rule's others.
H3. cel-expr: whether an int past 2^53 may meet a double is decided per clause, and the exact evaluation
    keeps cel-expr's verdict where the engine answers (its budget, uint(), type(), bytes()).
H4. @inv.guard over a sync wrapper of an async def checks the awaited result, never the coroutine.
M1. An edge hands its consumer exactly what it re-checked (no Checked subclass, no dict subclass).
M2. @inv.guard above @pydantic.validate_call is refused (it would check the uncoerced arguments).
L1. A timestamp getter whose zone moves an instant past year 1 or 9999 is a rule_error on every backend.
L2. matches() patterns have a shared size and nesting limit; the subset charges their compile.
L3. Enum map keys read as their values, records as maps, on every backend.
L4. JsonlReviews writes an int too long for decimal text instead of failing to open the review.
L5. The subset reads a NaN lazily, cel-expr eagerly: both rule_error when evaluated (documented).

The CEL tests run on every installed backend; the cel-expr-only ones skip without it.
"""
from __future__ import annotations

import asyncio
import collections
import datetime
import enum
import json
import pathlib
from typing import Any

import pytest

import invariants as inv
import invariants.testing
import round24 as r24

from invariants_cel import _regex, available_backends, get_backend
from invariants_cel.errors import CelError

BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]
HAS_CEL_EXPR = "cel-expr" in BACKENDS
needs_cel_expr = pytest.mark.skipif(not HAS_CEL_EXPR, reason="cel-expr not installed")
UTC = datetime.timezone.utc


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def outcome(backend: str, fn: str, bad: Any, *args: Any, **enforcer: Any) -> tuple[str, str | None]:
    """(kind, error) of one guarded call of round24.<fn>(*args) under a one-rule document."""
    with inv.isolated():
        inv.load(r24.doc(fn, bad, **enforcer), backend=backend)
        try:
            getattr(r24, fn)(*args)
        except inv.InvariantError as e:
            return e.outcome.kind, e.outcome.error
    return "ok", None


def kind(backend: str, expr: str, **args: Any) -> tuple[str, Any]:
    try:
        return "value", get_backend(backend).compile(expr).evaluate({"args": args})
    except CelError as e:
        return "error", str(e)


# --- H1: list-likes are lists or errors, never maps of their fields --------------------------------
def test_a_root_model_body_is_its_list(backend):
    lines = r24.Lines([{"sku": "a", "qty": 1}] * 500)
    assert outcome(backend, "bulk", "size(args.lines) > 100", lines)[0] == "violation"
    assert outcome(backend, "bulk", "args.lines.exists(l, l.qty > 0)", lines)[0] == "violation"
    assert outcome(backend, "bulk", "size(args.lines) > 1000", lines)[0] == "ok"


def test_a_root_model_is_its_root_wherever_it_is_read(backend):
    from pydantic import RootModel
    n, tags = RootModel[int](7), RootModel[list[str]](["a", "b"])
    assert kind(backend, "args.n > 5 && args.n == 7", n=n) == ("value", True)
    assert kind(backend, "args.r.n + 1 == 8", r={"n": n}) == ("value", True)
    assert kind(backend, "args.t[1] == 'b' && 'a' in args.t", t=tags) == ("value", True)


@pytest.mark.parametrize("make", [r24.Batch, collections.UserList, collections.deque, lambda xs: iter(xs),
                                  lambda xs: range(-1, 500)])
@pytest.mark.parametrize("bad", ["size(args.xs) > 100", "args.xs.exists(x, x < 0)", "args.xs.source == 'api'"])
def test_a_list_like_that_is_not_a_list_is_a_rule_error(backend, make, bad):
    got, err = outcome(backend, "bulk_any", bad, make(list(range(-1, 500))))
    assert got == "rule_error", (got, err)
    assert "not a list or tuple" in (err or "")


def test_an_annotated_list_like_class_is_refused_at_load(backend):
    with inv.isolated(), pytest.raises(inv.LoadError, match="not a list or tuple"):
        inv.load(r24.doc("bulk_batch", "size(args.xs) > 100"), backend=backend)


@pytest.mark.parametrize("v", [collections.UserList(range(500)), r24.Batch(range(500)), collections.UserList([])])
@pytest.mark.parametrize("expr", ["size(args.xs) > 100", "size(args.xs) == 1", "args.xs.exists(x, x == 499)",
                                  "args.xs.source == 'api'", "has(args.xs.source)", "args.xs == []"])
def test_list_likes_agree_on_every_backend(v, expr):
    assert {kind(b, expr, xs=v)[0] for b in BACKENDS} == {"error"}


# --- H2: a NUL errors only the clause that reads it --------------------------------------------------
@pytest.mark.parametrize("on_error", ["fail_closed", "fail_open_and_page"])
@pytest.mark.parametrize("note", ["hello", "hel\x00lo"])
def test_a_nul_in_one_field_never_hides_another_clauses_violation(backend, on_error, note):
    bad = [{"when": "args.amount > 100", "message": "too much"},
           {"when": "args.note == 'forbidden'", "message": "bad note"}]
    assert outcome(backend, "pay", bad, 10**6, note, on_error=on_error)[0] == "violation"


@needs_cel_expr
def test_the_clause_that_reads_a_nul_is_still_a_rule_error_on_cel_expr():
    bad = [{"when": "args.amount > 100", "message": "too much"},
           {"when": "args.note == 'forbidden'", "message": "bad note"}]
    got, err = outcome("cel-expr", "pay", bad, 1, "hel\x00lo")
    assert got == "rule_error" and "NUL" in (err or "")


# --- H3: exact int/double comparison, per clause, with cel-expr's verdict ---------------------------
def items(backend: str) -> list[dict[str, Any]]:      # 4,000 lines: past the subset's own budget
    return [{"qty": 1, "price": 1.5}] * (4000 if backend == "cel-expr" else 500) + [{"qty": -1, "price": 1.5}]

ORDER_BAD = [{"when": "args.items.exists(i, i.qty < 0 || i.price < 0.0)", "message": "negative"},
             {"when": "args.ref < 0", "message": "bad ref"}]


@pytest.mark.parametrize("on_error", ["fail_closed", "fail_open_and_page"])
@pytest.mark.parametrize("ref", [7, 2**60])
def test_a_big_int_in_one_argument_never_changes_another_clauses_verdict(backend, on_error, ref):
    assert outcome(backend, "order", ORDER_BAD, items(backend), ref, on_error=on_error)[0] == "violation"


@needs_cel_expr
@pytest.mark.parametrize("n", [100, 3400, 9998])
def test_the_exact_evaluation_has_cel_exprs_budget(n):
    big = 2**60
    for xs in ([1] * n + [2**53 + 1], [1] * n + [big]):
        assert kind("cel-expr", "args.xs.all(x, x < 1.5e30)", xs=xs) == ("value", True)
    assert kind("cel-expr", "args.xs.exists(x, x > 1.5)", xs=[big] + [1] * 100_000) == ("value", True)
    over = kind("cel-expr", "args.xs.all(x, x < 1.5e30)", xs=[1] * 10_000 + [big])
    assert over[0] == "error" and "cost limit exceeded" in over[1]   # the engine's budget still decides


@needs_cel_expr
@pytest.mark.parametrize("expr,args,want", [
    ("type(args.x) == int && args.y > 1.5", {"x": 2**60, "y": 2.0}, True),
    ("type(args.x) == uint || args.y > 1.5", {"x": 2**60, "y": 1.0}, False),
    ("args.u == 5u && args.y > 1.5", {"u": 2**60, "y": 2.0}, False),
    ("bytes(args.s) == bytes('a') && args.n > 1.5", {"s": "a", "n": 2**60}, True),
    ("type(args.t) == google.protobuf.Timestamp && args.n > 1.5",
     {"t": datetime.datetime(2020, 1, 1, tzinfo=UTC), "n": 2**60}, True),
    ("uint(args.s) == 9007199254740992.0", {"s": "9007199254740993"}, False),
    ("uint(args.s) > 9007199254740992.0", {"s": "9007199254740993"}, True),
    ("args.n == 9007199254740992.0", {"n": 2**53 + 1}, False),
    ("[args.n] == [9007199254740992.0]", {"n": 2**53 + 1}, False),
])
def test_the_exact_evaluation_has_every_cel_expr_function(expr, args, want):
    assert kind("cel-expr", expr, **args) == ("value", want)


def test_type_names_and_uint_stay_unknown_on_the_subset_backend():
    for expr in ("type(args.x) == int", "uint(args.x) == 1u", "bytes(args.s) == bytes('a')"):
        assert kind("subset", expr, x=1, s="a")[0] == "error"


# --- H4: a sync wrapper over an async def -----------------------------------------------------------
@pytest.mark.parametrize("bad", ["result.amount > 100", "has(result.amount) && result.amount > 100"])
def test_post_effect_checks_see_the_awaited_result(backend, bad):
    with inv.isolated():
        inv.load(r24.doc("refund", bad, timing="post_effect"), backend=backend)
        with pytest.raises(inv.InvariantError) as got:
            asyncio.run(r24.refund(10**9))
        assert got.value.outcome.kind == "violation"
        assert asyncio.run(r24.refund(5)) == r24.Refund(5)


def test_an_awaitable_that_is_not_a_coroutine_is_refused_for_post_effect_checks(backend):
    async def call() -> Any:
        return r24.refund_future(10**9)
    with inv.isolated():
        inv.load(r24.doc("refund_future", "has(result.amount) && result.amount > 100", timing="post_effect"),
                 backend=backend)
        with pytest.raises(TypeError, match="not a coroutine"):
            asyncio.run(call())


# --- M1: the consumer gets exactly what the edge re-checked -----------------------------------------
def test_a_checked_subclass_cannot_rebuild_itself_past_the_edge():
    class Evil(r24.CPlan):
        @classmethod
        def _inv_make(cls, value: Any, attempts: int, rule: str, verdict: str) -> Any:
            return inv.testing.make_checked(r24.CPlan, {"action": "delete", "n": 10**9})
    forged = object.__new__(Evil)
    for k, v in (("value", {"action": "read", "n": 1}), ("attempts", 1), ("rule", "P"), ("verdict", "ok")):
        object.__setattr__(forged, k, v)
    with inv.isolated():
        inv.load(r24.plan_doc())
        r24.SEEN.clear()
        with pytest.raises(TypeError, match="accepts only CPlan"):
            r24.act(forged)
        r24.act(r24.plan())
        assert r24.SEEN == [(r24.CPlan, {"action": "read", "n": 1})]


def test_a_dict_subclass_state_never_reaches_the_consumer():
    with inv.isolated():
        inv.load(r24.plan_doc())
        good = r24.plan()
        forged = inv.testing.make_checked(r24.CPlan, {"action": "delete", "n": 1})

        class Sneaky(dict):
            def __getitem__(self, k: Any) -> Any:
                return forged if k == "plan" else super().__getitem__(k)

            def get(self, k: Any, d: Any = None) -> Any:
                return forged if k == "plan" else super().get(k, d)
        r24.SEEN.clear()
        r24.act_state(Sneaky(plan=good))
        assert r24.SEEN == [(dict, {"action": "read", "n": 1})]


def test_a_dataclass_subclass_state_is_refused():
    with inv.isolated():
        inv.load(r24.plan_doc())
        good = r24.plan()

        class Sub(r24.DState):
            def __getattribute__(self, k: str) -> Any:
                return object.__getattribute__(self, k)
        with pytest.raises(TypeError, match="accepts only a DState state"):
            r24.act_dc(Sub(good))
        r24.SEEN.clear()
        r24.act_dc(r24.DState(good))
        assert r24.SEEN == [(r24.DState, {"action": "read", "n": 1})]


# --- M2: @inv.guard above @validate_call -------------------------------------------------------------
def test_a_guard_above_validate_call_is_refused():
    import functools

    from pydantic import validate_call

    def wire(amount: int, force: bool = False) -> str:
        return "sent"
    with pytest.raises(TypeError, match="below @validate_call"):
        inv.guard("W")(validate_call(wire))

    def logged(fn: Any) -> Any:
        @functools.wraps(fn)
        def w(*a: Any, **k: Any) -> Any:
            return fn(*a, **k)
        return w
    with pytest.raises(TypeError, match="below @validate_call"):
        inv.guard("W")(logged(validate_call(wire)))
    validate_call(inv.guard("W")(wire))      # the guard below validate_call checks the coerced arguments


# --- L1: getters at the range ends ---------------------------------------------------------------
@pytest.mark.parametrize("expr,t", [
    ("args.t.getFullYear('-23:59') < 5", datetime.datetime(1, 1, 1, tzinfo=UTC)),
    ("args.t.getMonth('-01:00') == 11", datetime.datetime(1, 1, 1, tzinfo=UTC)),
    ("args.t.getFullYear('+23:59') > 9000", datetime.datetime.max.replace(tzinfo=UTC)),
    ("args.t.getHours(args.z) == 0", datetime.datetime.max.replace(tzinfo=UTC)),
])
def test_a_getter_past_the_range_is_a_rule_error(backend, expr, t):
    got = kind(backend, expr, t=t, z="Asia/Tokyo")
    assert got[0] == "error" and "timestamp out of range" in got[1]


@pytest.mark.parametrize("on_error", ["fail_closed", "fail_open_and_page"])
def test_a_getter_past_the_range_is_on_errors_call(backend, on_error):
    got = outcome(backend, "when", "args.t.getFullYear('-23:59') < 5", datetime.datetime(1, 1, 1, tzinfo=UTC),
                  on_error=on_error)
    assert got[0] == ("rule_error" if on_error == "fail_closed" else "ok")


# --- L2: the pattern limit -------------------------------------------------------------------------
@pytest.mark.parametrize("pattern", ["(" * 5000 + ")" * 5000, "a" * 999_999, "[a-z]{1000}" * 50,
                                     "((((a{100}){100}){100}){100})", "(a|b|c|d)" * 100_000])
def test_a_data_pattern_over_the_limit_is_a_rule_error(backend, pattern):
    got = kind(backend, "args.s.matches(args.p)", s="a" * 100, p=pattern)
    assert got[0] == "error" and "pattern limit" in got[1]


def test_a_constant_pattern_over_the_limit_does_not_load(backend):
    with inv.isolated(), pytest.raises(inv.LoadError, match="pattern limit"):
        inv.load(r24.doc("pay", "args.note.matches('" + "[a-z]{1000}" * 6 + "')"), backend=backend)


def test_the_subset_charges_pattern_compiles_cached_or_not():
    from invariants_cel.subset import cost
    p = "[a-z]{400}"                       # ~400 instructions: about 25 units a call
    prog = get_backend("subset").compile(f"args.xs.exists(x, x.matches('{p}'))")
    cost._RE_CACHE.clear()
    first = kind("subset", f"args.xs.exists(x, x.matches('{p}'))", xs=["a"] * 600)
    second = kind("subset", f"args.xs.exists(x, x.matches('{p}'))", xs=["a"] * 600)
    assert first == second and first[0] == "error" and "cost limit" in first[1]
    assert prog.evaluate({"args": {"xs": ["a"] * 10}}) is False


def test_the_pattern_size_bound_covers_the_subset_program():
    for p in ["a", "(a|b)*c", "[a-z]{3,9}", "(?i)\\pL+", "((ab){2}|c?){5}", "x{2,}y", "\\Qa.b\\E+"]:
        assert _regex.pattern_size(p) >= len(_regex.compile_pattern(p))


def test_the_pattern_cache_is_bounded():
    from invariants_cel.subset import cost
    for i in range(cost._RE_CACHE_MAX + 50):
        kind("subset", "args.s.matches(args.p)", s="a", p=f"a{i}")
    assert len(cost._RE_CACHE) <= cost._RE_CACHE_MAX


# --- L3: Enum keys and records, alike on every backend ---------------------------------------------
class E(enum.Enum):
    A = 1
    S = "s"


class P:
    def __init__(self) -> None:
        self.x = 1


@pytest.mark.parametrize("expr,args,want", [
    ("args.m[1] == 'x'", {"m": {E.A: "x"}}, True),
    ("args.m.s == 'y'", {"m": {E.S: "y"}}, True),
    ("has(args.m.s)", {"m": {E.S: "y"}}, True),
    ("1 in args.m", {"m": {E.A: "x"}}, True),
    ("args.m.exists(k, k == 1)", {"m": {E.A: "x"}}, True),
    ("size(args.m) == 1", {"m": {E.A: "x"}}, True),
    ("size(args.r) == 1", {"r": P()}, True),
    ("'x' in args.r", {"r": P()}, True),
    ("args.r.exists(k, k == 'x')", {"r": P()}, True),
    ("args.r['x'] == 1", {"r": P()}, True),
])
def test_enum_keys_and_records_agree(backend, expr, args, want):
    assert kind(backend, expr, **args) == ("value", want)


def test_an_enum_key_next_to_its_value_is_a_repeated_key(backend):
    got = kind(backend, "size(args.m) == 2", m={E.A: "x", 1: "y"})
    assert got[0] == "error" and "repeated map key" in got[1]


# --- L4: a huge int in a review ----------------------------------------------------------------------
def test_a_huge_bound_int_still_opens_a_review(tmp_path, backend):
    path = tmp_path / "r.jsonl"
    with inv.isolated():
        inv.load(r24.doc("pay", [{"when": "args.note == 'x'", "message": "m"}], on_violation="route"),
                 backend=backend)
        inv.configure(review_store=inv.JsonlReviews(path))
        for amount in (10**6, 10**5000):
            with pytest.raises(inv.InvariantError) as got:
                r24.pay(amount, "x")
            assert got.value.review_id
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert lines[0]["bound"]["args"]["amount"] == 10**6
    assert lines[1]["bound"]["args"]["amount"] == f"<int {hex(10**5000)}>"


# --- L5: a NaN the clause never evaluates (documented) ----------------------------------------------
def test_an_unevaluated_nan_is_lazy_on_the_subset_eager_on_cel_expr():
    nan = float("nan")
    assert kind("subset", "true || args.f > 1.0", f=nan) == ("value", True)
    for b in BACKENDS:
        assert kind(b, "args.f > 1.0 || false", f=nan)[0] == "error"
    if HAS_CEL_EXPR:
        assert kind("cel-expr", "true || args.f > 1.0", f=nan)[0] == "error"
    docs = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "cel.md").read_text()
    assert "A NaN or ±Infinity that the clause reads but never evaluates" in docs
