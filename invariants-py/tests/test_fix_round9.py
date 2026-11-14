"""Regression tests for fix round 9 (certification round 7): each test failed before its fix.

1. datetime, timedelta, UUID values (and None in Optional fields) work in rules on every backend;
   Decimal, date, time, naive datetimes and sets are refused with a clear message, at load when
   the annotation shows them; the type checker knows timestamps and durations.
2. cel-expr: long arithmetic chains and deep nesting load (the non-finite check no longer nests
   per operation); past cel-cpp's limit, a LoadError names it.
3. engine computed values keep their CEL types (type(), string(), the duration getters).

The tests run on every installed CEL backend (parametrised), whatever INV_CEL_BACKEND says.
"""
from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Any

import pytest

import invariants as inv
import round9 as r9
from invariants_cel import available_backends, get_backend
from invariants_cel.errors import CelError

BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]
UTC = dt.timezone.utc
NOW = dt.datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
PLUS5 = dt.timezone(dt.timedelta(hours=5))


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def outcome(backend: str, fn: str, bad: str, *args: Any) -> tuple[str, str | None]:
    """(kind, error) of one guarded call of round9.<fn>(*args) under a one-clause rule."""
    with inv.isolated():
        inv.load(r9.doc(fn, bad), backend=backend)
        try:
            getattr(r9, fn)(*args)
        except inv.InvariantError as e:
            return e.outcome.kind, e.outcome.error
    return "ok", None


def load_error(backend: str, fn: str, bad: str) -> str:
    with pytest.raises(inv.LoadError) as ei:
        inv.load(r9.doc(fn, bad), install=False, backend=backend)
    return str(ei.value)


# --- 1. MAJOR: Python datetime / timedelta / UUID values in rules ----------------------------------
SHIP = [  # (clause, deadline, wait, expected)
    ("args.deadline < timestamp('2024-01-01T00:00:00Z') || args.wait > duration('1h')", NOW, dt.timedelta(minutes=5), "ok"),
    ("args.deadline < timestamp('2024-07-01T00:00:00Z')", NOW, dt.timedelta(0), "violation"),
    ("args.deadline == timestamp('2024-06-01T12:00:00Z')", NOW.astimezone(PLUS5), dt.timedelta(0), "violation"),
    ("args.wait > duration('1h')", NOW, dt.timedelta(hours=2), "violation"),
    ("args.wait >= duration('90s') && args.wait <= duration('1m30s')", NOW, dt.timedelta(seconds=90), "violation"),
    ("args.deadline + args.wait > timestamp('2024-06-01T12:30:00Z')", NOW, dt.timedelta(hours=1), "violation"),
    ("args.deadline - args.wait == timestamp('2024-06-01T11:00:00Z')", NOW, dt.timedelta(hours=1), "violation"),
    ("args.deadline - timestamp('2024-06-01T00:00:00Z') == duration('12h')", NOW, dt.timedelta(0), "violation"),
    ("args.wait + args.wait == duration('3m')", NOW, dt.timedelta(seconds=90), "violation"),
    ("string(args.wait) == '90s' && string(args.deadline) == '2024-06-01T12:00:00Z'", NOW, dt.timedelta(seconds=90), "violation"),
    ("args.deadline.getHours() == 12 && args.deadline.getHours('+05:30') == 17", NOW, dt.timedelta(0), "violation"),
    ("args.deadline.getDayOfWeek() == 6 && args.deadline.getMonth() == 5 && args.deadline.getDate() == 1",
     NOW, dt.timedelta(0), "violation"),
    ("args.wait.getMinutes() == 90 && args.wait.getSeconds() == 5400", NOW, dt.timedelta(minutes=90), "violation"),
    ("int(args.deadline) == 1717243200", NOW, dt.timedelta(0), "violation"),
]


@pytest.mark.parametrize("bad,deadline,wait,want", SHIP)
def test_datetime_and_timedelta_arguments_work(backend, bad, deadline, wait, want):
    kind, err = outcome(backend, "ship", bad, deadline, wait)
    assert kind == want, err


