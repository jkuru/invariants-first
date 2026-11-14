"""Regression tests for fix round 8 (certification round 6): each test failed before its fix.

Most tests run on every installed CEL backend (parametrised), whatever INV_CEL_BACKEND says.
"""
from __future__ import annotations

import ast
import json
import logging
import pathlib
import subprocess
import sys
import threading
from typing import Any

import pytest

import invariants as inv
import round8 as r8
import invariants_cel as cel
from invariants_cel import available_backends, get_backend
from invariants.integrations import pydantic as inv_pd

BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]
ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def iso(backend: str):
    with inv.isolated() as r:
        r8.REPLIES.clear()
        r8.FEEDBACK.clear()
        inv.load(r8.doc(), backend=backend)
        yield r


def rejected(fn: Any, *a: Any) -> inv.InvariantError | None:
    try:
        fn(*a)
    except inv.InvariantError as e:
        return e
    return None


def top(confidence: str) -> str:
    return f'{{"cause": "A", "confidence": {confidence}}}'


def nested(inner: str = "0.5", xs: str = "[0.5]", m: str = '{"k": 0.5}') -> str:
    return f'{{"inner": {{"cause": "A", "confidence": {inner}}}, "xs": {xs}, "m": {m}}}'


# --- 1. MAJOR: pydantic replies are strict; a "NaN" string is not a float -------------------------
NON_FINITE_TEXT = ['"NaN"', '"nan"', '"inf"', '"Infinity"', '"-Infinity"']


@pytest.mark.parametrize("token", NON_FINITE_TEXT + ['"0.5"', "true", "null", '"1"'])
def test_pydantic_parse_refuses_coercible_strings_at_top_level(token):
    with pytest.raises(inv.ParseError):
        inv.parse(top(token), r8.Top)


@pytest.mark.parametrize("where", ["inner", "xs", "m"])
@pytest.mark.parametrize("token", NON_FINITE_TEXT + ['"0.5"', "true"])
def test_pydantic_parse_refuses_coercible_strings_nested(where, token):
    raw = {"inner": nested(inner=token), "xs": nested(xs=f"[0.1, {token}]"), "m": nested(m=f'{{"k": {token}}}')}
    with pytest.raises(inv.ParseError):
        inv.parse(raw[where], r8.Nested)


def test_pydantic_strict_still_takes_plain_json():
    v = inv.parse(nested(inner="1"), r8.Nested)          # a JSON int is a float, as in a dataclass schema
    assert inv_pd.model_of(v).inner.confidence == 1.0
    assert inv.parse(top("0.25"), r8.Top).confidence == 0.25


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_pydantic_decoded_non_finite_is_refused_nested(value):
    with pytest.raises(inv.ParseError, match="non-finite"):
        inv.parse({"inner": {"cause": "A", "confidence": 0.5}, "xs": [value], "m": {}}, r8.Nested)
    with pytest.raises(inv.ParseError, match="non-finite"):
        inv.parse({"inner": {"cause": "A", "confidence": 0.5}, "xs": [], "m": {"k": value}}, r8.Nested)


@pytest.mark.parametrize("token", ['"NaN"', '"inf"', '"-Infinity"'])
def test_lax_pydantic_model_coerces_but_never_holds_non_finite(token):
    assert inv.parse(top('"0.5"'), r8.Lax).confidence == 0.5         # strict=False: pydantic's lax mode
    with pytest.raises(inv.ParseError, match="non-finite"):
        inv.parse(top(token), r8.Lax)


@pytest.mark.parametrize("token", NON_FINITE_TEXT)
def test_model_call_never_returns_ok_holding_non_finite_top(iso, token):
    r8.REPLIES[:] = [top(token), top(token)]                          # format_retries 1
    e = rejected(r8.tmodel, "p")
    assert e is not None and e.outcome.kind == "parse_error"


