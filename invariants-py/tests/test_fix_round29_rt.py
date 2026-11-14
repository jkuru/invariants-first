"""Regression tests for fix round 29 (types, loader, guard, pytest plugin, ablation digest, CEL): each
failed before its fix.

1. A PEP 695 alias whose value does not evaluate (`type Later = OnlyTyping`, OnlyTyping imported
   under TYPE_CHECKING) is an unresolved annotation (dyn; the lock pins it by name), never the
   NameError from inv.load / validate / lock.
2. Whether an enforcer can await an async provider is judged as the guard / edge wrapper runs: the
   function it decorates (a sync wrapper of an async def is sync, an async wrapper of a sync def is
   async), with a guard's post_effect checks and a model call over a sync wrapper of an async def
   running async. All four shapes per decorator.
3. Under pytest-xdist with no rule file, rules a conftest loads are rules (the controller refused
   them); with none at all it is still exit 4.
4. rule_digest covers the provider records the rule reads and its target records: switching a
   provider's function makes an earlier ablation report stale.
5. A timestamp getter given a null zone is a rule_error on every backend (the subset read it as UTC);
   any argument to a duration getter too.
6. `\\Q...\\E` followed by a quantifier repeats its last character only; an empty `\\Q\\E` is nothing; a
   leading repeat count is an error. The pattern limit counts the split literal.
7. A view / bind path into a field of a scalar parameter is a LoadError naming the rule.
8. RE2 searches UTF-8 bytes: a match may start inside a multi-byte character, where only `\\B` holds.
"""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from typing import Any

import pytest

import invariants as inv
import round29rt as m
from invariants import _typeinfo as ti
from invariants import cel
from invariants import typecheck as tc
from invariants.cel import CelError, _regex
from invariants.cel import values
from invariants.registry.coverage import rule_digest
from invariants.testing import make_checked

pytest_plugins = ["pytester"]


def _run(x: Any) -> Any:
    return asyncio.run(x) if asyncio.iscoroutine(x) else x


# --- 1: an alias whose value does not evaluate ----------------------------------------------------------
class TypeAliasType:
    """Shaped as typing.TypeAliasType (typing_extensions' too): its __value__ raises, as a PEP 695
    alias of a name imported under TYPE_CHECKING does when evaluated."""
    __name__ = "Later"
    __type_params__ = ()

    @property
    def __value__(self) -> Any:
        raise NameError("name 'OnlyTyping' is not defined")


def test_unevaluable_alias_is_unresolved_not_a_crash():
    a = TypeAliasType()
    assert ti.is_alias(a)
    assert ti.unalias(a) == "Later"
    assert tc.from_py(a) is tc.DYN
    from invariants.lock.pin_types import _type_schema
    assert _type_schema(a, {}, set()) == {"unresolved": "Later"}
    assert _type_schema(a, {}, set()) == _type_schema(TypeAliasType(), {}, set())      # stable


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 type statements")
def test_pep695_alias_of_type_checking_import_loads_and_locks(tmp_path, monkeypatch):
    (tmp_path / "laterapp.py").write_text(textwrap.dedent("""\
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from decimal import Decimal as OnlyTyping
        type Later = OnlyTyping
        type Box[T] = list[OnlyTyping]
        def refund(x: Later, b: Box[int], n: int) -> None: ...
        """))
    monkeypatch.syspath_prepend(str(tmp_path))
    doc = {"spec": "invariant/v0.1", "targets": {"py:laterapp.refund": {"effects": "none"}},
           "rules": [{"id": "R", "status": "candidate", "subject": {"target": "py:laterapp.refund"},
                      "bad": "args.n > 1 || args.x == 1"}]}
    reg = inv.load(doc, install=False)                     # args.x is dyn: compared with anything
    assert "R" in reg.rules
    rules = tmp_path / "invariants.yaml"
    rules.write_text(json.dumps(doc))
    from invariants.cli import main
    monkeypatch.chdir(tmp_path)
    assert main(["validate", str(rules)]) == 0
    assert main(["lock", str(rules)]) == 0
    lock = json.loads((tmp_path / "invariants.lock").read_text())
    types = lock["targets"]["py:laterapp.refund"]["types"]
    assert types["x"] == {"unresolved": "Later"}
    assert main(["lock", "--check", str(rules)]) == 0     # pinned stably


