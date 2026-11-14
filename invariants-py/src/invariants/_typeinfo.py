"""Reading Python annotations, shared by the load-time type check (typecheck), the loader's schemas and
provider types (loader.schemas) and the lock's pins (lock.pin_types, pin_jsonschema, pin_signatures),
so each sees the same underlying type: through PEP 695 aliases (`type X = ...`, `X[int]`, and
typing_extensions' TypeAliasType), NewType, Annotated, Optional/Union and Literal, and the same schema
classes (dataclass, TypedDict, pydantic model) and Enums. Internal: not part of the public API. Pure:
it imports no target module and never imports pydantic."""
from __future__ import annotations

import dataclasses
import enum
import sys
import types
import typing
from typing import Any

from .errors import LoadError

MAX_UNWRAP = 24                         # alias / NewType / Annotated layers followed (lock.contracts.MAX_DEPTH)


def is_alias(t: Any) -> bool:
    """A PEP 695 alias (typing.TypeAliasType, 3.12+, or typing_extensions' backport): judged by its type,
    never by reading __value__ (which evaluates the alias's value and can raise)."""
    return type(t).__name__ == "TypeAliasType"


def alias_target(t: Any) -> Any:
    """The value of the alias t, or, when evaluating it fails (`type Later = OnlyTyping` with OnlyTyping
    imported under TYPE_CHECKING: a NameError; any other error its value raises), the alias's name as a
    string: read as an unresolvable annotation is (dyn at load, `{"unresolved": name}` in the lock),
    stably and without the crash."""
    try:
        return t.__value__
    except Exception:
        return str(getattr(t, "__name__", None) or "?")


def is_newtype(t: Any) -> bool:
    """A typing.NewType (`UserId = NewType("UserId", int)`), or an object shaped as one."""
    return isinstance(t, typing.NewType) or hasattr(t, "__supertype__")


def unalias(t: Any) -> Any:
    """t with PEP 695 aliases and NewTypes expanded: `X` of `type X = ...` to its value, a NewType to its
    supertype, `X[int]` of `type X[T] = ...` to its value with the type parameters substituted
    (`list[T]` -> `list[int]`). t itself when it is no alias, or when its arguments do not fit the
    parameters (a TypeVarTuple, a ParamSpec, a wrong count): see alias_value."""
    for _ in range(MAX_UNWRAP):
        if is_alias(t):
            t = alias_target(t)
            continue
        if isinstance(t, typing.NewType):
            t = t.__supertype__
            continue
        origin = typing.get_origin(t)
        if not is_alias(origin):
            return t
        params, args = tuple(getattr(origin, "__type_params__", ())), typing.get_args(t)
        if len(params) != len(args) or not all(isinstance(p, typing.TypeVar) for p in params):
            return t
        try:
            t = substitute(alias_target(origin), dict(zip(params, args)))
        except Exception:                                      # a value typing cannot re-parameterise
            return t
    return t


def alias_value(t: Any) -> Any:
    """The value of the alias `X[...]` subscripts when unalias could not substitute its arguments, its
    type variables left free; None when t is no subscripted alias."""
    origin = typing.get_origin(t)
    return alias_target(origin) if is_alias(origin) else None


def substitute(value: Any, mapping: dict[Any, Any]) -> Any:
    """value (an alias's) with its free type variables replaced: `T` itself, or `list[T]`, `T | None`,
    `Annotated[T, Gt(0)]` re-parameterised through typing's own subscription."""
    if isinstance(value, typing.TypeVar):
        return mapping.get(value, value)
    free = tuple(getattr(value, "__parameters__", ()) or ())
    return value[tuple(mapping.get(p, p) for p in free)] if free else value


def is_annotated(t: Any) -> bool:
    return typing.get_origin(t) is typing.Annotated


def strip(t: Any) -> Any:
    """The underlying type: aliases, NewTypes and Annotated layers removed (metadata dropped)."""
    for _ in range(MAX_UNWRAP):
        t = unalias(t)
        if not is_annotated(t):
            return t
        t = typing.get_args(t)[0]
    return t


