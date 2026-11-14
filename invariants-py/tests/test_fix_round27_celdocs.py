"""Regression tests for fix round 27 (CEL, loader, docs): each test failed before its fix.

1. `isinstance()` in a clause was refused with advice that does not load (`type(x) == T`: the loader
   reads `int` as an undeclared name); the advice is now `type(x) == type(1)`, and a bare type name
   is refused with the same advice.
2. With a `view:` on any enforcer, the rule-level names (`names_for["rule"]`, what
   `inv.evaluate(rule, base)` uses) were only `bind`: an auto-bound provider was missing there, so
   the evaluation was a rule_error.
3. An unknown string escape (`'\\d+'`) was read as the bare letter on the subset, and on cel-expr too
   whenever the front end rewrote the clause (matches(), contains(), ...): `x.matches('\\d+')` was
   `x.matches('d+')`. It is now a parse error on every backend, with a hint (raw strings parse).
   `has(x.f)` returned presence without reading the value, where cel-expr's projection reads x.f
   whole; arithmetic inside a has() argument was not checked finite on cel-expr.
4. Docs and apps: fact-pipeline's dependency and CI match the other apps.

The CEL tests run on every installed backend; the cel-expr-only ones skip without it.
"""
from __future__ import annotations

import dataclasses
import decimal
import itertools
import pathlib
import sys
import textwrap
from typing import Any

import pytest

import invariants as inv
import invariants_cel as cel
from invariants_cel import _engine, available_backends, get_backend
from invariants_cel.errors import CelError, CelSyntaxError

BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]
needs_cel_expr = pytest.mark.skipif("cel-expr" not in BACKENDS, reason="cel-expr not installed")
REPO = pathlib.Path(__file__).resolve().parents[2]

_n = itertools.count()


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def ev(backend: str, expr: str, **env: Any) -> tuple[str, Any]:
    try:
        return "value", get_backend(backend).compile(expr).evaluate(env)
    except CelSyntaxError:
        return "syntax", None
    except CelError:
        return "error", None


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A fresh importable package per test: app(files) -> its package name."""
    made: list[str] = []

    def make(files: dict[str, str]) -> str:
        name = f"r27app{next(_n)}"
        for rel, text in files.items():
            p = tmp_path / name / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(text).replace("PKG", name))
        (tmp_path / name / "__init__.py").touch()
        made.append(name)
        return name

    monkeypatch.syspath_prepend(str(tmp_path))
    yield make
    for m in [m for m in sys.modules if any(m == n or m.startswith(n + ".") for n in made)]:
        del sys.modules[m]


SERVICE = """
import invariants as inv

@inv.provider("so_far")
def refunded_so_far(charge_id: str) -> int:
    return 100

class Svc:
    @inv.guard("R")
    def refund(self, charge_id: str, amount: int) -> None:
        pass
