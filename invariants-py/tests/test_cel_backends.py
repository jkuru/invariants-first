"""The engine backend (cel-expr): selection, front end, value conversion, cost, threads."""
from __future__ import annotations

import dataclasses
import enum
import threading

import pydantic
import pytest

import invariants as inv
from invariants import cel
from invariants.cel import _engine as ep
from invariants.cel import subset
from support import FIXTURES, RULES, raises

ENGINES = [b for b in ("cel-expr",) if b in cel.available_backends()]
ALL = ["subset", *ENGINES]
# The engine tests skip, rather than fail, without the `cel` extra (pip install -e '.[dev,cel]').
needs_engines = pytest.mark.skipif(not ENGINES, reason="needs the cel extra (cel-expr-python)")
needs_cel_expr = pytest.mark.skipif("cel-expr" not in ENGINES, reason="needs cel-expr-python (the cel extra)")


def ev(backend: str, src: str, **env):
    return cel.get_backend(backend).compile(src).evaluate(env)


# ---- selection ---------------------------------------------------------------------------

@pytest.mark.skipif(not __import__("os").environ.get("INV_REQUIRE_ENGINES"),
                    reason="set INV_REQUIRE_ENGINES=1 (CI does) to require the engine")
def test_cel_engines_installed_here():
    assert ENGINES == ["cel-expr"]
    assert set(ALL) <= set(cel.available_backends())


@pytest.mark.parametrize("name", ALL)
def test_cel_backend_from_env(monkeypatch, name):
    monkeypatch.setenv(cel.ENV_VAR, name)
    b = cel.get_backend()
    assert b.name == name and isinstance(b, cel.Backend) and isinstance(b.compile("1 == 1"), cel.Program)


@needs_engines
def test_cel_auto_order(monkeypatch):
    monkeypatch.setenv(cel.ENV_VAR, "auto")
    assert cel.resolve_name() == "cel-expr" and cel.get_backend().name == "cel-expr"
    monkeypatch.setitem(cel._REQUIRES, "cel-expr", "no_such_module_xyz")
    assert cel.resolve_name("auto") == "subset" and cel.get_backend("auto").name == "subset"


def test_cel_missing_engine_is_unavailable(monkeypatch):
    monkeypatch.setitem(cel._REQUIRES, "cel-expr", "no_such_module_xyz")
    assert "cel-expr" not in cel.available_backends()
    e = raises(ValueError, cel.get_backend, "cel-expr")
    assert e is not None and "not installed" in str(e)


def test_cel_register_backend_drops_engine_requirement(monkeypatch):
    monkeypatch.setitem(cel._REQUIRES, "fake", "no_such_module_xyz")
    cel.register_backend("fake", subset.SubsetBackend)
    try:
        assert "fake" in cel.available_backends() and cel.get_backend("fake").name == "subset"
    finally:
        cel._FACTORIES.pop("fake")
        cel._CACHE.pop("fake", None)


@pytest.mark.parametrize("name", ALL)
def test_cel_load_fixture_rules_with_each_backend(name):
    reg = inv.load(RULES, install=False, base_dir=FIXTURES, backend=name)
    base = inv.load(RULES, install=False, base_dir=FIXTURES, backend="subset")
    assert reg.backend_name == name and reg.notes == base.notes and set(reg.rules) == set(base.rules)
    for rid, rule in reg.rules.items():
        assert [p.references() for p, _ in rule.clauses] == [p.references() for p, _ in base.rules[rid].clauses]


@pytest.mark.parametrize("name", ALL)
def test_cel_four_outcomes_through_evaluate(name):
    rule = inv.load(RULES, install=False, base_dir=FIXTURES, backend=name).rules["P-2"]
    assert inv.evaluate(rule, {"args": {"job_id": ""}}).kind == "violation"
    assert inv.evaluate(rule, {"args": {"job_id": "J-1"}}).kind == "ok"
    assert inv.evaluate(rule, {"args": {"job_id": None}}).kind == "rule_error"


# ---- front end ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ENGINES)
def test_cel_engine_references_match_subset(name):
    src = "args.req.pool_id == 1 && old_self.sizes[k] > 0 && result.exists(s, s.x > 0)"
    assert cel.get_backend(name).compile(src).references() == cel.get_backend("subset").compile(src).references()
    assert cel.get_backend(name).compile("inv.unique_by(xs, x, x.k == y)").references() == {("xs",), ("y",)}


@pytest.mark.parametrize("name", ENGINES)
def test_cel_engine_negated_membership(name):
    p = cel.get_backend(name).compile('!(reply.cause in ["A", "B"])')
    assert p.negated_membership() == (("reply", "cause"), ["A", "B"])
    assert cel.get_backend(name).compile("x in [1]").negated_membership() is None


@pytest.mark.parametrize("name", ENGINES)
@pytest.mark.parametrize("src", ["1 +", "has(a)", "inv.sum()", "inv.unique_by(xs, 1, 1)", "[1].all(1, true)"])
def test_cel_engine_syntax_errors(name, src):
    assert raises(cel.CelSyntaxError, cel.get_backend(name).compile, src) is not None


