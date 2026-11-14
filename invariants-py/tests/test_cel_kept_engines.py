"""The two kept CEL backends (subset, cel-expr) after celpy was dropped: has() on a value with no
fields is an error (1), a spent cost budget is never absorbed by && || all exists exists_one (2),
(?i) matching folds case one character at a time as RE2 does (3), and engine error text keeps
request data out of the message unless debug (4).

Tests that name a backend run on every installed one (parametrised), whatever INV_CEL_BACKEND says.
"""
from __future__ import annotations

import pytest

from invariants import cel
from invariants.cel import CelError, available_backends, get_backend
from invariants.cel import _engine
from invariants.cel.subset import SubsetBackend

BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]


@pytest.fixture(params=BACKENDS)
def backend(request):
    return request.param


def ev(backend: str, src: str, **env):
    return get_backend(backend).compile(src).evaluate(env)


class Rec:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# --- 1. has() on null (or another value with no fields) is an error, as on cel-expr ---------------
@pytest.mark.parametrize("src,env", [
    ("has(x.f)", {"x": None}),
    ("has(x.f.g)", {"x": {"f": None}}),
    ("has(x.f)", {"x": 1}),
    ("has(x.f)", {"x": "s"}),
    ("has(x.f)", {"x": [1]}),
])
def test_has_on_a_value_with_no_fields_is_an_error(backend, src, env):
    with pytest.raises(CelError):
        ev(backend, src, **env)


@pytest.mark.parametrize("src,env,want", [
    ("has(x.f)", {"x": {"f": None}}, True),          # a null field is present
    ("has(x.f)", {"x": {"g": 1}}, False),
    ("has(x.f.g)", {"x": {"f": {"g": 1}}}, True),
    ("has(x.f) || true", {"x": None}, True),         # the error is absorbed like any other
])
def test_has_on_a_map_is_presence(backend, src, env, want):
    assert ev(backend, src, **env) is want


def test_has_on_a_record_is_presence():
    assert ev("subset", "has(x.f)", x=Rec(f=None)) is True
    assert ev("subset", "has(x.g)", x=Rec(f=None)) is False


# --- 2. a spent cost budget propagates through every error-absorbing form --------------------------
BIG = "size([1,2,3,4,5,6,7,8,9,10].map(a, [1,2,3,4,5,6,7,8,9,10].map(b, b))) > 0"


@pytest.mark.parametrize("src", [
    f"false && {BIG}",
    f"{BIG} && false",
    f"true || {BIG}",
    f"{BIG} || true",
    f"[0, 1].exists(x, x == 0 ? true : {BIG})",
    f"[1, 0].exists(x, x == 0 ? true : {BIG})",
    f"[0, 1].all(x, x == 0 ? false : {BIG})",
    f"[0, 1].exists_one(x, x == 0 ? true : {BIG})",
    "false && s.matches('(a|b)*c')",
])
def test_cost_limit_is_never_absorbed(src):
    prog = SubsetBackend(budget=60).compile(src)
    with pytest.raises(CelError, match="cost limit exceeded"):
        prog.evaluate({"s": "ab" * 2000})


def test_cost_limit_error_is_a_cel_error_and_other_errors_still_absorb():
    prog = SubsetBackend(budget=60).compile("false && m.missing > 0")
    assert prog.evaluate({"m": {}}) is False


# --- 3. (?i) folds case one character at a time (RE2's simple case folding) ------------------------
# The expected values are cel-expr's (RE2), checked when this was written.
FOLD = [
    ("ß", "(?i)[A-Z]", False), ("ß", "(?i)[a-z]", False), ("ß", "(?i)ss", False), ("SS", "(?i)ß", False),
    ("ﬁ", "(?i)[A-Z]", False), ("ﬁ", "(?i)fi", False),
    ("ẞ", "(?i)ß", True), ("ß", "(?i)ẞ", True),
    ("ı", "(?i)i", False), ("I", "(?i)ı", False), ("İ", "(?i)i", False), ("i", "(?i)İ", False),
    ("ſ", "(?i)s", True), ("S", "(?i)ſ", True), ("ſ", "(?i)[a-z]", True),
    ("K", "(?i)k", True), ("k", "(?i)K", True), ("K", "(?i)[a-z]", True),
    ("Σ", "(?i)ς", True), ("ς", "(?i)σ", True), ("ǅ", "(?i)ǆ", True), ("Ǆ", "(?i)ǅ", True),
    ("ß", "(?i)[^a-z]", True), ("K", "(?i)[^k]", False), ("é", "(?i)É", True), ("Ab", "(?i)aB", True),
]


