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
- a datetime without a timezone is refused (a ParseError, so format_retries apply), as for a
  dataclass schema: CEL timestamps are instants. A field annotated pydantic.NaiveDatetime opts out
  (and the loader refuses a rule that reads it);
- the parsed reply is the validated model instance itself (`isinstance(c.value, Model)`): Enum
  members, datetimes, nested models, and fields declared Field(exclude=True). Rules read it
  through cel/values.py like arguments, so a datetime field is a timestamp and a timedelta a
  duration; the loader types `reply` by the model's annotations. An edge re-validates the received
  instance (from its JSON form, strictly, as at parse) and hands its consumer the fresh copy, so a
  mutation of the received instance after the check never reaches the consumer;
- closed(Model) is the schema descriptor the core reads (field names, Literal fields for abstain
  and enum notes); it is never instantiated. model_of(reply) returns a fresh validated copy.
"""
from __future__ import annotations

import collections
import dataclasses
import datetime
import enum
import functools
import json
import math
import os
import pathlib
import threading
import types
import typing
from typing import Any, Callable, Iterator, ParamSpec, TypeVar, overload

import pydantic as _pd

from ..errors import LoadError, ParseError
from ..guard import Checked, ModelCallDecorator
from ..guard import model_call as _core_model_call
from ..guard import parse as _core_parse
from ..loader import read_document
from ..registry import Registry, current
from ..resolve import resolve_ref as _resolve_ref

P = ParamSpec("P")
R = TypeVar("R")
C = TypeVar("C", bound=Checked)
MODEL_ATTR = "__inv_pydantic__"        # on a closed() schema: the pydantic model it wraps
_JSON_SCALARS = (str, int, float, bool, type(None))


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


def _fields(v: Any) -> list[tuple[str, Any]]:
    """(name, annotation) of each field of a validated model or dataclass instance, else []."""
    if isinstance(v, _pd.BaseModel):
        return [(n, f.annotation) for n, f in type(v).model_fields.items()]
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        try:
            hints = typing.get_type_hints(type(v))
        except Exception:
            hints = {}
        return [(f.name, hints.get(f.name, f.type)) for f in dataclasses.fields(v)]
    return []


def _non_finite(v: Any, where: str, safe: str, depth: int = 0) -> tuple[str, str] | None:
    """(path, value-free path) of the first NaN or +-Infinity in validated values (models, dataclass
    instances, lists, tuples, sets, dict values), else None. A dict key is reply data: '*' in the
    value-free path."""
    if depth > 200:
        return (where or "(root)", safe or "(root)")
    if isinstance(v, float):
        return None if math.isfinite(v) else (where or "(root)", safe or "(root)")
    if (fields := _fields(v)):
        for n, _ in fields:
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


def _allows_naive(ann: Any, depth: int = 0) -> bool:
    """True when an annotation is (or holds) pydantic's NaiveDatetime: naive values are declared."""
    if depth > 16:
        return False
    if getattr(ann, "__name__", None) == "NaiveDatetime" and str(getattr(ann, "__module__", "")).startswith("pydantic"):
        return True
    return any(_allows_naive(a, depth + 1) for a in typing.get_args(ann))


def _naive(v: Any, where: str, depth: int = 0) -> str | None:
    """The value-free path of the first datetime without a timezone in validated values (models,
    dataclass instances, lists, tuples, sets, dict values), else None (fields annotated
    NaiveDatetime are skipped)."""
    if depth > 200:
        return None
    if isinstance(v, datetime.datetime):
        return (where or "(root)") if v.utcoffset() is None else None
    if (fields := _fields(v)):
        for n, ann in fields:
            if _allows_naive(ann):
                continue
            if (bad := _naive(getattr(v, n, None), f"{where}.{n}" if where else n, depth + 1)) is not None:
                return bad
    elif isinstance(v, dict):
        for x in v.values():
            if (bad := _naive(x, f"{where}.*" if where else "*", depth + 1)) is not None:
                return bad
    elif isinstance(v, (list, tuple, set, frozenset)):
        for i, x in enumerate(v):
            if (bad := _naive(x, f"{where}[{i}]", depth + 1)) is not None:
                return bad
    return None


# (model, JSON text) -> an instance validated from that text that passed the walks below (bounded,
# least recently used dropped). A later instance validated from the same text and equal to it holds
# the same values, so it passes them too: an edge or model_of re-validating the same reply (every
# consumer call) skips the walks, never the validation. NaN != NaN and a naive datetime never equals
# an aware one, so an instance the walks would refuse is never equal to a cached one.
_WALKED: collections.OrderedDict[tuple[type, str], _pd.BaseModel] = collections.OrderedDict()
_WALKED_MAX = 256
_WALKED_LOCK = threading.Lock()