# --- 2: async providers, judged by what the wrapper runs ------------------------------------------------
def _guard_case(shape: str, timing: str) -> tuple[Any, Any]:
    with inv.isolated() as reg:
        inv.load(m.guard_doc(shape, timing))
        fn = getattr(m, f"g_{shape}")
        ok = _run(fn(5))
        with pytest.raises(inv.Rejected):
            _run(fn(500 if timing == "pre_effect" else 50))
        return ok, [e["outcome"] for e in reg.sink]


@pytest.mark.parametrize("shape,timing,loads", [
    ("sync", "pre_effect", False), ("async", "pre_effect", True),
    ("sync_of_async", "pre_effect", False), ("async_of_sync", "pre_effect", True),
    ("sync", "post_effect", False), ("async", "post_effect", True),
    ("sync_of_async", "post_effect", True), ("async_of_sync", "post_effect", True)])
def test_guard_async_provider_by_shape(shape, timing, loads):
    if not loads:
        with pytest.raises(inv.LoadError, match="provider acap is async def"):
            inv.load(m.guard_doc(shape, timing), install=False)
        return
    ok, outcomes = _guard_case(shape, timing)
    assert ok == 50 and outcomes == ["ok", "violation"]


def test_guard_sync_wrapper_refusal_says_what_to_do():
    with pytest.raises(inv.LoadError, match="put the decorator directly on the async def"):
        inv.load(m.guard_doc("sync_of_async", "pre_effect"), install=False)


@pytest.mark.parametrize("shape,loads", [("sync", False), ("async", True), ("sync_of_async", False),
                                         ("async_of_sync", True)])
def test_edge_async_provider_by_shape(shape, loads):
    if not loads:
        with pytest.raises(inv.LoadError, match="provider acap is async def"):
            inv.load(m.edge_doc(shape), install=False)
        return
    fn = getattr(m, f"e_{shape}")
    with inv.isolated() as reg:
        inv.load(m.edge_doc(shape))
        assert _run(fn(make_checked(m.CheckedReply, m.Reply("REFUND", 5), rule=f"E-{shape}"))) == 5
        with pytest.raises(inv.Rejected):
            _run(fn(make_checked(m.CheckedReply, m.Reply("REFUND", 500), rule=f"E-{shape}")))
        assert [e["outcome"] for e in reg.sink] == ["ok", "violation"]


@pytest.mark.parametrize("shape,loads", [("sync", False), ("async", True), ("sync_of_async", True),
                                         ("async_of_sync", True)])
def test_model_call_async_provider_by_shape(shape, loads):
    if not loads:
        with pytest.raises(inv.LoadError, match="provider acap is async def"):
            inv.load(m.model_doc(shape), install=False)
        return
    fn = getattr(m, f"m_{shape}")
    with inv.isolated() as reg:
        inv.load(m.model_doc(shape))
        m.REPLIES[:] = [json.dumps({"action": "REFUND", "amount": 5}), json.dumps({"action": "REFUND", "amount": 500})]
        assert _run(fn("o")).amount == 5
        with pytest.raises(inv.Rejected):
            _run(fn("o"))
        assert [e["outcome"] for e in reg.sink] == ["ok", "violation"]


