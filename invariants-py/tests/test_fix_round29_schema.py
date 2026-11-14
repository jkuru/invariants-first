"""Regression tests for fix round 29 (pinned-schema judging). Each item below failed before its fix (a
few neighbouring cases that already held are kept beside them as guards). Every weakening comes with
an instance the old schema refuses and the new one accepts, checked with jsonschema's validator for
the schema's draft.

1. A change inside a $defs entry (or a schema-table component) that a $ref uses under not, oneOf, if
   or a contains with maxContains was judged by the change alone: {"not": {"$ref": "#/$defs/A"}} with
   A.maximum 5 -> 3 was a tightening. The polarity of each place is now carried through $refs (and
   through the table), and a place used both ways is judged both ways.
2. A list entry or named entry replaced by `true` or `false` (allOf[0], anyOf[i], prefixItems[i],
   oneOf[i], properties.a, $defs.X) was never judged.
3. Removing a patternProperties entry was not WEAKENED.
4. A structured const or enum value was diffed as a schema ({"const": {"a": 1}} -> {"a": 2}).
5. A removed `const: null` was read as absent.
6. Draft 7 / OpenAPI 3.0 `dependencies` was not a structural keyword.
7. Draft 4 / OpenAPI 3.0: maximum -> a numeric exclusiveMaximum counted as a tightening, though
   there a numeric exclusiveMaximum bounds nothing. The dialect is read from $schema, a boolean
   exclusive bound, or the OpenAPI version the pin now records.
8. Tightening `contains` while maxContains is set was a tightening; adding minContains: 0 too.
9. An $anchor, $dynamicAnchor or $id edit that re-points a $ref was not judged.
"""
from __future__ import annotations

import invariants_schema_diff.changes
import json
import pathlib

import pytest

from invariants import lock as lockmod
from invariants_schema_diff import schemas as schema_diff
from invariants_schema_diff.changes import _json_paths
from invariants_schema_diff.schemas import _schema_changes, constraint_weakening

D4 = "http://json-schema.org/draft-04/schema#"
D7 = "http://json-schema.org/draft-07/schema#"


def _weakened(old: object, new: object) -> list[str]:
    ra, rb = {"jsonschema": old}, {"jsonschema": new}
    return [p for p, x, y in _json_paths(ra, rb) if constraint_weakening(p, x, y, ra, rb)]


def _validator(schema: object):
    js = pytest.importorskip("jsonschema")
    uri = schema.get("$schema", "") if isinstance(schema, dict) else ""
    return js.Draft4Validator if "draft-04" in uri else js.Draft7Validator if "draft-07" in uri \
        else js.Draft202012Validator


def _real(old: object, new: object, instance: object) -> None:
    """The change does let more input through: old refuses the instance, new accepts it."""
    assert not _validator(old)(old).is_valid(instance) and _validator(new)(new).is_valid(instance)


