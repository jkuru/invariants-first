"""Resolved types: a signature's annotations and a schema class (dataclass, TypedDict, pydantic
model, {field: type} mapping) as JSON-able descriptions (resolved_schema, _type_schema), PEP 695
aliases expanded, and the content-addressed schema table (Schemas) each schema goes to once,
without its prose (strip_prose). Pure: no target module is imported."""
from __future__ import annotations

import json
import typing
from typing import Any

from invariants_cel import typeinfo as ti
from invariants_schema_diff.changes import MAX_DEPTH, _plain, _sha, canonical

from ..errors import LoadError
from .contracts import MAX_DEFS


# --- schemas: stripping, content addressing --------------------------------------------------------
SCHEMA_PROSE = frozenset({"title", "description", "summary", "tags", "examples", "example", "$comment",
                          "externalDocs"})
# fix round 35: dependentRequired and dependencies are keyed by property names too, and a
# discriminator's mapping by the values it maps: a property called `title` is not prose there either
_NAME_MAPS = frozenset({"properties", "$defs", "definitions", "patternProperties", "dependentSchemas", "fields",
                        "responses", "content", "schemas", "headers", "encoding", "dependentRequired",
                        "dependencies", "mapping"})
_DATA_KEYS = frozenset({"enum", "const", "default", "literal", "values", "required"})


