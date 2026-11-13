"""Taking the pins: `build` turns a loaded rule document into a version-3 lock. Signatures are pinned
as text and as resolved types, a parameter's Annotated constraints as JSON Schema (param_schemas),
pydantic models, dataclasses, TypedDicts and {field: type} mappings as resolved schemas or JSON Schema
(stdlib_json_schema without pydantic), and OpenAPI documents (a file or a live app) per operation. Each
schema goes to the content-addressed table (Schemas) once, without its prose. Imports target modules."""
from __future__ import annotations

import collections.abc
import dataclasses
import enum
import fnmatch
import importlib
import inspect
import json
import pathlib
import typing
from typing import Any

from ..errors import LoadError
from ..resolve import resolve
from .contracts import (DOC_FIELDS, ENFORCER_DEFAULTS, FEEDBACK_DEFAULT, LOCK_VERSION, MAX_DEFS, MAX_DEPTH,
                        MEDIATION_DOC, PY_CONTRACTS, SCHEMA_CONTRACTS, _plain, _sha, canonical)


def rule_hash(raw: dict[str, Any]) -> str:
    """Hash of a rule as written, without the computed `evidence` field (lock version 1's rule pin)."""
    return _sha(canonical({k: v for k, v in raw.items() if k != "evidence"}))


def expand_applies_to(doc: dict[str, Any]) -> dict[str, list[str]]:
    """Rule id -> sorted target keys its applies_to patterns match. A pattern that matches nothing fails."""
    keys = sorted((doc.get("targets") or {}).keys())
    out: dict[str, list[str]] = {}
    for raw in doc.get("rules") or []:
        if "applies_to" not in raw:
            continue
        pats = raw["applies_to"]
        pats = [pats] if isinstance(pats, str) else pats
        if not isinstance(pats, list) or not pats or not all(isinstance(p, str) for p in pats):
            raise LoadError(f"{raw['id']}: applies_to must be a pattern or a non-empty list of patterns")
        hits: set[str] = set()
        for p in pats:
            got = fnmatch.filter(keys, p)
            if not got:
                raise LoadError(f"{raw['id']}: applies_to pattern {p!r} matches no entry in targets:")
            hits.update(got)
        out[raw["id"]] = sorted(hits)
    return out


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


# --- signatures ---------------------------------------------------------------------------------
def _callable_hints(obj: Any) -> tuple[Any, dict[str, Any] | None]:
    """The unwrapped callable and its resolved annotations (a class's are its __init__'s), or None when
    a name cannot be resolved. signature_types and param_schemas read them; a signature pin reads once."""
    fn = inspect.unwrap(obj)
    src = fn.__init__ if inspect.isclass(fn) else fn
    try:
        return fn, typing.get_type_hints(src, include_extras=True)
    except Exception:                                          # an unresolvable name: keep the text
        return fn, None


def signature_types(obj: Any, pins: Schemas) -> dict[str, Any]:
    """Resolved parameter and return types of a callable: {param: type, "return": type}.

    Read with typing.get_type_hints, so `from __future__ import annotations` text and aliases are
    resolved; a dataclass, TypedDict or pydantic model is pinned by its fields in the schema table."""
    return _signature_types(*_callable_hints(obj), pins)


def _signature_types(fn: Any, hints: dict[str, Any] | None, pins: Schemas) -> dict[str, Any]:
    sig = inspect.signature(fn)
    out: dict[str, Any] = {}
    for name, p in sig.parameters.items():
        if p.annotation is inspect.Parameter.empty:
            continue
        out[name] = (_type_schema(hints[name], {}, set(), pins) if hints is not None and name in hints
                     else {"unresolved": str(p.annotation)})
    if sig.return_annotation is not inspect.Signature.empty and not inspect.isclass(fn):
        out["return"] = (_type_schema(hints["return"], {}, set(), pins) if hints is not None and "return" in hints
                         else {"unresolved": str(sig.return_annotation)})
    return _plain(out)