# (old, new, an instance the old schema refuses and the new one accepts)
WEAKENINGS = [
    # 1. polarity through $ref
    ({"not": {"$ref": "#/$defs/A"}, "$defs": {"A": {"maximum": 5}}},
     {"not": {"$ref": "#/$defs/A"}, "$defs": {"A": {"maximum": 3}}}, 4),
    ({"oneOf": [{"$ref": "#/$defs/A"}, {"minimum": 0}], "$defs": {"A": {"maximum": 5}}},
     {"oneOf": [{"$ref": "#/$defs/A"}, {"minimum": 0}], "$defs": {"A": {"maximum": 3}}}, 4),
    ({"if": {"$ref": "#/$defs/A"}, "then": {"type": "string"}, "$defs": {"A": {"maximum": 5}}},
     {"if": {"$ref": "#/$defs/A"}, "then": {"type": "string"}, "$defs": {"A": {"maximum": 3}}}, 4),
    ({"not": {"$ref": "#/$defs/B"}, "$defs": {"B": {"$ref": "#/$defs/A"}, "A": {"maximum": 5}}},  # two hops
     {"not": {"$ref": "#/$defs/B"}, "$defs": {"B": {"$ref": "#/$defs/A"}, "A": {"maximum": 3}}}, 4),
    # used both at +1 and under not: a tightening is weakened through the not
    ({"anyOf": [{"$ref": "#/$defs/A", "type": "string"}, {"not": {"$ref": "#/$defs/A"}}], "$defs": {"A": {"maximum": 5}}},
     {"anyOf": [{"$ref": "#/$defs/A", "type": "string"}, {"not": {"$ref": "#/$defs/A"}}], "$defs": {"A": {"maximum": 3}}},
     4),
    ({"type": "array", "contains": {"$ref": "#/$defs/A"}, "minContains": 0, "maxContains": 1,
      "$defs": {"A": {"type": "integer"}}},
     {"type": "array", "contains": {"$ref": "#/$defs/A"}, "minContains": 0, "maxContains": 1,
      "$defs": {"A": {"type": "integer", "minimum": 5}}}, [1, 2]),
    # 2. a subschema replaced whole
    ({"allOf": [{"maximum": 5}]}, {"allOf": [True]}, 10),
    ({"anyOf": [{"maximum": 5}, {"minimum": 100}]}, {"anyOf": [True, {"minimum": 100}]}, 10),
    ({"prefixItems": [{"type": "integer"}]}, {"prefixItems": [True]}, ["x"]),
    ({"oneOf": [{"type": "string"}, True]}, {"oneOf": [{"type": "string"}, False]}, "a"),
    ({"properties": {"a": {"type": "integer"}}}, {"properties": {"a": True}}, {"a": "x"}),
    ({"properties": {"a": {"$ref": "#/$defs/Y"}}, "$defs": {"Y": {"type": "integer"}}},
     {"properties": {"a": True}, "$defs": {"Y": {"type": "integer"}}}, {"a": "x"}),
    ({"$ref": "#/$defs/X", "$defs": {"X": {"type": "integer"}}}, {"$ref": "#/$defs/X", "$defs": {"X": True}}, "s"),
    ({"not": {"maximum": 5}}, {"not": False}, 3),                    # not: a schema -> false accepts all
    ({"type": "object", "additionalProperties": False}, {"type": "object", "properties": {"a": {}},
                                                         "additionalProperties": False}, {"a": 1}),
    # 3. patternProperties entry removed
    ({"type": "object", "patternProperties": {"^a": {"type": "integer"}, "^b": {}}},
     {"type": "object", "patternProperties": {"^b": {}}}, {"a": "x"}),
    # 4. structured const and enum
    ({"const": {"a": 1}}, {"const": {"a": 2}}, {"a": 2}),
    ({"enum": [{"a": 1}]}, {"enum": [{"a": 2}]}, {"a": 2}),
    ({"enum": [[1]]}, {"enum": [[2]]}, [2]),
    ({"const": {"maximum": 10}}, {"const": {"maximum": 5}}, {"maximum": 5}),
    # 5. const null
    ({"properties": {"k": {"const": None}}}, {"properties": {"k": {"const": "admin"}}}, {"k": "admin"}),
    ({"const": None}, {}, 3),
    # 6. Draft 7 dependencies
    ({"$schema": D7, "dependencies": {"a": ["b"]}}, {"$schema": D7}, {"a": 1}),
    ({"$schema": D7, "dependencies": {"a": ["b"], "c": ["d"]}}, {"$schema": D7, "dependencies": {"c": ["d"]}}, {"a": 1}),
    ({"$schema": D7, "dependencies": {"a": ["b", "c"]}}, {"$schema": D7, "dependencies": {"a": ["b"]}}, {"a": 1, "b": 2}),
    ({"$schema": D7, "dependencies": {"a": {"required": ["b"]}}}, {"$schema": D7, "dependencies": {"a": True}}, {"a": 1}),
    ({"dependentRequired": {"a": ["b"]}}, {}, {"a": 1}),
    # 7. Draft 4: a numeric exclusiveMaximum bounds nothing
    ({"$schema": D4, "maximum": 10}, {"$schema": D4, "exclusiveMaximum": 10}, 50),
    ({"$schema": D4, "minimum": 10}, {"$schema": D4, "exclusiveMinimum": 9}, -50),
    ({"$schema": D4, "maximum": 10, "exclusiveMaximum": True}, {"$schema": D4, "maximum": 10}, 10),
    # 8. contains with maxContains; minContains defaults to 1
    ({"contains": {"type": "integer"}, "minContains": 0, "maxContains": 1},
     {"contains": {"type": "integer", "minimum": 5}, "minContains": 0, "maxContains": 1}, [1, 2]),
    ({"contains": {"type": "integer"}, "maxContains": 1}, {"contains": {"const": 1}, "maxContains": 1}, [1, 2]),
    ({"type": "array", "contains": {"type": "integer"}}, {"type": "array", "contains": {"type": "integer"},
                                                          "minContains": 0}, ["x"]),
    # 9. a $ref re-pointed by an $anchor or $id edit
    ({"$ref": "#t", "$defs": {"A": {"$anchor": "t", "maximum": 5}, "B": {"maximum": 100}}},
     {"$ref": "#t", "$defs": {"A": {"maximum": 5}, "B": {"$anchor": "t", "maximum": 100}}}, 50),
    ({"$id": "http://x/root", "$ref": "a",
      "$defs": {"A": {"$id": "http://x/a", "maximum": 5}, "B": {"$id": "http://x/b", "maximum": 100}}},
     {"$id": "http://x/root", "$ref": "a",
      "$defs": {"A": {"$id": "http://x/b", "maximum": 5}, "B": {"$id": "http://x/a", "maximum": 100}}}, 50),
    ({"$schema": D7, "$ref": "#t", "definitions": {"A": {"$id": "#t", "maximum": 5}, "B": {"$id": "#u", "maximum": 100}}},
     {"$schema": D7, "$ref": "#t", "definitions": {"A": {"$id": "#u", "maximum": 5}, "B": {"$id": "#t", "maximum": 100}}},
     50),
    ({"$ref": "#t", "$defs": {"A": {"$anchor": "t", "maximum": 5}}},          # an anchor added with its schema
     {"$ref": "#t", "$defs": {"A": {"maximum": 5}, "B": {"$anchor": "t", "maximum": 100}}}, 50),
    ({"$ref": "#t", "$defs": {"A": {"$anchor": "t", "maximum": 5}}},          # a subschema added with the
     {"$ref": "#t", "$defs": {"B": {"$anchor": "t", "maximum": 100},         # same anchor now shadows it
                              "A": {"$anchor": "t", "maximum": 5}}}, 50),
]


