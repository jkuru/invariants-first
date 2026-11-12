"""Fix round 14 (certification round 11 findings): literals in `bad` keep their CEL type, so true, 1
and 1.0 never compare equal (R11-1); scaling by a factor of unknown sign has no direction (R11-2);
mirrored comparisons, De Morgan, `!!`, integer `> n` as `>= n+1`, renamed comprehension variables
and repeated disjuncts are equivalent (R11-3); `.__call__` and other dunder reads on sinks and
helpers are seen by the scan (R11-4); `inv` imports this checkout's source ahead of site-packages
(R11-5); `--check` validates the v3 schemas table (R11-6); diff and residual-path nits (R11-7)."""
from __future__ import annotations

import json
import pathlib
import textwrap

import pytest

from invariants import cli, lockfile, scan
from invariants import typecheck as tc
from invariants.cel import subset
from cli_support import run_inv, write_project

ROOT = pathlib.Path(__file__).resolve().parents[1]


# --- R11-1: type-tagged literals -------------------------------------------------------------------------
def test_literals_are_tagged_with_their_type():
    canon = [lockfile.canon_tree(subset.parse(t)) for t in ("x == true", "x == 1", "x == 1.0")]
    assert canon[0] != canon[1] != canon[2] != canon[0]
    assert lockfile.canon_tree(subset.parse("x == false")) != lockfile.canon_tree(subset.parse("x == 0"))
    assert lockfile.canon_tree(subset.parse("1.0 / x > -0.0")) != lockfile.canon_tree(subset.parse("1.0 / x > 0.0"))
    assert lockfile.canon_tree(subset.parse("x > -0")) == lockfile.canon_tree(subset.parse("x > 0"))   # int -0 is 0


@pytest.mark.parametrize("old,new", [
    ("args.m.approved == true", "args.m.approved == 1"),
    ("args.d == 1", "args.d == true"),
    ("args.d in [false, 5]", "args.d in [0, 5]"),
    ("args.m.k == (c ? true : false)", "args.m.k == (c ? 1 : 0)"),
    ("args.d / 2 > 2", "args.d / 2.0 > 2"),
    ("args.d == 2", "args.d == 2.0"),
    ("args.m.flags.exists(f, f == true)", "args.m.flags.exists(f, f == 1)"),
])
def test_a_literal_of_another_type_is_not_equivalent(old, new):
    assert lockfile.compare_bad(old, new, subset.parse) == "unknown"
    [c] = lockfile._bad_changes("rule R", old, new)
    assert c.weakened and "equivalent" not in c.what


def test_a_type_change_hidden_beside_a_stronger_edit_is_not_stronger():
    assert lockfile.compare_bad("args.n > 5 && args.m.ok == false", "args.n > 4 && args.m.ok == 0",
                                subset.parse) == "unknown"


def test_a_threshold_of_another_numeric_type_has_no_direction():
    assert lockfile.compare_bad("a > 5", "a > 6.0", subset.parse) == "unknown"
    assert lockfile.compare_bad("a > 5", "a > 6", subset.parse) == "weaker"


# --- R11-2: the sign of a factor ---------------------------------------------------------------------------
@pytest.mark.parametrize("old,new,want", [
    ("args.n * 2 > -10", "args.n * 3 > -10", "unknown"),      # n = -4: the old rule fires, the new one does not
    ("a > 4 * b", "a > 10 * b", "unknown"),
    ("args.x / 2 > 1", "args.x / 3 > 1", "unknown"),
    ("args.x / args.k > 1", "args.x / args.k + 1 > 1", "unknown"),
    ("size(xs) * 2 > 5", "size(xs) * 3 > 5", "stronger"),
    ("size(xs) * 3 > 5", "size(xs) * 2 > 5", "weaker"),
    ("a > 4 * size(b)", "a > 10 * size(b)", "weaker"),
    ("a > 2 * 3", "a > 2 * 4", "weaker"),                    # a literal times a literal
    ("a > -2 * 3", "a > -2 * 4", "unknown"),
    ("size(xs) / 2 > 1", "size(xs) / 3 > 1", "weaker"),
    ("uint(a) * 2 > 5", "uint(a) * 3 > 5", "unknown"),    # R12-1: a larger factor may overflow
    ("a + 1 > 5", "a + 2 > 5", "unknown"),                   # R12-1: a larger addend may overflow
    ("a - 1 > 5", "a - 2 > 5", "weaker"),
])
def test_scaling_needs_a_provably_non_negative_factor(old, new, want):
    assert lockfile.compare_bad(old, new, subset.parse) == want


