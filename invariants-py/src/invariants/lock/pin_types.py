"""Resolved types: a signature's annotations and a schema class (dataclass, TypedDict, pydantic
model, {field: type} mapping) as JSON-able descriptions (resolved_schema, _type_schema), PEP 695
aliases expanded, and the content-addressed schema table (Schemas) each schema goes to once,
without its prose (strip_prose). Pure: no target module is imported."""
from __future__ import annotations

import dataclasses
import enum
import json
import typing
from typing import Any

from ..errors import LoadError
from .contracts import MAX_DEFS, MAX_DEPTH, _plain, _sha, canonical


# --- schemas: stripping, content addressing --------------------------------------------------------
SCHEMA_PROSE = frozenset({"title", "description", "summary", "tags", "examples", "example", "$comment",
                          "externalDocs"})
_NAME_MAPS = frozenset({"properties", "$defs", "definitions", "patternProperties", "dependentSchemas", "fields",
                        "responses", "content", "schemas", "headers", "encoding"})
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
        return {"truncated": repr(t)[:80]}
    d = depth + 1
    if t is type(None) or t is None:
        return "null"
    u = _unalias(t)                                        # PEP 695: type X = ..., X[int]
    if u is not t:
        return _type_schema(u, defs, seen, pins, d)
    origin = typing.get_origin(t)
    if origin is typing.Literal:
        return {"literal": sorted((_plain(a) for a in typing.get_args(t)), key=repr)}
    if origin is typing.Annotated:
        return _type_schema(typing.get_args(t)[0], defs, seen, pins, d)
    if origin is not None:
        return _generic_schema(t, origin, defs, seen, pins, d)
    if hasattr(t, "__supertype__"):                        # typing.NewType
        return _type_schema(t.__supertype__, defs, seen, pins, d)
    if isinstance(t, type):
        if issubclass(t, enum.Enum):
            return {"enum": t.__qualname__, "values": sorted((repr(m.value) for m in t), key=str)}
        if _is_schema_class(t):
            return _schema_class_ref(t, defs, seen, pins)
        return getattr(t, "__qualname__", repr(t))
    if isinstance(t, typing.TypeVar):
        return f"~{t.__name__}"
    if isinstance(t, typing.ForwardRef):
        return {"unresolved": t.__forward_arg__}
    if isinstance(t, str):
        return {"unresolved": t}
    return repr(t)


def _generic_schema(t: Any, origin: Any, defs: dict[str, Any], seen: set[int], pins: Schemas | None,
                    d: int) -> Any:
    """A parameterised annotation (list[int], X | None, an alias of a generic) with its arguments."""
    if _is_alias(origin):                                  # X[...] its parameters do not take
        return {"generic": origin.__name__, "args": [_type_schema(a, defs, seen, pins, d) for a in typing.get_args(t)],
                "value": _type_schema(origin.__value__, defs, seen, pins, d)}
    name = getattr(origin, "__name__", None) or str(origin)
    if name in ("Union", "UnionType") or str(origin) in ("typing.Union", "<class 'types.UnionType'>"):
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


def _is_schema_class(t: Any) -> bool:
    return isinstance(t, type) and (dataclasses.is_dataclass(t) or _is_pydantic(t) or _is_typeddict(t))


def _is_pydantic(t: Any) -> bool:
    try:
        import pydantic
    except ImportError:
        return False
    return isinstance(t, type) and issubclass(t, pydantic.BaseModel)


def _is_typeddict(t: Any) -> bool:
    return isinstance(t, type) and issubclass(t, dict) and hasattr(t, "__annotations__") and hasattr(t, "__total__")


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
    if _is_pydantic(schema):
        out: Any = {"pydantic": _plain(schema.model_json_schema())}
    elif isinstance(schema, type) and (dataclasses.is_dataclass(schema) or _is_typeddict(schema)):
        out = _class_fields(schema, defs, seen)
    elif isinstance(schema, dict):
        out = {"fields": {str(k): _type_schema(v, defs, seen) for k, v in sorted(schema.items(), key=lambda kv: str(kv[0]))}}
    else:
        out = {"type": _type_schema(schema, defs, seen)}
    if top and defs:
        out = {**out, "$defs": _plain(defs)}
    return _plain(strip_prose(out)) if top else _plain(out)


class _ClassShape(typing.NamedTuple):
    kind: str                       # "dataclass" | "typeddict"
    fields: dict[str, Any]          # field name -> resolved annotation (Annotated kept), in field order
    required: list[str]             # sorted