@pytest.mark.parametrize("old,new,instance", WEAKENINGS)
def test_weakening_is_weakened(old, new, instance):
    _real(old, new, instance)
    assert _weakened(old, new), (old, new)


# changes that let no more input through
TIGHTENINGS = [
    ({"not": {"$ref": "#/$defs/A"}, "$defs": {"A": {"maximum": 5}}},          # looser under not: tighter
     {"not": {"$ref": "#/$defs/A"}, "$defs": {"A": {"maximum": 9}}}),
    ({"not": {"not": {"$ref": "#/$defs/A"}}, "$defs": {"A": {"maximum": 5}}},  # two nots cancel
     {"not": {"not": {"$ref": "#/$defs/A"}}, "$defs": {"A": {"maximum": 3}}}),
    ({"$ref": "#/$defs/A", "$defs": {"A": {"maximum": 5}}}, {"$ref": "#/$defs/A", "$defs": {"A": {"maximum": 3}}}),
    ({"not": {"maximum": 5}}, {"not": True}),
    ({"allOf": [{"maximum": 5}]}, {"allOf": [False]}),
    ({"anyOf": [True, {"minimum": 100}]}, {"anyOf": [{"maximum": 5}, {"minimum": 100}]}),
    ({"properties": {"a": True}}, {"properties": {"a": {"type": "integer"}}}),
    ({"enum": [{"a": 1}, {"a": 2}]}, {"enum": [{"a": 1}]}),
    ({"enum": [[1], [2]]}, {"const": [2]}),
    ({"const": {"a": 1}, "default": {"a": 1}}, {"const": {"a": 1}, "default": {"a": 2}}),
    ({"$schema": D7, "dependencies": {"a": ["b"]}}, {"$schema": D7, "dependencies": {"a": ["b", "c"]}}),
    ({"$schema": D4, "maximum": 10}, {"$schema": D4, "maximum": 10, "exclusiveMaximum": True}),
    ({"maximum": 10}, {"exclusiveMaximum": 10}),                              # 2020-12: a bound of its own
    ({"contains": {"type": "integer"}}, {"contains": {"type": "integer", "minimum": 5}}),   # no maxContains
    ({"$ref": "#t", "$defs": {"A": {"$anchor": "t", "maximum": 5}, "B": {"maximum": 5}}},
     {"$ref": "#t", "$defs": {"A": {"maximum": 5}, "B": {"$anchor": "t", "maximum": 5}}}),  # same body
]