# --- 3: xdist with rules a conftest loads -----------------------------------------------------------
def _conftest_app(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(xdapp=textwrap.dedent("""\
        import invariants as inv
        @inv.guard("R-1")
        def refund(amount: int) -> int:
            return amount
        """))
    (pytester.path / "laws.json").write_text(json.dumps({
        "spec": "invariant/v0.1", "targets": {"py:xdapp.refund": {"effects": "external"}},
        "rules": [{"id": "R-1", "statement": "small", "guard": "py:xdapp.refund", "bad": "args.amount > 100",
                   "tests": ["test_app.py::test_big"]}]}))
    pytester.makeconftest("import pathlib, invariants as inv\n"
                          "inv.load(pathlib.Path(__file__).parent / 'laws.json')\n")
    pytester.makepyfile(test_app=textwrap.dedent("""\
        import pytest, invariants as inv, xdapp
        def test_big():
            with pytest.raises(inv.Rejected):
                xdapp.refund(500)
        """))


def test_xdist_ablate_with_conftest_rules_runs(pytester):
    pytest.importorskip("xdist")
    _conftest_app(pytester)
    for extra in ([], ["-n", "2"]):
        r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:randomly", "--inv-ablate", *extra)
        assert r.ret == 0, r.stdout.str() + r.stderr.str()
        r.stdout.fnmatch_lines(["*R-1: load-bearing*", "*caught*test_app.py::test_big*"])


def test_xdist_ablate_with_an_empty_rule_file_is_a_usage_error(pytester):
    pytest.importorskip("xdist")
    _conftest_app(pytester)
    (pytester.path / "empty.json").write_text(json.dumps({"spec": "invariant/v0.1", "rules": []}))
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:randomly", "--inv-ablate", "-n", "2",
                                      "--inv-rules", "empty.json")
    assert r.ret == pytest.ExitCode.USAGE_ERROR and "no rules loaded" in r.stderr.str()


# --- 4: the digest covers the records a rule reads ----------------------------------------------------
def _digest(**kw: Any) -> str:
    return rule_digest(inv.load(m.digest_doc(**kw), install=False).rules["R-1"])


def test_digest_covers_provider_and_target_records():
    a = _digest()
    assert a == _digest()                                    # stable
    assert _digest(provider="cap_b") != a                    # the provider switched to another function
    assert _digest(effects="none") != a                      # the target's record changed


def test_switched_provider_makes_the_ablation_report_stale():
    from invariants.status_report import ablation_state
    reg_a = inv.load(m.digest_doc(), install=False)
    report = {"ablation": {"digests": {"R-1": rule_digest(reg_a.rules["R-1"])},
                           "rules": {"R-1": [{"test": "tests/test_x.py::test_big", "result": "caught"}]}}}
    assert ablation_state(reg_a.rules["R-1"], report) == "yes"
    reg_b = inv.load(m.digest_doc(provider="cap_b"), install=False)
    assert ablation_state(reg_b.rules["R-1"], report) == "no"


# --- 5: a getter's null zone ------------------------------------------------------------------------
def test_getter_null_zone_is_an_error_not_utc():
    import datetime as dt
    t = dt.datetime(2020, 1, 1, 5, tzinfo=dt.timezone.utc)
    assert values.getter("getHours", t) == 5
    with pytest.raises(CelError):
        values.getter("getHours", t, None)
    with pytest.raises(CelError):
        values.getter("getSeconds", dt.timedelta(seconds=1), None)
    doc = {"spec": "invariant/v0.1", "targets": {"py:round29rt.opening": {"effects": "none"}},
           "rules": [{"id": "OPEN", "guard": "py:round29rt.opening",
                      "bad": [{"when": "args.t.getHours(args.tz) < 8", "message": "outside hours"}]}]}
    for be in cel.available_backends():
        reg = inv.load(doc, install=False, backend=be)
        for tz, kind in ((None, "rule_error"), ("UTC", "violation")):
            o = inv.evaluate(reg.rules["OPEN"], {"args": {"t": t, "tz": tz}}, reg=reg)
            assert o.kind == kind, (be, tz, o)


def test_getter_null_zone_on_the_exact_path():
    import datetime as dt
    t = dt.datetime(2020, 1, 1, 9, tzinfo=dt.timezone.utc)
    for be in cel.available_backends():
        p = cel.get_backend(be).compile("t.getHours(z) == 9 && i > d")
        with pytest.raises(CelError):
            p.evaluate({"t": t, "z": None, "i": 2**53 + 1, "d": float(2**53)})