def strip_prose(node: Any) -> Any:
    """A schema without its prose (title, description, summary, tags, examples) at every level. Keys of
    name maps (properties, $defs, ...) are names, not keywords, so a property called `title` stays."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in SCHEMA_PROSE:
                continue
            if k in _DATA_KEYS:
                out[k] = v
            elif k in _NAME_MAPS and isinstance(v, dict):
                out[k] = {name: strip_prose(s) for name, s in v.items()}
            else:
                out[k] = strip_prose(v)
        return out
    if isinstance(node, list):
        return [strip_prose(v) for v in node]
    return node


class Schemas:
    """The content-addressed schema table of one lock: digest -> canonical schema.

    With inline=True (direct API use, no table to write), add() returns the schema itself."""

    def __init__(self, inline: bool = False) -> None:
        self.table: dict[str, Any] = {}
        self.inline = inline

    def add(self, schema: Any) -> Any:
        body = _plain(strip_prose(schema))
        if self.inline:
            return body
        digest = _sha(canonical(body))
        self.table[digest] = body
        return {"$ref": digest}


def _class_ref(t: type) -> str:
    return f"{t.__module__}.{t.__qualname__}"


def _type_schema(t: Any, defs: dict[str, Any], seen: set[int], pins: Schemas | None = None,
                 depth: int = 0) -> Any:
    """A JSON-able description of a resolved annotation. Literal values, unions, aliases and nested
    schemas are expanded. With `pins`, a schema class is pinned as its own entry in the schema table
    ({"$ref": digest}); without, it goes into `defs` by name (one closure)."""
    if depth > MAX_DEPTH:
        return {"truncated": _name(t)}
    d = depth + 1
    if t is type(None) or t is None:
        return "null"
    u = ti.unalias(t)                                      # PEP 695: type X = ..., X[int]; NewType
    if u is not t:
        return _type_schema(u, defs, seen, pins, d)
    origin = typing.get_origin(t)
    if ti.is_literal(t):
        return {"literal": sorted((_plain(a) for a in typing.get_args(t)), key=repr)}
    if ti.is_annotated(t):
        return _type_schema(typing.get_args(t)[0], defs, seen, pins, d)
    if origin is not None:
        return _generic_schema(t, origin, defs, seen, pins, d)
    if ti.is_newtype(t):                                   # a NewType-shaped object unalias did not take
        return _type_schema(t.__supertype__, defs, seen, pins, d)
    if isinstance(t, type):
        if ti.is_enum(t):
            return {"enum": t.__qualname__, "values": sorted((repr(m.value) for m in t), key=str)}
        if ti.is_schema_class(t):
            return _schema_class_ref(t, defs, seen, pins)
        return getattr(t, "__qualname__", repr(t))
    if isinstance(t, typing.TypeVar):
        return f"~{t.__name__}"
    if isinstance(t, typing.ForwardRef):
        return {"unresolved": t.__forward_arg__}
    if isinstance(t, str):
        return {"unresolved": t}
    return _opaque(t)


_NAMED_REPR = ("typing", "typing_extensions", "types")     # a special form or alias: its repr is names


def _name(t: Any) -> str:
    """A type's (or a generic's origin's) qualified name, else its own type's: never a repr."""
    o = typing.get_origin(t) or t
    if isinstance(o, type):
        return o.__qualname__
    return str(getattr(o, "_name", None) or getattr(o, "__name__", None) or f"a {type(o).__qualname__} object")


def _opaque(t: Any) -> Any:
    """An annotation object with no recognised form, pinned without its repr (which can carry a memory
    address, so the lock changes every run, or data the object holds): as before for Ellipsis, a
    typing special form (typing.Never: its repr is its name) and a Callable's parameter list of types;
    any other object by its type only."""
    if t is Ellipsis:
        return "Ellipsis"
    if type(t).__module__ in _NAMED_REPR:
        return repr(t)
    if isinstance(t, list):                                # Callable[[int, str], R]: the parameters
        return "[" + ", ".join(x if isinstance(x, str) else json.dumps(x, sort_keys=True)
                               for x in (repr(a) if isinstance(a, type) else _opaque(a) for a in t)) + "]"
    return {"object": f"{type(t).__module__}.{type(t).__qualname__}"}


def _generic_schema(t: Any, origin: Any, defs: dict[str, Any], seen: set[int], pins: Schemas | None,
                    d: int) -> Any:
    """A parameterised annotation (list[int], X | None, an alias of a generic) with its arguments."""
    value = ti.alias_value(t)
    if value is not None:                                  # X[...] its parameters do not take
        return {"generic": origin.__name__, "args": [_type_schema(a, defs, seen, pins, d) for a in typing.get_args(t)],
                "value": _type_schema(value, defs, seen, pins, d)}
    name = getattr(origin, "__name__", None) or str(origin)
    # a union, and (kept as pinned so far, so no lock changes) any generic class named Union or UnionType
    if ti.is_union(t) or name in ("Union", "UnionType"):
        opts = [_type_schema(a, defs, seen, pins, d) for a in typing.get_args(t)]
        return {"anyOf": sorted(opts, key=lambda o: json.dumps(o, sort_keys=True))}
    return {"generic": name, "args": [_type_schema(a, defs, seen, pins, d) for a in typing.get_args(t)]}


def _schema_class_ref(t: type, defs: dict[str, Any], seen: set[int], pins: Schemas | None) -> Any:
    """A dataclass, TypedDict or pydantic model as a reference: to its own schema-table entry with
    `pins`, else to its resolved schema in `defs` (at most MAX_DEFS of them)."""
    ref = _class_ref(t)
    if pins is not None:
        return pins.add({"name": ref, **resolved_schema(t)})
    if ref not in defs and id(t) not in seen:
        if len(defs) >= MAX_DEFS:
            return {"$ref": ref, "truncated": f"more than {MAX_DEFS} nested schemas"}
        seen.add(id(t))
        defs[ref] = None                           # placeholder: cuts recursion
        defs[ref] = resolved_schema(t, defs, seen)
    return {"$ref": ref}


def resolved_schema(schema: Any, defs: dict[str, Any] | None = None, seen: set[int] | None = None) -> Any:
    """The resolved, canonical schema of a closed schema: pydantic model_json_schema(), or the resolved
    annotations of a dataclass, TypedDict or {field: type} mapping (aliases and nested schema classes
    are expanded into $defs). Prose (title, description, examples, ...) is stripped."""
    top = defs is None
    defs = {} if defs is None else defs
    seen = set() if seen is None else seen
    model = getattr(schema, "__inv_pydantic__", None)          # a closed() wrapper of a pydantic model
    if model is not None:
        schema = model
    if ti.is_pydantic_model(schema):
        out: Any = {"pydantic": _plain(schema.model_json_schema())}
    elif ti.is_schema_class(schema):                       # a dataclass or TypedDict
        out = _class_fields(schema, defs, seen)
    elif isinstance(schema, dict):
        out = {"fields": {str(k): _type_schema(v, defs, seen) for k, v in sorted(schema.items(), key=lambda kv: str(kv[0]))}}
    else:
        out = {"type": _type_schema(schema, defs, seen)}
    if top and defs:
        out = {**out, "$defs": _plain(defs)}
    return _plain(strip_prose(out)) if top else _plain(out)


def class_shape(cls: type) -> ti.ClassShape:
    """invariants_cel.typeinfo.class_shape, its TypeCheckError (an annotation that does not resolve) raised as the
    LoadError the lock reports, with the same message and cause."""
    try:
        return ti.class_shape(cls)
    except ti.TypeCheckError as e:
        raise LoadError(str(e)) from e.__cause__


def _class_fields(schema: type, defs: dict[str, Any], seen: set[int]) -> dict[str, Any]:
    """A dataclass or TypedDict as its resolved field types and its required fields."""
    shape = class_shape(schema)
    return {shape.kind: schema.__qualname__,
            "fields": {n: _type_schema(t, defs, seen) for n, t in shape.fields.items()},
            "required": shape.required}