@pytest.mark.parametrize("text,pattern,want", FOLD)
def test_case_insensitive_matching_folds_single_characters(backend, text, pattern, want):
    assert ev(backend, "s.matches(p)", s=text, p=pattern) is want


# --- 4. engine error text: request data only in .detail ----------------------------------------------
@pytest.mark.parametrize("msg,want", [
    ('NOT_FOUND: Key not found in map : "cost limit exceeded SECRET"', "no such key"),
    ('NOT_FOUND: Key not found in map : "non-finite number SECRET"', "no such key"),
    ('NOT_FOUND: Key not found in map : "inv.zip() SECRET"', "no such key"),
    ('NOT_FOUND: Key not found in map : "timestamp(): SECRET"', "no such key"),
    ('NOT_FOUND: Key not found in map : "no matching overload SECRET"', "no such key"),
    ("INVALID_ARGUMENT: invalid regular expression: missing ): (cost limit exceeded SECRET",
     "invalid regular expression"),
    ("INVALID_ARGUMENT: invalid regular expression: missing ): (no such overload SECRET",
     "invalid regular expression"),
    ("INVALID_ARGUMENT: invalid regular expression: missing ): (timestamp(): SECRET", "invalid regular expression"),
    ("INVALID_ARGUMENT: modulus by zero", "division by zero"),
    ("UNKNOWN: No matching overloads found : _+_(string, int64)", "no such overload: _+_(string, int64)"),
    ("UNKNOWN: No matching overloads found : SECRET 'x'", "no such overload"),
    ("OUT_OF_RANGE: inv.zip(): length mismatch (1 vs 2)", "inv.zip(): length mismatch"),
    ("OUT_OF_RANGE: unknown timezone", "unknown timezone"),
    ("OUT_OF_RANGE: non-finite number (NaN or Infinity) from arithmetic (division by zero or overflow)",
     "non-finite number (NaN or Infinity) from arithmetic (division by zero or overflow)"),
    ("something new SECRET cost limit exceeded", "evaluation error"),
    ("INTERNAL: SECRET Iteration budget exceeded", "evaluation error"),
    # a kind's words later in the message (request data) never pick the kind: matched at the start only
    ("something new: SECRET no such key", "evaluation error"),
    ("UNKNOWN: SECRET invalid regular expression", "evaluation error"),
    ("INTERNAL: SECRET No matching overloads found : _+_(string, int64)", "evaluation error"),
])
def test_redact_never_returns_request_text(msg, want):
    assert _engine.redact(msg) == want


@pytest.mark.parametrize("src,env", [
    ("m[k] > 0", {"m": {"a": 1}, "k": "cost limit exceeded SECRET"}),
    ("m[k] > 0", {"m": {"a": 1}, "k": "non-finite number SECRET"}),
    ("m[k] > 0", {"m": {"a": 1}, "k": "Iteration budget exceeded SECRET"}),
    ("s.matches(p)", {"s": "x", "p": "(cost limit exceeded SECRET"}),
    ("s.matches(p)", {"s": "x", "p": "(timestamp(): SECRET"}),
])
def test_engine_error_message_holds_no_request_data(backend, src, env):
    with pytest.raises(CelError) as ei:
        ev(backend, src, **env)
    assert "SECRET" not in str(ei.value) and "cost limit" not in str(ei.value)
    assert "SECRET" in (ei.value.detail or "")


def test_zip_length_mismatch_names_no_lengths(backend):
    with pytest.raises(CelError) as ei:
        ev(backend, "size(inv.zip(a, b)) > 0", a=[1, 2, 3], b=[1, 2, 3, 4, 5, 6, 7])
    assert "length mismatch" in str(ei.value) and "3" not in str(ei.value) and "7" not in str(ei.value)
    assert "3 vs 7" in (ei.value.detail or "")


def test_cel_expr_cost_message_is_kept_whole():
    if "cel-expr" not in BACKENDS:
        pytest.skip("needs cel-expr-python")
    ten = "[1,2,3,4,5,6,7,8,9,10]"
    src = f"{ten}.all(a, {ten}.all(b, {ten}.all(c, {ten}.all(d, {ten}.all(f, true)))))"
    with pytest.raises(CelError, match=r"^cost limit exceeded \(cel-expr: at most 9,999 comprehension iterations"
                                       r" per clause, nested comprehensions multiply\)$"):
        ev("cel-expr", src)           # fix round 30: once, not 'cel-expr: cost limit exceeded (cel-expr: ...)'


# --- the strings extension's substring() is on neither backend ---------------------------------------
def test_substring_is_on_neither_backend(backend):
    with pytest.raises(CelError):
        ev(backend, "'abc'.substring(1) == 'bc'")
    assert "substring" not in getattr(get_backend(backend), "functions", cel.ENGINE_FUNCTIONS)