@pytest.mark.parametrize("old,new", TIGHTENINGS)
def test_tightening_is_not_weakened(old, new):
    assert _weakened(old, new) == [], (old, new)


# --- 1. polarity through the schema table --------------------------------------------------------
def _table(place: str, body: dict, tag: str) -> tuple[dict, str]:
    comp, wrap = f"sha256:b{tag}", f"sha256:w{tag}"
    return {comp: {"name": "B", "schema": body},
            wrap: {"name": "W", "schema": json.loads(place.replace("REF", json.dumps({"$ref": comp})))}}, wrap


@pytest.mark.parametrize("place,weak", [
    ('{"not": REF}', True), ('{"oneOf": [REF, {"type": "string"}]}', True), ('{"allOf": [REF]}', False),
    ('{"not": {"not": REF}}', False), ('{"properties": {"a": REF}}', False),
    ('{"type": "array", "contains": REF, "maxContains": 1}', True)])
def test_a_component_is_judged_at_the_polarity_it_is_named_at(place, weak):
    ta, wa = _table(place, {"maximum": 5}, "1")
    tb, wb = _table(place, {"maximum": 3}, "2")
    cs = _schema_changes([("contract c", "schema", wa, wb)], ta, tb)
    assert any(c.weakened for c in cs) is weak, [str(c) for c in cs]


def _openapi_pin(tmp_path: pathlib.Path, spec: dict) -> dict:
    (tmp_path / "api.json").write_text(json.dumps(spec))
    pins = lockmod.Schemas()
    pin = lockmod.contract_hash("api", {"type": "openapi", "ref": "api.json"}, tmp_path, ["a"], pins)
    return {"contracts": {"api": pin}, "schemas": pins.table}


def _spec(version: str, body: dict, components: dict | None = None) -> dict:
    return {"openapi": version, "paths": {"/a": {"post": {"operationId": "a", "responses": {},
            "requestBody": {"required": True, "content": {"application/json": {"schema": body}}}}}},
            "components": {"schemas": components or {}}}


def test_an_openapi_component_named_under_not_is_judged_the_other_way(tmp_path):
    body = {"type": "object", "properties": {"n": {"not": {"$ref": "#/components/schemas/Blocked"}}}}
    pins = {}
    for name, maximum in (("old", 5), ("tighter", 3), ("looser", 9)):
        (tmp_path / name).mkdir()
        pins[name] = _openapi_pin(tmp_path / name, _spec("3.1.0", body, {"Blocked": {"type": "integer",
                                                                                     "maximum": maximum}}))
    cs = lockmod.changes(pins["old"], pins["tighter"])        # 4 now passes `not`
    assert any(c.weakened and c.what == "schema.maximum" for c in cs), [str(c) for c in cs]
    assert not any(c.weakened for c in lockmod.changes(pins["old"], pins["looser"]))