@pytest.mark.parametrize("where", ["inner", "xs", "m"])
@pytest.mark.parametrize("token", ['"NaN"', '"inf"', '"-Infinity"'])
def test_model_call_never_returns_ok_holding_non_finite_nested(iso, where, token):
    raw = {"inner": nested(inner=token), "xs": nested(xs=f"[{token}]"), "m": nested(m=f'{{"k": {token}}}')}
    r8.REPLIES[:] = [raw[where]]
    e = rejected(r8.nmodel, "p")
    assert e is not None and e.outcome.kind == "parse_error"


def test_model_call_and_edge_agree_on_a_strict_reply(iso):
    r8.REPLIES[:] = [nested()]
    c = r8.nmodel("p")
    assert c.verdict == "ok" and r8.nuse(c).inner.confidence == 0.5      # the model instance


# --- 2. minor: non-finite arithmetic is a rule_error on every backend --------------------------------
@pytest.mark.parametrize("bad,x,y", [
    ("0.0 / 0.0 < 0.0 || 0.0 / 0.0 > 1.0", 1.0, 1.0),
    ("args.x / args.y > 1.0", 1.0, 0.0),
    ("args.x / args.y < 0.0", -1.0, 0.0),
    ("args.x * args.y - args.x * args.y > 1.0", 1e200, 1e200),
    ("args.x * args.y > 1.0", 1e200, 1e200),
    ("size([args.x * args.y]) == 1", 1e200, 1e200),
    ("double('NaN') > args.x", 1.0, 1.0),
    ("double('-Infinity') < args.x", 1.0, 1.0),
    ("inv.sum([args.x, args.y]) > 1.0", 1.7e308, 1.7e308),
])
def test_non_finite_double_made_in_a_clause_is_rule_error(backend, bad, x, y):
    with inv.isolated():
        inv.load(r8.one_rule(bad), backend=backend)
        e = rejected(r8.arith, x, y)
    assert e is not None and e.outcome.kind == "rule_error", (backend, bad, e)


def test_reply_made_nan_does_not_pass_a_range_check(iso):
    r8.REPLIES[:] = ['{"a": 0.0, "b": 0.0}']
    e = rejected(r8.rmodel, "p")
    assert e is not None and e.outcome.kind == "rule_error"


def test_finite_arithmetic_is_unchanged(backend):
    prog = get_backend(backend).compile("1.5 * 2.0 == 3.0 && 7 / 2 == 3 && 'a' + 'b' == 'ab' && [1] + [2] == [1, 2] "
                                        "&& -(2.0 - 3.0) == 1.0 && double(2) / 4.0 == 0.5 && double('1e3') == 1000.0")
    assert prog.evaluate({}) is True


@pytest.mark.parametrize("bad,kind", [("args.x / args.y > 1.0", "violation"), ("-args.x / args.y < 0.0", "violation"),
                                      ("args.y / args.y > 1.0 || args.y / args.y < 1.0", "ok")])
def test_allow_non_finite_division_is_ieee_on_every_backend(backend, bad, kind):
    with inv.isolated() as reg:
        inv.load(r8.one_rule(bad), backend=backend)
        inv.configure(reg, allow_non_finite=True)
        e = rejected(r8.arith, 1.0, 0.0)
    assert (e.outcome.kind if e is not None else "ok") == kind, (backend, bad, e)


def test_allow_non_finite_restores_ieee_division(backend):
    with inv.isolated() as reg:
        inv.load(r8.one_rule("args.x / args.y > 1.0"), backend=backend)
        inv.configure(reg, allow_non_finite=True)
        e = rejected(r8.arith, 1.0, 0.0)
    assert e is not None and e.outcome.kind == "violation"


# --- 3. minor: call arity and global macro forms are checked at load --------------------------------
@pytest.mark.parametrize("bad", [
    "size(args.x, 1) > 0", "args.x.size(1) > 0", "size() > 0", "'a'.startsWith()", "matches('a')",
    "'a'.matches('a', 'b')", "[1].exists(c)", "has(args.x, 1)", "double() > 0.0", "string() == ''",
    "dyn() == 1", "'a'.contains('a', 'b')", "contains('a')", "args.x.has(1)",
])
def test_wrong_arity_is_load_error(backend, bad):
    with pytest.raises(inv.LoadError, match="takes|method|macro|has()|needs"):
        inv.load(r8.one_rule(bad), install=False, backend=backend)