# --- 6 and 8: RE2 details --------------------------------------------------------------------------
def _m(pattern: str, text: str) -> bool:
    found, _ = _regex.search(_regex.compile_pattern(pattern), text, 10**6)
    return found


@pytest.mark.parametrize("pattern,text,want", [
    (r"^\Qab\E*$", "abbb", True), (r"^\Qab\E*$", "abab", False), (r"^\Qab\E*$", "a", True),
    (r"^\Qab\E*$", "", False), (r"^\Qab\E{2}$", "abb", True), (r"^\Qab\E{2}$", "abab", False),
    (r"^\Qa.b\E?$", "a.", True), (r"^\Qa.b\E?$", "axb", False), (r"^(\Qxy\E)+$", "xyxy", True),
    (r"^a\Q\E*$", "", True), (r"^a\Q\E*$", "aaa", True), (r"^a\Q\E{2}$", "aa", True),
    (r"^a\Q\E{2}$", "a", False), (r"^a*\Q\E+$", "", True), (r"(?i)^\Qab\E+$", "ABB", True)])
def test_quote_quantifier_repeats_its_last_character(pattern, text, want):
    assert _m(pattern, text) is want


@pytest.mark.parametrize("pattern", [r"\Q\E?", r"\Q\E*", r"(\Q\E+)", r"a|\Q\E*", r"{2}", r"a|{2}", r"({1,3})",
                                     r"\Q\E{2}"])
def test_quantifier_with_nothing_before_is_refused(pattern):
    with pytest.raises(_regex.RegexError):
        _regex.compile_pattern(pattern)


def test_pattern_limit_counts_the_quote_split():
    # 4,000 quoted characters then {2}: RE2 repeats the last one (4,001 instructions), not all of them
    pattern = r"\Q" + "a" * 4000 + r"\E{2}"
    assert _regex.pattern_size(pattern) <= _regex.MAX_PROGRAM
    _regex.compile_pattern(pattern)
    assert _regex.pattern_size(r"\Q\E") <= 1


@pytest.mark.parametrize("text,pattern,want", [
    ("aéa", r"\B", True), ("a😀a", r"\B", True), ("aéa", r"\B\B", True), ("aéa", r"\B(?:x)?", True),
    ("aéa", r"\B.*", True), ("aéa", r"\B(?:)", True), ("aéa", r"\B$", False), ("aéa", r"\B.", False),
    ("aéa", r"\Ba", False), ("aéa", r"\B\z", False), ("aéa", r"\B\b", False), ("aéa", r"\b\B", False),
    ("aéa", r"^a\B", False), ("aéa", r"\B.*$", False), ("aba", r"\B", True), ("a", r"\B", False),
    ("ab", r"a\B", True), ("é", r"\b", False)])
def test_word_boundaries_follow_re2_bytes(text, pattern, want):
    assert _m(pattern, text) is want
    for be in cel.available_backends():
        got = cel.get_backend(be).compile("x.matches(p)").evaluate({"x": text, "p": pattern})
        assert got is want, be


# --- 7: a view path into a scalar ------------------------------------------------------------------
def test_view_into_a_scalar_parameter_is_a_load_error():
    doc = {"spec": "invariant/v0.1", "targets": {"py:round29rt.refund": {"effects": "none"}},
           "rules": [{"id": "V", "status": "candidate", "subject": {"target": "py:round29rt.refund"},
                      "bad": "x > 1",
                      "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect",
                                     "view": {"x": "args.amount.m"}}]}]}
    with pytest.raises(inv.LoadError, match=r"^V: enforcer 0 \(decorator\) view x: 'args.amount.m' does not resolve"):
        inv.load(doc, install=False)


def test_quote_backends_agree():
    for pattern, text in [(r"^\Qab\E*$", "abbb"), (r"^\Qab\E*$", "abab"), (r"a\Q\E*", "x")]:
        got = {be: cel.get_backend(be).compile("x.matches(p)").evaluate({"x": text, "p": pattern})
               for be in cel.available_backends()}
        assert len(set(got.values())) == 1, (pattern, text, got)

