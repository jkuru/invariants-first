"""Python types the rule file names or the code declares: provider types, signatures, annotations and
closed schemas (dataclass, {field: type} or pydantic model). guard/ and lock/ read schemas through these."""
from __future__ import annotations

import dataclasses
import enum
import functools
import inspect
import typing
from typing import Any, Callable

from invariants_cel import typeinfo as ti

from ..errors import LoadError
from ..resolve import resolve

_BUILTIN_TYPES: dict[str, type] = {"int": int, "str": str, "bool": bool, "float": float, "list": list, "dict": dict}


def _parse_type(s: str) -> tuple[type, bool]:
    """Provider type: 'int' | 'py:mod.Class', optionally suffixed ' | null'."""
    if not isinstance(s, str):
        raise LoadError(f"provider type {s!r} must be a string")
    if s.replace(" ", "").endswith("|null"):
        base, nullable = s.strip()[: -len("null")].rstrip().rstrip("|").strip(), True
    else:
        base, nullable = s.strip(), False
    t = _BUILTIN_TYPES.get(base) or (resolve(base) if base.startswith("py:") else None)
    if not isinstance(t, type):
        raise LoadError(f"provider type {s!r} does not name a type")
    return t, nullable


def _type_set(ann: Any, _depth: int = 0) -> frozenset[Any]:
    """The types an annotation stands for, through aliases, NewTypes and Annotated: Optional[UserId]
    (UserId = NewType("UserId", int)) is {int, NoneType}."""
    ann = ti.strip(ann)
    members = ti.union_args(ann) if _depth < ti.MAX_UNWRAP else None      # `type U = U | int`: bounded
    return frozenset({ann}) if members is None else frozenset().union(*(_type_set(a, _depth + 1) for a in members))


def _params(obj: Any) -> set[str]:
    fn = inspect.unwrap(obj)
    try:
        return set(inspect.signature(fn).parameters) if callable(fn) else set()
    except (TypeError, ValueError):      # no signature; inspect's message would carry the object's repr
        return set()


def _no_pydantic(model: type) -> type:
    raise RuntimeError("invariants.loader.schemas.pydantic_closed is not wired (the package wires it on import)")


# integrations.pydantic.closed, wired by the package (invariants/__init__.py), so the loader never
# imports the integration that builds on it
pydantic_closed: Callable[[type], type] = _no_pydantic


def closed_schema(schema: Any) -> Any:
    """A pydantic model becomes its closed dataclass (integrations.pydantic.closed); others are unchanged."""
    if ti.is_pydantic_model(schema):
        return pydantic_closed(schema)
    return schema


def type_hints(obj: Any, who: str, error: type[Exception] = TypeError,
               localns: dict[str, Any] | None = None) -> dict[str, Any]:
    """typing.get_type_hints(obj), or `error` naming the annotation that does not resolve (never a raw
    NameError). localns: names to try when the module's globals do not hold one (a class defined in
    the function that applies the decorator, under `from __future__ import annotations`)."""
    try:
        return typing.get_type_hints(obj)
    except NameError as e:
        first = e
    except Exception as e:
        raise error(f"{who}: its annotations do not resolve ({type(e).__name__}: {e})") from None
    if localns:
        try:
            return typing.get_type_hints(obj, localns=localns)
        except Exception:
            pass
    name = getattr(first, "name", None) or str(first)
    raise error(f"{who}: the annotation name {name!r} does not resolve (with `from __future__ import "
                f"annotations`, annotations are read by name: define {name} at module level or import it "
                f"there)") from None


@functools.lru_cache(maxsize=512)
def _dataclass_fields(schema: type) -> dict[str, Any]:
    return type_hints(schema, f"schema {getattr(schema, '__qualname__', type(schema).__qualname__)!r}")


def schema_fields(schema: Any) -> dict[str, Any]:
    """Field name -> type of a closed schema (dataclass, {field: type} or pydantic model). Cached per class."""
    schema = closed_schema(schema)
    if isinstance(schema, type) and dataclasses.is_dataclass(schema):
        return dict(_dataclass_fields(schema))
    return (type_hints(schema, f"schema {getattr(schema, '__qualname__', type(schema).__qualname__)!r}")
            if dataclasses.is_dataclass(schema) else dict(schema))


def literal_eq(v: Any, a: Any) -> bool:
    """v equals the Literal member a (an Enum member stands for its value), with the same plain type:
    True is not 1, and no user-defined __eq__ ever runs."""
    if isinstance(a, enum.Enum):
        a = a.value
    return type(v) is type(a) and type(a) in (str, int, float, bool, bytes, type(None)) and v == a


def schema_name(schema: Any) -> str:
    """A closed schema's name for messages: the class name, or 'the {field: type} schema'."""
    n = getattr(schema, "__name__", None)
    return n if isinstance(n, str) else "the {field: type} schema"
