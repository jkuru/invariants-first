"""Regression tests for fix round 35 (external audit, weakening detection): each test failed before
its fix, except where a test says it pins behaviour that already held.

1. CEL: -(-9223372036854775808) was folded into the literal 9223372036854775808, which no int64
   reaches, and the unary minus (an error site: it overflows at run time) was dropped, so
   `x < 1` -> `x < -(-9223372036854775808)` was 'stronger' though the new condition always raises
   (under fail_open_and_page: lets every call through). It is now left a unary minus.
2. Schema: when either side was Draft 4 (an OpenAPI 3.0 dialect, a boolean exclusiveMaximum), both
   were read as Draft 4, so a Draft 2020-12 `exclusiveMaximum: 10` (a bound) to a Draft 4
   `exclusiveMaximum: true` with no maximum (no bound) was no weakening. Each side is now read in its
   own dialect.
3. Schema: a whole pinned schema removed (an entry's pydantic schema, a contract's schema, an OpenAPI
   parameter schema), or a request body, its content map, a media type or a response removed above
   the schemas, was no weakening.
4. Pins judged as JSON Schema (pin_json_schema): a pydantic model a dataclass or mapping names (pinned
   as {"pydantic": ...} in $defs) was not judged; an opaque Python type (x-python-type: Decimal)
   changed or removed was no weakening; tuple[int] (one item) read as tuple[int, ...] (any number);
   a dict's key type was ignored.
"""
from __future__ import annotations

import copy
import typing

import pytest

from invariants_cel import subset
from invariants_schema_diff import cel_canon, cel_mono, schema_changes
from invariants_schema_diff.changes import _json_paths
from invariants_schema_diff.schemas import _schema_entry_changes, constraint_weakening, pin_json_schema

INT_MIN = "-9223372036854775808"


# ---- 1. CEL: INT_MIN negated ----------------------------------------------------------------------

def test_int_min_negated_is_not_folded() -> None:
    tree = cel_canon.canon_tree(subset.parse(f"-({INT_MIN})"))
    assert tree == ("un", "-", ("lit", "int", -2 ** 63))


@pytest.mark.parametrize("old", ["x < 1", "x < 9223372036854775807"])
def test_int_min_negated_threshold_is_not_stronger(old: str) -> None:
    assert cel_mono.compare_bad(old, f"x < -({INT_MIN})", subset.parse) != "stronger"


def test_int_min_negated_is_an_error_site() -> None:
    from invariants_schema_diff import cel_sites
    old = cel_canon.canon_tree(subset.parse("x"))
    new = cel_canon.canon_tree(subset.parse(f"x || -({INT_MIN}) > 0"))
    assert "arithmetic (-)" in cel_sites.new_error_sites(old, new)


@pytest.mark.parametrize("text,value", [("-(9223372036854775807)", -(2 ** 63 - 1)), ("-(1)", -1), ("-(1.5)", -1.5)])
def test_in_range_negation_still_folds(text: str, value: typing.Any) -> None:
    """Pins behaviour that held: a negation that stays in int64 (or a double) is still a literal."""
    got = cel_canon.canon_tree(subset.parse(text))
    assert got[0] == "lit" and got[2] == value


def test_negation_folds_up_to_int64_max() -> None:
    """The parser reads only INT_MIN as a negative literal; a tree may hold any (the boundary)."""
    assert cel_canon.canon_tree(("un", "-", ("lit", -(2 ** 63 - 1)))) == ("lit", "int", 2 ** 63 - 1)


def test_folded_negation_keeps_its_comparison_direction() -> None:
    """Pins behaviour that held: with folding, -(5) -> -(3) is a plain threshold edit."""
    assert cel_mono.compare_bad("x > -(5)", "x > -(3)", subset.parse) == "weaker"


# ---- 2. Draft 4 against Draft 2020-12 bounds -----------------------------------------------------

def _weak(old: typing.Any, new: typing.Any) -> list[str]:
    return [c.what for c in schema_changes(old, new) if c.weakened]


