"""Regression tests for fix round 35, the CEL engine's part (an external audit of the engines' parity
and of the load-time type check; the pydantic items are in invariants-py's
tests/test_fix_round35_audit.py, since this package's tests do not install pydantic).

H4. mark_finite and needs_finite took any uint() of a literal for a uint literal (5u parses as
    uint(5)) and left it unchecked, so uint("9007199254740993") never reached the exact evaluation:
    `uint("9007199254740993") == 9007199254740992.0` was true on cel-expr (cel-cpp rounds the uint to
    a double), false exactly. Only an int literal is a uint literal now.
H5. parse_duration read each number with fractions.Fraction, which raises ValueError (Python's
    4,300-digit limit), not CelError, on a longer one: duration(<5,000 digits>s) was a ValueError at
    load on cel-expr and in the type checker. Such a number is out of range (its whole part) or finer
    than a microsecond (its fraction) and is never read.
M1. cel-expr's matches() left a string literal operand unchecked: under a string-size limit shorter
    than it, `"abcdefgh".matches("a")` was true there, a rule_error on the subset.
M2. _engine._fields read a `__slots__ = "xy"` as the slots x and y, missed a base class's slots, and
    left out a @property, which a rule reads by name: `o.y == 2 && size(o) == 1` was true on the
    subset and a rule_error "no such key 'y'" on cel-expr (which converts o whole for size()). A record
    read whole now has its slots, its bases' slots and its public properties.
M3. inv.sum's int64 overflow ("integer value out of int64 range") was redacted to "evaluation error"
    on cel-expr; it is "value out of range", as other overflows are there.
M4. parse_timestamp took an offset with 60 or more minutes (+00:60); cel-cpp read a literal leap
    second (23:59:60) and a duration literal finer than a microsecond that the subset refuses. Such
    literals are now read by the subset's parser on cel-expr too (dyn()).
M5. The subset's regex parser took \\x{+41}, \\x{ 41}, \\x{4_1} (int() accepts them), \\p{Lx},
    \\p{Cn}, \\p{LC} (any two letters starting with a category letter) and \\p{^^L}; RE2 refuses them.
M6. The type checker passed `args.n[0]` on an int, `{1.5: "a"}` and `{null: 1}`, `null in m` and
    `d.getHours("UTC")` on a duration: each is an error on every backend.
M7. (documented, not changed) the subset reads values lazily, cel-expr converts every path a clause
    reads before it runs: a refused value in a branch not taken is a result on the subset, a rule_error
    on cel-expr (docs/cel.md, Known engine differences). Never two different values.
"""
from __future__ import annotations

import dataclasses
import datetime
import decimal

import pytest

from invariants_cel import _regex, available_backends, get_backend, typecheck, values
from invariants_cel._engine import mark_finite, needs_finite, redact, to_plain
from invariants_cel.errors import CelError
from invariants_cel.subset import parse
from cel_support import outcome

BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]
CEL_EXPR = pytest.mark.skipif("cel-expr" not in available_backends(), reason="cel-expr not installed")


def _run(backend: str, expr: str, env: dict | None = None) -> tuple[str, str | None]:
    return outcome(get_backend(backend).compile(expr), env or {})


# ---- H4. uint() of a string literal is checked ------------------------------------------------------

@CEL_EXPR
@pytest.mark.parametrize("expr,want", [
    ('uint("9007199254740993") == 9007199254740992.0', "ok"),
    ('uint("9007199254740993") > 9007199254740992.0', "violation"),
    ('uint("9007199254740993") != 9007199254740992.0', "violation"),
    ("9007199254740993u == 9007199254740992.0", "ok"),
    ("uint(9007199254740993) > 9007199254740992.0", "violation"),
    ('uint("5") == 5.0', "violation"),
])
def test_uint_of_a_string_literal_compares_exactly(expr, want):
    assert _run("cel-expr", expr) == (want, None)