def _signature_pin(obj: Any, label: str, pins: Schemas) -> dict[str, Any]:
    fn = inspect.unwrap(obj)
    try:
        text = f"{getattr(fn, '__qualname__', label)}{inspect.signature(fn)}"
    except (TypeError, ValueError) as e:
        raise LoadError(f"{label}: no signature ({e})") from e
    pin: dict[str, Any] = {"signature": text}
    fn, hints = _callable_hints(obj)
    types = _signature_types(fn, hints, pins)
    if types:
        pin["types"] = types
    bounds = _param_schemas(fn, hints)
    if bounds:
        pin["param_schemas"] = bounds
    return pin


def _constrains(m: Any, depth: int = 0) -> bool:
    """An Annotated metadata object that constrains the value: an annotated_types constraint (Gt,
    Interval, Len, ...), a pydantic Field(...) or StringConstraints carrying one, or any object with a
    gt/ge/lt/le/multiple_of/pattern/min_length/max_length attribute. FastAPI's Depends(...) and other
    markers do not."""
    if depth > 4:
        return False
    if type(m).__module__.split(".")[0] == "annotated_types":
        return True
    if any(getattr(m, a, None) is not None for a in (*_BOUND_ATTRS, *_LEN_ATTRS)):
        return True
    meta = getattr(m, "metadata", None)
    return isinstance(meta, list) and any(_constrains(x, depth + 1) for x in meta)


def _has_constraints(t: Any, depth: int = 0) -> bool:
    """t is, or holds (in a union, a container, an alias), an Annotated type with a constraint."""
    if depth > MAX_DEPTH:
        return False
    u = _unalias(t)
    if u is not t:
        return _has_constraints(u, depth + 1)
    origin = typing.get_origin(t)
    if _is_alias(origin) and _has_constraints(origin.__value__, depth + 1):
        return True                                            # X[...] its parameters do not take
    args = typing.get_args(t)
    if typing.get_origin(t) is typing.Annotated:
        return any(_constrains(m) for m in args[1:]) or _has_constraints(args[0], depth + 1)
    return any(_has_constraints(a, depth + 1) for a in args if not isinstance(a, (str, int, float, bool)))


def _annotation_schema(t: Any) -> Any:
    """The JSON Schema of one annotation, constraints included: pydantic's TypeAdapter when pydantic is
    installed and can describe it, else the stdlib reading (bounds from metadata attributes)."""
    try:
        import pydantic
        return _plain(strip_prose(pydantic.TypeAdapter(t).json_schema()))
    except Exception:
        defs: dict[str, Any] = _Defs()
        top = _json_type(t, defs, 0)
        return _plain(strip_prose({**top, "$defs": defs} if defs else top))


def param_schemas(obj: Any) -> dict[str, Any]:
    """{param: JSON Schema} for each parameter (and `return`) whose annotation carries a constraint
    (`Annotated[int, Field(gt=0)]`, `Annotated[str, MaxLen(10)]`, also inside Optional or list). The
    resolved `types` drop Annotated metadata, so these pins are what makes a constraint removed or
    loosened in a signature a weakening (judged like any pinned schema). Empty when none has one."""
    return _param_schemas(*_callable_hints(obj))


def _param_schemas(fn: Any, hints: dict[str, Any] | None) -> dict[str, Any]:
    if hints is None:                                          # unresolvable: the signature text pins it
        return {}
    return {name: _annotation_schema(t) for name, t in sorted(hints.items())
            if not (inspect.isclass(fn) and name == "return") and _has_constraints(t)}


# --- contracts ----------------------------------------------------------------------------------
def _python_ref(name: str, ref: str) -> Any:
    mod_name, _, attr = ref.partition(":")
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as e:
        raise LoadError(f"contract {name}: cannot import {mod_name}: {e}") from e
    obj: Any = mod
    for part in [p for p in attr.split(".") if p]:
        if not hasattr(obj, part):
            raise LoadError(f"contract {name}: {mod_name} has no {attr}")
        obj = getattr(obj, part)
    return obj


_SHARED_SCHEMAS = "#/components/schemas/"
_PARAM_KEYS = ("name", "in", "required", "schema", "content", "style", "explode", "allowEmptyValue",
               "allowReserved")


