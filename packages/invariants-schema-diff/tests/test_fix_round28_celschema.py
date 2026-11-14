"""Regression tests for fix round 28 (pinned-schema judging): each test failed before its fix.

1. Lock: with unevaluatedProperties or unevaluatedItems false (or a schema) on a schema or one it is
   an in-place subschema of, adding or changing an applicator (allOf, dependentSchemas, if,
   contains, $ref, a property inside an allOf branch, ...) was judged a tightening, though what the
   applicator evaluates is then no longer refused: it is now WEAKENED.
2. Lock: a Draft 4 / OpenAPI 3.0 boolean exclusiveMaximum (exclusiveMinimum) true -> false was not
   WEAKENED.
   (Items 3 to 5, the CEL engine's, are in packages/invariants-cel/tests/test_fix_round28_cel.py.)
"""
from __future__ import annotations

import pytest

from invariants_schema_diff.changes import _json_paths
from invariants_schema_diff.schemas import constraint_weakening

def _weakened(old: dict, new: dict) -> list[str]:
    ra, rb = {"jsonschema": old}, {"jsonschema": new}
    return [p for p, x, y in _json_paths(ra, rb) if constraint_weakening(p, x, y, ra, rb)]


def _check_jsonschema(old: dict, new: dict, instance: object, draft: str = "Draft202012Validator") -> None:
    js = pytest.importorskip("jsonschema")
    v = getattr(js, draft)
    assert not v(old).is_valid(instance) and v(new).is_valid(instance)


OBJ = {"type": "object", "properties": {"a": {"type": "integer"}}}
ARR = {"type": "array", "prefixItems": [{"type": "integer"}]}

# (old, new, an instance the old schema refuses and the new one accepts)
UNEVALUATED_WEAKENINGS = [
    ({**OBJ, "unevaluatedProperties": False},
     {**OBJ, "unevaluatedProperties": False, "allOf": [{"properties": {"b": {}}}]}, {"a": 1, "b": "x"}),
    ({**OBJ, "unevaluatedProperties": False},
     {**OBJ, "unevaluatedProperties": False, "dependentSchemas": {"a": {"properties": {"b": {}}}}}, {"a": 1, "b": "x"}),
    ({**OBJ, "unevaluatedProperties": False},
     {**OBJ, "unevaluatedProperties": False, "if": {"properties": {"b": {}}}}, {"a": 1, "b": "x"}),
    ({**ARR, "unevaluatedItems": False}, {**ARR, "unevaluatedItems": False, "contains": {"type": "string"}}, [1, "x"]),
    ({**ARR, "unevaluatedItems": False}, {**ARR, "unevaluatedItems": False, "allOf": [{"items": True}]}, [1, "x"]),
    ({**ARR, "unevaluatedItems": False}, {"type": "array", "prefixItems": [{"type": "integer"}, {}],
                                          "unevaluatedItems": False}, [1, "x"]),
    # an entry added inside an in-place subschema the old schema already had
    ({"type": "object", "unevaluatedProperties": False, "allOf": [{"properties": {"a": {}}}]},
     {"type": "object", "unevaluatedProperties": False, "allOf": [{"properties": {"a": {}, "b": {}}}]},
     {"a": 1, "b": 2}),
    ({"type": "object", "unevaluatedProperties": False, "allOf": [{"properties": {"a": {}}}]},
     {"type": "object", "unevaluatedProperties": False, "allOf": [{"properties": {"a": {}}, "additionalProperties": True}]},
     {"a": 1, "b": 2}),
    ({"type": "object", "unevaluatedProperties": False, "allOf": [{"properties": {"a": {}}}]},
     {"type": "object", "unevaluatedProperties": False, "allOf": [{"properties": {"a": {}}, "patternProperties": {"^b": {}}}]},
     {"a": 1, "b": 2}),
    # an entry added to a dependentSchemas or a patternProperties the old schema already had
    ({**OBJ, "unevaluatedProperties": False, "dependentSchemas": {"z": {}}},
     {**OBJ, "unevaluatedProperties": False, "dependentSchemas": {"z": {}, "a": {"properties": {"b": {}}}}},
     {"a": 1, "b": "x"}),
    ({"type": "object", "unevaluatedProperties": False, "allOf": [{"patternProperties": {"^a": {}}}]},
     {"type": "object", "unevaluatedProperties": False, "allOf": [{"patternProperties": {"^a": {}, "^b": {}}}]},
     {"a": 1, "b": 2}),
    # a $defs entry reached by a $ref
    ({"$ref": "#/$defs/A", "unevaluatedProperties": False, "$defs": {"A": {"properties": {"a": {}}}}},
     {"$ref": "#/$defs/A", "unevaluatedProperties": False, "$defs": {"A": {"properties": {"a": {}, "b": {}}}}},
     {"a": 1, "b": 2}),
    ({**OBJ, "unevaluatedProperties": False},
     {**OBJ, "unevaluatedProperties": False, "$ref": "#/$defs/B", "$defs": {"B": {"properties": {"b": {}}}}},
     {"a": 1, "b": 2}),
    # a schema for the unevaluated ones, not only false
    ({**OBJ, "unevaluatedProperties": {"type": "integer"}},
     {**OBJ, "unevaluatedProperties": {"type": "integer"}, "allOf": [{"properties": {"b": {}}}]}, {"a": 1, "b": "x"}),
]