@pytest.mark.parametrize("bad,want", [
    ("args.order.placed > args.order.placed", "ok"),
    ("args.order.placed == null", "ok"),
    ("has(args.order.placed) && args.order.total < 0.0", "ok"),
    ("has(args.order.placed) && args.order.closes == null", "violation"),
    ("args.order.placed < timestamp('2025-01-01T00:00:00Z')", "violation"),
])
def test_datetime_field_of_a_dataclass(backend, bad, want):
    kind, err = outcome(backend, "place", bad, r9.Order(5.0, NOW))
    assert kind == want, err


@pytest.mark.parametrize("bad,at,lease,want", [
    ("args.at == null", None, None, "violation"),
    ("args.at != null && args.at < timestamp('2025-01-01T00:00:00Z')", None, None, "ok"),
    ("args.at != null && args.at < timestamp('2025-01-01T00:00:00Z')", NOW, None, "violation"),
    ("args.lease == null || args.lease > duration('1h')", NOW, dt.timedelta(minutes=5), "ok"),
    ("args.lease == null || args.lease > duration('1h')", NOW, None, "violation"),
])
def test_none_in_optional_time_fields(backend, bad, at, lease, want):
    kind, err = outcome(backend, "when", bad, at, lease)
    assert kind == want, err


def test_none_in_optional_field_has_and_null(backend):
    o = r9.Order(1.0, NOW, None)
    assert outcome(backend, "place", "has(args.order.closes) && args.order.closes == null", o)[0] == "violation"
    assert outcome(backend, "place", "args.order.closes != null && args.order.closes < args.order.placed", o)[0] == "ok"


U = uuid.UUID("12345678-1234-5678-1234-567812345678")


@pytest.mark.parametrize("bad,want", [
    ("args.u == '12345678-1234-5678-1234-567812345678'", "violation"),
    ("args.u in args.us", "violation"),
    ("args.us[0] == args.u && size(args.u) == 36", "violation"),
    ("args.u.startsWith('0000')", "ok"),
])
def test_uuid_is_a_string(backend, bad, want):
    kind, err = outcome(backend, "ident", bad, U, [U])
    assert kind == want, err


def test_uuid_error_is_not_misleading(backend):
    kind, err = outcome(backend, "untyped", "args.x > 0", U)       # was 'integer value out of int64 range'
    assert kind == "rule_error" and "out of int64 range" not in (err or "")


@pytest.mark.parametrize("value,needle", [
    (decimal.Decimal("1.50"), "Decimal is not a CEL type"),
    (dt.date(2024, 1, 1), "datetime.date is not a CEL type"),
    (dt.time(9, 30), "datetime.time is not a CEL type"),
    (dt.datetime(2024, 1, 1), "naive datetime"),
    ({1, 2}, "set is not a CEL type"),
])
@pytest.mark.parametrize("bad", ["args.x == args.x", "args.x == null", "has(args.x) && args.x != null"])
def test_refused_values_are_a_clear_rule_error_at_run_time(backend, value, needle, bad):
    kind, err = outcome(backend, "untyped", bad, value)
    assert kind == "rule_error" and needle in (err or ""), err


@pytest.mark.parametrize("fn,bad,needle", [
    ("money", "args.v > 0.0", "Decimal is not a CEL type"),
    ("money", "args.v == null", "int minor units"),
    ("day", "args.d == null", "datetime.date is not a CEL type"),
    ("naive", "args.t == null", "naive datetime"),
    # fix round 10: size(args.items) and args.items.exists(i, i.qty > 10) read no price, so they load
    # (tests/test_fix_round10.py); reading an Item whole or its price is still refused
    ("cart", "args.items == []", "args.items[].price"),
    ("cart", "args.items.exists(i, i.price == null)", "Decimal"),
])
def test_refused_annotations_are_a_load_error(backend, fn, bad, needle):
    assert needle in load_error(backend, fn, bad)


def test_aware_datetime_annotation_loads(backend):
    assert outcome(backend, "aware", "args.t > timestamp('2024-01-01T00:00:00Z')", NOW)[0] == "violation"