def is_union(t: Any) -> bool:
    """typing.Union[...], Optional[...] or X | Y."""
    return typing.get_origin(t) in (typing.Union, types.UnionType)


def union_args(t: Any) -> tuple[Any, ...] | None:
    """The members of a union (as written: not unaliased), or None when t is no union."""
    return typing.get_args(t) if is_union(t) else None


def is_literal(t: Any) -> bool:
    return typing.get_origin(t) is typing.Literal


def is_enum(t: Any) -> typing.TypeGuard[type[enum.Enum]]:
    """An Enum class (IntEnum, StrEnum, Flag and str/int mixins included), not a member."""
    return isinstance(t, type) and issubclass(t, enum.Enum)


def is_typeddict(t: Any) -> bool:
    """A TypedDict class, typing's or typing_extensions' (which typing.is_typeddict misses on 3.11)."""
    return isinstance(t, type) and issubclass(t, dict) and hasattr(t, "__annotations__") and hasattr(t, "__total__")


def is_pydantic_model(t: Any) -> bool:
    """A pydantic BaseModel subclass (never imports pydantic: unimported, no class can be one)."""
    mod = sys.modules.get("pydantic")
    return mod is not None and isinstance(t, type) and issubclass(t, mod.BaseModel)


def is_schema_class(t: Any) -> bool:
    """A class whose fields are a closed record: a dataclass, a TypedDict or a pydantic model."""
    return isinstance(t, type) and (dataclasses.is_dataclass(t) or is_pydantic_model(t) or is_typeddict(t))


class ClassShape(typing.NamedTuple):
    kind: str                       # "dataclass" | "typeddict"
    fields: dict[str, Any]          # field name -> resolved annotation (Annotated kept), in field order
    required: list[str]             # sorted


def class_shape(cls: type) -> ClassShape:
    """A dataclass or TypedDict's resolved fields and its required ones (the lock's resolved schema and
    its stdlib JSON Schema are read from it)."""
    try:
        hints = typing.get_type_hints(cls, include_extras=True)
    except Exception as e:                                 # an unresolvable forward reference
        raise LoadError(f"schema {cls.__qualname__}: cannot resolve its annotations ({e})") from e
    if dataclasses.is_dataclass(cls):
        fields = dataclasses.fields(cls)
        return ClassShape("dataclass", {f.name: hints[f.name] for f in fields},
                          sorted(f.name for f in fields
                                 if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING))
    # Required[], NotRequired[] and ReadOnly[] (typing's or typing_extensions') qualify the key, not its
    # value: they are unwrapped, and Required / NotRequired decide the key's required-ness over the
    # class's total= (typing.TypedDict before 3.13 does not see them inside typing_extensions' ReadOnly)
    required = set(getattr(cls, "__required_keys__", ()))
    keys: dict[str, Any] = {}
    for n, t in hints.items():
        keys[n], quals = key_qualifiers(t)
        if "Required" in quals:
            required.add(n)
        elif "NotRequired" in quals:
            required.discard(n)
    return ClassShape("typeddict", keys, sorted(required))


_KEY_QUALIFIERS = frozenset({"Required", "NotRequired", "ReadOnly"})


def key_qualifiers(t: Any) -> tuple[Any, set[str]]:
    """A TypedDict key's annotation without its Required / NotRequired / ReadOnly wrappers (also inside
    Annotated, whose metadata stays), and the names of the wrappers it had."""
    origin = typing.get_origin(t)
    if origin is typing.Annotated:
        inner, quals = key_qualifiers(typing.get_args(t)[0])
        return (typing.Annotated[(inner, *t.__metadata__)] if quals else t), quals
    name = getattr(origin, "_name", None)
    if name in _KEY_QUALIFIERS and type(origin).__module__ in ("typing", "typing_extensions"):
        inner, quals = key_qualifiers(typing.get_args(t)[0])
        return inner, {*quals, name}
    return t, set()