@pytest.mark.parametrize("old,new,instance", UNEVALUATED_WEAKENINGS)
def test_applicator_added_under_unevaluated_is_weakened(old, new, instance):
    _check_jsonschema(old, new, instance)
    assert _weakened(old, new), (old, new)


TIGHTENINGS = [
    # no unevaluated* keyword in scope: adding an applicator only tightens (as before)
    (OBJ, {**OBJ, "allOf": [{"properties": {"b": {"type": "string"}}}]}),
    (OBJ, {**OBJ, "dependentSchemas": {"a": {"required": ["b"]}}}),
    ({**ARR}, {**ARR, "contains": {"type": "integer"}}),
    # the unevaluated keyword judges another instance (the root's), not the property's
    ({"type": "object", "unevaluatedProperties": False, "properties": {"x": {"type": "object"}}},
     {"type": "object", "unevaluatedProperties": False,
      "properties": {"x": {"type": "object", "allOf": [{"properties": {"b": {}}}]}}}),
    # a false subschema evaluates nothing
    ({**ARR, "unevaluatedItems": False}, {**ARR, "unevaluatedItems": False, "items": False}),
    # a property that is required and constrained, as for additionalProperties: false
    ({"type": "object", "unevaluatedProperties": False, "allOf": [{"properties": {"a": {}}}]},
     {"type": "object", "unevaluatedProperties": False,
      "allOf": [{"properties": {"a": {}, "b": {"type": "integer"}}, "required": ["b"]}]}),
]


@pytest.mark.parametrize("old,new", TIGHTENINGS)
def test_applicator_without_unevaluated_in_scope_still_tightens(old, new):
    assert _weakened(old, new) == []


def test_unevaluated_scope_names_the_keyword():
    old = {**OBJ, "unevaluatedProperties": False}
    new = {**old, "allOf": [{"properties": {"b": {}}}]}
    ra, rb = {"jsonschema": old}, {"jsonschema": new}
    why = constraint_weakening("jsonschema.allOf", None, new["allOf"], ra, rb)
    assert why is not None and "unevaluatedProperties is false" in why


@pytest.mark.parametrize("key,bound,value", [("exclusiveMaximum", "maximum", 10), ("exclusiveMinimum", "minimum", 0)])
def test_draft4_boolean_exclusive_true_to_false_is_weakened(key, bound, value):
    old = {"type": "number", bound: 10 if bound == "maximum" else 0, key: True}
    new = {**old, key: False}
    _check_jsonschema(old, new, value, "Draft4Validator")
    assert _weakened(old, new) == [f"jsonschema.{key}"]
    assert _weakened(new, old) == []                             # false -> true tightens