def test_a_uint_typed_field_is_non_negative():
    typer = lockfile.make_typer([{"args": tc.T({"map"}, fields={"u": tc.UINT, "n": tc.INT})}])
    u = lockfile.canon_tree(subset.parse("args.u"), typer)
    n = lockfile.canon_tree(subset.parse("args.n"), typer)
    assert lockfile._nonneg(u, typer) and not lockfile._nonneg(n, typer)
    # the polarity is decided, but since R12-1 a larger factor can overflow, so it is not "stronger"
    assert lockfile.compare_bad("args.u * 2 > 5", "args.u * 3 > 5", subset.parse, typer) == "unknown"
    assert lockfile.compare_bad("args.n * 2 > 5", "args.n * 3 > 5", subset.parse, typer) == "unknown"


# --- R11-3: equivalent rewrites ----------------------------------------------------------------------------
ENV = {"args": tc.T({"map"}, fields={"n": tc.INT, "size": tc.INT, "x": tc.DOUBLE, "b": tc.BOOL,
                                     "xs": tc.T({"list"}, elem=tc.INT)})}
TYPED = lockfile.make_typer([ENV])


@pytest.mark.parametrize("old,new", [
    ("args.size > 10000", "10000 < args.size"),
    ("args.size < 1 || args.size > 10000", "1 > args.size || 10000 < args.size"),
    ("args.size <= 5", "5 >= args.size"),
    ("!(a && b)", "!a || !b"),
    ("!(a || b || c)", "!a && !b && !c"),
    ("!(a == b)", "a != b"),
    ("!(a != b)", "a == b"),
    ("!!(args.n > 5)", "args.n > 5"),
    ("!!has(a.b)", "has(a.b)"),
    ("args.xs.exists(x, x > 5)", "args.xs.exists(y, y > 5)"),
    ("args.xs.exists(a, args.xs.all(b, a >= b))", "args.xs.exists(p, args.xs.all(q, p >= q))"),
    ("xs.exists(a, a.exists(a, a > 0))", "xs.exists(p, p.exists(q, q > 0))"),
    ("args.n > 5 || args.n > 5", "args.n > 5"),
    ("a > 1 || b || a > 1", "b || a > 1"),
    ("a > 1 && a > 1 && b", "b && a > 1"),
    ("x in [1, 2, 2]", "x in [2, 1]"),
])
def test_rewrites_that_hold_for_every_value_are_the_same(old, new):
    assert lockfile.compare_bad(old, new, subset.parse) == "same"


# Declared `int` annotations are not trusted: Python does not enforce them, so 10000.5 can arrive.
@pytest.mark.parametrize("old,new", [
    ("args.size > 10000", "args.size >= 10001"),
    ("args.size < 1 || args.size > 10000", "args.size < 1 || args.size >= 10001"),
    ("args.size < 1", "args.size <= 0"),
    ("args.size < 1 || args.size > 10000", "!(args.size >= 1 && args.size <= 10000)"),
    ("!(args.n < 3)", "args.n >= 3"),
])
def test_integer_rewrites_on_declared_int_fields_are_not_trusted(old, new):
    assert lockfile.compare_bad(old, new, subset.parse, TYPED) != "same"


@pytest.mark.parametrize("old,new", [
    ("size(args.xs) > 3", "size(args.xs) >= 4"),
    ("size(xs) > 3", "4 <= size(xs)"),
    ("!!args.b", "args.b"),
    ("args.xs.exists(v, size(args.xs) > 2 && v > 1)", "args.xs.exists(w, size(args.xs) >= 3 && w > 1)"),
])
def test_integer_rewrites_need_provable_integers(old, new):
    assert lockfile.compare_bad(old, new, subset.parse, TYPED) == "same"


@pytest.mark.parametrize("old,new", [
    ("args.x > 5.0", "args.x >= 6.0"),                         # a double: 5.5
    ("args.x < 1.0 || args.x > 10.0", "!(args.x >= 1.0 && args.x <= 10.0)"),   # NaN
    ("!(args.x < 1.0)", "args.x >= 1.0"),
    ("args.d > 5", "args.d >= 6"),                             # dyn
    ("!!args.d", "args.d"),                                    # dyn: !! of a non-bool is an error
    ("args.xs.exists(n, n > 5)", "args.xs.exists(n, n >= 6)"),   # the macro variable is not args.n
    ("args.n > 9223372036854775807", "args.n >= 9223372036854775808"),
])
def test_rewrites_that_need_a_type_are_not_assumed(old, new):
    assert lockfile.compare_bad(old, new, subset.parse, TYPED) != "same"


