"""Pydantic v2 models as closed schemas for model calls, and the 'pydantic' contract type.

The core accepts a dataclass as a closed schema. closed(Model) builds one from a pydantic
model, so the core parse, retry, abstain and edge logic run unchanged:

- the model (and every model nested in it) must set extra='forbid', or LoadError;
- Literal and Enum fields are presented as Literal, so the loader's abstain-value membership
  check and the "held by the schema" note apply to them;
- pydantic validates on construction, in strict mode over the reply's JSON form
  (model_validate_json(strict=True)): "0.5", true and "NaN" are not floats, as with a dataclass
  schema. A model whose model_config sets strict=False explicitly opts out (pydantic's lax
  coercion). Either way a NaN or +-Infinity anywhere in the validated values is refused;
- a ValidationError (or a non-finite value) becomes ParseError: its str() is value-free
  ('loc: error type', reply-made keys as '*'), for events and logs; its .feedback ('loc: message')
  is what the core feeds back as inv_feedback;
- the reply's attributes hold the model's JSON-mode values (enum values, not members, datetime
  text); model_of(reply) returns a new pydantic instance validated from them on each call (so it
  is always what an edge re-validates; changing it changes nothing else). Rules read the instance
  (guard.rule_view), through cel/values.py like arguments, so a datetime field is a timestamp and a
  timedelta a duration; the loader types `reply` by the model's annotations. An edge re-check, a
  copy and an unpickled reply all validate again from the JSON-mode attributes.
"""
from __future__ import annotations

import dataclasses
import enum
import functools
import importlib
import json
import math
import pathlib
import types
import typing
from typing import Any, Callable, Iterator, ParamSpec, TypeVar

import pydantic as _pd

from ..errors import LoadError, ParseError
from ..guard import INSTANCE_ATTR
from ..guard import model_call as _core_model_call
from ..guard import parse as _core_parse
from ..loader import read_document
from ..registry import Registry, current

P = ParamSpec("P")
R = TypeVar("R")
MODEL_ATTR = "__inv_pydantic__"        # on a closed() schema: the pydantic model it wraps
_INSTANCE_ATTR = INSTANCE_ATTR         # on a reply: the validated pydantic instance (what rules read)


def is_model(obj: Any) -> bool:
    """True for a pydantic.BaseModel subclass."""
    return isinstance(obj, type) and issubclass(obj, _pd.BaseModel)


def _nested_models(ann: Any) -> Iterator[type[_pd.BaseModel]]:
    if is_model(ann):
        yield ann
    for a in typing.get_args(ann):
        yield from _nested_models(a)


def check_closed(model: type[_pd.BaseModel], _seen: set[type] | None = None) -> None:
    """LoadError unless model and every model nested in its fields forbid extra fields and use no aliases."""
    seen = _seen if _seen is not None else set()
    if model in seen:
        return
    seen.add(model)
    extra = model.model_config.get("extra")
    if extra != "forbid":
        raise LoadError(f"{model.__name__}: a closed schema needs model_config extra='forbid' (it is {extra!r})")
    for name, f in model.model_fields.items():
        if f.alias not in (None, name) or f.validation_alias not in (None, name):
            raise LoadError(f"{model.__name__}.{name}: field aliases are not supported in a closed schema")
        for sub in _nested_models(f.annotation):
            check_closed(sub, seen)


def enum_values(ann: Any) -> list[Any] | None:
    """The closed set of a Literal or Enum field (None included when optional), else None."""
    nullable = False
    if typing.get_origin(ann) in (typing.Union, types.UnionType):
        rest = [a for a in typing.get_args(ann) if a is not type(None)]
        if len(rest) != 1:
            return None
        nullable, ann = len(rest) != len(typing.get_args(ann)), rest[0]
    if typing.get_origin(ann) is typing.Literal:
        values = list(typing.get_args(ann))
    elif isinstance(ann, type) and issubclass(ann, enum.Enum):
        values = [m.value for m in ann]
    else:
        return None
    return values + [None] if nullable else values


def field_names(model: type[_pd.BaseModel], _seen: set[type] | None = None) -> frozenset[str]:
    """Every field name declared by model and the models nested in it."""
    seen = _seen if _seen is not None else set()
    if model in seen:
        return frozenset()
    seen.add(model)
    out = set(model.model_fields)
    for f in model.model_fields.values():
        for sub in _nested_models(f.annotation):
            out |= field_names(sub, seen)
    return frozenset(out)