@pytest.mark.parametrize("bad", ["exists([1], c, c < 0)", "all([1], c, c > 0)", "map([1], c, c) == [1]"])
def test_global_macro_form_is_load_error(backend, bad):
    with pytest.raises(inv.LoadError, match="macro"):
        inv.load(r8.one_rule(bad), install=False, backend=backend)


def test_subset_parser_refuses_global_macro_with_syntax_error():
    with pytest.raises(cel.CelSyntaxError, match="macro"):
        cel.subset.parse("exists(xs, c, c < 0)")


def test_subset_evaluator_never_asserts_on_a_hand_built_global_macro():
    tree = ("call", "exists", None, [("id", "c"), ("lit", True)])
    with pytest.raises(cel.CelError):
        cel.subset.evaluate(tree, {})


def test_global_forms_cel_allows_still_load(backend):
    for bad in ("contains('ab', 'a')", "startsWith('ab', 'a')", "matches('ab', 'a')", "size('ab') == 2"):
        inv.load(r8.one_rule(bad), install=False, backend=backend)


def test_library_has_no_assert_statements():
    """python -O strips assert: the library must never rely on one."""
    found = []
    for p in (ROOT / "src" / "invariants").rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Assert):
                found.append(f"{p.name}:{node.lineno}")
    assert found == []


def test_arity_and_global_macro_checks_hold_under_python_O():
    code = ("import invariants as inv, sys; sys.path.insert(0, %r); import round8 as r8\n"
            "for bad in ('exists([1], c, c < 0)', 'size(args.x, 1) > 0'):\n"
            "    try:\n"
            "        inv.load(r8.one_rule(bad), install=False, backend='subset')\n"
            "    except inv.LoadError:\n"
            "        continue\n"
            "    raise SystemExit('loaded: ' + bad)\n"
            "from invariants import cel\n"
            "try:\n"
            "    cel.subset.evaluate(('call', 'exists', None, [('id', 'c'), ('lit', True)]), {})\n"
            "except cel.CelError:\n"
            "    print('ok')\n") % str(ROOT / "tests" / "fixtures")
    out = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


