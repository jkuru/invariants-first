"""Parsing a model reply into a closed schema: inv.parse, the field checks it runs, the view a rule
reads, and the re-check an @inv.edge applies to a received value.

Every failure is a ParseError whose str() is value-free (field path and error kind) and whose
.feedback is the full text fed back to the model.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime
import enum
import json
import math
import re
import reprlib
import types
import typing
import uuid
from typing import Any

from ..cel import CelError, values as cel_values
from ..errors import ParseError
from ..loader import closed_schema, literal_eq, schema_fields
from ..registry import Registry, current
from .checked import _DICTS, _LISTS, FrozenDict, _thaw


# --- parsing a reply into a closed schema --------------------------------------------------------
_REPR = reprlib.Repr()
_REPR.maxstring, _REPR.maxother, _REPR.maxlist, _REPR.maxdict, _REPR.maxlevel = 40, 40, 4, 4, 2
_MAX_DEPTH = 32                                 # nesting of typed fields (list[list[...]], nested dataclasses)


def _show(v: Any) -> str:
    """A short repr of a reply value for parse_error text (never raises, never long)."""
    try:
        return _REPR.repr(v)
    except Exception:
        return "<unprintable value>"


def _tname(t: Any) -> str:
    try:
        return t.__name__ if isinstance(t, type) else str(t).replace("typing.", "")
    except Exception:
        return "<type>"


_same = literal_eq


def supported_field_type(t: Any, _depth: int = 0) -> bool:
    """True for a field type parse can check: plain classes, Any/object, Literal, Optional/Union,
    list[T], dict[str, T] and nested dataclasses."""
    if _depth > _MAX_DEPTH:
        return False
    if t is Any or t is None or isinstance(t, type) and typing.get_origin(t) is None:
        return True
    origin, args = typing.get_origin(t), typing.get_args(t)
    if origin is typing.Literal:
        return True
    if origin in (typing.Union, types.UnionType):
        return all(supported_field_type(a, _depth + 1) for a in args)
    if origin is list:
        return len(args) == 1 and supported_field_type(args[0], _depth + 1)
    if origin is dict:
        return len(args) == 2 and args[0] is str and supported_field_type(args[1], _depth + 1)
    return False


def _err(safe: str, full: str | None = None) -> ParseError:
    """ParseError whose str() is safe (field path and error kind, no reply value or reply-made key)
    and whose .feedback is full (the text fed back to the model as inv_feedback)."""
    return ParseError(safe, feedback=full)


def _vt(v: Any) -> str:
    """The JSON kind of a reply value, for value-free parse_error text."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, (list, tuple)):
        return "list"
    if isinstance(v, dict):
        return "object"
    return "value"


_NON_FINITE = "non-finite number (NaN or Infinity) is not allowed"


def _finite(v: Any, where: str, depth: int, safe: str | None = None) -> Any:
    """v, unless it is (or a list or object in it holds) a NaN or +-Infinity: ParseError."""
    safe = where if safe is None else safe
    if depth > _MAX_DEPTH:
        raise _err(f"{safe}: nested too deeply", f"{where}: nested too deeply")
    if isinstance(v, float):
        if not math.isfinite(v):
            raise _err(f"field {safe}: {_NON_FINITE}", f"field {where}: {_NON_FINITE}")
    elif type(v) in _LISTS:
        for i, x in enumerate(v):
            _finite(x, f"{where}[{i}]", depth + 1, f"{safe}[{i}]")
    elif type(v) in _DICTS:
        for k, x in v.items():
            _finite(x, f"{where}.{k}", depth + 1, f"{safe}.*")     # a reply-made key: only in the feedback
    return v