def safe_feedback(err: _pd.ValidationError, names: frozenset[str]) -> str:
    """'loc: error type; ...' with no input value and no key the reply made up (a loc part that is
    not a declared field name or a list index is '*'): the text for events, logs and exceptions."""
    parts = []
    for d in err.errors(include_url=False, include_input=False, include_context=False):
        loc = ".".join(str(p) if isinstance(p, int) or p in names else "*" for p in d["loc"]) or "(root)"
        parts.append(f"{loc}: {d['type']}")
    return "; ".join(parts)


def feedback(err: _pd.ValidationError) -> str:
    """A short, model-facing text for a ValidationError: 'loc: message; ...' (no inputs, no URLs)."""
    parts = []
    for d in err.errors(include_url=False, include_input=False, include_context=False):
        loc = ".".join(str(p) for p in d["loc"]) or "(root)"
        parts.append(f"{loc}: {d['msg']}")
    return "; ".join(parts)


def is_lax(model: type[_pd.BaseModel]) -> bool:
    """True when the model opts out of strict reply validation: model_config sets strict=False explicitly."""
    return model.model_config.get("strict") is False


def _non_finite(v: Any, where: str, safe: str, depth: int = 0) -> tuple[str, str] | None:
    """(path, value-free path) of the first NaN or +-Infinity in validated values (models, lists,
    tuples, sets, dict values), else None. A dict key is reply data: '*' in the value-free path."""
    if depth > 200:
        return (where or "(root)", safe or "(root)")
    if isinstance(v, float):
        return None if math.isfinite(v) else (where or "(root)", safe or "(root)")
    if isinstance(v, _pd.BaseModel):
        for n in type(v).model_fields:
            sub = f"{where}.{n}" if where else n
            if (bad := _non_finite(getattr(v, n, None), sub, f"{safe}.{n}" if safe else n, depth + 1)) is not None:
                return bad
    elif isinstance(v, dict):
        for k, x in v.items():
            if (bad := _non_finite(x, f"{where}.{k}" if where else str(k), f"{safe}.*" if safe else "*",
                                   depth + 1)) is not None:
                return bad
    elif isinstance(v, (list, tuple, set, frozenset)):
        for i, x in enumerate(v):
            if (bad := _non_finite(x, f"{where}[{i}]", f"{safe}[{i}]", depth + 1)) is not None:
                return bad
    return None


def validate(model: type[_pd.BaseModel], data: dict[str, Any]) -> _pd.BaseModel:
    """data validated into model: strict over its JSON form unless the model is lax; ParseError on failure.

    The validated instance is then walked: a NaN or +-Infinity anywhere in it (lax mode turns the
    string "NaN" into one) is a ParseError, so a reply that parses never holds one.
    """
    try:
        if is_lax(model):
            inst = model.model_validate(data)
        else:
            try:
                text = json.dumps(data, allow_nan=False)
            except (TypeError, ValueError):
                raise ParseError("reply holds a value that is not JSON data") from None
            inst = model.model_validate_json(text, strict=True)
    except _pd.ValidationError as e:
        raise ParseError(safe_feedback(e, field_names(model)), feedback=feedback(e)) from None
    bad = _non_finite(inst, "", "")
    if bad is not None:
        what = "non-finite number (NaN or Infinity) is not allowed"
        raise ParseError(f"field {bad[1]}: {what}", feedback=f"field {bad[0]}: {what}")
    return inst


@functools.cache
def closed(model: type[_pd.BaseModel]) -> type:
    """The closed-schema dataclass for a pydantic model (cached; the same class for the same model)."""
    if not is_model(model):
        raise TypeError(f"{model!r} is not a pydantic.BaseModel subclass")
    check_closed(model)
    hints: dict[str, Any] = {}
    for name, f in model.model_fields.items():
        values = enum_values(f.annotation)
        hints[name] = typing.Literal[tuple(values)] if values else object

    def __post_init__(self: Any) -> None:
        inst = validate(model, {n: getattr(self, n) for n in hints})
        dumped = inst.model_dump(mode="json")
        for n in hints:
            object.__setattr__(self, n, dumped[n])
        object.__setattr__(self, _INSTANCE_ATTR, inst)

    def __reduce__(self: Any) -> Any:      # pickles by model: the class itself is not importable by name
        return (_rebuild, (model, {n: getattr(self, n) for n in hints}))

    cls = dataclasses.make_dataclass(model.__name__, list(hints.items()), frozen=True,
                                     namespace={"__post_init__": __post_init__, "__reduce__": __reduce__,
                                                MODEL_ATTR: model,
                                                "__doc__": f"Closed schema for {model.__qualname__}."})
    cls.__module__, cls.__qualname__ = model.__module__, model.__qualname__
    return cls