def test_only_an_int_literal_is_a_uint_literal():
    assert not needs_finite(parse("uint(5) == 5u"))
    assert needs_finite(parse('uint("5") == 5u'))
    assert needs_finite(parse("uint(5.0) == 5u"))
    assert mark_finite(parse('uint("5")'))[0] == "fin"
    assert mark_finite(parse("uint(5)"))[0] == "call"


# ---- H5. a very long duration number is a CelError ---------------------------------------------------

LONG = "1" * 5000


@pytest.mark.parametrize("text,msg", [
    (LONG + "s", "duration out of range"),
    ("0." + LONG + "s", "sub-microsecond"),
    ("-" + LONG + "h", "duration out of range"),
    ("1s" + LONG + "ns", "duration out of range"),
    ("1" + "0" * 21 + "ns", "duration out of range"),          # 22 digits: past CEL's 21 digits of ns
])
def test_a_very_long_duration_number_is_a_cel_error(text, msg):
    with pytest.raises(CelError, match=msg):
        values.parse_duration(text)


@pytest.mark.parametrize("text,us", [
    ("0" * 5000 + "1.5" + "0" * 5000 + "s", 1_500_000),        # zeros only: read as 1.5s
    ("315576000000s", 315_576_000_000 * 10**6),                  # CEL's range end
    ("315576000000999999000ns", 315_576_000_000_999_999),        # 21 digits of ns
    ("0.000001000000000000000000000000s", 1),
    ("0.1h", 360 * 10**6),
])
def test_a_long_duration_number_within_range_still_parses(text, us):
    assert values.parse_duration(text) == datetime.timedelta(microseconds=us)


def test_a_fraction_of_thirty_digits_is_still_read():
    with pytest.raises(CelError, match="sub-microsecond"):
        values.parse_duration("0." + "1" * 30 + "s")
    # read exactly: two 30-digit fractions that sum to one second are one second
    assert values.parse_duration("0." + "0" * 29 + "1s0." + "9" * 30 + "s") == datetime.timedelta(seconds=1)
    with pytest.raises(CelError, match="sub-microsecond"):
        values.parse_duration("0." + "0" * 30 + "1s0." + "9" * 31 + "s")


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("expr", [f'duration("{LONG}s") > duration("1s")', 'duration(s) > duration("1s")'])
def test_a_very_long_duration_is_a_rule_error_on_both(backend, expr):
    got = _run(backend, expr, {"s": LONG + "s"})
    assert got[0] == "rule_error" and "duration out of range" in (got[1] or ""), got


def test_the_type_checker_refuses_a_very_long_duration_literal():
    with pytest.raises(typecheck.TypeMismatch, match="duration out of range"):
        typecheck.check_clause(f'duration("{LONG}s") > duration("1s")', {})


# ---- M1. matches() checks a literal operand on cel-expr too -------------------------------------------

@pytest.fixture
def short_limit():
    old = values.set_string_limit(5)
    yield
    values.set_string_limit(old)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("expr", ['"abcdefgh".matches("a")', '"a".matches("abcdefgh")'])
def test_matches_checks_a_literal_operand(backend, expr, short_limit):
    got = _run(backend, expr)
    assert got[0] == "rule_error" and "string-size limit" in (got[1] or ""), got


@pytest.mark.parametrize("backend", BACKENDS)
def test_matches_on_short_literals_still_runs(backend, short_limit):
    assert _run(backend, '"abc".matches("^a")') == ("violation", None)


@CEL_EXPR
def test_a_literal_pattern_over_the_pattern_limit_is_a_rule_error_on_cel_expr():
    pat = "(" * 70 + "a" + ")" * 70
    for backend in BACKENDS:
        got = _run(backend, f's.matches("{pat}")', {"s": "a"})
        assert got[0] == "rule_error" and "pattern limit" in (got[1] or ""), (backend, got)


# ---- M2. slots read whole ---------------------------------------------------------------------------

class _OneSlot:
    __slots__ = "xy"

    def __init__(self) -> None:
        self.xy = 1


class _Base:
    __slots__ = ("a",)


class _Derived(_Base):
    __slots__ = ("b",)

    def __init__(self) -> None:
        self.a, self.b = 1, 2