@pytest.mark.parametrize("bad", [
    "args.deadline < 5", "args.deadline == 'x'", "args.deadline < args.wait", "args.wait * 2 > args.wait",
    "-args.wait < args.wait", "args.deadline + args.deadline > args.deadline", "args.deadline.year == 2024",
    "args.wait.getFullYear() == 1", "size(args.deadline) > 0", "args.deadline > 0.0",
    "args.deadline < timestamp('2024-01-01')", "args.wait > duration('1d')",
    "args.deadline.getHours('Mars/Base') == 1",
])
def test_ill_typed_time_clauses_are_load_errors(backend, bad):
    assert "ill-typed" in load_error(backend, "ship", bad)


def test_uuid_annotation_is_a_string_to_the_type_checker(backend):
    assert "ill-typed" in load_error(backend, "ident", "args.u > 5")


def test_naive_datetime_message_never_assumes_a_timezone(backend):
    kind, err = outcome(backend, "ship", "args.deadline < timestamp('2024-01-01T00:00:00Z')",
                        dt.datetime(2023, 1, 1), dt.timedelta(0))
    assert kind == "rule_error" and "never assumes a timezone" in (err or "")


def test_timestamp_text_from_data_uses_the_shared_parser(backend):
    for text, want in [("2024-06-01T12:00:00Z", "violation"), ("2024-06-01T17:00:00+05:00", "violation"),
                       ("2024-06-01", "rule_error"), ("2024-06-01T12:00:60Z", "rule_error"),
                       ("2024-06-01T12:00:00.123456789Z", "rule_error")]:
        assert outcome(backend, "untyped", "timestamp(args.x) == timestamp('2024-06-01T12:00:00Z')", text)[0] == want, text
    for text, want in [("1h30m", "violation"), ("5400s", "violation"), ("1d", "rule_error"), ("1", "rule_error")]:
        assert outcome(backend, "untyped", "duration(args.x) == duration('90m')", text)[0] == want, text


def test_cel_expr_needs_protobuf_and_says_so(monkeypatch):
    if "cel-expr" not in BACKENDS:
        pytest.skip("cel-expr not installed")
    from invariants_cel import expr_python

    def missing() -> Any:
        raise ImportError("no protobuf")
    monkeypatch.setattr(expr_python, "_pb_types", missing)
    msg = load_error("cel-expr", "ship", "args.wait > duration('1h')")
    assert "protobuf" in msg
    kind, err = outcome("cel-expr", "untyped", "args.x > duration('1h')", dt.timedelta(hours=2))
    assert kind == "rule_error" and "protobuf" in (err or "")


# --- 2. minor: cel-expr nesting after the non-finite rewrite ---------------------------------------
def sum_of(k: int, term: str = "args.a") -> str:
    return " + ".join([term] * k)


@pytest.mark.parametrize("k", [16, 50, 200])
def test_long_sums_load_and_agree(backend, k):
    assert outcome(backend, "sums", f"{sum_of(k)} > {k - 1}.0", 1.0, 0.0)[0] == "violation"
    assert outcome(backend, "sums", f"{sum_of(k)} > {k + 1}.0", 1.0, 0.0)[0] == "ok"
    kind, err = outcome(backend, "sums", f"{sum_of(k)} > 0.0", 1e308, 0.0)
    assert kind == "rule_error" and "non-finite" in (err or "")


def test_long_mixed_chain_keeps_left_to_right_order(backend):
    # 1e16 + 1.0 + ... (1.0 is lost each time, left to right); a re-association would keep them
    bad = "args.a" + " + args.b" * 40 + " - args.a == 0.0"
    assert outcome(backend, "sums", bad, 1e16, 1.0)[0] == "violation"


def test_fifteen_nested_operations(backend):
    bad = "(" * 15 + "args.a" + " * 1.0)" * 15 + " > 0.5"
    assert outcome(backend, "sums", bad, 1.0, 0.0)[0] == "violation"
    bad = "(" * 15 + "args.a" + " * args.b)" * 15 + " > 0.5"
    kind, err = outcome(backend, "sums", bad, 1e300, 1e300)
    assert kind == "rule_error" and "non-finite" in (err or "")