def _rebuild(model: type[_pd.BaseModel], data: dict[str, Any]) -> Any:
    """Unpickle a closed-schema reply: validated again through closed(model)."""
    return closed(model)(**data)


def schema_for(schema: Any) -> Any:
    """closed(schema) for a pydantic model; any other schema unchanged."""
    return closed(schema) if is_model(schema) else schema


def model_of(reply: Any) -> _pd.BaseModel:
    """A new pydantic instance validated from a reply's JSON attributes (the reply, or a Checked holding one).

    Every call validates again (strict, as at parse) and returns a fresh instance: it always equals
    the JSON form an edge re-validates and judges, and changing it never changes the reply or what
    another caller gets. A reply whose attributes were tampered with into something the model
    refuses raises ParseError. Keep the result in a local rather than calling this in a loop.
    """
    from ..guard import Checked
    if isinstance(reply, Checked):
        reply = object.__getattribute__(reply, "value")
    model = getattr(type(reply), MODEL_ATTR, None)
    if model is None or not dataclasses.is_dataclass(reply):
        raise TypeError(f"{type(reply).__name__} was not parsed through a closed pydantic schema")
    return validate(model, {f.name: object.__getattribute__(reply, f.name) for f in dataclasses.fields(reply)})


def parse(raw: Any, schema: Any) -> Any:
    """inv.parse, also accepting a pydantic model as the schema."""
    return _core_parse(raw, schema_for(schema))


def model_call(rule_id: str, schema: Any) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """inv.model_call, also accepting a pydantic model as the schema."""
    return _core_model_call(rule_id, schema_for(schema))


# --- contracts ----------------------------------------------------------------------------------
def resolve_ref(ref: Any) -> Any:
    """'package.module:Class' -> the object: import the module, then walk attributes. No eval."""
    mod, sep, attr = str(ref).partition(":")
    if not isinstance(ref, str) or not sep or not mod or not attr:
        raise LoadError(f"contract ref {ref!r} must be 'module:Class'")
    try:
        obj = importlib.import_module(mod)
    except ImportError as e:
        raise LoadError(f"contract ref {ref}: cannot import {mod}: {e}") from e
    for p in attr.split("."):
        if p.startswith("_") or not hasattr(obj, p):
            raise LoadError(f"contract ref {ref}: {p!r} does not exist")
        obj = getattr(obj, p)
    return obj


def resolve_contract(spec: Any, name: str = "?") -> type[_pd.BaseModel]:
    """A {type: pydantic, ref: 'module:Class'} contract -> the closed model class, or LoadError."""
    if not isinstance(spec, dict) or spec.get("type") != "pydantic":
        raise LoadError(f"contract {name}: not a pydantic contract")
    model = resolve_ref(spec.get("ref"))
    if not is_model(model):
        raise LoadError(f"contract {name}: {spec.get('ref')} is not a pydantic.BaseModel subclass")
    check_closed(model)
    return model


def check_contracts(source: str | pathlib.Path | dict[str, Any],
                    reg: Registry | None = None) -> dict[str, type[_pd.BaseModel]]:
    """Resolve every pydantic contract in a rule file and check the rules that name one.

    A rule whose subject.contract is a pydantic contract must use that model: model_policy.schema
    names it, and its @inv.model_call target was decorated with it. Returns {contract name: model}.
    """
    doc = source if isinstance(source, dict) else read_document(source)
    contracts = doc.get("contracts") or {}
    models = {name: resolve_contract(c, name) for name, c in contracts.items()
              if isinstance(c, dict) and c.get("type") == "pydantic"}
    problems = []
    for rule in (reg or current()).rules.values():
        name = (rule.raw.get("subject") or {}).get("contract")
        if name not in models or rule.status == "retired":
            continue
        model = models[name]
        pol = rule.policy or {}
        if pol.get("schema") not in (None, model.__name__):
            problems.append(f"{rule.id}: model_policy.schema {pol['schema']!r} is not contract {name} "
                            f"({model.__name__})")
        schema = getattr(rule.target, "__inv_schema__", None)
        if schema is not None and getattr(schema, MODEL_ATTR, schema) is not model:
            problems.append(f"{rule.id}: {rule.target_key} parses into {getattr(schema, '__name__', schema)}, "
                            f"not contract {name} ({model.__module__}:{model.__qualname__})")
    if problems:
        raise LoadError("; ".join(problems))
    return models
