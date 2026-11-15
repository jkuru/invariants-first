"""Round 35 (external audit, the lock's weakening detection): each test failed before its fix.

1. strip_prose removed a property called `title` (`description`, ...) from dependentRequired and
   dependencies (and a discriminator mapping value called so), which are keyed by names, not prose:
   a dependency on such a property was dropped from the pin, so removing it was no change.
2. schema_problems passed a lock whose "schemas" is null while its entries name schema-table entries.
3. A model-call target's reply_schema removed was no weakening; so was a contract's whole schema.
4. A dataclass contract's stdlib JSON Schema read tuple[int] as any number of ints and ignored a
   dict's key type, and a pydantic model a dataclass reply schema names (pinned in its $defs as
   {"pydantic": ...}) was not judged: loosening its Field bounds was no weakening.
"""
from __future__ import annotations

import dataclasses
import typing

import pydantic

import invariants.lock as lockmod
from invariants.lock.pin_jsonschema import stdlib_json_schema
from invariants.lock.pin_types import strip_prose


# ---- 1. names are not prose -----------------------------------------------------------------------

def test_strip_prose_keeps_names_in_dependency_maps() -> None:
    s = {"type": "object", "title": "T",
         "dependentRequired": {"title": ["description"], "a": ["title"]},
         "dependencies": {"description": ["a"], "summary": {"required": ["b"], "title": "x"}},
         "discriminator": {"propertyName": "kind", "mapping": {"title": "#/components/schemas/T"}}}
    got = strip_prose(s)
    assert "title" not in got
    assert got["dependentRequired"] == {"title": ["description"], "a": ["title"]}
    assert got["dependencies"] == {"description": ["a"], "summary": {"required": ["b"]}}
    assert got["discriminator"]["mapping"] == {"title": "#/components/schemas/T"}


def test_dependency_on_a_title_property_removed_is_weakened() -> None:
    def lock(dep: dict[str, typing.Any]) -> dict[str, typing.Any]:
        pins = lockmod.Schemas()
        body = {"type": "object", "properties": {"title": {"type": "string"}, "b": {"type": "string"}}, **dep}
        ref = pins.add({"name": "m.M", "pydantic": body})
        return {"contracts": {"c": {"type": "python", "schema": ref}}, "schemas": pins.table}
    got = lockmod.changes(lock({"dependentRequired": {"title": ["b"]}}), lock({}))
    assert any(c.weakened for c in got), [str(c) for c in got]


# ---- 2. "schemas": null ---------------------------------------------------------------------------

def test_schema_problems_with_a_null_table() -> None:
    ref = {"$ref": "sha256:" + "0" * 64}
    lock = {"contracts": {"c": {"type": "python", "schema": ref}}, "schemas": None}
    assert any("names no entry" in p for p in lockmod.schema_problems(lock))
    assert lockmod.schema_problems({"contracts": {}, "schemas": None}) == []


# ---- 3. reply_schema and a contract's schema removed ----------------------------------------------

def _pinned(body: dict[str, typing.Any]) -> tuple[dict[str, typing.Any], dict[str, typing.Any]]:
    pins = lockmod.Schemas()
    ref = pins.add({"name": "m.R", "pydantic": body})
    return ref, pins.table


BODY = {"type": "object", "properties": {"a": {"type": "integer", "maximum": 5}}}


def test_reply_schema_removed_is_weakened() -> None:
    ref, tab = _pinned(BODY)
    old = {"targets": {"py:m.f": {"effects": "pure", "reply_schema": ref}}, "schemas": tab}
    new = {"targets": {"py:m.f": {"effects": "pure"}}, "schemas": {}}
    got = [c for c in lockmod.changes(old, new) if "reply_schema" in c.what]
    assert got and all(c.weakened for c in got)
    assert not any(c.weakened for c in lockmod.changes(new, old))    # a reply schema added: tightens


def test_contract_schema_removed_is_weakened() -> None:
    ref, tab = _pinned(BODY)
    old = {"contracts": {"c": {"type": "python", "schema": ref}}, "schemas": tab}
    new = {"contracts": {"c": {"type": "python"}}, "schemas": {}}
    assert any(c.weakened for c in lockmod.changes(old, new))
    assert not any(c.weakened for c in lockmod.changes(new, old))


# ---- 4. stdlib JSON Schema and pydantic models in $defs --------------------------------------------

@dataclasses.dataclass
class Shapes:
    one: tuple[int]
    two: tuple[int, str]
    many: tuple[int, ...]
    none: tuple[()]
    keyed: dict[typing.Literal["a", "b"], int]
    plain: dict[str, int]


def test_stdlib_json_schema_matches_pydantic_for_tuples_and_dict_keys() -> None:
    got = stdlib_json_schema(Shapes)["properties"]
    want = strip_prose(pydantic.TypeAdapter(Shapes).json_schema())["properties"]
    assert got == want


class Inner(pydantic.BaseModel):
    n: int = pydantic.Field(le=5)


class Inner9(pydantic.BaseModel):
    n: int = pydantic.Field(le=9)


@dataclasses.dataclass
class Reply5:
    inner: Inner


@dataclasses.dataclass
class Reply9:
    inner: Inner9


def test_loosened_pydantic_model_named_by_a_dataclass_reply_is_weakened() -> None:
    def lock(reply: type) -> dict[str, typing.Any]:
        pins = lockmod.Schemas()
        body = lockmod.resolved_schema(reply)
        body["$defs"] = {"m.Inner": next(iter(body["$defs"].values()))}      # one name on both sides
        body["fields"]["inner"] = {"$ref": "m.Inner"}
        body["dataclass"] = "Reply"
        ref = pins.add({"name": "m.Reply", **body})
        return {"targets": {"py:m.f": {"effects": "pure", "reply_schema": ref}}, "schemas": pins.table}
    a, b = lock(Reply5), lock(Reply9)
    assert "pydantic" in next(iter(a["schemas"].values()))["$defs"]["m.Inner"]
    assert any(c.weakened for c in lockmod.changes(a, b)), [str(c) for c in lockmod.changes(a, b)]
    assert not any(c.weakened for c in lockmod.changes(b, a))