def validate(model: type[_pd.BaseModel], data: dict[str, Any]) -> _pd.BaseModel:
    """data validated into model: strict over its JSON form unless the model is lax; ParseError on failure.

    The validated instance is then walked: a NaN or +-Infinity anywhere in it (lax mode turns the
    string "NaN" into one) is a ParseError, so a reply that parses never holds one.
    """
    key: tuple[type, str] | None = None
    try:
        if is_lax(model):
            inst = model.model_validate(data)
        else:
            try:
                text = json.dumps(data, allow_nan=False)
            except (TypeError, ValueError):
                pre = _non_finite(data, "", "")       # json.dumps would only say "not JSON data"
                if pre is not None:
                    what = "non-finite number (NaN or Infinity) is not allowed"
                    raise ParseError(f"field {pre[1]}: {what}", feedback=f"field {pre[0]}: {what}") from None
                raise ParseError("reply holds a value that is not JSON data") from None
            inst = model.model_validate_json(text, strict=True)
            key = (model, text)
            with _WALKED_LOCK:
                seen = _WALKED.get(key)
                if seen is not None:
                    _WALKED.move_to_end(key)
            if seen is not None and type(seen) is type(inst) and seen == inst:
                return inst
    except _pd.ValidationError as e:
        raise ParseError(safe_feedback(e, field_names(model)), feedback=feedback(e)) from None
    bad = _non_finite(inst, "", "")
    if bad is not None:
        what = "non-finite number (NaN or Infinity) is not allowed"
        raise ParseError(f"field {bad[1]}: {what}", feedback=f"field {bad[0]}: {what}")
    naive = _naive(inst, "")
    if naive is not None:
        raise ParseError(f"field {naive}: a datetime without a timezone is not allowed",
                         feedback=f"field {naive}: a datetime without a timezone is not allowed; give an "
                                  f"offset or Z (2024-01-31T09:30:00Z)")
    if key is not None:
        kept = model.model_validate_json(key[1], strict=True)   # its own copy, never the one handed out
        with _WALKED_LOCK:             # (validated outside the lock: held only for the dict operations)
            _WALKED[key] = kept
            while len(_WALKED) > _WALKED_MAX:
                _WALKED.popitem(last=False)
    return inst


def _after_fork_in_child() -> None:
    """A fresh _WALKED_LOCK and an empty cache in a forked child: another thread of the parent may have
    held the lock (and been mid-update) at the fork, and no thread of the child would ever release it."""
    global _WALKED_LOCK
    _WALKED_LOCK = threading.Lock()
    _WALKED.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


@functools.cache
def _adapter(model: type[_pd.BaseModel], name: str) -> _pd.TypeAdapter[Any]:
    return _pd.TypeAdapter(typing.cast(Any, model.model_fields[name].annotation))


def json_fields(model: type[_pd.BaseModel], inst: _pd.BaseModel) -> dict[str, Any]:
    """Every field of a model instance in its JSON form (Field(exclude=True) fields too: a dump
    leaves them out, and validating the dump again would then miss them)."""
    return {n: _adapter(model, n).dump_python(getattr(inst, n), mode="json") for n in model.model_fields}


@functools.cache
def closed(model: type[_pd.BaseModel]) -> type:
    """The closed-schema dataclass for a pydantic model (cached; the same class for the same model)."""
    if not is_model(model):
        raise TypeError(f"{model!r} is not a pydantic.BaseModel subclass")
    check_closed(model)
    hints: dict[str, Any] = {}
    for name, f in model.model_fields.items():
        values = enum_values(f.annotation)
        # a closed set of JSON scalars is a Literal (abstain checks, enum notes); an Enum of tuples,
        # lists or other values is left to pydantic, which validates it
        scalar = bool(values) and all(type(v) in _JSON_SCALARS for v in values or ())
        hints[name] = typing.Literal[tuple(values or ())] if scalar else object

    def __post_init__(self: Any) -> None:
        raise TypeError(f"closed({model.__name__}) is a schema descriptor: inv.parse returns {model.__name__} "
                        f"instances")

    def build(cls: Any, data: dict[str, Any]) -> _pd.BaseModel:
        return validate(model, data)

    def raw(cls: Any, inst: _pd.BaseModel) -> dict[str, Any]:
        return json_fields(model, inst)

    cls = dataclasses.make_dataclass(model.__name__, list(hints.items()), frozen=True,
                                     namespace={"__post_init__": __post_init__, "__inv_build__": classmethod(build),
                                                "__inv_raw__": classmethod(raw), MODEL_ATTR: model,
                                                "__doc__": f"Closed schema for {model.__qualname__}."})
    cls.__module__, cls.__qualname__ = model.__module__, model.__qualname__
    return cls


def schema_for(schema: Any) -> Any:
    """closed(schema) for a pydantic model; any other schema unchanged."""
    return closed(schema) if is_model(schema) else schema


def model_of(reply: Any) -> _pd.BaseModel:
    """A fresh copy of a pydantic reply (the reply, or a Checked holding one), validated again.

    The reply already is the model instance (c.value); this gives a copy validated strictly from
    its JSON form, as an edge does, so changing the copy never changes the reply or what another
    caller gets. A reply tampered with into something the model refuses raises ParseError.
    """
    if isinstance(reply, Checked):
        reply = object.__getattribute__(reply, "value")
    if not isinstance(reply, _pd.BaseModel):
        raise TypeError(f"{type(reply).__name__} was not parsed through a closed pydantic schema")
    model = type(reply)
    return validate(model, json_fields(model, reply))


def parse(raw: Any, schema: Any) -> Any:
    """inv.parse, also accepting a pydantic model as the schema."""
    return _core_parse(raw, schema_for(schema))


@overload
def model_call(rule_id: str, schema: Any, *, returns: type[C]) -> ModelCallDecorator[C]: ...


@overload
def model_call(rule_id: str, schema: Any, *, returns: None = None) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def model_call(rule_id: str, schema: Any, *, returns: Any = None) -> Any:
    """inv.model_call, also accepting a pydantic model as the schema."""
    return _core_model_call(rule_id, schema_for(schema), returns=returns)


# --- contracts ----------------------------------------------------------------------------------
def resolve_ref(ref: Any) -> Any:
    """'package.module:Class' -> the object: import the module, then walk attributes. No eval."""
    mod, sep, attr = str(ref).partition(":")
    if not isinstance(ref, str) or not sep or not mod or not attr:
        raise LoadError(f"contract ref {ref!r} must be 'module:Class'")
    return _resolve_ref(ref, f"contract ref {ref}", public=True)    # confined under inv mcp


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