class _WithProperty:
    def __init__(self) -> None:
        self.x = 1

    @property
    def y(self) -> int:
        return 2


@dataclasses.dataclass
class _DataWithProperty:
    a: int = 1

    @property
    def twice(self) -> int:
        return 2 * self.a

    @property
    def _hidden(self) -> int:
        return 0

    @property
    def method(self):
        return len


class _Raising:
    @property
    def y(self) -> int:
        raise RuntimeError("secret")


def test_a_record_read_whole_has_every_slot():
    assert to_plain(_OneSlot()) == {"xy": 1}
    assert to_plain(_Derived()) == {"a": 1, "b": 2}
    assert to_plain(_WithProperty()) == {"x": 1, "y": 2}   # a property, as a rule reads it by name
    assert to_plain(_DataWithProperty()) == {"a": 1, "twice": 2}
    with pytest.raises(CelError, match="reading 'y'"):
        to_plain(_Raising())


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("expr,env", [
    ("size(o) == 2 && o == {'a': 1, 'b': 2}", {"o": _Derived()}),
    ("o == {'xy': 1}", {"o": _OneSlot()}),
    ("'a' in o", {"o": _Derived()}),
    ("o.y == 2 && size(o) == 2 && o == {'x': 1, 'y': 2}", {"o": _WithProperty()}),
    ("o.twice == 2 && size(o) == 2", {"o": _DataWithProperty()}),
])
def test_slots_read_whole_on_both(backend, expr, env):
    assert _run(backend, expr, env) == ("violation", None)


# ---- M3. inv.sum overflow ------------------------------------------------------------------------------

def test_int64_overflow_text_is_value_out_of_range():
    assert redact("integer value out of int64 range") == "value out of range"
    assert redact("uint value out of range") == "value out of range"
    assert redact("a b c out of range") == "evaluation error"     # three words: not the pattern


@pytest.mark.parametrize("backend", BACKENDS)
def test_inv_sum_overflow_is_named(backend):
    got = _run(backend, "inv.sum(xs) > 0", {"xs": [2**62, 2**62]})
    assert got[0] == "rule_error" and got[1] in ("integer overflow", "value out of range"), got


# ---- M4. timestamp offsets and literals read by the subset's parser --------------------------------

@pytest.mark.parametrize("text", ["2024-01-01T00:00:00+00:60", "2024-01-01T00:00:00-05:99", "2024-01-01T00:00:00+0060"])
def test_an_offset_with_60_minutes_is_refused(text):
    with pytest.raises(CelError, match="not a valid date and time"):
        values.parse_timestamp(text)


def test_an_offset_with_59_minutes_parses():
    t = values.parse_timestamp("2024-01-01T00:00:00+05:59")
    assert t == datetime.datetime(2023, 12, 31, 18, 1, tzinfo=datetime.timezone.utc)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("expr,msg", [
    ('timestamp("2024-01-01T00:00:00+00:60") == timestamp("2023-12-31T23:00:00Z")', "not a valid date"),
    ('timestamp("2016-12-31T23:59:60Z") == timestamp("2017-01-01T00:00:00Z")', "not a valid date"),
    ('duration("0.0000001s") == duration("0s")', "sub-microsecond"),
    ('duration("99999999999999h") > duration("1s")', "duration out of range"),
])
def test_a_literal_the_subset_refuses_is_a_rule_error_on_both(backend, expr, msg):
    got = _run(backend, expr)
    assert got[0] == "rule_error" and msg in (got[1] or ""), got


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_good_literal_still_evaluates(backend):
    assert _run(backend, 'timestamp("2024-01-01T00:00:00+05:59") < timestamp("2024-01-01T00:00:00Z") '
                         '&& duration("1.5s") == duration("1500ms")') == ("violation", None)


# ---- M5. RE2 escapes ---------------------------------------------------------------------------------