def _check(v: Any, t: Any, where: str, depth: int, safe: str | None = None) -> Any:
    """v checked against field type t (and converted: a nested dataclass is built); ParseError if not.

    A NaN or +-Infinity fails whatever t says: every comparison with NaN is false, so a bound
    check in bad (`x < 0.0 || x > 1.0`) would pass it. safe: where, with reply-made map keys
    replaced by '*' (for the value-free text of the ParseError).
    """
    safe = where if safe is None else safe
    if depth > _MAX_DEPTH:
        raise _err(f"{safe}: nested too deeply", f"{where}: nested too deeply")
    if isinstance(v, float) and not math.isfinite(v):
        raise _err(f"field {safe}: {_NON_FINITE}", f"field {where}: {_NON_FINITE}")
    if t is Any or t is object:          # a copy: whoever handed v over cannot change it after the check
        return copy.deepcopy(_finite(v, where, depth, safe))
    origin, args = typing.get_origin(t), typing.get_args(t)
    if origin is typing.Literal:
        if not any(_same(v, a) for a in args):
            raise _err(f"field {safe}: {_vt(v)} is not one of the allowed values",
                       f"field {where}: {_show(v)} is not one of {list(args)}")
        return v
    if origin in (typing.Union, types.UnionType):
        for a in args:
            try:
                return _check(v, a, where, depth + 1, safe)
            except ParseError:
                continue
        raise _err(f"field {safe}: {_vt(v)} is not {_tname(t)}", f"field {where}: {_show(v)} is not {_tname(t)}")
    if t is None or t is type(None):
        if v is not None:
            raise _err(f"field {safe}: {_vt(v)} is not null", f"field {where}: {_show(v)} is not null")
        return v
    if origin is list:
        if type(v) not in _LISTS:
            raise _err(f"field {safe}: {_vt(v)} is not a list", f"field {where}: {_show(v)} is not a list")
        return [_check(x, args[0], f"{where}[{i}]", depth + 1, f"{safe}[{i}]") for i, x in enumerate(v)]
    if origin is dict:
        if type(v) not in _DICTS:
            raise _err(f"field {safe}: {_vt(v)} is not an object", f"field {where}: {_show(v)} is not an object")
        out = {}
        for k, x in v.items():
            if type(k) is not str:
                raise _err(f"field {safe}: a key is not a string", f"field {where}: key {_show(k)} is not a string")
            out[k] = _check(x, args[1], f"{where}.{k}", depth + 1, f"{safe}.*")
        return out
    if isinstance(t, type) and dataclasses.is_dataclass(t):
        return _build(v, t, f"{where}.", depth + 1, f"{safe}.")
    if t in _TEXT_TYPES:
        return _from_text(v, t, where, safe)
    if not isinstance(t, type):
        raise _err(f"field {safe}: unsupported field type {_tname(t)}")
    if t is float and type(v) is int:
        return float(v)
    if not isinstance(v, t) or (t in (int, float) and isinstance(v, bool)):
        raise _err(f"field {safe}: {_vt(v)} is not {_tname(t)}", f"field {where}: {_show(v)} is not {_tname(t)}")
    return v


# Field types a JSON reply carries as text, and the form each takes (the same forms pydantic reads).
_TEXT_TYPES = (datetime.datetime, datetime.timedelta, uuid.UUID)
_TEXT_FORMS = {datetime.datetime: "an RFC 3339 timestamp with a timezone (2024-01-31T09:30:00Z)",
               datetime.timedelta: "a duration ('90s', '1h30m' or ISO 8601 'PT1H30M')",
               uuid.UUID: "a UUID"}
_ISO_DURATION = re.compile(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d{1,6})?)S)?)?", re.ASCII)


def _iso_duration(s: str) -> datetime.timedelta | None:
    """An ISO 8601 duration of weeks, days, hours, minutes and seconds ('PT5M', 'P1DT2H', '-PT0.5S'),
    as pydantic writes a timedelta; None if s is not one (years and months have no fixed length)."""
    m = _ISO_DURATION.fullmatch(s)
    if not m or not any(m.groups()[1:]) or s.endswith("T"):
        return None
    sign, w, d, h, mi, sec = m.groups()
    us = round(float(sec or 0) * 10**6) + (((int(w or 0) * 7 + int(d or 0)) * 24 + int(h or 0)) * 60
                                           + int(mi or 0)) * 60 * 10**6
    return datetime.timedelta(microseconds=-us if sign == "-" else us)


