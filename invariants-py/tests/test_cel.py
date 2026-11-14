"""CEL adapter: backend protocol and selection, and the subset evaluator."""
from __future__ import annotations

import dataclasses

import pytest

import invariants as inv
import invariants_cel as cel
from invariants_cel import subset
from support import FIXTURES, RULES, raises


def ev(src: str, **env):
    return cel.get_backend("subset").compile(src).evaluate(env)


def test_default_backend_is_subset(monkeypatch):
    monkeypatch.delenv(cel.ENV_VAR, raising=False)
    b = cel.get_backend()
    assert b.name == "subset" and isinstance(b, cel.Backend)
    assert isinstance(b.compile("1 == 1"), cel.Program)
    assert "subset" in cel.available_backends()


def test_backend_from_env_and_unknown(monkeypatch):
    monkeypatch.setenv(cel.ENV_VAR, "nope")
    assert raises(ValueError, cel.get_backend) is not None
    assert raises(ValueError, cel.get_backend, "nope") is not None


def test_register_backend_and_load_with_it():
    compiled = []

    class Recording(subset.SubsetBackend):
        name = "recording"

        def compile(self, expr):
            compiled.append(expr)
            return super().compile(expr)
    cel.register_backend("recording", Recording)
    try:
        reg = inv.load(RULES, install=False, base_dir=FIXTURES, backend="recording")
        assert reg.backend_name == "recording" and "leased + n > size" in compiled
        reg2 = inv.load(RULES, install=False, base_dir=FIXTURES, backend=Recording())
        assert reg2.backend_name == "recording"
    finally:
        cel._FACTORIES.pop("recording")
        cel._CACHE.pop("recording", None)


def test_references():
    p = cel.get_backend().compile("args.req.pool_id == 1 && old_self.sizes[k] > 0 && result.exists(s, s.x > 0)")
    assert p.references() == {("args", "req", "pool_id"), ("old_self", "sizes"), ("k",), ("result",)}
    assert cel.get_backend().compile("inv.sum(xs) > 0").references() == {("xs",)}
    assert subset.free_names(subset.parse("[a, {b: c}]")) == {"a", "b", "c"}


def test_negated_membership():
    p = cel.get_backend().compile('!(reply.cause in ["A", "B"])')
    assert p.negated_membership() == (("reply", "cause"), ["A", "B"])
    assert cel.get_backend().compile("reply.cause in ['A']").negated_membership() is None


@pytest.mark.parametrize("src,env,want", [
    ("1 + 2 * 3", {}, 7), ("7 / 2", {}, 3), ("-7 / 2", {}, -3), ("-7 % 2", {}, -1), ("7.0 / 2.0", {}, 3.5),
    ("'a' + \"b\"", {}, "ab"), ("[1] + [2]", {}, [1, 2]), ("size('abc')", {}, 3), ("x.size()", {"x": [1, 2]}, 2),
    ("'abc'.contains('b') && 'abc'.startsWith('a') && 'abc'.endsWith('c')", {}, True),
    ("x ? 1 : 2", {"x": False}, 2), ("1 in [1, 2]", {}, True), ("'k' in {'k': 1}", {}, True),
    ("[1, 2, 3].filter(v, v > 1)", {}, [2, 3]), ("[1, 2].map(v, v * 2)", {}, [2, 4]),
    ("[1, 2].all(v, v > 0)", {}, True), ("[1, 2].exists_one(v, v > 1)", {}, True),
    ("inv.unique_by([1, 2, 1], v, v)", {}, False), ("inv.unique_by([1, 2], v, v)", {}, True),
    ("inv.zip([1], [2])", {}, [[1, 2]]), ("inv.sum([1, 2.5])", {}, 3.5),
    ("1 == 1.0", {}, True), ("1 == 'a'", {}, False), ("has(x.y)", {"x": {"y": 1}}, True),
    ("has(x.y)", {"x": {}}, False), ("x.y", {"x": {"y": 2}}, 2), ("m['a']", {"m": {"a": 3}}, 3),
    ("true || 1 / 0 == 1", {}, True), ("1 / 0 == 1 || true", {}, True), ("false && x.nope", {"x": {}}, False),
    ("null == null", {}, True),
])
def test_subset_evaluates(src, env, want):
    assert ev(src, **env) == want


@pytest.mark.parametrize("src,env", [
    ("1 / 0", {}), ("1 + 1.0", {}), ("x.nope", {"x": {}}), ("y", {}), ("[1][3]", {}), ("m['b']", {"m": {}}),
    ("!1", {}), ("-'a'", {}), ("1 < 'a'", {}), ("inv.zip([1], [1, 2])", {}), ("inv.sum(['a'])", {}),
    ("size(1)", {}), ("nope()", {}), ("1 / 0 == 1 || false", {}), ("x._private", {"x": object()}),
])
def test_subset_errors_are_cel_errors(src, env):
    with pytest.raises(cel.CelError):
        ev(src, **env)


@pytest.mark.parametrize("src", ["a +", "has(a)", "x.exists(1, true)", "a $ b", "(a", "a b"])
def test_subset_syntax_errors(src):
    with pytest.raises(cel.CelSyntaxError):
        cel.get_backend().compile(src)


def test_subset_cost_limit():
    p = subset.SubsetBackend(budget=50).compile("[1,2,3,4,5,6,7,8,9,10].map(a, [1,2,3,4,5].map(b, a * b))")
    with pytest.raises(cel.CelError, match="cost limit"):
        p.evaluate({})


def test_subset_reads_dataclass_fields_not_methods():
    @dataclasses.dataclass
    class D:
        x: int

        def m(self) -> int:
            return 1
    assert ev("d.x", d=D(3)) == 3
    with pytest.raises(cel.CelError):
        ev("d.m", d=D(3))
