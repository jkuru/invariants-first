"""JSON Schema of a dataclass or TypedDict contract (json_schema): pydantic's TypeAdapter when it
can describe it, else stdlib_json_schema, read from the resolved annotations (types, Literal,
unions, containers, enums, nested classes as $defs, and the bounds Annotated metadata carries)."""
from __future__ import annotations

import collections.abc
import dataclasses
import enum
import typing
from typing import Any

from .contracts import MAX_DEPTH, _plain
from .pin_types import _class_shape, _is_alias, _is_typeddict, _unalias, strip_prose


def json_schema(cls: type) -> Any:
    """The JSON Schema of a dataclass or TypedDict contract, so constraint_weakening judges it as it
    judges a pydantic model: pydantic's TypeAdapter when pydantic is installed (it reads Field and
    annotated_types constraints), else stdlib_json_schema. Prose is stripped."""
    try:
        import pydantic
    except ImportError:
        return _plain(strip_prose(stdlib_json_schema(cls)))
    try:
        return _plain(strip_prose(pydantic.TypeAdapter(cls).json_schema()))
    except Exception:                                          # a type pydantic cannot describe
        return _plain(strip_prose(stdlib_json_schema(cls)))


_JSON_TYPES = {bool: "boolean", int: "integer", float: "number", str: "string", type(None): "null"}
_ARRAYS = frozenset({list, tuple, set, frozenset, collections.abc.Sequence, collections.abc.Set,
                     collections.abc.MutableSequence, collections.abc.Iterable})
_MAPS = frozenset({dict, collections.abc.Mapping, collections.abc.MutableMapping})

# annotated_types (and pydantic's Field metadata) attribute -> JSON Schema keyword (strings, then arrays)
_BOUND_ATTRS = {"gt": "exclusiveMinimum", "ge": "minimum", "lt": "exclusiveMaximum", "le": "maximum",
                "multiple_of": "multipleOf", "pattern": "pattern"}
_LEN_ATTRS = {"min_length": ("minLength", "minItems"), "max_length": ("maxLength", "maxItems")}


class _Defs(dict[str, Any]):
    """The $defs of one stdlib JSON Schema, and the class each key was written for (`owners`)."""

    def __init__(self) -> None:
        super().__init__()
        self.owners: dict[str, type] = {}


def stdlib_json_schema(cls: type) -> dict[str, Any]:
    """A JSON Schema of a dataclass or TypedDict from its resolved annotations, without pydantic:
    types, Literal, Optional and unions, containers, enums, nested dataclasses ($defs), and the
    bounds an Annotated metadata object carries as gt/ge/lt/le/min_length/max_length attributes."""
    defs: dict[str, Any] = _Defs()
    top = _local_def(_json_type(cls, defs, 0), defs)
    return {**top, "$defs": defs} if defs else top


def _local_def(top: Any, defs: dict[str, Any]) -> Any:
    """The top class's object schema in place of its {"$ref"}, unless a definition refers back to it
    (a self-referential class): then the reference and its definition stay, as pydantic writes it."""
    ref = top.get("$ref", "") if isinstance(top, dict) else ""
    if not ref.startswith("#/$defs/") or _refers_to(defs, ref):
        return top
    return defs.pop(ref[len("#/$defs/"):])


def _refers_to(node: Any, ref: str) -> bool:
    if isinstance(node, dict):
        return node.get("$ref") == ref or any(_refers_to(v, ref) for v in node.values())
    if isinstance(node, list):
        return any(_refers_to(v, ref) for v in node)
    return False


