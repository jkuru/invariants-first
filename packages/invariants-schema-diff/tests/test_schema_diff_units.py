"""The schema-diff judges' pure functions, tested directly (no mocks, no files): the CEL edit judge
(cel_sites, cel_canon, cel_mono) and the JSON Schema judge (schemas)."""
from __future__ import annotations

import typing

import pytest

from invariants_cel import subset
from invariants_schema_diff import cel_canon, cel_mono, cel_sites, changes
from invariants_schema_diff import schemas as schema_diff

# --- cel_mono ---------------------------------------------------------------------------------------
X = ("id", "x")
ONE, TWO, ZERO = ("lit", "int", 1), ("lit", "int", 2), ("lit", "int", 0)


@pytest.mark.parametrize("node, raises", [
    (("sel", X, "f"), True),                              # a missing field
    (("idx", X, ONE), True),
    (("call", "has", None, [("sel", X, "f")]), False),    # has() is total
    (("call", "size", None, [X]), True),
    (("un", "-", X), True),
    (("nary", "+", (ONE, TWO)), False),                   # literals: provably in int64
    (("nary", "+", (X, ONE)), True),                      # x may be near int64 max
    (("bin", "/", X, TWO), False),                        # a non-zero literal divisor other than -1
    (("bin", "/", X, ZERO), True),
    (("bin", "%", X, ("lit", "int", -1)), True),
    (("bin", "==", X, ONE), False),
])
def test_can_raise(node: typing.Any, raises: bool) -> None:
    assert cel_sites._can_raise(node) is raises


def _in(*items: typing.Any) -> tuple[typing.Any, ...]:
    return ("bin", "in", X, ("list", list(items)))


def test_walk_in_list_grown_shrunk_and_mixed() -> None:
    out: list[int] = []
    assert cel_mono._walk_in(_in(ONE), _in(ONE, TWO), 1, out, None, None) and out == [1]
    out = []
    assert cel_mono._walk_in(_in(ONE, TWO), _in(ONE), 1, out, None, None) and out == [-1]
    assert not cel_mono._walk_in(_in(ONE), _in(TWO), 1, [], None, None)       # one added, one removed
    assert not cel_mono._walk_in(_in(ONE), _in(ONE, TWO), 0, [], None, None)   # unknown direction
    assert not cel_mono._walk_in(_in(ONE), ("bin", "in", ("id", "y"), ("list", [ONE])), 1, [], None, None)


def test_walk_dispatch_undecidable_kinds() -> None:
    assert cel_mono._walk(("id", "a"), ("id", "b"), 1, []) is False              # no step for `id`
    assert cel_mono._walk(("un", "!", X), ("un", "-", X), 1, []) is False
    out: list[int] = []
    assert cel_mono._walk(("nary", "||", (X, ("id", "y"))), X, 1, out) and out == [-1]   # a disjunct dropped


def test_canon_chain_flattens_and_drops_repeats() -> None:
    tree = cel_mono.canon_tree(subset.parse("a || (b || a)"))
    assert tree == ("nary", "||", cel_canon._order([("id", "a"), ("id", "b")]))
    assert cel_mono.canon_tree(subset.parse("(x > 1) || (x > 1)")) == cel_mono.canon_tree(subset.parse("x > 1"))


# --- schema_diff ------------------------------------------------------------------------------------
@pytest.mark.parametrize("key, old, new, node, why", [
    ("maximum", 10, 20, {}, "maximum raised: larger values pass"),
    ("maximum", 10, 5, {}, None),
    ("minLength", 3, 1, {}, "minLength lowered: smaller values pass"),
    ("maximum", 10, None, {}, "maximum removed: no upper bound"),
    ("minimum", 1, None, {"type": "integer", "exclusiveMinimum": 0}, None),   # ge=1 -> gt=0 on an integer
    ("minimum", 1, None, {"type": "number", "exclusiveMinimum": 0}, "minimum removed: no lower bound"),
])
def test_bound_weakening(key: str, old: typing.Any, new: typing.Any, node: typing.Any, why: str | None) -> None:
    assert schema_diff._bound_weakening(key, old, new, node) == why


def test_enum_weakening() -> None:
    assert schema_diff._enum_weakening("", ["a", "b"], None, {"const": "a"}) is None        # narrowed to a const
    assert schema_diff._enum_weakening("", ["a"], None, {}) == "enum removed: any value passes"
    assert schema_diff._enum_weakening("added", None, ["c"], {}) == "enum widened: more values pass"
    assert schema_diff._enum_weakening("removed", ["c"], None, {}) is None                  # members dropped


@pytest.mark.parametrize("segs, node, model", [
    (["pydantic", "additionalProperties"], None, True),                                       # the root
    (["pydantic", "$defs", "M", "additionalProperties"], None, True),
    (["pydantic", "properties", "tags", "additionalProperties"], {"type": "object"}, False),   # a map
    (["pydantic", "properties", "m", "additionalProperties"], {"properties": {}}, True),
])
def test_model_object(segs: list[str], node: typing.Any, model: bool) -> None:
    assert schema_diff._model_object(segs, node) is model


def test_prefix_items_extended() -> None:
    old, new = [{"type": "string"}], [{"type": "string"}, {"type": "integer"}]
    assert schema_diff._prefix_items_extended(old, new, {}) is None                    # no items: was open
    assert schema_diff._prefix_items_extended(old, new, {"items": {"type": "integer"}}) is None   # same check
    why = schema_diff._prefix_items_extended(old, new, {"items": {"type": "string"}})
    assert why is not None and why.startswith("prefixItems extended to 2")


def test_keyword_weakening_through_constraint_weakening() -> None:
    root = {"pydantic": {"properties": {"a": {"type": "integer", "maximum": 5}}}}
    assert schema_diff.constraint_weakening("pydantic.properties.a.maximum", 5, 9, root, root) \
        == "maximum raised: larger values pass"
    assert schema_diff._keyword_weakening(["pydantic", "properties", "a", "type"], "", "number", "integer",
                                          root, root) is None                          # number -> integer
    assert schema_diff.constraint_weakening("other.properties.a.maximum", 5, 9) is None   # not judged
    # a structural keyword removed says what no longer applies (not only that the schema changed)
    assert schema_diff.constraint_weakening("pydantic.properties.a.items", {"type": "string"}, None) \
        == "items removed: what it checked no longer applies"
    assert schema_diff.constraint_weakening("pydantic.properties.a.items", {"type": "string"}, {}) \
        == "items changed: the schema may accept more input"


def test_schema_entry_changes_judges_each_leaf() -> None:
    sa = {"name": "m.M", "pydantic": {"properties": {"a": {"maximum": 5}}}}
    sb = {"name": "m.M", "pydantic": {"properties": {"a": {"maximum": 9}}}}
    diffs = changes._json_paths(sa, sb)
    (c,) = schema_diff._schema_entry_changes("schema m.M", "used by target t", sa, sb, diffs)
    assert c.weakened and c.what == "pydantic.properties.a.maximum"
    assert c.note == "maximum raised: larger values pass; used by target t"