def _from_text(v: Any, t: type, where: str, safe: str) -> Any:
    """A datetime, timedelta or UUID field: the value itself (a dataclass reply re-checked at an
    edge), or its JSON text. A datetime must carry a timezone, as CEL timestamps are instants."""
    form = _TEXT_FORMS[t]
    if isinstance(v, t):
        if isinstance(v, datetime.datetime) and v.utcoffset() is None:
            raise _err(f"field {safe}: a datetime without a timezone is not allowed",
                       f"field {where}: a datetime without a timezone is not allowed; give an offset or Z")
        return v
    bad = _err(f"field {safe}: {_vt(v)} is not {form}", f"field {where}: {_show(v)} is not {form}")
    if type(v) is not str or len(v) > 100:
        raise bad
    try:
        if t is datetime.datetime:
            return cel_values.parse_timestamp(v)
        if t is datetime.timedelta:
            d = _iso_duration(v)
            return d if d is not None else cel_values.parse_duration(v)
        return uuid.UUID(v)
    except (CelError, ValueError, OverflowError):
        raise bad from None


def instance_class(schema: Any) -> Any:
    """The class of the values parse builds for a closed schema: the pydantic model of a closed
    pydantic schema, the dataclass itself, or None for a {field: type} map (a plain dict)."""
    model = getattr(schema, "__inv_pydantic__", None)
    if model is not None:
        return model
    return schema if isinstance(schema, type) and dataclasses.is_dataclass(schema) else None


def _build(data: Any, schema: Any, prefix: str, depth: int, safe: str | None = None) -> Any:
    """data (a JSON object, or an instance of exactly the schema's instance class) checked field by
    field and rebuilt: a new dict for a {field: type} map, a new instance for a dataclass, a new
    model instance (validated by pydantic) for a closed pydantic schema."""
    safe = prefix if safe is None else safe
    cls = instance_class(schema)
    if cls is not None and type(data) is cls:
        raw = getattr(schema, "__inv_raw__", None)
        data = raw(data) if raw is not None else {f.name: object.__getattribute__(data, f.name)
                                                  for f in dataclasses.fields(schema)}
    if type(data) not in _DICTS:
        what, swhat = ("reply", "reply") if not prefix else (f"field {prefix[:-1]}", f"field {safe[:-1]}")
        raise _err(f"{swhat} is not an object (got {_vt(data)})", f"{what} is not an object (got {type(data).__name__})")
    fields = schema_fields(schema)
    names = list(data)
    bad_keys = [k for k in names if type(k) is not str]
    if bad_keys:
        raise _err(f"{safe}field names must be strings", f"{prefix}field names must be strings (got {_show(bad_keys[0])})")
    extra, missing = set(names) - set(fields), set(fields) - set(names)
    if extra:
        raise _err(f"{safe}extra fields forbidden ({len(extra)} not in the schema)",
                   f"{prefix}extra fields forbidden: {sorted(extra)[:10]}")
    if missing:
        raise _err(f"{safe}missing fields: {sorted(missing)}")        # schema field names, not reply data
    out = {k: _check(data[k], t, f"{prefix}{k}", depth, f"{safe}{k}") for k, t in fields.items()}
    build = getattr(schema, "__inv_build__", None)
    if build is not None:
        return build(out)
    if isinstance(schema, type) and dataclasses.is_dataclass(schema):
        return schema(**out)
    return out


def parse_error_text(e: ParseError, reg: Registry) -> str:
    """The text of a parse_error for outcomes, events, logs and exceptions: value-free, unless debug."""
    return (getattr(e, "feedback", None) or str(e)) if reg.debug else str(e)


def _no_constant(name: str) -> Any:
    """json.loads accepts NaN, Infinity and -Infinity, which RFC 8259 JSON does not have."""
    raise ParseError(f"not JSON: {name} is not a JSON number (non-finite numbers are refused)")