@pytest.mark.parametrize("old,new", [
    ({"type": "number", "exclusiveMaximum": 10}, {"type": "number", "exclusiveMaximum": True}),
    ({"type": "number", "exclusiveMaximum": 10}, {"type": "number", "maximum": 11, "exclusiveMaximum": True}),
    ({"type": "number", "exclusiveMaximum": 10}, {"type": "number", "maximum": 10, "exclusiveMaximum": False}),
    ({"type": "number", "maximum": 10}, {"type": "number", "maximum": 11, "exclusiveMaximum": True}),
    ({"type": "number", "exclusiveMinimum": 0}, {"type": "number", "exclusiveMinimum": True}),
    ({"type": "number", "exclusiveMinimum": 0}, {"type": "number", "minimum": -1, "exclusiveMinimum": True}),
    ({"type": "number", "exclusiveMinimum": 0}, {"type": "number", "minimum": 0}),
    ({"type": "number", "maximum": 10, "exclusiveMaximum": True}, {"type": "number", "exclusiveMaximum": 11}),
    ({"type": "number", "maximum": 10, "exclusiveMaximum": True}, {"type": "number", "maximum": 10}),
    # the tighter of a 2020-12 pair: x < 10 (not x <= 10), x > 5 (not x >= 0)
    ({"type": "number", "maximum": 10, "exclusiveMaximum": 10}, {"type": "number", "maximum": 10, "exclusiveMaximum": False}),
    ({"type": "number", "minimum": 0, "exclusiveMinimum": 5}, {"type": "number", "minimum": 3, "exclusiveMinimum": True}),
])
def test_cross_dialect_bound_loosened_is_weakened(old: typing.Any, new: typing.Any) -> None:
    assert _weak(old, new)


@pytest.mark.parametrize("old,new", [
    ({"type": "number", "exclusiveMaximum": 10}, {"type": "number", "maximum": 10, "exclusiveMaximum": True}),
    ({"type": "number", "exclusiveMaximum": 10}, {"type": "number", "maximum": 9, "exclusiveMaximum": False}),
    ({"type": "number", "maximum": 5, "exclusiveMaximum": 10}, {"type": "number", "maximum": 5, "exclusiveMaximum": False}),
    ({"type": "number", "exclusiveMinimum": 0}, {"type": "number", "minimum": 0, "exclusiveMinimum": True}),
    ({"type": "number", "maximum": 10, "exclusiveMaximum": True}, {"type": "number", "exclusiveMaximum": 10}),
    ({"type": "number", "maximum": 10, "exclusiveMaximum": True}, {"type": "number", "maximum": 12, "exclusiveMaximum": 10}),
])
def test_cross_dialect_same_or_tighter_bound_is_not_weakened(old: typing.Any, new: typing.Any) -> None:
    assert not _weak(old, new)


# ---- 3. a whole schema removed --------------------------------------------------------------------

def _entry_weak(a: dict[str, typing.Any], b: dict[str, typing.Any], f: str | None = None) -> list[str]:
    xa, xb = (a, b) if f is None else (a.get(f), b.get(f))
    return [p for p, x, y in _json_paths(xa, xb, f or "") if constraint_weakening(p, x, y, a, b)]


MODEL = {"type": "object", "properties": {"a": {"type": "integer", "maximum": 5}}}


def test_entry_schema_removed_is_weakened() -> None:
    assert _entry_weak({"name": "M", "pydantic": MODEL}, {"name": "M"}) == ["pydantic"]
    assert _entry_weak({"schema": {"$ref": "sha256:" + "0" * 64}}, {}) == ["schema"]


def test_entry_schema_added_or_loose_removed_is_not_weakened() -> None:
    assert _entry_weak({"name": "M"}, {"name": "M", "pydantic": MODEL}) == []
    assert _entry_weak({"name": "M", "jsonschema": {}}, {"name": "M"}) == []
    assert _entry_weak({"name": "M", "jsonschema": True}, {"name": "M"}) == []


OP = {"method": "post", "path": "/x",
      "requestBody": {"required": True, "content": {"application/json": {"type": "integer", "maximum": 5}}},
      "parameters": [{"in": "query", "name": "q", "schema": {"type": "integer", "maximum": 3}},
                     {"in": "query", "name": "c", "content": {"application/json": {"type": "string", "maxLength": 3}}}],
      "responses": {"200": {"application/json": {"type": "integer"}}}}


def _without(*path: typing.Any) -> dict[str, typing.Any]:
    op = copy.deepcopy(OP)
    node = op
    for k in path[:-1]:
        node = node[k]
    del node[path[-1]]
    return op


@pytest.mark.parametrize("path", [
    ("requestBody",), ("requestBody", "content"), ("requestBody", "content", "application/json"),
    ("parameters", 0, "schema"), ("parameters", 1, "content"), ("parameters", 1, "content", "application/json"),
    ("responses", "200"), ("responses", "200", "application/json"), ("responses",),
])
def test_operation_schema_removed_is_weakened(path: tuple[typing.Any, ...]) -> None:
    a, b = {"operations": {"op": OP}}, {"operations": {"op": _without(*path)}}
    assert _entry_weak(a, b, "operations")
    assert not _entry_weak(b, a, "operations")             # added back: only tightens