def test_without_declared_types_a_field_is_not_an_integer():
    assert lockfile.compare_bad("args.size > 10000", "args.size >= 10001", subset.parse) == "unknown"


def test_swapped_macro_variables_differ():
    assert lockfile.compare_bad("xs.exists(a, xs.all(b, a >= b))", "xs.exists(a, xs.all(b, b >= a))",
                                subset.parse) == "unknown"


def test_a_repeated_clause_removed_is_not_weakened():
    got = lockfile._bad_changes("rule R", [{"when": "a > 1", "message": "m"}, {"when": "a > 1", "message": "n"}],
                                [{"when": "a > 1", "message": "m"}])
    assert got and not any(c.weakened for c in got), [str(c) for c in got]
    assert "repeated clause removed" in str(got[0]) and "fewer" not in str(got[0])
    got = lockfile._bad_changes("rule R", ["a > 1 || a > 1"], ["a > 1"])
    assert [c.weakened for c in got] == [False] and "equivalent" in got[0].what


def test_regrouped_arithmetic_is_not_the_same():
    # floating-point rounding and int overflow depend on the grouping
    assert lockfile.compare_bad("x + 1.0 + 1.0 > 9007199254740992.0", "x + (1.0 + 1.0) > 9007199254740992.0",
                                subset.parse) == "unknown"
    assert lockfile.compare_bad("n + 9223372036854775807 + -9223372036854775807 > 0",
                                "n + (9223372036854775807 + -9223372036854775807) > 0", subset.parse) == "unknown"
    assert lockfile.compare_bad("x + 1.0 > 2.0", "1.0 + x > 2.0", subset.parse) == "same"


MOD = """
from typing import Any
import invariants as inv

@inv.guard("C-1")
def f(n: int = 0, x: float = 0.0, m: dict[str, Any] = {}) -> None:
    pass
"""
RULES = """spec: invariant/v0.1
targets:
  "py:mod14.f": {effects: none}
rules:
  - id: C-1
    rev: 1
    status: enforce
    subject: {target: "py:mod14.f"}
    bad: BAD
    enforcers:
      - {point: decorator, timing: pre_effect, mode: enforce}
    falsified_by: ["t::t"]
"""


def _locked(tmp_path, bad):
    root = write_project(tmp_path / "p", {"mod14.py": MOD, "invariants.yaml": RULES.replace("BAD", json.dumps(bad))})
    proc = run_inv("lock", root / "invariants.yaml")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return root


def _relock(root, bad):
    (root / "invariants.yaml").write_text(RULES.replace("BAD", json.dumps(bad)))
    return run_inv("lock", root / "invariants.yaml")


@pytest.mark.parametrize("old,new,ok", [
    ("args.m.approved == true", "args.m.approved == 1", False),             # R11-1, the repro
    ("args.n < 1 || args.n > 10000", "!(args.n >= 1 && args.n <= 10000)", False),   # NaN/float can arrive
    ("args.n < 1 || args.n > 10000", "args.n < 1 || args.n >= 10001", False),
    ("args.n < 1 || args.n > 10000", "1 > args.n || 10000 < args.n", True),
    ("args.x > 5.0", "args.x >= 6.0", False),
    ("args.n * 2 > -10", "args.n * 3 > -10", False),                        # R11-2, the repro
])
def test_plain_lock_uses_the_declared_types(tmp_path, old, new, ok):
    root = _locked(tmp_path, old)
    proc = _relock(root, new)
    out = proc.stdout + proc.stderr
    if ok:
        assert proc.returncode == 0 and "WEAKENED" not in out and "equivalent rewrite" in out, out
    else:
        assert proc.returncode == 1 and "refused:" in proc.stderr and "WEAKENED" in out, out


# --- R11-4: dunder calls in the scan ------------------------------------------------------------------------
STORE = textwrap.dedent("""
    def sink(c):
        c.execute("INSERT INTO leases VALUES (1)")
    def only_door(c):
        c.execute("UPDATE leases SET c = 3")
    def door(c):
        only_door(c)
        sink(c)
    """)


def _doc():
    return {"targets": {"py:app.store.door": {"effects": "external"}},
            "rules": [{"id": "T-1", "status": "enforce", "subject": {"target": "py:app.store.door"},
                       "bad": "false", "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}],
                       "mediation": {"sink": "py:app.store.sink", "door": "py:app.store.door", "tables": ["leases"]}}]}


def _scan(tmp_path, body):
    files = {"app/__init__.py": "", "app/store.py": STORE,
             "app/bypass.py": "import operator\nfrom app import store\n\ndef x(c):\n    " + body + "\n"}
    return {(f.code, f.file) for f in scan.scan(_doc(), [write_project(tmp_path, files)]).findings}