@pytest.mark.parametrize("src", [
    "inv.sum(result) != args.total_jobs || inv.zip(result, args.capacities).exists(p, p[0] < 0 || p[0] > p[1])",
    "!xs.exists_one(x, x.k) && -1 < 0 ? a.b[0] : {'k': [1, 2.5, null, true]}['k'].size() == 4",
    "has(a.b) && !(c in ['x', \"y\"]) && 'q\\'s'.startsWith('q') && -(3) == -3 && 7 % 2 == 1",
])
def test_cel_unparse_round_trips(src):
    tree = subset.parse(src)
    printed = ep.unparse(tree, {"inv.unique": "inv.unique"})
    assert subset.parse(printed) == tree


def test_cel_unparse_renames_extensions():
    t = subset.parse("inv.sum(inv.zip(a, b)) > 0 && inv.unique_by(xs, x, x)")
    out = ep.unparse(t, {"inv.sum": "S", "inv.zip": "Z", "inv.unique": "U"})
    assert out == "((S(Z(a, b))) > (0)) && (U(xs.map(x, x)))"


def test_cel_decode_string():
    bs = chr(92)
    assert subset.decode_string(f"a{bs}n{bs}x41{bs}102{bs}u00e9{bs}U0001F600{bs}{bs}{bs}\"é") == "a\nABé\U0001F600\\\"é"
    assert subset.parse('"é"') == ("lit", "é")


# ---- values reaching the engine ----------------------------------------------------------

class Color(enum.Enum):
    RED = "red"


@dataclasses.dataclass
class Req:
    pool_id: str
    sizes: tuple[int, ...]
    color: Color = Color.RED
    _secret: int = 7


class Model(pydantic.BaseModel):
    name: str
    tags: list[str]


class Service:
    def __init__(self) -> None:
        self.reads = 0
        self.conn = object()        # not convertible; must never be read

    @property
    def sizes(self) -> dict[str, int]:
        self.reads += 1
        return {"p1": 3}

    @property
    def broken(self) -> int:
        raise RuntimeError("db down")

    def method(self) -> int:
        return 1


@pytest.mark.parametrize("name", ALL)
def test_cel_values_from_python_objects(name):
    r = Req("p1", (1, 2))
    assert ev(name, 'r.pool_id == "p1" && r.sizes[1] == 2 && size(r.sizes) == 2', r=r) is True
    assert ev(name, 'm.name == "a" && "x" in m.tags', m=Model(name="a", tags=["x"])) is True
    svc = Service()
    assert ev(name, 'self.sizes["p1"] == 3', self=svc) is True and svc.reads >= 1
    bad = ["r._secret == 7", "self.method == 1", "self.nope == 1", "self.broken == 1"]
    for src in bad:
        with pytest.raises(cel.CelError):
            ev(name, src, r=r, self=svc)


@pytest.mark.parametrize("name", ALL)
def test_cel_enum_compares_as_its_value(name):
    assert ev(name, 'r.color == "red"', r=Req("p", ())) is True
    assert ev(name, 'c in ["red"]', c=Color.RED) is True


@pytest.mark.parametrize("name", ENGINES)
def test_cel_engine_value_conversion(name):
    assert ev(name, "r.sizes", r=Req("p", (1, 2))) == [1, 2]
    with pytest.raises(cel.CelError):
        ev(name, "size(s) == 1", s={1})
    loop: dict = {}
    loop["me"] = loop
    assert ev(name, "size(x) == 1", x=loop) is True      # size() reads only the keys (as on the subset)
    with pytest.raises(cel.CelError):
        ev(name, "x == x", x=loop)


def test_cel_projection_reads_only_referenced_paths():
    svc = Service()
    out = ep.project({"self": svc, "unused": object()}, frozenset({("self", "sizes")}))
    assert out == {"self": {"sizes": {"p1": 3}}} and svc.reads == 1
    assert ep.project({"a": {"b": 1, "c": 2}}, frozenset({("a", "b"), ("a",)})) == {"a": {"b": 1, "c": 2}}
    assert ep.project({"a": {"c": 2}}, frozenset({("a", "b")})) == {"a": {}}
    assert ep.project({"a": None}, frozenset({("a", "b")})) == {"a": None}


@pytest.mark.parametrize("name", ENGINES)
def test_cel_engine_results_are_plain_python(name):
    v = ev(name, '[true, 1, 2.5, "s", null, {"k": [1]}]')
    assert v == [True, 1, 2.5, "s", None, {"k": [1]}]
    assert [type(x) for x in v] == [bool, int, float, str, type(None), dict]
    assert type(v[5]["k"][0]) is int


# ---- cost and concurrency -----------------------------------------------------------------

NESTED = "[1,2,3,4,5,6,7,8,9,10].map(a, [1,2,3,4,5,6,7,8,9,10].map(b, [1,2,3,4,5,6,7,8,9,10].map(c, c)))"


@needs_cel_expr
def test_cel_expr_has_no_configurable_cost_limit():
    b = cel.get_backend("cel-expr")
    assert b.cost_limit is False
    assert len(b.compile(NESTED).evaluate({})) == 10


@pytest.mark.parametrize("name", ALL)
def test_cel_concurrent_evaluation(name):
    prog = cel.get_backend(name).compile("inv.sum(xs) == n && xs.all(x, x >= 0)")
    errors: list[str] = []

    def work(i: int) -> None:
        for j in range(30):
            xs = [i, j, 1]
            if prog.evaluate({"xs": xs, "n": i + j + 1}) is not True or prog.evaluate({"xs": xs, "n": -1}) is not False:
                errors.append(f"{i}/{j}")
    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
