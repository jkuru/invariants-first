"""Regression tests for fix round 10 (certification round 8): each test failed before its fix.

1. MAJOR: a pydantic reply's datetime, timedelta, UUID, Enum and nested fields reach rules as CEL
   values (the validated instance, through cel/values.py), not their JSON strings; reply clauses
   are type-checked against the model's annotations (NaiveDatetime, Decimal, date: LoadError).
   Dataclass and {field: type} replies take timestamps, durations and UUIDs from their JSON text.
   Through model_call, the edge re-check, deepcopy and pickle.
2. A timezone argument: a constant offset of 24 h or more is a LoadError (not a raw ValueError);
   a timezone from data is checked the same way on every backend (cel-expr took '' and '+99:00').
3. CEL has no int-double arithmetic and no double %: a LoadError that says how to convert; the
   engines agree at run time (the subset computed double % double).
4. A list read only through a comprehension's element fields (or size()) converts just those fields:
   an Item with a Decimal price loads when the rule reads only qty.

Every test runs on every installed CEL backend (parametrised), whatever INV_CEL_BACKEND says.
"""
from __future__ import annotations

import copy
import datetime as dt
import decimal
import pickle
from typing import Any

import pytest

import invariants as inv
import round10 as r10
from invariants.cel import available_backends, get_backend
from invariants.cel.errors import CelError
from invariants.integrations import pydantic as inv_pd

BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]
UTC = dt.timezone.utc
NOW = dt.datetime(2024, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def model_outcome(backend: str, rule: str, bad: str | list[str], raw: str) -> tuple[str, Any]:
    """(kind or 'ok', the Checked or the error) of one model call under rule."""
    ask = {"R-P": r10.ask_p, "R-D": r10.ask_d, "R-N": r10.ask_n}[rule]
    with inv.isolated():
        inv.load(r10.model_doc(rule, bad), backend=backend)
        r10.REPLY["v"] = raw
        try:
            return "ok", ask()
        except inv.InvariantError as e:
            return e.outcome.kind, e.outcome.error


def model_load_error(backend: str, rule: str, bad: str) -> str:
    with pytest.raises(inv.LoadError) as ei:
        inv.load(r10.model_doc(rule, bad), install=False, backend=backend)
    return str(ei.value)


def outcome(backend: str, fn: str, bad: str | list[str], *args: Any) -> tuple[str, str | None]:
    with inv.isolated():
        inv.load(r10.doc(fn, bad), backend=backend)
        try:
            getattr(r10, fn)(*args)
        except inv.InvariantError as e:
            return e.outcome.kind, e.outcome.error
    return "ok", None


def load_error(backend: str, fn: str, bad: str) -> str:
    with pytest.raises(inv.LoadError) as ei:
        inv.load(r10.doc(fn, bad), install=False, backend=backend)
    return str(ei.value)


# --- 1. MAJOR: typed model replies --------------------------------------------------------------
REPLY_CLAUSES = [  # (clause, violation for the default reply?)
    ("reply.at < timestamp('2024-07-01T00:00:00Z')", True),
    ("reply.at == timestamp('2024-06-01T12:00:00Z')", True),
    ("reply.wait > duration('1h')", False),
    ("reply.wait == duration('5m')", True),
    ("reply.at + reply.wait == timestamp('2024-06-01T12:05:00Z')", True),
    ("reply.uid == '00000000-0000-0000-0000-000000000007'", True),
    ("reply.maybe == null", True),
    ("reply.maybe != null && reply.maybe < reply.at", False),
    ("reply.leg.at == reply.at && reply.leg.wait == duration('300s')", True),
    ("reply.legs.exists(l, l.wait > duration('30m'))", True),
    ("reply.legs.all(l, l.at >= reply.at)", True),
    ("string(reply.at) == '2024-06-01T12:00:00Z' && string(reply.wait) == '300s'", True),
    ("reply.at.getHours() == 12", True),
]


@pytest.mark.parametrize("bad,fires", REPLY_CLAUSES)
def test_pydantic_reply_fields_are_cel_values(backend, bad, fires):
    kind, got = model_outcome(backend, "R-P", bad, r10.p_reply())
    assert kind == ("violation" if fires else "ok"), got


@pytest.mark.parametrize("bad,fires", REPLY_CLAUSES)
def test_dataclass_reply_fields_are_cel_values(backend, bad, fires):
    kind, got = model_outcome(backend, "R-D", bad, r10.d_reply())
    assert kind == ("violation" if fires else "ok"), got


@pytest.mark.parametrize("bad,fires", [
    ("reply.col == 'r'", True), ("reply.col == 'b'", False),
    ("reply.waits['x'] == duration('30s')", True),
    ("size(reply.legs) == 2", True),
])
def test_pydantic_enum_and_dict_fields(backend, bad, fires):
    kind, got = model_outcome(backend, "R-P", bad, r10.p_reply())
    assert kind == ("violation" if fires else "ok"), got


def test_pydantic_optional_datetime_with_a_value(backend):
    raw = r10.p_reply(maybe="2024-05-01T00:00:00+02:00")
    kind, _ = model_outcome(backend, "R-P", "reply.maybe != null && reply.maybe < reply.at", raw)
    assert kind == "violation"


@pytest.mark.parametrize("rule,bad", [
    ("R-P", "reply.at == 'x'"), ("R-P", "reply.wait > 5"), ("R-P", "reply.at < reply.wait"),
    ("R-P", "reply.leg.at > duration('1s')"), ("R-P", "reply.uid > 5"), ("R-P", "reply.nope == 1"),
    ("R-D", "reply.at == 'x'"), ("R-D", "reply.wait > 5"), ("R-D", "reply.leg.wait < reply.at"),
])
def test_reply_clauses_are_type_checked(backend, rule, bad):
    assert "ill-typed" in model_load_error(backend, rule, bad)


@pytest.mark.parametrize("bad,needle", [
    ("reply.at < timestamp('2024-01-01T00:00:00Z')", "naive datetime"),
    ("reply.price > 0", "Decimal is not a CEL type"),
    ("reply.day == null", "datetime.date is not a CEL type"),
])
def test_refused_reply_field_types_are_a_load_error(backend, bad, needle):
    assert needle in model_load_error(backend, "R-N", bad)


def test_unread_refused_reply_fields_do_not_block_loading(backend):
    raw = '{"cause": "A", "at": "2024-01-01T00:00:00", "price": "1.10", "day": "2024-01-31", "note": "x"}'
    kind, c = model_outcome(backend, "R-N", "reply.note == 'y'", raw)
    assert kind == "ok" and c.price == "1.10"          # attributes keep their JSON form
    assert inv_pd.model_of(c).price == decimal.Decimal("1.10")


def test_pydantic_reply_attributes_keep_the_json_form_and_model_of_is_typed(backend):
    kind, c = model_outcome(backend, "R-P", "reply.wait > duration('1h')", r10.p_reply())
    assert kind == "ok" and c.at == "2024-06-01T12:00:00Z" and c.col == "r"
    m = inv_pd.model_of(c)
    assert m.at == NOW and m.wait == dt.timedelta(minutes=5) and m.col is r10.Color.RED


def _checked(backend: str, rule: str, raw: str, bad: str) -> Any:
    with inv.isolated():
        inv.load(r10.model_doc(rule, bad), backend=backend)
        r10.REPLY["v"] = raw
        return {"R-P": r10.ask_p, "R-D": r10.ask_d}[rule]()


@pytest.mark.parametrize("rule,raw", [("R-P", r10.p_reply()), ("R-D", r10.d_reply())])
@pytest.mark.parametrize("copier", [lambda c: c, copy.deepcopy, lambda c: pickle.loads(pickle.dumps(c))])
def test_edge_recheck_reads_typed_values_on_copies(backend, rule, raw, copier):
    bad = "reply.at > timestamp('2024-06-01T12:00:00Z') || reply.legs.exists(l, l.wait > duration('2h'))"
    with inv.isolated():
        inv.load(r10.model_doc(rule, bad), backend=backend)
        r10.REPLY["v"] = raw
        c = {"R-P": r10.ask_p, "R-D": r10.ask_d}[rule]()
        assert c.verdict == "ok"
        use = {"R-P": r10.use_p, "R-D": r10.use_d}[rule]
        assert use(copier(c)) == "used"


@pytest.mark.parametrize("rule,raw", [("R-P", r10.p_reply()), ("R-D", r10.d_reply())])
def test_edge_catches_a_tampered_time_field(backend, rule, raw):
    bad = "reply.at > timestamp('2024-06-01T12:00:00Z')"
    with inv.isolated():
        inv.load(r10.model_doc(rule, bad), backend=backend)
        r10.REPLY["v"] = raw
        c = {"R-P": r10.ask_p, "R-D": r10.ask_d}[rule]()
        value = object.__getattribute__(c, "value")
        later = "2030-01-01T00:00:00Z" if rule == "R-P" else dt.datetime(2030, 1, 1, tzinfo=UTC)
        object.__setattr__(value, "at", later)          # tampered after the model call
        with pytest.raises(inv.Rejected) as ei:
            {"R-P": r10.use_p, "R-D": r10.use_d}[rule](c)
        assert ei.value.outcome.kind == "violation"


@pytest.mark.parametrize("over,needle", [
    ({"at": "2024-06-01T12:00:00"}, "timestamp with a timezone"), ({"at": 5}, "number is not"),
    ({"wait": "5 minutes"}, "is not a duration"), ({"wait": "P1Y"}, "is not a duration"),
    ({"uid": "not-a-uuid"}, "is not a UUID"),
])
def test_dataclass_reply_bad_time_text_is_a_value_free_parse_error(backend, over, needle):
    kind, err = model_outcome(backend, "R-D", "false", r10.d_reply(**over))
    assert kind == "parse_error" and needle in err
    assert "not-a-uuid" not in err and "5 minutes" not in err


def test_dataclass_reply_accepts_cel_and_iso_durations():
    for text, want in [("90s", 90), ("1h30m", 5400), ("PT1H30M", 5400), ("P1DT1S", 86401), ("-PT0.5S", -0.5),
                       ("PT0S", 0)]:
        r = inv.parse(r10.d_reply(wait=text), r10.DReply)
        assert r.wait == dt.timedelta(seconds=want), text


# --- 2. timezone arguments ------------------------------------------------------------------------
@pytest.mark.parametrize("tz", ["+24:00", "+25:00", "+99:00", "-24:00", "+05:60", ""])
def test_constant_bad_timezone_is_a_load_error(backend, tz):
    assert "unknown timezone" in load_error(backend, "zoned", f"args.t.getHours('{tz}') == 1")


@pytest.mark.parametrize("tz,want", [
    ("+99:00", "rule_error"), ("+24:00", "rule_error"), ("", "rule_error"), ("Bogus/Zone", "rule_error"),
    ("+05:60", "rule_error"), ("+05:30", "violation"), ("America/New_York", "ok"), ("UTC", "ok"),
])
def test_timezone_from_data_is_checked_on_every_backend(backend, tz, want):
    kind, err = outcome(backend, "zoned", "args.t.getHours(args.tz) == 17", NOW, tz)
    assert kind == want, err
    if want == "rule_error":
        assert "unknown timezone" in (err or "")


def test_values_zone_raises_cel_error_only():
    from invariants.cel import values
    for tz in ("+24:00", "+99:59", "-30:00"):
        with pytest.raises(CelError, match="unknown timezone"):
            values.zone(tz)


# --- 3. numeric arithmetic ------------------------------------------------------------------------
@pytest.mark.parametrize("bad,hint", [
    ("args.price * args.qty > 100.0", "double(args.qty)"),
    ("args.qty * args.price > 100.0", "double(args.qty)"),
    ("args.price + 1 > 2.0", "double(1)"),
    ("args.n + 1.5 > 2.0", "double(args.n)"),
    ("args.x % 2.0 > 0.0", "int and uint only"),
    ("args.n - args.x < 0.0", "double(args.n)"),
    ("-args.price * args.qty < 0.0", "double(args.qty)"),
])
def test_mixed_int_double_arithmetic_is_a_load_error(backend, bad, hint):
    msg = load_error(backend, "mixed", bad)
    assert "ill-typed" in msg and "no overload" in msg and hint in msg


@pytest.mark.parametrize("bad,want", [
    ("args.price * double(args.qty) > 100.0", "violation"),
    ("int(args.price) * args.qty == 100", "violation"),
    ("args.price > args.qty", "violation"),            # comparison across int and double is CEL
    ("args.n == 3.0", "violation"),
    ("args.n % 2 == 1", "violation"),
    ("args.price / 2.0 > 10.0", "violation"),
])
def test_explicit_conversions_and_cross_type_comparison_load_and_run(backend, bad, want):
    assert outcome(backend, "mixed", bad, 25.5, 4, 3, 1.5)[0] == want


@pytest.mark.parametrize("src", ["a.x * a.i > 0.0", "a.i * a.x > 0.0", "a.x + a.i > 0.0", "a.x - a.i > 0.0",
                                 "a.x / a.i > 0.0", "a.x % 2.0 > 0.0"])
def test_engines_refuse_mixed_arithmetic_and_double_modulo(src):
    env = {"a": {"x": 1.5, "i": 1}}
    for b in BACKENDS:
        with pytest.raises(CelError):
            get_backend(b).compile(src).evaluate(env)


# --- 4. only the element fields a rule reads are converted ---------------------------------------
ITEMS = [r10.Item(3, decimal.Decimal("1.10")), r10.Item(-1, decimal.Decimal("2.00"))]
ORDERS = [r10.Order("o1", ITEMS)]
MAP = {"a": ITEMS[0]}


@pytest.mark.parametrize("bad,want", [
    ("args.items.exists(i, i.qty < 0)", "violation"),
    ("args.items.all(i, i.qty > 0)", "ok"),
    ("args.items.exists_one(i, i.qty == 3)", "violation"),
    ("size(args.items) == 2", "violation"),
    ("args.items.size() > 5", "ok"),
    ("args.items.map(i, i.qty)[0] == 3", "violation"),
    ("inv.unique_by(args.items, i, i.qty)", "violation"),
    ("inv.sum(args.items.map(i, i.qty)) == 2", "violation"),
    ("args.orders.exists(o, o.id == 'o1' && o.items.exists(i, i.qty < 0))", "violation"),
    ("size(args.m) == 1", "violation"),
    ("args.m.exists(k, k == 'a')", "violation"),
    ("args.items.exists(i, true)", "violation"),
])
def test_unread_element_fields_are_never_converted(backend, bad, want):
    kind, err = outcome(backend, "cart", bad, ITEMS, ORDERS, MAP)
    assert kind == want, err


@pytest.mark.parametrize("bad", [
    "args.items.exists(i, i.price == null)", "args.items == []", "args.items[0].qty > 0",
    "args.items.filter(i, i.qty > 0).size() > 0", "args.items.exists(i, i == null)",
    "args.orders.exists(o, o.items.exists(i, i.price == null))", "args.m['a'].qty > 0",
    "args.m.a.price == null", "'a' in args.m",
])
def test_reading_a_refused_field_or_a_whole_element_is_still_refused(backend, bad):
    assert "cannot be read by a rule" in load_error(backend, "cart", bad)


def test_engine_projection_skips_unread_fields():
    from invariants.cel._engine import project
    from invariants.cel.subset import ELEM, SIZE
    data = {"items": ITEMS, "m": MAP}
    assert project(data, frozenset({("items", ELEM, "qty")})) == {"items": [{"qty": 3}, {"qty": -1}]}
    assert project(data, frozenset({("items", SIZE)})) == {"items": [None, None]}
    assert project(data, frozenset({("m", SIZE)})) == {"m": {"a": None}}
    with pytest.raises(CelError, match="Decimal"):
        project(data, frozenset({("items", ELEM, "qty"), ("items",)}))