def parse(raw: Any, schema: Any, *, max_size: int | None = None) -> Any:
    """Parse a reply into a closed schema (dataclass, {field: type} or pydantic model); extra or missing fields fail.

    raw is JSON text (str or bytes) or an already-decoded object. Any failure, whatever the reply
    holds, is a ParseError: nothing else escapes. A {field: type} schema gives a new dict, a
    dataclass schema a new instance. max_size: the longest JSON text accepted (default the
    registry's max_reply_size, 1,000,000 characters or bytes; 0 for no limit). NaN, Infinity and
    -Infinity (which Python's json accepts) and any non-finite float in a decoded value fail.
    """
    schema = closed_schema(schema)
    try:
        if isinstance(raw, (str, bytes, bytearray)):
            limit = current().max_reply_size if max_size is None else max_size
            if limit and len(raw) > limit:
                raise ParseError(f"reply is too large ({len(raw)} > {limit} characters)")
            try:
                data = json.loads(raw, parse_constant=_no_constant)
            except json.JSONDecodeError as e:
                raise ParseError(f"not JSON: {e.msg}") from None
            except (UnicodeDecodeError, ValueError) as e:
                raise ParseError(f"not JSON: {type(e).__name__}") from None
        else:
            data = raw
        if isinstance(data, dict) and type(data) is not dict:
            data = dict(data)                       # a dict subclass: plain data only from here on
        if type(data) is not dict and not (instance_class(schema) is not None and type(data) is instance_class(schema)):
            raise ParseError(f"reply is not an object (got {type(data).__name__})")
        return _build(data, schema, "", 0)
    except ParseError:
        raise
    except RecursionError:
        raise ParseError("reply is nested too deeply") from None
    except MemoryError:
        raise ParseError("reply is too large") from None
    except Exception as e:                          # a hostile value, a schema __post_init__ that raised
        raise ParseError(f"reply could not be checked against the schema ({type(e).__name__})") from None


# --- what a rule reads, and the edge's re-check --------------------------------------------------
INSTANCE_ATTR = "_inv_model"        # (0.x) on a closed pydantic reply: the validated model instance


def rule_view(reply: Any) -> Any:
    """What a rule reads as `reply`: the parsed reply itself (a dict, a dataclass instance, or the
    validated pydantic model instance, whose datetime, timedelta, UUID, Enum and nested fields are
    mapped to CEL by cel/values.py exactly as arguments are)."""
    try:
        return object.__getattribute__(reply, INSTANCE_ATTR)
    except Exception:
        return reply


def _field_of(reply: Any, name: str) -> Any:
    """A field of a parsed reply: a key of a {field: type} reply, an attribute of a dataclass or
    pydantic reply (an Enum member stands for its value)."""
    if type(reply) is dict:
        return reply.get(name)
    v = object.__getattribute__(reply, name)
    return v.value if isinstance(v, enum.Enum) else v


def check_schema(schema: Any, who: str) -> None:
    """TypeError unless schema is a closed schema whose every field type parse can check."""
    if not (isinstance(schema, dict) or (isinstance(schema, type) and dataclasses.is_dataclass(schema))):
        raise TypeError(f"{who}: schema must be a dataclass, a {{field: type}} map or a pydantic model "
                        f"(got {schema!r})")
    for k, t in schema_fields(schema).items():
        if not isinstance(k, str) or not supported_field_type(t):
            raise TypeError(f"{who}: field {k!r}: unsupported field type {_tname(t)} (use plain classes, Any, "
                            f"Literal, Optional, list[T], dict[str, T] or a nested dataclass)")


def recheck(reply: Any, schema: Any) -> Any:
    """The received value re-validated against the closed schema (a fresh copy); ParseError if it does not fit.

    A dataclass schema takes an instance of exactly that class, a closed pydantic schema an
    instance of exactly its model, a {field: type} map a plain dict: extra and missing fields,
    out-of-enum values and wrong types fail as they do in inv.parse.
    """
    cls = instance_class(schema)
    if cls is not None:
        if type(reply) is not cls:
            got = type(reply)
            raise ParseError(f"value was not built by inv.parse from the closed schema "
                             f"{cls.__name__} (got {got.__module__}.{got.__qualname__})")
    elif type(reply) not in (dict, FrozenDict):
        raise ParseError(f"value was not built by inv.parse from the closed {{field: type}} schema "
                         f"(got {type(reply).__module__}.{type(reply).__qualname__})")
    return parse(_thaw(reply), schema)