@pytest.mark.parametrize("pat", [r"\x{+41}", r"\x{ 41}", r"\x{4_1}", r"\x+4", r"\x{}", r"[\x{+41}]", r"\p{Lx}",
                                 r"\p{Cn}", r"\p{LC}", r"\p{^^L}", r"\pX", r"\p{^}", r"\P{^^L}"])
def test_an_escape_re2_refuses_is_refused(pat):
    with pytest.raises(_regex.RegexError):
        _regex.compile_pattern(pat)


@pytest.mark.parametrize("pat", [r"\p{Lx}", r"\x{+41}", r"\p{^^L}"])
def test_the_type_checker_refuses_such_a_pattern(pat):
    with pytest.raises(typecheck.TypeMismatch, match="bad pattern"):
        typecheck.check_clause('"A".matches("' + pat.replace("\\", "\\\\") + '")', {})


@pytest.mark.parametrize("pat", [r"\x{41}", r"\x41", r"\p{Lu}", r"\pL", r"\p{^L}", r"\P{Nd}", r"\p{Zs}", r"\p{Cs}",
                                 r"\p{Any}"])
def test_an_escape_re2_takes_is_taken(pat):
    _regex.compile_pattern(pat)


def test_a_script_is_still_left_to_re2():
    with pytest.raises(_regex.RegexError, match="unsupported"):
        _regex.compile_pattern(r"\p{Greek}")
    with pytest.raises(_regex.RegexError, match="unsupported"):
        _regex.compile_pattern(r"\p{Yi}")


@CEL_EXPR
@pytest.mark.parametrize("pat", [r"\\x{+41}", r"\\p{Lx}", r"\\p{^^L}", r"\\p{Cn}"])
def test_both_backends_refuse_such_a_pattern(pat):
    for backend in BACKENDS:
        assert _run(backend, f'"A".matches("{pat}")')[0] == "rule_error", backend


# ---- M6. the type checker -----------------------------------------------------------------------------

ENV = {"args": typecheck.T({"map"}, fields={"n": typecheck.INT, "m": typecheck.T({"map"}, elem=typecheck.INT),
                                            "d": typecheck.DURATION, "t": typecheck.TIMESTAMP})}


@pytest.mark.parametrize("expr,msg", [
    ("args.n[0] == 1", "index"),
    ('"abc"[0] == "a"', "index"),
    ('{1.5: "a"}.size() == 1', "map key"),
    ("{null: 1}.size() == 1", "map key"),
    ("null in args.m", "no overload for in"),
    ('args.d.getHours("UTC") > 1', "with a timezone"),
])
def test_the_type_checker_refuses(expr, msg):
    with pytest.raises(typecheck.TypeMismatch, match=msg):
        typecheck.check_clause(expr, ENV)


@pytest.mark.parametrize("expr", [
    'args.m["a"] == 1', "[1, 2][0] == 1", '"a" in args.m', "1.0 in {1: 2}", '{"a": 1, 2: 3, true: 4}.size() == 3',
    'args.t.getHours("UTC") > 1', "args.d.getHours() > 1", "null in [1, null]", "dyn(args.n)[0] == 1",
    "{args.n: 1}.size() == 1",
])
def test_the_type_checker_still_passes(expr):
    typecheck.check_clause(expr, ENV)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("expr", ["args.n[0] == 1", '{1.5: "a"}.size() == 1', "{null: 1}.size() == 1",
                                  "null in args.m", 'args.d.getHours("UTC") > 1'])
def test_what_it_refuses_is_an_error_on_every_backend(backend, expr):
    env = {"args": {"n": 5, "m": {"a": 1}, "d": datetime.timedelta(hours=2)}}
    assert _run(backend, expr, env)[0] == "rule_error"


# ---- M7. eager projection on cel-expr: documented ------------------------------------------------------

@CEL_EXPR
def test_a_refused_value_not_needed_differs_only_as_result_against_rule_error():
    env = {"args": {"x": decimal.Decimal("1")}}
    assert _run("subset", "false && args.x == 1", env) == ("ok", None)
    assert _run("cel-expr", "false && args.x == 1", env)[0] == "rule_error"