def test_operation_unrelated_change_is_not_weakened() -> None:
    """Pins behaviour that held: a change above the schemas that removes none is no weakening."""
    op = {**copy.deepcopy(OP), "summary": "x"}
    assert _entry_weak({"operations": {"op": OP}}, {"operations": {"op": op}}, "operations") == []


def test_schema_removed_under_a_negative_polarity_is_judged_the_other_way() -> None:
    a, b = {"operations": {"op": OP}}, {"operations": {"op": _without("requestBody")}}
    p, x, y = _json_paths(b["operations"], a["operations"], "operations")[0]
    neg = frozenset({-1})
    assert constraint_weakening(p, x, y, b, a, neg) is not None    # a schema added where tighter is looser
    p, x, y = _json_paths(a["operations"], b["operations"], "operations")[0]
    assert constraint_weakening(p, x, y, a, b, neg) is None


# ---- 4. pins judged as JSON Schema ----------------------------------------------------------------

def _pin_weak(a: dict[str, typing.Any], b: dict[str, typing.Any]) -> list[str]:
    return [c.what for c in _schema_entry_changes("schema X", "", a, b, _json_paths(a, b)) if c.weakened]


def _model(mx: int) -> dict[str, typing.Any]:
    return {"type": "object", "properties": {"a": {"type": "integer"}, "i": {"$ref": "#/$defs/I"}},
            "$defs": {"I": {"type": "object", "properties": {"z": {"type": "integer", "maximum": mx}}}}}


def test_pydantic_model_in_defs_is_judged() -> None:
    a = {"fields": {"m": {"$ref": "mod.M"}}, "$defs": {"mod.M": {"pydantic": _model(5)}}}
    b = {"fields": {"m": {"$ref": "mod.M"}}, "$defs": {"mod.M": {"pydantic": _model(9)}}}
    assert _pin_weak(a, b) == ["jsonschema.$defs.mod:M.$defs.I.properties.z.maximum"]
    assert _pin_weak(b, a) == []
    js = pin_json_schema(a)
    assert js["$defs"]["mod:M"]["properties"]["i"] == {"$ref": "#/$defs/mod:M/$defs/I"}


def test_pydantic_model_in_defs_refs_resolve_with_jsonschema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    js = pin_json_schema({"fields": {"m": {"$ref": "mod.M"}}, "$defs": {"mod.M": {"pydantic": _model(5)}}})
    v = jsonschema.Draft202012Validator(js)
    assert v.is_valid({"m": {"i": {"z": 5}}}) and not v.is_valid({"m": {"i": {"z": 6}}})


@pytest.mark.parametrize("old,new", [("Decimal", "str"), ("Decimal", "datetime"),
                                     ({"generic": "Box", "args": ["int"]}, {"generic": "Box", "args": ["Any"]})])
def test_opaque_python_type_changed_is_weakened(old: typing.Any, new: typing.Any) -> None:
    assert _pin_weak({"fields": {"a": old}}, {"fields": {"a": new}})


def test_opaque_python_type_added_or_kept_is_not_weakened() -> None:
    assert not _pin_weak({"fields": {"a": "Any"}}, {"fields": {"a": "Decimal"}})     # Any narrowed
    assert not _pin_weak({"fields": {"a": "Decimal"}, "required": ["a"]}, {"fields": {"a": "Decimal"}, "required": ["a", "b"]})
    assert not _pin_weak({"fields": {"a": {"generic": "Box", "args": ["int"]}}},
                         {"fields": {"a": {"generic": "Box", "args": ["int"]}, "b": "Decimal"}})


def _tuple(*args: str) -> dict[str, typing.Any]:
    return {"fields": {"a": {"generic": "tuple", "args": list(args)}}}


def test_tuple_arity() -> None:
    assert pin_json_schema(_tuple("int"))["properties"]["a"] == {
        "type": "array", "prefixItems": [{"type": "integer"}], "minItems": 1, "maxItems": 1}
    assert pin_json_schema(_tuple())["properties"]["a"] == {"type": "array", "minItems": 0, "maxItems": 0}
    assert _pin_weak(_tuple("int"), _tuple("int", "Ellipsis"))
    assert _pin_weak(_tuple("int", "str"), _tuple("int"))
    assert not _pin_weak(_tuple("int", "str"), _tuple("int", "str"))


def _dict(key: typing.Any) -> dict[str, typing.Any]:
    return {"fields": {"a": {"generic": "dict", "args": [key, "int"]}}}


def test_dict_key_type() -> None:
    assert _pin_weak(_dict({"literal": ["a", "b"]}), _dict("str"))
    assert _pin_weak(_dict({"literal": ["a"]}), _dict({"literal": ["a", "b"]}))
    assert not _pin_weak(_dict({"literal": ["a", "b"]}), _dict({"literal": ["a"]}))
