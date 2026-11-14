"""Regression tests for fix round 11 (certification round 9): each test failed before its fix.

1. R9-1: a NaN inside a list, map or record compared whole (==, !=, in) is a rule_error on the
   subset too, as on cel-expr (the subset used Python's ==, where a shared NaN object
   equals itself). With allow_non_finite it compares by CEL rules: NaN != NaN, never identity.
2. R9-2: model_of(c) is a fresh instance validated from the reply's JSON attributes on each call,
   and an edge calls its consumer with a new Checked over the copy the rule judged: a received
   value mutated or swapped after the model call never reaches the consumer unvalidated.
3. R9-3: pydantic's BaseModel API (model_fields, model_config, copy, ...) is not a field: a clause
   that reads one is a LoadError that lists only the real fields, and it is absent at run time.
4. R9-4: digits are ASCII only in durations, timestamps, offsets and rule text; a zone name with a
   NUL or a control character is refused on every backend; cel-expr refuses any string holding a
   NUL (its C++ runtime cut 'ab\\x00cd' to 'ab').
5. R9-5: map keys and indexes follow CEL on every backend: {1: 'a'}[1.0] == 'a', a bool is never a
   number key, a list index may be an integral double, a map literal key is int, bool or string.
6. R9-6: the sdist ships .gitignore and its tests run against an installed package.

Every test runs on every installed CEL backend (parametrised), whatever INV_CEL_BACKEND says.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import pickle
from typing import Any

import pytest

import invariants as inv
import round11 as r11
from invariants_cel import available_backends, get_backend
from invariants_cel.errors import CelError
from invariants.integrations import pydantic as inv_pd

BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]
UTC = dt.timezone.utc
NOW = dt.datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
NAN = float("nan")
ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def outcome(backend: str, fn: str, bad: str | list[str], *args: Any, allow_nan: bool = False) -> tuple[str, Any]:
    with inv.isolated():
        reg = inv.load(r11.doc(fn, bad), backend=backend)
        if allow_nan:
            inv.configure(reg, allow_non_finite=True)
        try:
            getattr(r11, fn)(*args)
        except inv.InvariantError as e:
            return e.outcome.kind, e.outcome.error
    return "ok", None


def load_error(backend: str, fn: str, bad: str) -> str:
    with pytest.raises(inv.LoadError) as ei:
        inv.load(r11.doc(fn, bad), install=False, backend=backend)
    return str(ei.value)


def evaluate(backend: str, expr: str, env: dict[str, Any] | None = None) -> Any:
    return get_backend(backend).compile(expr).evaluate(env or {})


# --- 1. R9-1: whole-value equality with a nested NaN --------------------------------------------
def nan_item() -> r11.It:
    return r11.It(1, NAN, [])


WHOLE = [
    "args.items == args.items",
    "args.items != args.items",
    "args.items[0] == args.m.k",
    "args.m.k == args.m.k",
    "args.m == args.m",
    "args.items.exists(i, i == i)",
    "args.items.exists(i, i != i)",
    "args.anyv == args.anyv",
    "args.anyv != args.anyv",
    "[args.anyv] == [args.anyv]",
    "args.items.exists(i, i in args.items)",
    "args.m.k in args.items",
    "args.xs == args.xs",
    "1.0 in args.xs",
    "args.mp == args.mp",
    "'a' in args.mp",
]


@pytest.mark.parametrize("bad", WHOLE)
@pytest.mark.parametrize("plain", [False, True], ids=["records", "dicts"])
def test_nan_inside_a_value_compared_whole_is_a_rule_error(backend, bad, plain):
    it: Any = {"q": 1, "x": NAN, "subs": []} if plain else nan_item()
    sub: Any = {"x": NAN} if plain else r11.Sub(NAN)
    kind, err = outcome(backend, "whole", bad, [it], {"k": it}, "k", sub, [1.0, NAN], {"a": 1.0, "b": NAN})
    assert kind == "rule_error" and "non-finite" in err


def test_nan_nested_two_levels_down_is_seen(backend):
    it = r11.It(1, 1.0, [r11.Sub(NAN)])
    kind, err = outcome(backend, "whole", "args.items == args.items", [it], {}, "k", None, [], {})
    assert kind == "rule_error" and "non-finite" in err


def test_finite_values_compared_whole_are_unchanged(backend):
    a, b = r11.It(1, 2.0, [r11.Sub(3.0)]), r11.It(1, 2.0, [r11.Sub(3.0)])
    assert outcome(backend, "whole", "args.items[0] == args.m.k", [a], {"k": b}, "k", None, [], {})[0] == "violation"
    assert outcome(backend, "whole", "args.items != args.items", [a], {"k": b}, "k", None, [], {})[0] == "ok"
    assert outcome(backend, "whole", "args.m.k in args.items", [a], {"k": b}, "k", None, [], {})[0] == "violation"
    c = r11.It(1, 2.5, [r11.Sub(3.0)])
    assert outcome(backend, "whole", "args.m.k in args.items", [a], {"k": c}, "k", None, [], {})[0] == "ok"


def test_a_record_equals_the_map_of_its_fields(backend):
    """A record compared whole is the map of its fields on every backend (the subset compared classes)."""
    rec, plain = r11.Sub(1.0), {"x": 1.0}
    assert outcome(backend, "whole", "args.anyv == args.m.k", [], {"k": plain}, "k", rec, [], {})[0] == "violation"


@pytest.mark.parametrize("bad, fires", [
    ("args.xs == args.xs", False),          # CEL: NaN != NaN, so a list holding one is not equal to itself
    ("args.xs != args.xs", True),
    ("args.items == args.items", False),
    ("args.mp == args.mp", False),
    ("1.0 in args.xs", True),
])
def test_allowed_nan_compares_by_cel_rules_not_identity(backend, bad, fires):
    kind, _ = outcome(backend, "whole", bad, [nan_item()], {}, "k", None, [1.0, NAN], {"a": NAN}, allow_nan=True)
    assert kind == ("violation" if fires else "ok")


# --- 2. R9-2: model_of() and the edge hand out only validated values ---------------------------
def model_load(backend: str, rule: str, bad: str) -> Any:
    return inv.load(r11.model_doc(rule, bad), backend=backend)


FUTURE = dt.datetime(2031, 1, 1, tzinfo=UTC)
BAD = "reply.legs.exists(l, l.at > timestamp('2030-01-01T00:00:00Z'))"


def test_model_of_is_a_fresh_validated_copy(backend):
    with inv.isolated():
        model_load(backend, "R-M", BAD)
        r11.REPLY["v"] = r11.reply()
        c = r11.ask()
        m1 = inv_pd.model_of(c)
        assert isinstance(m1, r11.Rep) and m1.legs[0].at == NOW
        m1.legs[0].at = FUTURE                         # changing the copy changes nothing else
        m2 = inv_pd.model_of(c)
        assert m2 is not m1 and m2.legs[0].at == NOW
        assert inv_pd.model_of(object.__getattribute__(c, "value")) == m2      # the reply itself too


def test_a_mutated_model_of_instance_never_reaches_the_consumer(backend):
    with inv.isolated():
        model_load(backend, "R-M", BAD)
        r11.REPLY["v"] = r11.reply()
        c = r11.ask()
        inv_pd.model_of(c).legs[0].at = FUTURE           # a copy: the reply is unchanged
        assert r11.use(c) == "used"
        assert r11.SEEN["m"].legs[0].at == NOW
        assert inv_pd.model_of(r11.SEEN["c"]).legs[0].at == NOW


def test_a_mutated_reply_is_judged_as_received_and_the_consumer_gets_a_copy(backend):
    with inv.isolated():
        model_load(backend, "R-M", BAD)
        r11.REPLY["v"] = r11.reply()
        c = r11.ask()
        assert r11.use(c) == "used"
        got = object.__getattribute__(r11.SEEN["c"], "value")
        assert got == c.value and got is not c.value and got.legs[0] is not c.value.legs[0]
        c.value.legs[0].at = FUTURE                     # the received instance, changed: the edge sees it
        with pytest.raises(inv.Rejected):
            r11.use(c)


def test_tampered_json_attributes_are_still_caught_at_the_edge(backend):
    with inv.isolated():
        model_load(backend, "R-M", BAD)
        r11.REPLY["v"] = r11.reply()
        c = r11.ask()
        object.__setattr__(object.__getattribute__(c, "value"), "legs", [{"at": "2031-01-01T00:00:00Z", "score": 1.0}])
        r11.SEEN.clear()
        with pytest.raises(inv.Rejected):
            r11.use(c)
        assert r11.SEEN == {}
        with pytest.raises(inv.ParseError):              # nor does model_of hand out an invalid form
            object.__setattr__(object.__getattribute__(c, "value"), "at", "not a time")
            inv_pd.model_of(c)


def test_the_consumer_gets_an_equal_new_checked(backend):
    with inv.isolated():
        model_load(backend, "R-M", BAD)
        r11.REPLY["v"] = r11.reply()
        c = r11.ask()
        r11.use(c)
        got = r11.SEEN["c"]
        assert got == c and got is not c and type(got) is type(c)
        assert got.verdict == "ok" and got.attempts == c.attempts and got.rule == c.rule
        assert object.__getattribute__(got, "value") is not object.__getattribute__(c, "value")
        assert hash(got) == hash(c)
        assert copy.deepcopy(c) == c and pickle.loads(pickle.dumps(c)) == c


def test_a_dataclass_reply_consumer_gets_the_rechecked_copy(backend):
    with inv.isolated():
        model_load(backend, "R-D", "reply.wait > duration('1h')")
        r11.REPLY["v"] = json.dumps({"cause": "A", "wait": "PT5M"})
        c = r11.ask_d()
        got = r11.use_d(c)
        assert got == c and got is not c and got.wait == c.wait


def test_model_of_refuses_a_reply_of_another_schema():
    with pytest.raises(TypeError, match="closed pydantic schema"):
        inv_pd.model_of(r11.DReply("A", dt.timedelta(1)))


# --- 3. R9-3: pydantic's BaseModel API is not a field ------------------------------------------
API = ["model_fields", "model_config", "model_extra", "model_computed_fields", "copy", "dict", "json",
       "model_dump", "construct", "validate", "schema"]


@pytest.mark.parametrize("name", API)
def test_basemodel_api_is_not_a_reply_field(backend, name):
    with pytest.raises(inv.LoadError) as ei:
        inv.load(r11.model_doc("R-M", f"has(reply.{name})"), install=False, backend=backend)
    assert f"no field '{name}'" in str(ei.value)
    with pytest.raises(inv.LoadError):
        inv.load(r11.model_doc("R-M", f"reply.{name} == 1"), install=False, backend=backend)


def test_no_field_error_lists_only_the_real_fields(backend):
    with pytest.raises(inv.LoadError) as ei:
        inv.load(r11.model_doc("R-M", "reply.nope == 1"), install=False, backend=backend)
    msg = str(ei.value)
    assert "(fields: at, cause, first, legs, note)" in msg
    fields = msg.partition("no field")[2]       # the message names the enforcer (point model_call) too
    assert "model_" not in fields and "copy" not in fields
    with pytest.raises(inv.LoadError) as ei:
        inv.load(r11.model_doc("R-M", "reply.legs[0].nope == 1"), install=False, backend=backend)
    assert "(fields: at, score)" in str(ei.value)


def test_basemodel_api_is_not_a_field_of_a_pydantic_argument(backend):
    assert "no field 'model_fields'" in load_error(backend, "parg", "size(args.r.model_fields) > 0")
    rep = r11.Rep.model_validate(json.loads(r11.reply()))
    assert outcome(backend, "parg", "args.r.cause == 'A' && args.r.first == 'f'", rep)[0] == "violation"


def test_basemodel_api_is_absent_at_run_time(backend):
    """Read past the type checker (an Any-typed path), a BaseModel attribute is no field."""
    rep = r11.Rep.model_validate(json.loads(r11.reply()))
    prog = get_backend(backend).compile("has(r.model_config)")
    assert prog.evaluate({"r": rep}) is False
    with pytest.raises(CelError):
        get_backend(backend).compile("r.model_fields == 1").evaluate({"r": rep})
    assert get_backend(backend).compile("r.cause == 'A' && r.first == 'f'").evaluate({"r": rep}) is True


# --- 4. R9-4: ASCII digits, zone names and NUL ---------------------------------------------------
@pytest.mark.parametrize("text", ["PT٥M", "P١D", "PT1.٥S"])
def test_iso_duration_takes_ascii_digits_only(text):
    with pytest.raises(inv.ParseError):
        inv.parse(json.dumps({"cause": "A", "wait": text}), r11.DReply)
    assert inv.parse(json.dumps({"cause": "A", "wait": "PT5M"}), r11.DReply).wait == dt.timedelta(minutes=5)


@pytest.mark.parametrize("text", ["٥m", "1h٣٠m", "٥.5s"])
def test_cel_duration_text_takes_ascii_digits_only(backend, text):
    assert outcome(backend, "zoned", "duration(args.s) > duration('1s')", NOW, "UTC", text)[0] == "rule_error"


def test_timestamp_text_takes_ascii_digits_only(backend):
    bad = "timestamp(args.s) > timestamp('2000-01-01T00:00:00Z')"
    assert outcome(backend, "zoned", bad, NOW, "UTC", "٢٠٢٤-01-01T00:00:00Z")[0] == "rule_error"
    assert outcome(backend, "zoned", bad, NOW, "UTC", "2024-01-01T00:00:00Z")[0] == "violation"


@pytest.mark.parametrize("zone", ["+٠٥:٣٠", "+05:٣٠", "Asia/Kolkata\x00", "UTC\x00", "UTC\n", "Europe/\tLondon",
                                  "America/New_York\x7f", "Ｕｔｃ"])
def test_bad_zone_from_data_is_a_rule_error(backend, zone):
    assert outcome(backend, "zoned", "args.t.getHours(args.tz) == 99", NOW, zone, "")[0] == "rule_error"


@pytest.mark.parametrize("zone", ["+٠٥:٣٠", "Asia/Kolkata\\x00", "UTC\\n"])
def test_bad_constant_zone_is_a_load_error(backend, zone):
    assert "unknown timezone" in load_error(backend, "zoned", f"args.t.getHours('{zone}') == 99")


@pytest.mark.parametrize("zone, hour", [("+05:30", 17), ("America/New_York", 8), ("Etc/GMT+5", 7),
                                        ("America/Port-au-Prince", 8), ("UTC", 12)])
def test_good_zones_still_work(backend, zone, hour):
    t = dt.datetime(2024, 7, 1, 12, 0, tzinfo=UTC)
    assert evaluate(backend, "t.getHours(z)", {"t": t, "z": zone}) == hour


def test_rule_text_digits_are_ascii(backend):
    with pytest.raises(inv.LoadError):
        inv.load(r11.doc("keyed", "args.d == ٣"), install=False, backend=backend)


@pytest.mark.parametrize("expr, env", [
    ('s == "ab"', {"s": "ab\x00cd"}),
    ("size(s) == 2", {"s": "ab\x00cd"}),
    ('"a" in m', {"m": {"a\x00x": 1}}),
    ("b == c", {"b": b"ab\x00cd", "c": b"ab"}),
    ('xs[0] == "ab"', {"xs": ["ab\x00"]}),
])
def test_a_nul_in_data_never_truncates(backend, expr, env):
    """cel-expr refuses a NUL-holding string (its runtime would cut it); the others read it whole."""
    try:
        got = evaluate(backend, expr, env)
    except CelError as e:
        assert backend == "cel-expr" and "NUL" in str(e)
    else:
        assert backend != "cel-expr" and got is False


def test_nul_free_strings_are_unchanged_on_cel_expr(backend):
    assert evaluate(backend, 's == "ab" && m["k"] == "v"', {"s": "ab", "m": {"k": "v"}}) is True


# --- 5. R9-5: map keys and indexes -----------------------------------------------------------------
@pytest.mark.parametrize("expr, value", [
    ("{1: 'a'}[1.0]", "a"),
    ("m[d]", "a"),
    ("[7, 8][1.0]", 8),
    ("xs[d]", 20),
    ("{true: 'a'}[true]", "a"),
    ("1.0 in {1: 'a'}", True),
    ("true in {1: 'a'}", False),
    ("1 in {true: 'a'}", False),
    ("{1: 'a'} == {true: 'a'}", False),
    ("b in m", False),
])
def test_map_keys_and_indexes_agree(backend, expr, value):
    got = evaluate(backend, expr, {"m": {1: "a"}, "d": 1.0, "xs": [10, 20], "b": True})
    assert got == value and type(got) is type(value)


@pytest.mark.parametrize("expr", ["{1: 'a'}[1.5]", "{1: 'a'}[true]", "{true: 'a'}[1]", "m[b]", "[7, 8][1.5]",
                                  "[7, 8][true]", "{1.0: 'a'}", "{d: 'a'}", "{null: 1}", "{[1]: 1}",
                                  "{1: 'a', 1: 'b'}", "{'a': 1, 'a': 2}"])
def test_bad_keys_and_indexes_are_errors(backend, expr):
    with pytest.raises(CelError):
        evaluate(backend, f"size([{expr}]) == 1", {"m": {1: "a"}, "d": 2.5, "b": True})


def test_map_index_by_double_through_a_guard(backend):
    assert outcome(backend, "keyed", "args.m[args.d] == 'a'", {1: "a"}, 1.0, True, [1])[0] == "violation"
    assert outcome(backend, "keyed", "args.m[args.d] == 'a'", {1: "a"}, 1.5, True, [1])[0] == "rule_error"


# --- 6. R9-6: the sdist's tests run against an installed package ----------------------------------
def test_sdist_ships_gitignore():
    lines = (ROOT / "MANIFEST.in").read_text().splitlines()
    assert any(line.startswith("include ") and ".gitignore" in line.split() for line in lines)


def test_tests_tolerate_an_installed_package():
    r3 = (ROOT / "tests" / "test_fix_round3.py").read_text()
    assert 'if not (ROOT / ".gitignore").exists()' in r3
    r6 = (ROOT / "tests" / "test_fix_round6.py").read_text()
    assert '"site-packages"' in r6 and 'env["MYPYPATH"] = str(src)' in r6