# --- 4. minor: overlapping registry-scope ablations are counted -------------------------------------
def test_overlapping_registry_ablations_are_reference_counted(iso):
    a_in, b_in, a_out, b_check, b_out = (threading.Event() for _ in range(5))
    seen: dict[str, bool] = {}

    def a() -> None:
        with inv.using(iso), inv.ablated("G-A", scope="registry"):
            a_in.set()
            b_in.wait(5)
        a_out.set()

    def b() -> None:
        a_in.wait(5)
        with inv.using(iso), inv.ablated("G-A", scope="registry"):
            b_in.set()
            a_out.wait(5)
            seen["inside_after_a_exit"] = rejected(r8.arith, 5.0, 1.0) is None
        b_out.set()

    ts = [threading.Thread(target=a), threading.Thread(target=b)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert seen == {"inside_after_a_exit": True}
    assert rejected(r8.arith, 5.0, 1.0) is not None           # both blocks closed: enforced again


def test_nested_registry_ablation_ends_with_the_outer_block(iso):
    with inv.ablated("G-A", scope="registry"):
        with inv.ablated("G-A", scope="registry"):
            pass
        assert rejected(r8.arith, 5.0, 1.0) is None
    assert rejected(r8.arith, 5.0, 1.0) is not None


# --- 5. minor: parse_error text in exceptions, events and logs carries no reply values -------------
SECRET = "SECRET-PII-1"


@pytest.mark.parametrize("model,reply", [
    ("tmodel", f'{{"cause": "{SECRET}", "confidence": 0.5}}'),
    ("tmodel", f'{{"cause": "A", "confidence": 0.5, "{SECRET}": 1}}'),
    ("tmodel", f'{{"cause": "A", "confidence": "{SECRET}"}}'),
    ("dmodel", f'{{"cause": "{SECRET}", "confidence": 0.5, "tags": {{}}}}'),
    ("dmodel", f'{{"cause": "A", "confidence": 0.5, "tags": {{}}, "{SECRET}": 1}}'),
    ("dmodel", f'{{"cause": "A", "confidence": 0.5, "tags": {{"{SECRET}": "x"}}}}'),
])
def test_parse_error_text_is_value_free_outside_feedback(iso, caplog, model, reply):
    events: list[Any] = []
    stop = inv.observe(lambda rid, kind, ev: events.append((rid, kind, ev)))
    try:
        r8.REPLIES[:] = [reply, reply]
        with caplog.at_level(logging.DEBUG, logger="invariants"):
            e = rejected(getattr(r8, model), "p")
    finally:
        stop()
    assert e is not None and e.outcome.kind == "parse_error"
    assert SECRET not in str(e) and SECRET not in e.outcome.error
    assert SECRET not in json.dumps(e.attempts, default=str)
    assert SECRET not in repr(events)
    assert SECRET not in caplog.text
    # the model still gets the full text (pydantic's own messages never quote the input)
    assert any(f and (SECRET in f or "valid number" in f) for f in r8.FEEDBACK)


def test_parse_error_feedback_holds_the_full_text():
    with pytest.raises(inv.ParseError) as ei:
        inv.parse(f'{{"cause": "{SECRET}", "confidence": 0.5, "tags": {{}}}}', r8.DictReply)
    assert SECRET in ei.value.feedback and SECRET not in str(ei.value)
    assert "field cause" in str(ei.value)


def test_debug_puts_the_full_parse_error_text_back(iso):
    inv.configure(iso, debug=True)
    r8.REPLIES[:] = [f'{{"cause": "{SECRET}", "confidence": 0.5}}'] * 2
    e = rejected(r8.tmodel, "p")
    assert e is not None and SECRET in e.outcome.error


# --- 6. minor: conversions agree across engines -----------------------------------------------------
@pytest.mark.parametrize("expr", [
    "double('0x10') == 16.0", "int(-9223372036854775808.0) < 0", "int('٣') == 3", "int('1_0') == 10",
    "double('1_0') == 10.0", "int('0x10') == 16", "bool(1)", "double('٣') == 3.0",
])
def test_lenient_conversions_are_errors_on_every_backend(backend, expr):
    with pytest.raises(cel.CelError):
        get_backend(backend).compile(expr).evaluate({})


@pytest.mark.parametrize("expr", ["string(null) == 'None'", "int(null) == 0", "double(null) == 0.0"])
def test_conversion_of_null_is_an_error_on_every_backend(backend, expr):
    with pytest.raises(cel.CelError):
        get_backend(backend).compile(expr).evaluate({})


def test_string_of_bool_agrees(backend):
    assert get_backend(backend).compile("string(true) == 'true' && string(false) == 'false'").evaluate({}) is True


# --- 7. minor: CHANGELOG has the Unreleased section CONTRIBUTING points to --------------------------
def test_changelog_has_unreleased_section_named_by_contributing():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    contributing = (ROOT / "CONTRIBUTING.md").read_text()
    assert "\n## [Unreleased]\n" in changelog
    assert '"Unreleased"' in contributing or "[Unreleased]" in contributing


# --- found during this round: computed durations and timestamps compare equal on the engine --------
@pytest.mark.parametrize("expr", [
    "duration('2s') - duration('1s') == duration('1s')",
    "timestamp('2024-01-01T00:00:01Z') - duration('1s') == timestamp('2024-01-01T00:00:00Z')",
    "timestamp('2024-01-01T00:00:01Z') - timestamp('2024-01-01T00:00:00Z') == duration('1s')",
    "duration('1s') in [duration('2s') - duration('1s')]",
    "timestamp('2024-01-01T00:00:00Z') + duration('1s') > timestamp('2024-01-01T00:00:00Z')",
])
def test_engine_time_arithmetic_compares_equal(expr):
    for b in [b for b in BACKENDS if b != "subset"]:
        assert get_backend(b).compile(expr).evaluate({}) is True, b