"""


def doc(pkg: str, bad: str, **more: Any) -> dict[str, Any]:
    return {"spec": "invariant/v0.1",
            "providers": {"so_far": {"type": "int", "from": f"py:{pkg}.service.refunded_so_far",
                                     "args": {"charge_id": "args.charge_id"}}},
            "rules": [{"id": "R", "guard": f"py:{pkg}.service.Svc.refund", "bad": bad, **more}]}


def load_error(d: dict[str, Any], backend: str | None = None) -> str:
    with inv.isolated(), pytest.raises(inv.LoadError) as ei:
        inv.load(d, **({"backend": backend} if backend else {}))
    return str(ei.value)


# --- 1. type() advice -------------------------------------------------------------------------------
def test_isinstance_advice_is_a_clause_that_loads(app, backend):
    pkg = app({"service.py": SERVICE})
    msg = load_error(doc(pkg, "isinstance(args.amount, int)"), backend)
    assert "type(x) == type(1)" in msg and "type(x) == T " not in msg, msg
    if backend == "cel-expr":          # the advice itself loads where type() exists
        with inv.isolated():
            inv.load(doc(pkg, "type(args.amount) != type(1)"), backend=backend)


@needs_cel_expr
@pytest.mark.parametrize("name,lit", [("int", "1"), ("double", "1.0"), ("string", "''"), ("google", "timestamp(0)")])
def test_a_bare_type_name_says_how_to_compare_types(app, name, lit):
    pkg = app({"service.py": SERVICE})
    tail = ".protobuf.Timestamp" if name == "google" else ""
    msg = load_error(doc(pkg, f"type(args.amount) == {name}{tail}"), "cel-expr")
    assert f"{name} is a CEL type name, which a clause cannot read" in msg, msg
    assert f"type(x) == type({lit})" in msg
    with inv.isolated():                # the suggested form loads
        inv.load(doc(pkg, f"type(args.amount) == type({lit})"), backend="cel-expr")


# --- 2. the rule-level names with a view ---------------------------------------------------------------
@pytest.mark.parametrize("view", [{}, {"amt": "args.amount"}])
def test_rule_level_names_bind_auto_providers_when_an_enforcer_has_a_view(app, backend, view):
    pkg = app({"service.py": SERVICE})
    d = doc(pkg, "so_far + amt > 150", bind={"amt": "args.amount"}, status="detect",
            enforcers=[{"point": "decorator", "timing": "pre_effect", "mode": "detect", "view": view}])
    del d["rules"][0]["guard"]
    d["rules"][0]["subject"] = {"target": f"py:{pkg}.service.Svc.refund"}
    with inv.isolated():
        reg = inv.load(d, backend=backend)
        rule = reg.rules["R"]
        assert rule.names_for["rule"] == {"amt": "args.amount", "so_far": "provider.so_far"}
        base = {"args": {"charge_id": "c", "amount": 60}}
        assert inv.evaluate(rule, base, reg=reg).kind == "violation"
        assert inv.evaluate(rule, base, 0, reg=reg).kind == "violation"


def test_a_name_only_a_view_declares_is_still_checked_on_each_enforcer(app):
    pkg = app({"service.py": SERVICE})
    d = doc(pkg, "amt > 1", status="detect",
            enforcers=[{"point": "decorator", "timing": "pre_effect", "mode": "detect", "view": {"amt": "args.amount"}},
                       {"point": "monitor", "timing": "post_effect", "mode": "detect"}])
    del d["rules"][0]["guard"]
    d["rules"][0]["subject"] = {"target": f"py:{pkg}.service.Svc.refund"}
    assert "reads undeclared name(s) ['amt']" in load_error(d)


# --- 3. string escapes --------------------------------------------------------------------------------
@pytest.mark.parametrize("expr", [r"x.matches('\d+')", r"x == '\q'", r"'\d' == 'd'", r"x.contains('\s')",
                                  r"x == '\400'", r"x == '\0'", r"x == '\xZZ'", "x == 'a\nb'",
                                  r"x == r'a\'b'", r"x == r'a\'", r'x == R"\"'])
def test_an_escape_cel_does_not_have_is_a_parse_error(backend, expr):
    assert ev(backend, expr, x="123") == ("syntax", None)


def test_the_escape_error_says_how_to_write_a_regex(app, backend):
    pkg = app({"service.py": SERVICE})
    msg = load_error(doc(pkg, r"args.charge_id.matches('^\d+$')"), backend)
    assert r"\d is not a CEL escape" in msg and r"r'\d+'" in msg and r"'\\d+'" in msg, msg


@pytest.mark.parametrize("expr,want", [
    (r"x.matches(r'^\d+$')", True), (r"x.matches(R'^\d+$')", True), (r'x.matches(r"^\d+$")', True),
    (r"x.matches('^\\d+$')", True), (r"x.matches('\\d') && !x.matches('\\D')", True),
    (r"size(r'\n') == 2", True), (r"size('\n') == 1", True), (r"size(r'a\\') == 3", True),
    (r"r'\d' == '\\d'", True), (r"""'\?\`\'\"' == '?`' + "'" + '"'""", True),
    (r"'\a\b\f\n\r\t\v' == '\x07\x08\x0c\x0a\x0d\x09\x0b'", True), (r"'\101\x41A\U00000041' == 'AAAA'", True),
    (r"'\377' == 'ÿ'", True), (r"x.contains(r'\d') || x.startsWith('1\\')", False), (r"r == 1", True),
])
def test_valid_escapes_and_raw_strings_agree_on_every_backend(backend, expr, want):
    assert ev(backend, expr, x="123", r=1) == ("value", want)


@needs_cel_expr
@pytest.mark.parametrize("s", ["d+", "\\d+", "a\"b'c", "\\", "\n\t\x01\x7f", "é😀", "\\\\'", "`?", "\u2028"])
def test_the_rewritten_text_keeps_every_string_exactly(s):
    lit = _engine._lit(s)
    assert cel.subset.parse(lit) == ("lit", s)
    # a clause the front end rewrites for cel-expr (matches/contains): the literal round-trips
    for b in BACKENDS:
        assert get_backend(b).compile(f"x.contains({lit}) && x == {lit}").evaluate({"x": s}) is True


# --- 3b. has() ----------------------------------------------------------------------------------------
@dataclasses.dataclass
class Rec:
    f: Any = None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 2**70, decimal.Decimal("1.5"), {1},
                                 {"h": float("nan")}, {"h": [1, {2}]}], ids=repr)
@pytest.mark.parametrize("shape", ["map", "record"])
def test_has_reads_the_field_whole(backend, bad, shape):
    x = {"f": bad} if shape == "map" else Rec(bad)
    for expr in ("has(x.f)", "has(m[1].f)", "has(xs[0].f) ? 1 == 1 : 1 == 1"):
        assert ev(backend, expr, x=x, m={1: x}, xs=[x]) == ("error", None), expr


def test_has_on_a_generator_field_is_an_error(backend):
    assert ev(backend, "has(x.f)", x={"f": (i for i in range(2))}) == ("error", None)


@pytest.mark.parametrize("ok", [None, 1, "s", [1], {"h": 1}, Rec(1)], ids=repr)
def test_has_of_a_plain_present_field_is_true(backend, ok):
    assert ev(backend, "has(x.f)", x={"f": ok}) == ("value", True)
    assert ev(backend, "has(x.g)", x={"f": ok}) == ("value", False)


@pytest.mark.parametrize("expr", ["has({'a': {'f': double(s)}}.a.f)", "has({'a': {'f': d * d}}.a.f)",
                                  "has({'a': {'f': 1.0 / z}}.a.f)"])
def test_arithmetic_inside_has_is_checked_finite(backend, expr):
    assert ev(backend, expr, s="NaN", d=1e300, z=0.0) == ("error", None)


def test_the_finite_rewrite_keeps_has_a_selection():
    tree = cel.subset.parse("has({'a': {'f': d * d}}.a.f)")
    marked = _engine.mark_finite(tree)
    assert marked[0] == "call" and marked[1] == "has" and marked[3][0][0] == "sel"
    assert "fin" in repr(marked)


# --- 4. apps and docs ---------------------------------------------------------------------------------
def test_every_app_depends_on_the_library_the_same_way():
    for a in ("pool-service", "fact-pipeline", "support-agent"):
        text = (REPO / "apps" / a / "pyproject.toml").read_text()
        assert '"invariants-first[' in text and "]>=0.1.0" in text, a
        assert '"invariants-first"' not in text, a


def test_every_app_ci_reports_standing():
    for a in ("pool-service", "fact-pipeline", "support-agent"):
        text = (REPO / "apps" / a / "ci.sh").read_text()
        assert '"$INV" status' in text, a
        assert text.index('"$INV" status') < text.index("-m pytest"), a


def test_docs_name_no_removed_mode_or_malformed_install():
    start = (REPO / "invariants-py" / "docs" / "start.md").read_text()
    assert "pip install -e . invariants-first" not in start
    evaluation = (REPO / "invariants-py" / "docs" / "evaluation.md").read_text()
    assert "found with `--sql`" not in evaluation and "since removed" in evaluation
    import invariants_scan as scan
    scan_md = (REPO / "invariants-py" / "docs" / "scan.md").read_text()
    lock_md = (REPO / "invariants-py" / "docs" / "lockfile.md").read_text()
    assert "`invariants.scan.SKIP_DIRS`" in scan_md and "`invariants.scan.SKIP_DIRS`" in lock_md
    assert "SKIP_DIRS" in scan.__all__
    for name in sorted(scan.SKIP_DIRS):
        assert f"`{name}`" in scan_md


def test_the_design_guide_module_table_names_every_submodule():
    src = REPO / "invariants-py" / "src" / "invariants"
    guide = (REPO / "invariants-py" / "docs" / "design-guide.md").read_text()
    table = guide[guide.index("| Module | Does |"):guide.index("### 11.2 Commands")]
    for pkg in ("loader", "registry", "guard", "lock", "bootstrap", "cli", "mcp", "pytest_plugin", "integrations"):
        for f in sorted((src / pkg).glob("*.py")):
            if f.stem in ("__init__", "__main__"):
                continue
            assert f"`{f.stem}`" in table or f"`{pkg.split('/')[-1]}.{f.stem}`" in table, f"{pkg}/{f.name}"
    for f in sorted(src.glob("*.py")):
        if f.stem != "__init__":
            assert f"`{f.name}`" in table, f.name
    # the three libraries (packages/), each a row of its own, with every module
    for lib, dist in (("invariants_cel", "invariants-cel"), ("invariants_scan", "invariants-scan"),
                      ("invariants_schema_diff", "invariants-schema-diff")):
        row = next(line for line in table.splitlines() if line.startswith(f"| `{lib}` (`{dist}`) |"))
        top = REPO / "packages" / dist / "src" / lib
        for f in sorted(top.rglob("*.py")):
            if f.stem in ("__init__", "__main__"):
                continue
            sub = f.parent.relative_to(top).as_posix()
            assert f"`{f.stem}`" in row or f"`{sub}.{f.stem}`" in row, f"{lib}/{f.relative_to(top)}"