def _json_type(t: Any, defs: dict[str, Any], depth: int) -> dict[str, Any]:
    if depth > MAX_DEPTH:
        return {}
    d = depth + 1
    if t is Any:
        return {}
    u = _unalias(t)
    if u is not t:                                             # PEP 695: type X = ..., and X[int]
        return _json_type(u, defs, d)
    if _is_alias(typing.get_origin(t)):                        # X[...] its parameters do not take
        return {**_json_type(typing.get_origin(t).__value__, defs, d), "x-python-type": repr(t)}
    if t in _JSON_TYPES:
        return {"type": _JSON_TYPES[t]}
    origin, args = typing.get_origin(t), typing.get_args(t)
    if origin is typing.Annotated:
        return {**_json_type(args[0], defs, d), **_annotated_bounds(args[0], args[1:])}
    if origin is typing.Literal:
        vals = [_plain(a) for a in args]
        return {"const": vals[0]} if len(vals) == 1 else {"enum": vals}
    if origin is typing.Union or type(t).__name__ == "UnionType":
        return {"anyOf": [_json_type(a, defs, d) for a in args]}
    if origin in _ARRAYS or t in _ARRAYS:
        items = [a for a in args if a is not Ellipsis]
        return {"type": "array", **({"items": _json_type(items[0], defs, d)} if len(items) == 1 else
                                    {"prefixItems": [_json_type(a, defs, d) for a in items]} if items else {})}
    if origin in _MAPS or t in _MAPS:
        return {"type": "object", **({"additionalProperties": _json_type(args[1], defs, d)} if len(args) == 2 else {})}
    if isinstance(t, type) and issubclass(t, enum.Enum):
        return {"enum": [_plain(m.value) for m in t]}
    if isinstance(t, type) and (dataclasses.is_dataclass(t) or _is_typeddict(t)):
        return _json_object(t, defs, d)
    return {"x-python-type": getattr(t, "__qualname__", repr(t))}


def _annotated_bounds(base: Any, meta: tuple[Any, ...]) -> dict[str, Any]:
    base = _unalias(base)                                      # an alias of a list is an array
    while typing.get_origin(base) is typing.Annotated:
        base = _unalias(typing.get_args(base)[0])
    array = typing.get_origin(base) in _ARRAYS or base in _ARRAYS
    out: dict[str, Any] = {}
    # pydantic's Field(le=...) keeps its bounds as annotated_types objects in .metadata, not as
    # attributes: this fallback is what pins a TypedDict pydantic refuses (typing.TypedDict < 3.12)
    flat: list[Any] = []
    for m in meta:
        inner = getattr(m, "metadata", None)
        flat.extend([m, *inner] if isinstance(inner, list) else [m])
    for m in flat:
        for attr, kw in _BOUND_ATTRS.items():
            if getattr(m, attr, None) is not None:
                out[kw] = _plain(getattr(m, attr))
        for attr, (text_kw, array_kw) in _LEN_ATTRS.items():
            if getattr(m, attr, None) is not None:
                out[array_kw if array else text_kw] = getattr(m, attr)
    return out


def _json_object(cls: type, defs: dict[str, Any], depth: int) -> dict[str, Any]:
    """A dataclass or TypedDict as {"$ref": "#/$defs/Name"}, its object schema in `defs`."""
    name = _def_key(cls, defs)
    ref = {"$ref": f"#/$defs/{name}"}
    if name in defs:
        return ref
    if isinstance(defs, _Defs):
        defs.owners[name] = cls
    defs[name] = {}                                            # placeholder: cuts recursion
    shape = _class_shape(cls)
    defs[name] = {"type": "object", "properties": {n: _json_type(t, defs, depth) for n, t in shape.fields.items()},
                  "required": shape.required}
    return ref


def _def_key(cls: type, defs: dict[str, Any]) -> str:
    """The $defs key of a class, as pydantic writes it: its name (`Inner`, not `Outer.Inner`: a key
    holds no ".", which would split a diff path), or `module__Outer__Inner` when another class of the
    same name is already there (two classes are never one definition). On such a clash pydantic
    renames both classes and this renames only the later one, so there the two readings keep
    separate paths; a pin compares a lock with one taken the same way, so that is no change."""
    owners = defs.owners if isinstance(defs, _Defs) else {}
    found = [k for k, c in owners.items() if c is cls]
    if found:
        return found[0]
    key = _key_text(cls.__name__)
    if key not in defs:
        return key
    key = base = _key_text(f"{cls.__module__}__{cls.__qualname__}")
    n = 1
    while key in defs:
        n += 1
        key = f"{base}__{n}"
    return key


def _key_text(text: str) -> str:
    """A $defs key: letters, digits, _ and -; "." becomes "__" (pydantic's form), anything else "_"."""
    return "".join(ch if ch.isalnum() or ch in "_-" else "__" if ch == "." else "_" for ch in text)
