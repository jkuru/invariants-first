"""matches() on the subset backend: linear time (no ReDoS), RE2 syntax, steps charged to the cost budget."""
from __future__ import annotations

import time

import pytest
from hypothesis import given, settings, strategies as st

import invariants_cel as cel
from cel_support import outcome as clause_outcome
from invariants_cel import _regex

PATHOLOGICAL = ["^([a-z0-9]+-?)+$", "^(a+)+$", "(a|a)*b", "(a*)*b", "^(a|aa)+$", "(x+x+)+y"]


def outcome(backend: str, expr: str, env: dict) -> str:
    prog = cel.get_backend(backend).compile(expr)
    return clause_outcome(prog, env)[0]


@pytest.mark.parametrize("backend", cel.available_backends())
@pytest.mark.parametrize("pattern", PATHOLOGICAL)
def test_nested_quantifiers_run_in_linear_time(backend: str, pattern: str) -> None:
    # Python re takes seconds on these at n=27 and doubles per character.
    start = time.perf_counter()
    kind = outcome(backend, f's.matches("{pattern}")', {"s": "a" * 40 + "!"})
    assert time.perf_counter() - start < 1.0
    assert kind == "ok"


def test_subset_matches_long_input_ends_in_rule_error_within_budget() -> None:
    start = time.perf_counter()
    kind = outcome("subset", 's.matches("(a|a)*b")', {"s": "a" * 200_000})
    assert time.perf_counter() - start < 2.0
    assert kind == "rule_error"


def test_subset_matches_charges_the_budget() -> None:
    prog = cel.get_backend("subset").compile('s.matches("(a|b)*c")')
    budget = [cel.subset.DEFAULT_BUDGET]
    cel.subset.evaluate(prog.ast, {"s": "ab" * 500}, budget)
    assert budget[0] < cel.subset.DEFAULT_BUDGET - 100


def test_subset_matches_ordinary_inputs_fit_the_budget() -> None:
    assert outcome("subset", 's.matches("^[a-z0-9-]+$")', {"s": "a-b" * 300}) == "violation"


@pytest.mark.parametrize("pattern", ["(?=a)", "(?!a)", "(?<=a)b", "(a)\\1", "a**", "a{1001}", "[z-a]", "(", "\\e"])
def test_subset_refuses_non_re2_patterns(pattern: str) -> None:
    with pytest.raises(_regex.RegexError):
        _regex.compile_pattern(pattern)
    assert outcome("subset", "s.matches(p)", {"s": "ab", "p": pattern}) == "rule_error"


def test_program_size_is_capped() -> None:
    with pytest.raises(_regex.RegexError, match="too large"):
        _regex.compile_pattern("(a{1000}){1000}")


FIXED = [("b+c", "abbbcd"), ("^(ab)*$", "ababa"), ("x{2,3}y", "axy"), ("(?i)hello", "HeLLo"),
         ("\\bfoo\\b", "afoob"), ("a$", "a\n"), ("(?m)a$", "a\nb"), ("(?s)a.b", "a\nb"), ("a.b", "a\nb"),
         ("[[:digit:]]+", "x9"), ("\\p{Lu}", "abC"), ("\\d", "٣"), ("[]a]", "]"), ("a{,2}", "a{,2}"),
         ("\\Qa.b\\E", "axb"), ("(?P<x>a)b", "ab"), ("\\x{263a}", "☺"), ("[^a-c]", "abc")]


@pytest.mark.parametrize("pattern,text", FIXED)
def test_engine_agrees_with_re2_on_fixed_cases(pattern: str, text: str) -> None:
    re2 = pytest.importorskip("re2")
    got = _regex.search(_regex.compile_pattern(pattern), text, 10**7)[0]
    assert got == (re2.search(pattern, text) is not None)


_atom = st.sampled_from(["a", "b", ".", "[ab]", "[^a]", "\\d", "\\w", "(a|b)", "(?:ab)", "^", "$", "\\b", "x"])
_quant = st.sampled_from(["", "", "*", "+", "?", "{2}", "{1,3}", "{0,}", "*?"])
_patterns = st.lists(st.tuples(_atom, _quant), min_size=1, max_size=6).map(
    lambda xs: "".join(a + (q if a not in ("^", "$", "\\b") else "") for a, q in xs))


@settings(max_examples=300, deadline=None)
@given(pattern=_patterns, text=st.text(alphabet="ab1x \n", max_size=12))
def test_engine_agrees_with_re2(pattern: str, text: str) -> None:
    re2 = pytest.importorskip("re2")
    got = _regex.search(_regex.compile_pattern(pattern), text, 10**7)[0]
    assert got == (re2.search(pattern, text) is not None), (pattern, text)
