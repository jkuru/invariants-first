"""Signature pins: a callable's signature as text, its parameter and return types resolved
(signature_types), and the JSON Schema of each parameter whose annotation carries a constraint
(param_schemas)."""
from __future__ import annotations

import inspect
import typing
from typing import Any

from .. import _typeinfo as ti
from ..errors import LoadError
from .contracts import MAX_DEPTH, _plain
from .pin_jsonschema import _BOUND_ATTRS, _LEN_ATTRS, _Defs, _json_type
from .pin_types import Schemas, _type_schema, strip_prose


def _callable_hints(obj: Any) -> tuple[Any, dict[str, Any] | None]:
    """The unwrapped callable and its resolved annotations (a class's are its __init__'s), or None when
    a name cannot be resolved. signature_types and a signature pin read them."""
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
    except (TypeError, ValueError) as e:    # inspect's message carries the object's repr: name its type only
        raise LoadError(f"{label}: no signature (a {type(fn).__name__} object, not a function or class)") from e
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
    u = ti.unalias(t)
    if u is not t:
        return _has_constraints(u, depth + 1)
    value = ti.alias_value(t)
    if value is not None and _has_constraints(value, depth + 1):
        return True                                            # X[...] its parameters do not take
    args = typing.get_args(t)
    if ti.is_annotated(t):
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


def _param_schemas(fn: Any, hints: dict[str, Any] | None) -> dict[str, Any]:
    if hints is None:                                          # unresolvable: the signature text pins it
        return {}
    return {name: _annotation_schema(t) for name, t in sorted(hints.items())
            if not (inspect.isclass(fn) and name == "return") and _has_constraints(t)}