@pytest.mark.parametrize("body,want", [
    ("store.sink(c)", ("bypass", "app/bypass.py")),
    ("store.sink.__call__(c)", ("bypass", "app/bypass.py")),
    ("store.sink.__call__.__call__(c)", ("bypass", "app/bypass.py")),
    ("getattr(store.sink, '__call__')(c)", ("bypass", "app/bypass.py")),
    ("store.only_door.__call__(c)", ("forbidden/raw-sql", "app/store.py")),
    ("getattr(store.only_door, '__call__')(c)", ("forbidden/raw-sql", "app/store.py")),
    ("store.sink.__get__(1)(c)", ("unresolved/sink-escapes", "app/bypass.py")),
    ("f = store.sink.__call__; f(c)", ("unresolved/sink-escapes", "app/bypass.py")),
    ("store.only_door.__func__", ("forbidden/raw-sql", "app/store.py")),
    ("getattr(store.only_door, '__get__')(1)(c)", ("forbidden/raw-sql", "app/store.py")),
    ("operator.methodcaller('__call__', c)(store.sink)", ("unresolved/sink-escapes", "app/bypass.py")),
    ("store.sink.__wrapped__(c)", ("forbidden/unwrap", "app/bypass.py")),
    ("getattr(store.sink, '__wrapped__')(c)", ("forbidden/unwrap", "app/bypass.py")),
    ("operator.methodcaller('__wrapped__')", ("forbidden/unwrap", "app/bypass.py")),
])
def test_dunder_spellings_of_a_call_are_seen(tmp_path, body, want):
    assert want in _scan(tmp_path, body)


def test_harmless_dunders_do_not_escape(tmp_path):
    got = _scan(tmp_path, "print(store.sink.__name__, store.only_door.__doc__)")
    assert not {code for code, _ in got} & {"bypass", "unresolved/sink-escapes", "forbidden/raw-sql"}, got


# --- R11-5: import from this checkout -----------------------------------------------------------------------
PYPROJECT = '[project]\nname = "demo"\nversion = "0"\n[tool.setuptools.packages.find]\nwhere = ["src"]\n'
DEMO = """
import invariants as inv

@inv.guard("D-1")
def f(n: TYPE = 0) -> None:
    pass
"""
DEMO_RULES = """spec: invariant/v0.1
targets:
  "py:demo14.f": {effects: none}
rules:
  - id: D-1
    rev: 1
    status: enforce
    subject: {target: "py:demo14.f"}
    bad: 'args.n < 0'
    enforcers:
      - {point: decorator, timing: pre_effect, mode: enforce}
    falsified_by: ["t::t"]
"""


def test_import_roots_put_this_checkouts_source_first(tmp_path):
    root = write_project(tmp_path / "proj", {"pyproject.toml": PYPROJECT, "src/demo14/__init__.py": "",
                                             "rules/invariants.yaml": DEMO_RULES})
    got = cli.import_roots(root / "rules" / "invariants.yaml", [str(tmp_path / "extra")])
    assert got[0] == (tmp_path / "extra").resolve()
    assert got[1] == (root / "src").resolve()
    assert (root / "rules").resolve() in got and root.resolve() in got
    assert cli.project_root(root / "rules" / "invariants.yaml") == root.resolve()


def test_lock_pins_this_checkout_not_another_on_the_path(tmp_path, monkeypatch):
    other = write_project(tmp_path / "other", {"src/demo14.py": DEMO.replace("TYPE", "int")})
    root = write_project(tmp_path / "proj", {"pyproject.toml": PYPROJECT, "src/demo14.py": DEMO.replace("TYPE", "int"),
                                             "invariants.yaml": DEMO_RULES})
    monkeypatch.setenv("PYTHONPATH", str(other / "src"))      # like an editable install of another checkout
    rf = root / "invariants.yaml"
    assert run_inv("lock", rf).returncode == 0
    (root / "src" / "demo14.py").write_text(DEMO.replace("TYPE", "float"))
    proc = run_inv("lock", "--check", rf)
    assert proc.returncode == 1 and "'int' -> 'float'" in proc.stderr, proc.stdout + proc.stderr
    assert "warning" not in proc.stderr


