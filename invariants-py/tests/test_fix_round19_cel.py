"""Fix round 19 (CEL parity, mapping types): each test failed before its fix.

1. Number literals follow CEL's grammar on every backend: an int literal outside int64 is a
   CelSyntaxError (only `-9223372036854775808`, one minus sign directly on the literal, reaches
   INT_MIN, as in cel-cpp); scientific (1e5, .5e-3) and hex (0x10) literals compile; a uint
   literal (1u, 0x10u) is uint(n) and outside uint64 a CelSyntaxError; a double literal that
   overflows to Infinity is a CelSyntaxError (the library refuses non-finite doubles).
2. A collections.abc.Mapping that is not a dict (MappingProxyType, a custom Mapping) is a CEL map
   on the subset as on cel-expr (field selection, index, size, in, has, macros); providers.walk
   follows its keys; a snapshot of one is plain data, copied as a dict.

Every CEL test runs on every installed backend (parametrised), whatever INV_CEL_BACKEND says.
"""
from __future__ import annotations

import types
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from invariants import providers, snapshot
from invariants.cel import available_backends, get_backend
from invariants.cel.errors import CelError, CelSyntaxError

BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]
INT_MIN, INT_MAX = -2**63, 2**63 - 1


def run(backend: str, expr: str, env: dict[str, Any] | None = None) -> Any:
    return get_backend(backend).compile(expr).evaluate(env or {})


# --- 1. number literals ---------------------------------------------------------------------------
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("expr", [
    "9223372036854775808", "-9223372036854775809", "-(9223372036854775808)", "--9223372036854775808",
    "- -9223372036854775808", "---9223372036854775808", "1 - 9223372036854775808", "1-9223372036854775808",
    "[9223372036854775808]", "0x8000000000000000", "0xffffffffffffffffff", "-0x8000000000000001",
    "18446744073709551616u", "0x10000000000000000u", "uint(18446744073709551615)",
    "1e400", "-1e400", "1.5e999", "1e", "1e+", "0x", "0xg", "1.5u", "1e5u", "1.", "1_000", "0x1p3", "a.5",
])
def test_bad_number_literals_do_not_compile(backend: str, expr: str) -> None:
    with pytest.raises(CelSyntaxError):
        get_backend(backend).compile(expr)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("expr,want", [
    ("9223372036854775807", INT_MAX), ("-9223372036854775808", INT_MIN), ("- 9223372036854775808", INT_MIN),
    ("-0x8000000000000000", INT_MIN), ("0x7fffffffffffffff", INT_MAX), ("1 + -9223372036854775808", INT_MIN + 1),
    ("-9223372036854775808 + 1", INT_MIN + 1), ("[-9223372036854775808][0]", INT_MIN), ("--1", 1), ("---1", -1),
    ("1e5", 100000.0), ("1E5", 100000.0), ("1e+5", 100000.0), ("1e-3", 0.001), ("1.5e3", 1500.0), ("1.5E+3", 1500.0),
    ("2E-2", 0.02), (".5", 0.5), ("-.5", -0.5), ("-1e5", -100000.0), ("1.0e-400", 0.0), (".5e1", 5.0),
    ("0x10", 16), ("0X1f", 31), ("0xFF", 255), ("0x010", 16), ("007", 7),
    ("1e5 == 100000.0", True), ("0x10 == 16", True), ("1e300 * 1.0 > 1e299", True),
])
def test_number_literals_evaluate_as_in_cel(backend: str, expr: str, want: Any) -> None:
    got = run(backend, expr)
    assert got == want and type(got) is type(want)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_negative_literal_binds_before_member_access_as_in_cel(backend: str) -> None:
    with pytest.raises(CelError):        # (-9223372036854775808).size(): no size() on int, at run time
        run(backend, "-9223372036854775808.size() == 1")


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("expr", ["1u == 1u", "0x10u == 16u", "0x10U == 16u", "18446744073709551615u > 0u",
                                  "9223372036854775808u > 1u", "{1u: 'a'}[1u] == 'a'"])
def test_uint_literals_are_uint_values(backend: str, expr: str) -> None:
    prog = get_backend(backend).compile(expr)          # a uint literal always compiles...
    if backend == "subset":
        with pytest.raises(CelError, match="uint"):    # ...and on the subset, which has no uint, is uint()'s error
            prog.evaluate({})
    else:
        assert prog.evaluate({}) is True


@pytest.mark.parametrize("backend", BACKENDS)
def test_uint_literal_reads_as_uint_call(backend: str) -> None:
    from invariants.cel import subset
    assert subset.parse("5u") == subset.parse("uint(5)")
    assert subset.parse("0x10u") == subset.parse("uint(16)")


def test_rewritten_text_keeps_a_large_uint_literal() -> None:
    """The cel-expr rewrite prints uint(n) as nu, so a value past int64 stays a valid literal."""
    if "cel-expr" not in BACKENDS:
        pytest.skip("cel-expr not installed")
    assert run("cel-expr", "inv.unique_by([1, 2], x, x) && 18446744073709551615u > 1u") is True
    assert run("cel-expr", "1.5 / 2.0 > 0.0 && 18446744073709551615u > 1u") is True