def _class_shape(cls: type) -> _ClassShape:
    """A dataclass or TypedDict's resolved fields and its required ones: what both its resolved schema
    (_class_fields) and its stdlib JSON Schema (_json_object) are read from."""
    try:
        hints = typing.get_type_hints(cls, include_extras=True)
    except Exception as e:                                 # an unresolvable forward reference
        raise LoadError(f"schema {cls.__qualname__}: cannot resolve its annotations ({e})") from e
    if dataclasses.is_dataclass(cls):
        fields = dataclasses.fields(cls)
        return _ClassShape("dataclass", {f.name: hints[f.name] for f in fields},
                           sorted(f.name for f in fields
                                  if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING))
    # Required[], NotRequired[] and ReadOnly[] (typing's or typing_extensions') qualify the key, not its
    # value: they are unwrapped, and Required / NotRequired decide the key's required-ness over the
    # class's total= (typing.TypedDict before 3.13 does not see them inside typing_extensions' ReadOnly)
    required = set(getattr(cls, "__required_keys__", ()))
    keys: dict[str, Any] = {}
    for n, t in hints.items():
        keys[n], quals = _key_qualifiers(t)
        if "Required" in quals:
            required.add(n)
        elif "NotRequired" in quals:
            required.discard(n)
    return _ClassShape("typeddict", keys, sorted(required))


_KEY_QUALIFIERS = frozenset({"Required", "NotRequired", "ReadOnly"})


def _key_qualifiers(t: Any) -> tuple[Any, set[str]]:
    """A TypedDict key's annotation without its Required / NotRequired / ReadOnly wrappers (also inside
    Annotated, whose metadata stays), and the names of the wrappers it had."""
    origin = typing.get_origin(t)
    if origin is typing.Annotated:
        inner, quals = _key_qualifiers(typing.get_args(t)[0])
        return (typing.Annotated[(inner, *t.__metadata__)] if quals else t), quals
    name = getattr(origin, "_name", None)
    if name in _KEY_QUALIFIERS and type(origin).__module__ in ("typing", "typing_extensions"):
        inner, quals = _key_qualifiers(typing.get_args(t)[0])
        return inner, {*quals, name}
    return t, set()


def _class_fields(schema: type, defs: dict[str, Any], seen: set[int]) -> dict[str, Any]:
    """A dataclass or TypedDict as its resolved field types and its required fields."""
    shape = _class_shape(schema)
    return {shape.kind: schema.__qualname__,
            "fields": {n: _type_schema(t, defs, seen) for n, t in shape.fields.items()},
            "required": shape.required}



def _is_alias(t: Any) -> bool:
    """A PEP 695 alias (typing.TypeAliasType, 3.12+, or typing_extensions' backport)."""
    return type(t).__name__ == "TypeAliasType" and hasattr(t, "__value__")


def _unalias(t: Any) -> Any:
    """t with a PEP 695 alias expanded: `X` of `type X = ...` to its value, `X[int]` of `type X[T] = ...`
    to its value with the type parameters substituted (`list[T]` -> `list[int]`). t itself when it is
    no alias, or when its arguments do not fit the parameters (a TypeVarTuple, a ParamSpec, a wrong
    count): the callers then pin the arguments and the value side by side, so neither is dropped."""
    for _ in range(MAX_DEPTH):
        if _is_alias(t):
            t = t.__value__
            continue
        origin = typing.get_origin(t)
        if not _is_alias(origin):
            return t
        params, args = tuple(getattr(origin, "__type_params__", ())), typing.get_args(t)
        if len(params) != len(args) or not all(isinstance(p, typing.TypeVar) for p in params):
            return t
        try:
            t = _substitute(origin.__value__, dict(zip(params, args)))
        except Exception:                                      # a value typing cannot re-parameterise
            return t
    return t


def _substitute(value: Any, mapping: dict[Any, Any]) -> Any:
    """value (an alias's) with its free type variables replaced: `T` itself, or `list[T]`, `T | None`,
    `Annotated[T, Gt(0)]` re-parameterised through typing's own subscription."""
    if isinstance(value, typing.TypeVar):
        return mapping.get(value, value)
    free = tuple(getattr(value, "__parameters__", ()) or ())
    return value[tuple(mapping.get(p, p) for p in free)] if free else value