def test_a_module_imported_from_outside_the_checkout_is_warned_about(tmp_path, monkeypatch):
    other = write_project(tmp_path / "other", {"demo14.py": DEMO.replace("TYPE", "int")})
    root = write_project(tmp_path / "proj", {"pyproject.toml": PYPROJECT, "invariants.yaml": DEMO_RULES})
    monkeypatch.setenv("PYTHONPATH", str(other))
    proc = run_inv("lock", root / "invariants.yaml")
    assert proc.returncode == 0, proc.stderr
    assert "warning: module demo14 was imported from" in proc.stderr and "--path" in proc.stderr
    proc = run_inv("verify", root / "invariants.yaml")
    assert "warning: module demo14" in proc.stderr
    proc = run_inv("lock", "--check", "--path", str(other), root / "invariants.yaml")
    assert proc.returncode == 0 and "warning" not in proc.stderr, proc.stderr


def test_the_readme_documents_path():
    readme = (ROOT / "docs" / "lockfile.md").read_text()          # moved out of the README with the CLI detail
    assert "[--path DIR]..." in readme and "ahead of\nsite-packages" in readme


# --- R11-6: the schemas table ------------------------------------------------------------------------------
C2 = """  - id: C-2
    rev: 1
    status: enforce
    subject: {target: "py:mod14.g"}
    bad: 'args.r.a < 0'
    enforcers:
      - {point: decorator, timing: pre_effect, mode: enforce}
    falsified_by: ["t::t"]
"""

@pytest.fixture
def locked(tmp_path):
    root = write_project(tmp_path / "s", {"mod14.py": MOD + textwrap.dedent("""
        import dataclasses

        @dataclasses.dataclass
        class Row:
            a: int
            b: str

        @inv.guard("C-2")
        def g(r: Row) -> None:
            pass
        """), "invariants.yaml": RULES.replace("BAD", "'args.n < 0'") + C2})
    proc = run_inv("lock", root / "invariants.yaml")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lock = json.loads((root / "invariants.lock").read_text())
    assert lock["schemas"] and lockfile.schema_problems(lock) == []
    return root


def _tamper(root, fn):
    p = root / "invariants.lock"
    lock = json.loads(p.read_text())
    fn(lock)
    p.write_text(json.dumps(lock))
    return lock


@pytest.mark.parametrize("fn,msg", [
    (lambda d: next(iter(d["schemas"].values())).__setitem__("x", 1), "is not the sha256 of its body"),
    (lambda d: d["schemas"].pop(next(iter(d["schemas"]))), "names no entry in the schemas table"),
    (lambda d: d["schemas"].__setitem__(lockfile._sha(lockfile.canonical({"type": "string"})), {"type": "string"}),
     "no entry refers to it"),
])
def test_check_validates_the_schemas_table(locked, fn, msg):
    lock = _tamper(locked, fn)
    assert any(msg in p for p in lockfile.schema_problems(lock))
    proc = run_inv("lock", "--check", locked / "invariants.yaml")
    assert proc.returncode == 1 and "schemas table is damaged" in proc.stderr and msg in proc.stderr, proc.stderr
    proc = run_inv("lock", locked / "invariants.yaml")                  # a plain lock rewrites it
    assert proc.returncode == 0 and "damaged" in proc.stdout, proc.stdout + proc.stderr
    assert run_inv("lock", "--check", locked / "invariants.yaml").returncode == 0


def test_older_locks_have_no_table_to_check():
    assert lockfile.schema_problems({"lock_version": 2, "schemas": {"sha256:x": {}}}) == []


# --- R11-7: diff text and residual paths --------------------------------------------------------------------
def test_a_replaced_list_member_prints_as_old_to_new():
    got = [str(c) for c in lockfile.changes({"targets": {"t": {"types": {"return": {"args": ["int"]}}}}},
                                            {"targets": {"t": {"types": {"return": {"args": ["float"]}}}}})]
    assert got == ['target t: types.return.args: ["int"] -> ["float"]']
    got = [str(c) for c in lockfile.changes({"targets": {"t": {"types": {"a": ["int"]}}}},
                                            {"targets": {"t": {"types": {"a": ["int", "str"]}}}})]
    assert got == ['target t: types.a added: (none) -> ["str"]']


def test_removing_a_residual_path_is_weakened():
    def med(paths):
        return {"rules": {"R": {"semantic": {"mediation": {"sink": "py:s", "residual_paths": paths}}}}}
    [c] = lockfile.changes(med(["a DBA with shell access"]), med([]))
    assert c.weakened and "standing" in c.note
    [c] = lockfile.changes(med([]), med(["a DBA with shell access"]))
    assert not c.weakened
    [c] = lockfile.changes(med(["a"]), med(["a", "b"]))
    assert not c.weakened
    assert lockfile.policy("rule.mediation.residual_paths") == lockfile.ANALYSED


def test_the_readme_has_no_development_history():
    readme = (ROOT / "README.md").read_text().lower()
    assert "fix round" not in readme and "certification round" not in readme