# --- 2. Mapping types -----------------------------------------------------------------------------
class M(Mapping):
    """A read-only mapping that is not a dict."""

    def __init__(self, d: dict[Any, Any]) -> None:
        self._d = d

    def __getitem__(self, k: Any) -> Any:
        return self._d[k]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)


class Boom(M):
    def __getitem__(self, k: Any) -> Any:
        raise RuntimeError("secret")

    def __contains__(self, k: object) -> bool:
        raise RuntimeError("secret")

    def __iter__(self) -> Iterator[Any]:
        raise RuntimeError("secret")

    def __len__(self) -> int:
        raise RuntimeError("secret")


MAPPINGS = [types.MappingProxyType, M]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("mk", MAPPINGS)
@pytest.mark.parametrize("expr", [
    "m.a == 1", "m['a'] == 1", "size(m) == 2", "m.size() == 2", "'a' in m", "!('z' in m)", "has(m.a)", "!has(m.z)",
    "m.all(k, k in ['a', 'b'])", "m.exists(k, m[k] == 2)", "m.exists_one(k, k == 'b')", "m.map(k, k).size() == 2",
    "m.filter(k, k == 'a') == ['a']", "o.n.x == 5", "has(o.n.x)", "'x' in o.n", "m == {'a': 1, 'b': 2}",
    "m.b + o.n.x == 7",
])
def test_a_mapping_is_a_cel_map(backend: str, mk: Any, expr: str) -> None:
    env = {"m": mk({"a": 1, "b": 2}), "o": types.SimpleNamespace(n=mk({"x": 5}))}
    assert run(backend, expr, env) is True


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("mk", MAPPINGS)
def test_a_missing_key_in_a_mapping_is_an_error(backend: str, mk: Any) -> None:
    with pytest.raises(CelError):
        run(backend, "m.z == 1", {"m": mk({"a": 1})})
    with pytest.raises(CelError):
        run(backend, "m['z'] == 1", {"m": mk({"a": 1})})


@pytest.mark.parametrize("expr", ["m.a == 1", "m['a'] == 1", "size(m) == 1", "'a' in m", "has(m.a)",
                                  "m.all(k, true)"])
def test_a_mapping_that_raises_is_a_cel_error_on_the_subset(expr: str) -> None:
    with pytest.raises(CelError) as ei:
        run("subset", expr, {"m": Boom({"a": 1})})
    assert "secret" not in str(ei.value)


@pytest.mark.parametrize("mk", MAPPINGS)
def test_walk_follows_mapping_keys(mk: Any) -> None:
    root = types.SimpleNamespace(cfg=mk({"limit": 3, "inner": mk({"x": 1})}))
    assert providers.walk(root, ["cfg", "limit"]) == 3
    assert providers.walk(root, ["cfg", "inner", "x"]) == 1
    with pytest.raises(CelError, match="no such key"):
        providers.walk(root, ["cfg", "nope"])
    with pytest.raises(CelError) as ei:
        providers.walk(types.SimpleNamespace(cfg=Boom({})), ["cfg", "x"])
    assert "secret" not in str(ei.value)


@pytest.mark.parametrize("mk", MAPPINGS)
def test_a_mapping_snapshot_is_plain_and_copied_as_a_dict(mk: Any) -> None:
    under = {"a": 1, "nested": [1, 2], "deep": {"q": 1}}
    obj = types.SimpleNamespace(cfg=mk(under), wrap=[mk({"k": [1]})])
    assert snapshot.is_plain(mk(under)) and snapshot.is_plain([mk({"k": 1})])
    assert not snapshot.is_plain(mk({"a": object()}))
    rule = types.SimpleNamespace(snapshot=[("cfg",), ("wrap",)])
    snap = snapshot.take_snapshot(rule, obj)     # type: ignore[arg-type]
    under["a"] = 99
    under["nested"].append(3)
    under["deep"]["q"] = 2
    assert snap["cfg"] == {"a": 1, "nested": [1, 2], "deep": {"q": 1}} and type(snap["cfg"]) is dict
    assert snap["wrap"] == [{"k": [1]}] and type(snap["wrap"][0]) is dict


@pytest.mark.parametrize("mk", MAPPINGS)
def test_a_snapshot_steps_through_a_mapping(mk: Any) -> None:
    obj = types.SimpleNamespace(cfg=mk({"limit": 3}))
    rule = types.SimpleNamespace(snapshot=[("cfg", "limit"), ("cfg", "nope")])
    assert snapshot.take_snapshot(rule, obj) == {"cfg": {"limit": 3}}     # type: ignore[arg-type]


def test_a_dataclass_holding_a_mappingproxy_snapshots() -> None:
    import dataclasses

    @dataclasses.dataclass
    class Cfg:
        m: Any

    under = {"a": 1}
    obj = types.SimpleNamespace(cfg=Cfg(types.MappingProxyType(under)))
    rule = types.SimpleNamespace(snapshot=[("cfg",)])
    snap = snapshot.take_snapshot(rule, obj)     # type: ignore[arg-type]
    under["a"] = 2
    assert snap["cfg"].m == {"a": 1}
