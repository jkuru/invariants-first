"""Regression tests for fix round 28, the CEL engine's part (items 1 and 2, the schema judge's, are in
packages/invariants-schema-diff/tests/test_fix_round28_celschema.py): each test failed before its fix.

3. CEL regex (subset): under (?i), \\w \\W \\d \\s \\pX \\PX and [:name:] ignored the flag, so the
   subset and RE2 disagreed ((?i)\\p{Lu} on 'a', (?i)\\W on U+212A); RE2 closes the class under case
   folding, then negates. Also \\_ (a literal _ in RE2) was refused and \\é (an error in RE2) taken.
4. CEL conversions (subset): int()/uint()/double() took Unicode whitespace around the digits
   ('500\\xa0'), where cel-expr (absl) takes ASCII whitespace only, and U+001C..U+001F leaked a raw
   ValueError out of Program.evaluate.
5. CEL front end: cel-cpp's parser drops an even run of ! or unary - without checking the operand,
   so `!!args.s == "x"` was true on cel-expr and an error on the subset; the text sent to cel-expr
   is now re-printed (`!(!(args.s))`) whenever the tree has such a run.
"""
from __future__ import annotations

import pytest

from invariants_cel import _engine, available_backends, get_backend
from invariants_cel.errors import CelError

BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]


# ---- 3. regex case folding on Perl, Unicode and POSIX classes --------------------------------------

K, LONG_S = "\u212a", "\u017f"           # Kelvin sign (folds to k), long s (folds to s)
REGEX_ROWS = [
    (r"(?i)\p{Lu}", "a", True), (r"(?i)\p{Ll}", "A", True), (r"(?i)\P{Lu}", "a", False),
    (r"(?i)\p{^Lu}", "a", False), (r"(?i)\P{^Lu}", "a", True),
    (r"(?i)\w", K, True), (r"(?i)\w", LONG_S, True), (r"(?i)\W", K, False), (r"(?i)\W", LONG_S, False),
    (r"(?i)[^\W]", K, True), (r"(?i)[\W]", K, False), (r"(?i)[\Wk]", K, True),
    (r"(?i)[[:word:]]", LONG_S, True), (r"(?i)[[:^word:]]", K, False), (r"(?i)[^[:^lower:]]", "A", True),
    (r"(?i)[^\P{Lu}]", "a", True), (r"(?i)(?-i:\w)", K, False), (r"\W", K, True), (r"\w", K, False),
    (r"\_", "_", True), (r"[\_]", "_", True),
]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("pattern,s,want", REGEX_ROWS)
def test_regex_classes_fold_case_as_re2(backend, pattern, s, want):
    prog = get_backend(backend).compile(f"args.s.matches(r'^{pattern}$')")
    assert prog.evaluate({"args": {"s": s}}) is want


@pytest.mark.parametrize("backend", BACKENDS)
def test_regex_non_ascii_escape_is_refused(backend):
    prog = get_backend(backend).compile("args.s.matches(r'\\é')")
    with pytest.raises(CelError):
        prog.evaluate({"args": {"s": "é"}})


# ---- 4. conversions: ASCII whitespace only, and every failure a CelError ---------------------------

@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("fn", ["int", "double"])
@pytest.mark.parametrize("text", ["500\xa0", "\xa0500", "500\x85", "500\u2028", "\u3000500", "500\x1c", "\x1f500",
                                  "\xa0nan", "+-nan", "--inf"])
def test_conversion_refuses_unicode_whitespace(backend, fn, text):
    prog = get_backend(backend).compile(f"{fn}(args.s) > 1.0" if fn == "double" else f"{fn}(args.s) > 1")
    with pytest.raises(CelError):
        prog.evaluate({"args": {"s": text}})


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("text", [" 500", "500\t", "\n500\r", "\v500\f"])
def test_conversion_takes_ascii_whitespace(backend, text):
    assert get_backend(backend).compile("int(args.s) == 500").evaluate({"args": {"s": text}}) is True
    assert get_backend(backend).compile("double(args.s) == 500.0").evaluate({"args": {"s": text}}) is True


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("text", ["\xa0nan", "inf　", "\x85-infinity"])
def test_nonfinite_text_refuses_unicode_whitespace_when_allowed(backend, text):
    from invariants_cel.errors import ALLOW_NON_FINITE
    token = ALLOW_NON_FINITE.set(True)
    try:
        with pytest.raises(CelError):
            get_backend(backend).compile("double(args.s) != 1.0").evaluate({"args": {"s": text}})
        assert get_backend(backend).compile("double(args.s) != 1.0").evaluate({"args": {"s": " -Inf\t"}}) is True
    finally:
        ALLOW_NON_FINITE.reset(token)


def test_any_conversion_failure_is_a_cel_error(monkeypatch):
    from invariants_cel.subset import convert

    def broken(v):
        raise ValueError("a case the text guard missed")
    monkeypatch.setitem(convert._CONVERTERS, "int", broken)
    with pytest.raises(CelError, match="conversion failed"):
        get_backend("subset").compile("int(args.s) > 1").evaluate({"args": {"s": "1"}})


# ---- 5. doubled unary operators reach cel-expr re-printed ------------------------------------------

DOUBLED = ['!!args.s == "x"', '--args.s == "x"', '! !args.s == "x"', '- -args.s == "x"', '!!!!args.s == "x"',
           '--args.i == args.i', '!!args.n == null', '--args.l == [1]']


@pytest.mark.parametrize("src", DOUBLED)
def test_doubled_unary_is_reprinted(src):
    text = _engine.FrontEnd(src).text_for(_engine.INV_FUNCS, {})
    assert text != src and ("!(!(" in text or "-(-(" in text)


def test_single_unary_keeps_the_source():
    for src in ['!args.b', '-args.i > 0', '!(-args.i > 0)', '-(!args.b)']:
        assert _engine.FrontEnd(src).text_for(_engine.INV_FUNCS, {}) == src


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("src", DOUBLED)
def test_doubled_unary_checks_its_operand(backend, src):
    env = {"args": {"s": "x", "n": None, "i": -9223372036854775808, "l": [1]}}
    with pytest.raises(CelError):
        get_backend(backend).compile(src).evaluate(env)


@pytest.mark.parametrize("backend", BACKENDS)
def test_doubled_unary_on_valid_operands(backend):
    env = {"args": {"b": True, "i": 5}}
    assert get_backend(backend).compile("!!args.b && --args.i == 5").evaluate(env) is True