def _openapi_file(name: str, path: pathlib.Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise LoadError(f"contract {name}: {path.name} cannot be read as UTF-8 text ({type(e).__name__})") from e
    except OSError as e:
        raise LoadError(f"contract {name}: {path.name} cannot be read ({type(e).__name__})") from e
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
            return yaml.safe_load(text)
        except Exception as e:
            raise LoadError(f"contract {name}: {path.name} is neither JSON nor YAML ({e})") from e


def _openapi_app(name: str, ref: str) -> Any:
    """The OpenAPI document of a live app (`module:app`, for example a FastAPI app): app.openapi()."""
    app = _python_ref(name, ref)
    make = getattr(app, "openapi", None)
    if not callable(make):
        raise LoadError(f"contract {name}: {ref} has no openapi() method (a FastAPI app has one)")
    try:
        return make()
    except Exception as e:
        raise LoadError(f"contract {name}: {ref}.openapi() failed ({type(e).__name__}: {e})") from e


def _operation_ids(spec: Any) -> list[str]:
    return sorted({op["operationId"] for item in (spec.get("paths") or {}).values() if isinstance(item, dict)
                   for op in item.values() if isinstance(op, dict) and isinstance(op.get("operationId"), str)})


class _OpenAPIDoc:
    """One OpenAPI document being pinned: local $refs resolved, shared component schemas sent to the
    schema table (`pins`) once, labelled with `label`; errors name the contract."""

    def __init__(self, name: str, label: str, spec: dict[str, Any], pins: Schemas) -> None:
        self.name, self.label, self.spec, self.pins = name, label, spec, pins

    def lookup(self, ref: str) -> Any:
        target: Any = self.spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                raise LoadError(f"contract {self.name}: unresolvable $ref {ref}")
            target = target[part]
        return target

    def schema(self, node: Any, stack: tuple[str, ...]) -> Any:
        """A schema object with local $refs resolved; a shared component becomes a table entry."""
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/"):
                if ref in stack:
                    return {"$ref": ref, "recursive": True}
                body = self.schema(self.lookup(ref), (*stack, ref))
                got = (self.pins.add({"name": f"{self.label}{ref[1:]}", "schema": body})
                       if ref.startswith(_SHARED_SCHEMAS) else body)
                rest = {k: self.schema(v, stack) for k, v in node.items() if k != "$ref" and k not in SCHEMA_PROSE}
                return {"allOf": [got], **rest} if rest else got
            return {k: (v if k in _DATA_KEYS else self.schema(v, stack)) for k, v in node.items()}
        if isinstance(node, list):
            return [self.schema(v, stack) for v in node]
        return node

    def deref(self, node: Any) -> Any:
        """A parameter, request body or response object behind a $ref."""
        seen: set[str] = set()
        while isinstance(node, dict) and isinstance(node.get("$ref"), str) and node["$ref"].startswith("#/"):
            if node["$ref"] in seen:
                raise LoadError(f"contract {self.name}: $ref cycle at {node['$ref']}")
            seen.add(node["$ref"])
            node = self.lookup(node["$ref"])
        return node

    def content(self, c: Any) -> dict[str, Any]:
        return {mt: self.schema((m or {}).get("schema"), ()) for mt, m in sorted((c or {}).items())
                if isinstance(m, dict)}

    def parameters(self, item: dict[str, Any], op: dict[str, Any]) -> dict[tuple[str, str], Any]:
        """The path item's and the operation's parameters, keyed by (in, name): the operation's win."""
        params: dict[tuple[str, str], Any] = {}
        for p in [*(item.get("parameters") or []), *(op.get("parameters") or [])]:
            p = self.deref(p)
            if isinstance(p, dict):
                params[(str(p.get("in")), str(p.get("name")))] = {
                    k: (self.schema(v, ()) if k == "schema" else self.content(v) if k == "content" else v)
                    for k, v in p.items() if k in _PARAM_KEYS}
        return params

    def operation(self, route: str, method: str, item: dict[str, Any], op: dict[str, Any]) -> dict[str, Any]:
        """One operation: method, path, and the schemas of its parameters, request body and responses."""
        params = self.parameters(item, op)
        entry: dict[str, Any] = {"method": method.upper(), "path": route}
        if params:
            entry["parameters"] = [params[k] for k in sorted(params)]
        if "requestBody" in op:
            rb = self.deref(op["requestBody"]) or {}
            entry["requestBody"] = {"required": bool(rb.get("required", False)),
                                    "content": self.content(rb.get("content"))}
        if op.get("responses"):
            entry["responses"] = {str(code): self.content((self.deref(r) or {}).get("content"))
                                  for code, r in sorted(op["responses"].items(), key=lambda kv: str(kv[0]))}
        return entry


def _openapi_operations(name: str, label: str, spec: Any, ops: list[str], pins: Schemas) -> dict[str, Any]:
    """The operations named by rules, keyed by operationId: method, path, and the schemas of their
    parameters, request body and responses. Shared component schemas go to the schema table once."""
    if not isinstance(spec, dict):
        raise LoadError(f"contract {name}: {label} is not an OpenAPI document")
    doc = _OpenAPIDoc(name, label, spec, pins)
    found: dict[str, Any] = {}
    for route, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if isinstance(op, dict) and op.get("operationId") in ops:
                found[op["operationId"]] = strip_prose(doc.operation(route, method, item, op))
    missing = sorted(set(ops) - set(found))
    if missing:
        raise LoadError(f"contract {name}: {label} has no operation {', '.join(missing)}")
    return _plain(found)


def contract_hash(name: str, c: dict[str, Any], base_dir: pathlib.Path,
                  operations: list[str] | None = None, pins: Schemas | None = None) -> dict[str, Any]:
    """Pin one contract at contract granularity (see the module docstring). Without `pins`, schemas are
    inlined in the entry instead of going to a schema table."""
    pins = Schemas(inline=True) if pins is None else pins
    ctype = c.get("type")
    if ctype == "openapi" and "app" in c:
        return _openapi_app_pin(name, c, operations, pins)
    ref = c.get("ref")
    if not isinstance(ref, str) or not ref:
        raise LoadError(f"contract {name}: needs a string ref")
    entry: dict[str, Any] = {"type": ctype, "ref": ref}
    if ctype in PY_CONTRACTS or (ctype in SCHEMA_CONTRACTS and ":" in ref):
        return _python_contract_pin(name, ref, entry, pins)
    f = base_dir / ref
    if not f.is_file():
        raise LoadError(f"contract {name}: {ref} not found next to the rule file")
    if ctype == "openapi" and operations:
        entry["operations"] = _openapi_operations(name, f.name, _openapi_file(name, f), sorted(set(operations)), pins)
        return entry
    try:
        entry["hash"] = _sha(f.read_bytes())
    except OSError as e:
        raise LoadError(f"contract {name}: {f.name} cannot be read ({type(e).__name__})") from e
    if ctype == "openapi":
        _pin_every_operation(name, f, entry, pins)
    return entry


def _python_contract_pin(name: str, ref: str, entry: dict[str, Any], pins: Schemas) -> dict[str, Any]:
    """A contract that is a Python object (`module:attr`): a schema class, mapping or Enum as its
    schema, a callable as its signature, any other object by the digest of its source."""
    obj = _python_ref(name, ref)
    if inspect.ismodule(obj):
        return entry                                # its targets and providers carry the pins
    if (isinstance(obj, type) and dataclasses.is_dataclass(obj)) or _is_typeddict(obj):
        entry["schema"] = pins.add({"name": _class_ref(obj), "jsonschema": json_schema(obj)})
    elif _is_schema_class(obj) or isinstance(obj, dict) or getattr(obj, "__inv_pydantic__", None) is not None:
        label = _class_ref(obj) if isinstance(obj, type) else ref
        entry["schema"] = pins.add({"name": label, **resolved_schema(obj)})
    elif isinstance(obj, type) and issubclass(obj, enum.Enum):
        entry["schema"] = pins.add({"name": _class_ref(obj), **_plain({"type": _type_schema(obj, {}, set())})})
    elif callable(obj):
        entry.update(_signature_pin(obj, f"contract {name}", pins))
    else:
        try:
            entry["hash"] = _sha(inspect.getsource(obj).encode())
        except (OSError, TypeError) as e:
            raise LoadError(f"contract {name}: cannot pin {ref} ({e})") from e
    return entry


def _pin_every_operation(name: str, f: pathlib.Path, entry: dict[str, Any], pins: Schemas) -> None:
    """A whole-file OpenAPI contract (no rule names an operation of it): any edit re-pins the digest,
    and every operation is pinned too, so a looser schema in one of them is judged a weakening."""
    try:
        spec = _openapi_file(name, f)
        ids = _operation_ids(spec) if isinstance(spec, dict) else []
        if ids:
            trial = Schemas(inline=pins.inline)            # merged only when every operation reads
            entry["operations"] = _openapi_operations(name, f.name, spec, ids, trial)
            pins.table.update(trial.table)
    except LoadError:
        pass                                        # not a document it can read: the digest pins it


def _openapi_app_pin(name: str, c: dict[str, Any], operations: list[str] | None, pins: Schemas) -> dict[str, Any]:
    """An `openapi` contract read from a live app (`app: module:app`): the operations rules (or the
    contract's `operation`) name, or every operation when none does: the document is generated, so
    there is no file to digest."""
    app = c.get("app")
    if not isinstance(app, str) or ":" not in app:
        raise LoadError(f"contract {name}: app must be `module:attribute` (a FastAPI app, for example)")
    if "ref" in c:
        raise LoadError(f"contract {name}: give ref (an OpenAPI file) or app (a live app), not both")
    spec = _openapi_app(name, app)
    named = [*(operations or []), *([c["operation"]] if isinstance(c.get("operation"), str) else [])]
    ops = sorted(set(named)) if named else _operation_ids(spec if isinstance(spec, dict) else {})
    return {"type": "openapi", "app": app, "operations": _openapi_operations(name, app, spec, ops, pins)}


# --- building -----------------------------------------------------------------------------------
def normalize_semantic(sem: dict[str, Any], compares_to: Any = None) -> dict[str, Any]:
    """Fill the defaults the loader applies, so writing a default out (or leaving it out) is no change:
    enforcer on_violation/on_error, model_policy format_retries, semantic_resamples and feedback."""
    sem = dict(sem)
    sem.pop("status", None)                            # the entry holds it once
    if isinstance(sem.get("mediation"), dict):
        sem["mediation"] = {k: v for k, v in sem["mediation"].items() if k not in MEDIATION_DOC}
    if isinstance(sem.get("enforcers"), list):
        sem["enforcers"] = [({**ENFORCER_DEFAULTS, **e} if isinstance(e, dict) and e.get("mode") else e)
                            for e in sem["enforcers"]]
    pol = sem.get("model_policy")
    if isinstance(pol, dict):
        held = (compares_to if compares_to is not None else sem.get("compares_to")) == "held_data"
        pol = {"format_retries": 2, "semantic_resamples": 0 if held else 1, **pol}
        fb = pol.get("feedback")
        pol["feedback"] = {**FEEDBACK_DEFAULT, **fb} if isinstance(fb, dict) else (fb if "feedback" in pol
                                                                                 else dict(FEEDBACK_DEFAULT))
        sem["model_policy"] = pol
    return _plain(sem)


def rule_semantics(raw: dict[str, Any]) -> dict[str, Any]:
    """The part of a rule its meaning depends on; prose fields, id, rev and status are left out."""
    sem = {k: v for k, v in raw.items() if k not in (*DOC_FIELDS, "id", "rev", "evidence")}
    return normalize_semantic(sem)


def _doc_digests(raw: dict[str, Any]) -> dict[str, str]:
    out = {k: _sha(canonical(raw[k])) for k in DOC_FIELDS if k in raw}
    med = raw.get("mediation")
    if isinstance(med, dict):
        out.update({f"mediation.{k}": _sha(canonical(med[k])) for k in MEDIATION_DOC if k in med})
    return out


def _provider_pin(name: str, p: dict[str, Any], pins: Schemas) -> dict[str, Any]:
    entry = {k: p.get(k) for k in ("type", "from", "args")}
    if "concurrent" in p:                               # pinned only when set: older locks stay fresh
        entry["concurrent"] = p["concurrent"]
    src = p.get("from")
    if isinstance(src, str) and src.startswith("py:"):
        try:
            entry.update(_signature_pin(resolve(src), f"provider {name}", pins))
        except Exception as e:
            raise LoadError(f"provider {name}: cannot pin {src} ({e})") from e
    return _plain(entry)


def scan_pin(roots: list[str], exclude: list[str]) -> dict[str, list[str]]:
    """The scan configuration a lock pins: source roots relative to the rule file, and exclude globs."""
    return {"roots": sorted(set(roots)), "exclude": sorted(set(exclude))}


def mediated(doc: dict[str, Any]) -> bool:
    return any(isinstance(r, dict) and r.get("mediation") for r in doc.get("rules") or [])


def build(doc: dict[str, Any], base_dir: pathlib.Path, scan: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """The lock for a rule document. Imports target modules; call after the file loads cleanly.
    `scan` (scan_pin of the roots and excludes `inv check` scans) is pinned when a rule has a sink."""
    expanded = expand_applies_to(doc)
    declared = doc.get("targets") or {}
    pins = Schemas()
    rules: dict[str, Any] = {}
    keys: set[str] = set(declared)
    for raw in doc.get("rules") or []:
        rules[raw["id"]] = _rule_pin(raw, expanded)
        keys.update(rules[raw["id"]]["targets"])
    ops = _subject_operations(doc)
    targets = {key: _target_pin(key, declared, pins) for key in sorted(keys)}
    providers = {n: _provider_pin(n, p, pins) for n, p in sorted((doc.get("providers") or {}).items())
                 if isinstance(p, dict)}
    contracts = {n: contract_hash(n, c, base_dir, ops.get(n), pins)
                 for n, c in sorted((doc.get("contracts") or {}).items())}
    lock = {"lock_version": LOCK_VERSION, "spec": doc.get("spec"), "rules": rules, "targets": targets,
            "providers": providers, "contracts": contracts, "schemas": pins.table}
    if scan is not None and mediated(doc):
        lock["scan"] = scan_pin(scan["roots"], scan["exclude"])
    return _plain(lock)


def _rule_pin(raw: dict[str, Any], expanded: dict[str, list[str]]) -> dict[str, Any]:
    """A rule's entry: rev, status, the targets it covers (subject, edges, applies_to), its semantic
    content and the digests of its documentation."""
    subj = raw.get("subject") or {}
    tk = subj.get("target")
    edges = sorted(e["target"] for e in raw.get("enforcers") or [] if isinstance(e, dict) and "target" in e)
    rule_targets = sorted({*([tk] if tk else []), *edges, *expanded.get(raw["id"], [])})
    return {"rev": raw.get("rev"), "status": raw.get("status"), "targets": rule_targets,
            "semantic": rule_semantics(raw), "doc": _doc_digests(raw)}


def _subject_operations(doc: dict[str, Any]) -> dict[str, list[str]]:
    """Contract name -> the operations rule subjects name on it (an OpenAPI file pins only those)."""
    ops: dict[str, list[str]] = {}
    for raw in doc.get("rules") or []:
        subj = raw.get("subject") or {}
        if isinstance(subj.get("contract"), str) and isinstance(subj.get("operation"), str):
            ops.setdefault(subj["contract"], []).append(subj["operation"])
    return ops


def _target_pin(key: str, declared: dict[str, Any], pins: Schemas) -> dict[str, Any]:
    """A target's entry: its declared effects (external when undeclared), its signature pin, and the
    reply schema of a model-call target."""
    t = declared.get(key, {"effects": "external"})
    pin: dict[str, Any] = {"effects": t.get("effects")}
    obj = resolve(key)
    pin.update(_signature_pin(obj, f"target {key}", pins))
    schema = getattr(obj, "__inv_schema__", None)
    if schema is not None:
        label = _class_ref(schema) if isinstance(schema, type) else key
        pin["reply_schema"] = pins.add({"name": label, **resolved_schema(schema)})
    return pin