@pytest.mark.parametrize("bad,a,b", [
    ("1.0 / (args.a / args.b) > 0.0", 1.0, 0.0),            # x / Infinity == 0: the divisor is checked
    ("1.0 / (args.a * args.a) == 0.0", 1e200, 0.0),
    ("(args.a * args.a) - (args.a * args.a) == 0.0", 1e200, 0.0),
    ("-(args.a * args.a) < 0.0", 1e200, 0.0),
    ("int(args.a * args.a) > 0", 1e200, 0.0),
])
def test_non_finite_still_caught_with_one_check_per_tree(backend, bad, a, b):
    kind, err = outcome(backend, "sums", bad, a, b)    # (the subset refuses x / 0.0 itself)
    assert kind == "rule_error" and ("non-finite" in (err or "") or "division by zero" in (err or "")), err


@pytest.mark.parametrize("op,want", [("||", "violation"), ("&&", "ok")])
def test_long_logic_chain_with_arithmetic_loads(backend, op, want):
    # the rewrite printed (a) || (b) nested, which cel-cpp cannot balance: 33 clauses failed to load
    bad = f" {op} ".join([f"args.a * {i}.0 > 58.5" for i in range(60)])
    assert outcome(backend, "sums", bad, 1.0, 0.0)[0] == want


def test_chunk_variable_never_captures_a_rule_identifier(backend):
    bad = "[1.0].exists(__inv_s0, " + sum_of(12, "__inv_s0") + " == 12.0)"
    assert outcome(backend, "sums", bad, 1.0, 0.0)[0] == "violation"


def test_too_deep_for_cel_expr_is_a_clear_load_error():
    if "cel-expr" not in BACKENDS:
        pytest.skip("cel-expr not installed")
    msg = load_error("cel-expr", "sums", "(" * 40 + "args.a" + " * 1.0)" * 40 + " > 0.0")
    assert "T-SUM" in msg and "32 levels" in msg and "__inv" not in msg


def test_cel_expr_text_stays_small():
    if "cel-expr" not in BACKENDS:
        pytest.skip("cel-expr not installed")
    from invariants_cel._engine import FrontEnd
    be = get_backend("cel-expr")
    src = sum_of(50) + " > 1.0"
    text = FrontEnd(src).text_for(be._NATIVE, be._RENAME, be._FIN)  # type: ignore[attr-defined]
    assert text.count("inv.__fin") == 1 and len(text) < 3 * len(src)


# --- 3. minor: engine computed values --------------------------------------------------------------
ENGINE_ONLY = [
    ("type(args.a * 2.0) == double", True), ("type(args.a + args.a) == double", True),
    ("type(args.a - 1.0) == double", True), ("type(2 * 3) == int", True),
]
AGREED = [
    ("string(duration('2s') - duration('1s'))", "1s"),
    ("string(duration('-1.5s'))", "-1.500s"),
    ("string(timestamp('2024-01-01T00:00:00+05:00') + duration('1.5s'))", "2023-12-31T19:00:01.500Z"),
    ("(duration('2s') - duration('1s')).getSeconds()", 1),
    ("(duration('1s') + duration('500ms')).getMilliseconds()", 500),
    ("(timestamp('2024-01-01T00:00:00Z') - timestamp('2023-12-31T23:00:00Z')).getMinutes()", 60),
    ("int(timestamp('1969-12-31T23:59:59.5Z'))", -1),
]


@pytest.mark.parametrize("expr,want", ENGINE_ONLY)
def test_engine_computed_types(expr, want):
    for b in [x for x in BACKENDS if x != "subset"]:
        assert get_backend(b).compile(expr).evaluate({"args": {"a": 1.5}}) == want, b


@pytest.mark.parametrize("expr,want", AGREED)
def test_computed_durations_and_timestamps_agree(backend, expr, want):
    got = get_backend(backend).compile(expr).evaluate({})
    assert got == want and type(got) is type(want)


@pytest.mark.parametrize("expr", ["-duration('1s') < duration('0s')", "duration('1s') * 2 > duration('1s')",
                                  "duration('1s') / 2 > duration('0s')"])
def test_duration_has_no_negation_or_scaling(backend, expr):
    with pytest.raises(CelError):
        get_backend(backend).compile(expr).evaluate({})