# --- 7. the OpenAPI version decides the dialect ---------------------------------------------------
@pytest.mark.parametrize("version,weak", [("3.0.3", True), ("3.1.0", False)])
def test_openapi_30_reads_a_numeric_exclusive_bound_as_draft_4(tmp_path, version, weak):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    old = _openapi_pin(tmp_path / "a", _spec(version, {"type": "integer", "maximum": 10}))
    new = _openapi_pin(tmp_path / "b", _spec(version, {"type": "integer", "exclusiveMaximum": 10}))
    assert (old["contracts"]["api"].get("dialect") == "openapi-3.0") is weak
    cs = lockmod.changes(old, new)
    assert any(c.weakened for c in cs) is weak, [str(c) for c in cs]


def test_openapi_30_components_record_the_dialect(tmp_path):
    pin = _openapi_pin(tmp_path, _spec("3.0.3", {"$ref": "#/components/schemas/N"}, {"N": {"maximum": 10}}))
    (entry,) = pin["schemas"].values()
    assert entry["dialect"] == "openapi-3.0"
    assert schema_diff._dialect(entry, "schema") == "draft4"


# --- 5. a removed const: null, through a pydantic pin -----------------------------------------------
def test_a_pydantic_literal_none_changed_is_weakened():
    pydantic = pytest.importorskip("pydantic")
    import typing

    class A(pydantic.BaseModel):
        kind: typing.Literal[None] = None

    class B(pydantic.BaseModel):
        kind: typing.Literal["admin"] = "admin"

    def lock(m):
        pins = lockmod.Schemas()
        ref = pins.add({"name": "m.R", **lockmod.resolved_schema(m)})
        return {"contracts": {"r": {"type": "pydantic", "schema": ref}}, "schemas": pins.table}
    cs = lockmod.changes(lock(A), lock(B))
    assert any(c.weakened and c.what.endswith("kind.const") for c in cs), [str(c) for c in cs]


# --- the judge follows the path through names that hold "." or "[" ---------------------------------
def test_a_property_name_with_a_dot_or_bracket_is_one_key():
    old = {"properties": {"a.b": {"maximum": 5}, "x[0]": {"maximum": 5}}}
    new = {"properties": {"a.b": {"maximum": 9}, "x[0]": True}}
    assert sorted(_weakened(old, new)) == ["jsonschema.properties.a.b.maximum", "jsonschema.properties.x[0]"]
    assert schema_diff._keys({"p": {"a.b": {"c": 1}}}, {"p": {}}, "p.a.b.c") == ["p", "a.b", "c"]


def test_an_openapi_30_lock_taken_before_the_dialect_pin_repins_without_a_weakening(tmp_path):
    spec = _spec("3.0.3", {"$ref": "#/components/schemas/N"},
                 {"N": {"type": "integer", "maximum": 10, "exclusiveMaximum": True}})
    new = _openapi_pin(tmp_path, spec)
    (digest, body), = new["schemas"].items()
    before = {k: v for k, v in body.items() if k != "dialect"}           # as an older lock pinned it
    old_digest = invariants_schema_diff.changes._sha(invariants_schema_diff.changes.canonical(before))
    contract = {k: v for k, v in new["contracts"]["api"].items() if k != "dialect"}
    old = {"contracts": {"api": json.loads(json.dumps(contract).replace(digest, old_digest))},
           "schemas": {old_digest: before}}
    cs = lockmod.changes(old, new)
    assert cs and not any(c.weakened for c in cs), [str(c) for c in cs]
