"""Regression tests for fix round 7 (certification round 5): each test failed before its fix.

Most tests run on every installed CEL backend (parametrised), whatever INV_CEL_BACKEND says.
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import logging
import math
import threading
from typing import Any

import pytest

import invariants as inv
import round7 as r7
from invariants.cel import available_backends, get_backend
from invariants.loader import closed_schema
from invariants.testing import make_checked

pytest_plugins = ["pytester"]

BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]
NAN, INF = float("nan"), float("inf")


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def iso(backend: str):
    with inv.isolated() as r:
        r7.REPLIES.clear()
        inv.load(r7.doc(), backend=backend)
        yield r


def rejected(fn: Any, *a: Any) -> inv.InvariantError | None:
    try:
        fn(*a)
    except inv.InvariantError as e:
        return e
    return None


# --- 1. BLOCKER: NaN and Infinity never get past a range check ------------------------------------
@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
@pytest.mark.parametrize("kind", list(r7.MODELS))
def test_model_reply_with_non_finite_number_is_parse_error(iso, kind, token):
    model = r7.MODELS[kind][0]
    r7.REPLIES[:] = [f'{{"cause": "A", "confidence": {token}}}']
    e = rejected(model, "p")
    assert e is not None and e.outcome.kind == "parse_error"
    assert "non-finite" in e.outcome.error or "not a JSON number" in e.outcome.error


@pytest.mark.parametrize("kind", list(r7.MODELS))
def test_model_reply_finite_still_judged(iso, kind):
    model = r7.MODELS[kind][0]
    r7.REPLIES[:] = ['{"cause": "A", "confidence": 0.5}']
    assert model("p").verdict == "ok"
    r7.REPLIES[:] = ['{"cause": "A", "confidence": 1.5}']
    assert rejected(model, "p").outcome.kind == "violation"


@pytest.mark.parametrize("kind", list(r7.MODELS))
def test_decoded_reply_with_nan_is_parse_error(iso, kind):
    model = r7.MODELS[kind][0]
    r7.REPLIES[:] = [{"cause": "A", "confidence": NAN}]
    assert rejected(model, "p").outcome.kind == "parse_error"


@pytest.mark.parametrize("kind", list(r7.MODELS))
def test_edge_refuses_forged_nan(iso, kind):
    _, use, out, _ = r7.MODELS[kind]
    schema = {"dict": r7.DictReply, "dataclass": r7.DcReply, "pydantic": r7.PdReply}[kind]
    if kind == "dict":
        value: Any = {"cause": "A", "confidence": NAN}
    else:
        value = object.__new__(closed_schema(schema))
        for k, v in (("cause", "A"), ("confidence", NAN)):
            object.__setattr__(value, k, v)
    forged = make_checked(out, value, verdict="ok")
    e = rejected(use, forged)
    assert e is not None and e.outcome.kind == "parse_error" and "non-finite" in e.outcome.error


@pytest.mark.parametrize("value", [{"x": NAN}, {"x": [1.0, INF]}, {"x": {"y": -INF}}])
def test_parse_any_field_refuses_nested_non_finite(value):
    with pytest.raises(inv.ParseError, match="non-finite"):
        inv.parse(value, {"x": Any})


def test_parse_any_field_refuses_nan_token():
    with pytest.raises(inv.ParseError, match="NaN"):
        inv.parse('{"x": [NaN]}', {"x": Any})


@pytest.mark.parametrize("x", [NAN, INF, -INF])
def test_guard_arg_nan_is_rule_error_not_ok(iso, x):
    e = rejected(r7.score, x, [])
    assert e is not None and e.outcome.kind == "rule_error" and "non-finite" in e.outcome.error


def test_guard_nan_inside_a_list_is_rule_error(backend):
    with inv.isolated():
        inv.load(r7.one_rule("args.xs.exists(v, v < 0.0 || v > 1.0)"), backend=backend)
        assert r7.score(0.5, [0.5]) == 0.5
        e = rejected(r7.score, 0.5, [0.5, NAN])
        assert e is not None and e.outcome.kind == "rule_error"


@pytest.mark.parametrize("bad", ["args.xs[1] > 1.0", "args.x in args.xs", "inv.sum(args.xs) > 1.0",
                                 "args.xs == [0.25, 0.5]"])
def test_guard_nan_read_any_way_is_rule_error(backend, bad):
    with inv.isolated():
        inv.load(r7.one_rule(bad), backend=backend)
        e = rejected(r7.score, 0.5, [0.25, NAN])
        assert e is not None and e.outcome.kind == "rule_error", bad


def test_post_effect_nan_result_is_rule_error(iso):
    e = rejected(r7.compute, NAN)
    assert e is not None and e.outcome.kind == "rule_error"
    assert r7.compute(2.0) == 4.0


def test_allow_non_finite_restores_ieee_semantics(iso, backend):
    inv.configure(allow_non_finite=True)
    assert math.isnan(r7.score(NAN, []))                  # NaN passes the NaN-unsafe clause (IEEE)
    with inv.isolated():
        inv.load(r7.one_rule("!(args.x >= 0.0 && args.x <= 1.0)"), backend=backend)
        inv.configure(allow_non_finite=True)
        e = rejected(r7.score, NAN, [])                   # the NaN-safe form catches it
        assert e is not None and e.outcome.kind == "violation"
        assert r7.score(0.5, []) == 0.5


def test_fastapi_nan_body_is_refused():
    from fastapi.testclient import TestClient
    with inv.isolated():
        inv.load(r7.SHOP)
        c = TestClient(r7.shop_app())
        assert c.post("/orders", json={"sku": "a", "quantity": 5}).status_code == 200
        r = c.post("/orders", json={"sku": "a", "quantity": 50})
        assert r.status_code != 200 and r.json()["outcome"] == "violation"
        r = c.post("/orders", content='{"sku": "a", "quantity": NaN}', headers={"content-type": "application/json"})
        assert r.status_code != 200 and r.json()["outcome"] == "rule_error"


# --- 2. MAJOR: function names are checked against the backend at load -----------------------------
@pytest.mark.parametrize("bad", ["nosuchfn(args.x) > 0.0", "args.x.foo() > 0.0", "args.xs.exist(v, v > 0.0)",
                                 "size(args.xs) > 0 && args.xs.sizee() > 0", "inv.nosuch(args.xs) > 0"])
def test_unknown_function_is_load_error(backend, bad):
    with pytest.raises(inv.LoadError, match=f"{backend} CEL backend does not have"):
        inv.load(r7.one_rule(bad), install=False, backend=backend)


@pytest.mark.parametrize("bad", ["uint(1) > uint(0)", "bytes('a') == bytes('a')", "type(args.x) == type(1.0)"])
def test_subset_refuses_engine_only_functions_and_engines_run_them(backend, bad):
    if backend == "subset":
        with pytest.raises(inv.LoadError, match="subset CEL backend does not have"):
            inv.load(r7.one_rule(bad), install=False, backend=backend)
        return
    with inv.isolated():
        inv.load(r7.one_rule(bad), backend=backend)
        assert rejected(r7.score, 0.5, []).outcome.kind == "violation"


# fix round 9: the subset has timestamp() and duration() too (and the getters)
@pytest.mark.parametrize("bad", ["timestamp('2020-01-01T00:00:00Z') > timestamp('2019-01-01T00:00:00Z')",
                                 "duration('1s') > duration('0s')"])
def test_every_backend_runs_timestamp_and_duration(backend, bad):
    with inv.isolated():
        inv.load(r7.one_rule(bad), backend=backend)
        assert rejected(r7.score, 0.5, []).outcome.kind == "violation"


AGREED = [("int(2.7) == 2", True), ("int(-2.7) == -2", True), ("int('42') == 42", True),
          ("int(' 42') == 42", True), ("int('+5') == 5", True), ("int(true) == 1", True),
          ("double(3) == 3.0", True), ("double('1.5') == 1.5", True), ("double('1e3') == 1000.0", True),
          ("string(42) == '42'", True), ("string('a') == 'a'", True), ("bool('true')", True),
          ("bool('TRUE')", True), ("bool('1')", True), ("bool('f')", False), ("dyn(1) == 1", True),
          ("int(args.x * 10.0) == 5", True), ("size('ab') == 2", True)]
ERRORS = ["int('4.2') == 4", "int('0x10') == 16", "int(1.0 / 0.5 * 9223372036854775807.0) > 0",
          "bool('yes')", "double('x') > 1.0", "int('1_000') == 1000"]


@pytest.mark.parametrize("expr,want", AGREED)
def test_conversions_agree_on_every_backend(backend, expr, want):
    with inv.isolated():
        inv.load(r7.one_rule(expr), backend=backend)
        e = rejected(r7.score, 0.5, [])
        assert (e is not None and e.outcome.kind == "violation") is want, (expr, e and e.outcome)


@pytest.mark.parametrize("expr", ERRORS)
def test_subset_conversion_errors(expr):
    with inv.isolated():
        inv.load(r7.one_rule(expr), backend="subset")
        assert rejected(r7.score, 0.5, []).outcome.kind == "rule_error"


def test_subset_string_of_double_is_a_clear_error():
    prog = get_backend("subset").compile("string(1.5) == '1.5'")
    with pytest.raises(inv.cel.CelError, match="string\\(double\\)"):
        prog.evaluate({})


def test_conversion_is_typed_at_load(backend):
    with pytest.raises(inv.LoadError, match="ill-typed"):
        inv.load(r7.one_rule("int(args.x) == 'a'"), install=False, backend=backend)
    with pytest.raises(inv.LoadError, match="one argument"):
        inv.load(r7.one_rule("int(args.x, 2) > 0"), install=False, backend=backend)
    with pytest.raises(inv.LoadError, match="global"):
        inv.load(r7.one_rule("args.x.int() > 0"), install=False, backend=backend)


@pytest.mark.parametrize("b", [b for b in BACKENDS if b != "subset"])
@pytest.mark.parametrize("name", sorted(inv.cel.ENGINE_FUNCTIONS))
def test_engine_function_list_is_real(b, name):
    """Every name the engine backends allow is one the engine evaluates (not 'no such function')."""
    samples = {"size": "size('ab') == 2", "contains": "'ab'.contains('a')", "startsWith": "'ab'.startsWith('a')",
               "endsWith": "'ab'.endsWith('b')", "matches": "'ab'.matches('a')", "has": "has({'a': 1}.a)",
               "all": "[1].all(x, x > 0)", "exists": "[1].exists(x, x > 0)", "exists_one": "[1].exists_one(x, x > 0)",
               "map": "[1].map(x, x) == [1]", "filter": "[1].filter(x, x > 0) == [1]", "int": "int(1) == 1",
               "uint": "uint(1) == uint(1)", "double": "double(1) == 1.0", "string": "string(1) == '1'",
               "bytes": "bytes('a') == bytes('a')", "bool": "bool('true')", "type": "type(1) == int",
               "dyn": "dyn(1) == 1", "timestamp": "timestamp('2020-01-01T00:00:00Z') == timestamp('2020-01-01T00:00:00Z')",
               "duration": "duration('1s') == duration('1s')", "inv.sum": "inv.sum([1]) == 1",
               "inv.zip": "size(inv.zip([1], [2])) == 1", "inv.unique_by": "inv.unique_by([1], x, x)"}
    getters = {"getFullYear": 2020, "getMonth": 0, "getDayOfYear": 0, "getDayOfMonth": 0, "getDate": 1,
               "getDayOfWeek": 3, "getHours": 0, "getMinutes": 0, "getSeconds": 0, "getMilliseconds": 0}
    expr = samples.get(name) or f"timestamp('2020-01-01T00:00:00Z').{name}() == {getters[name]}"
    assert get_backend(b).compile(expr).evaluate({}) is True


def test_ablate_reports_rule_error_only_falsifier_as_inconclusive(pytester: pytest.Pytester):
    pytester.makeini("[pytest]\npythonpath = .\n")
    pytester.makepyfile(app7='''
import invariants as inv

@inv.guard("R-1")
def pay(amount: int, limits: dict) -> int:
    return amount
''', test_app7='''
import pytest
import invariants as inv
import app7

def test_r1_falsifier():
    with pytest.raises(inv.Rejected):      # passes only because limits has no 'max' key: rule_error
        app7.pay(5, {})
''')
    doc = {"spec": "invariant/v0.1", "targets": {"py:app7.pay": {"effects": "none"}},
           "rules": [{"id": "R-1", "status": "enforce", "subject": {"target": "py:app7.pay"},
                      "bad": "args.amount > args.limits['max']",
                      "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}],
                      "falsified_by": ["test_app7.py::test_r1_falsifier"]}]}
    (pytester.path / "rules.json").write_text(json.dumps(doc))
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--inv-rules", "rules.json", "--inv-ablate")
    assert r.ret == pytest.ExitCode.TESTS_FAILED
    r.stdout.fnmatch_lines(["*no falsifier caught the ablation of R-1*",
                            "*inconclusive (rule_error, not violation)*test_app7.py::test_r1_falsifier*"])


# --- 3. minor: an edge enforcer needs the model call's rule -----------------------------------------
def test_edge_only_rule_is_load_error():
    with pytest.raises(inv.LoadError, match="edge enforcer belongs on the rule of the @inv.model_call"):
        inv.load(r7.EDGE_ONLY, install=False)


# --- 4. minor: rule_error text carries no request values unless debug -----------------------------
SECRET_CASES = [("args.m[args.k] > 0", {"m": {"a": 1}, "k": "SECRET-KEY-1", "s": "x", "p": "x"}, True),
                ("args.s.matches(args.p)", {"m": {}, "k": "x", "s": "x", "p": "(SECRET-PAT"}, True),
                ("args.m['a'] > 0 && args.s.x == 1", {"m": {"a": 1}, "k": "x", "s": "SECRET-S", "p": "x"}, False)]


@pytest.mark.parametrize("bad,args,in_detail", SECRET_CASES)
def test_rule_error_text_is_redacted(backend, bad, args, in_detail, caplog):
    with inv.isolated() as reg, inv.capture(reg) as events:
        inv.load(r7.doc(g3=bad), backend=backend)
        with caplog.at_level(logging.DEBUG, logger="invariants"):
            e = rejected(r7.lookup, args["m"], args["k"], args["s"], args["p"])
        assert e is not None and e.outcome.kind == "rule_error"
        assert "SECRET" not in str(e) and "SECRET" not in (e.outcome.error or "")
        assert "SECRET" not in json.dumps(events, default=str) and "SECRET" not in caplog.text
        inv.configure(debug=True)                  # debug shows the engine's own text
        e = rejected(r7.lookup, args["m"], args["k"], args["s"], args["p"])
        assert e is not None and e.outcome.kind == "rule_error"
        if in_detail:
            assert "SECRET" in (e.outcome.error or ""), e.outcome.error


def test_missing_field_named_in_rule_is_still_named(backend):
    rule = inv.load({"rules": [{"id": "C-1", "status": "candidate", "bad": "args.y.zz"}]},
                    install=False, backend=backend).rules["C-1"]
    out = inv.evaluate(rule, {"args": {"y": {}}})
    assert out.kind == "rule_error" and "zz" in out.error


# --- 5. minor: inv.ablated() is context-local by default ------------------------------------------
def _other_thread_passes(reg: inv.Registry) -> bool:
    got: list[bool] = []

    def run() -> None:
        with inv.using(reg):
            got.append(rejected(r7.score, 5.0, []) is None)
    t = threading.Thread(target=run)
    t.start()
    t.join()
    return got[0]


def test_ablated_does_not_leak_to_other_threads(iso):
    with inv.ablated("G-1"):
        assert r7.score(5.0, []) == 5.0                   # ablated here
        assert _other_thread_passes(iso) is False         # still enforced in another thread
    assert rejected(r7.score, 5.0, []) is not None


def test_ablated_registry_scope_reaches_other_threads(iso):
    with inv.ablated("G-1", scope="registry"):
        assert _other_thread_passes(iso) is True
    assert _other_thread_passes(iso) is False


def test_ablated_reaches_tasks_started_inside(iso):
    async def main() -> bool:
        with inv.ablated("G-1"):
            return await asyncio.create_task(asyncio.to_thread(lambda: rejected(r7.score, 5.0, []) is None))
    assert asyncio.run(main()) is True


def test_ablated_bad_scope():
    with pytest.raises(ValueError, match="scope"):
        inv.ablated("G-1", scope="nope")


# --- 6. minor: load() copies the caller's dict ------------------------------------------------------
def test_load_does_not_alias_or_mutate_the_callers_dict(iso, backend):
    d = r7.doc()
    before = copy.deepcopy(d)
    inv.load(d, backend=backend)
    assert d == before                                    # no defaults written into it
    for r in d["rules"]:
        for e in r["enforcers"]:
            e["mode"] = "off"
    assert rejected(r7.score, 5.0, []) is not None        # the live guard did not change


def test_load_refuses_uncopyable_document():
    class NoCopy:
        def __deepcopy__(self, memo: Any) -> Any:
            raise RuntimeError("no")
    with pytest.raises(inv.LoadError, match="could not be copied"):
        inv.load({"rules": [], "contracts": {"x": NoCopy()}}, install=False)


def test_dataclass_reply_is_still_built(iso):
    r7.REPLIES[:] = ['{"cause": "B", "confidence": 1}']
    c = r7.cmodel("p")
    assert dataclasses.is_dataclass(c.value) and c.value.confidence == 1.0 and math.isfinite(c.value.confidence)
