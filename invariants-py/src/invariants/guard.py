"""Decorators: @inv.guard, @inv.model_call, @inv.edge, and inv.Checked.

Each works on sync and async functions. Provider timing: a pre_effect enforcer reads its
providers before the call; a post_effect enforcer reads them after it. Async providers are
awaited by async guards, model calls and edges.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime
import functools
import inspect
import json
import math
import re
import enum
import reprlib
import sys
import time
import types
import typing
import uuid
from typing import (Any, Awaitable, Callable, Coroutine, Iterator, ParamSpec, Protocol, TypeVar,
                    overload)

from .cel import CelError, values as cel_values
from .errors import CoverageError, Escalation, ParseError
from .evaluate import aact, ablated, act, evaluate, report
from .loader import closed_schema, literal_eq, schema_fields, type_hints
from .model import GUARD_POINTS, Outcome, Rule
from .providers import ProviderFailed, prefetch_async, prefetch_many
from .registry import WRAPPED, Handoff, Registry, _wraps, current, note_decision
from .resolve import resolve
from .snapshot import take_snapshot

P = ParamSpec("P")
R = TypeVar("R")

# --- guard --------------------------------------------------------------------------------------
def _descriptor(fn: Any, deco: Callable[[Any], Any]) -> Any:
    """Wrap the function inside a classmethod or staticmethod and re-wrap it; None for a plain function."""
    if isinstance(fn, (classmethod, staticmethod)):
        return type(fn)(deco(fn.__func__))
    return None


def _hints(fn: Any, who: str) -> dict[str, Any]:
    """fn's resolved annotations; TypeError naming the one that does not resolve. Call it from a
    decorator's deco(): an annotation naming a class local to the function that applies the
    decorator (string annotations under `from __future__ import annotations`) is looked up in that
    function's locals and in fn's closure."""
    localns: dict[str, Any] = {}
    f: types.FrameType | None = sys._getframe(1)
    while f is not None and f.f_globals.get("__name__") == __name__:
        f = f.f_back
    if f is not None and f.f_locals is not f.f_globals:
        localns.update(f.f_locals)
    try:
        localns.update(inspect.getclosurevars(fn).nonlocals)
    except Exception:
        pass
    return type_hints(fn, who, TypeError, localns)


# --- guard --------------------------------------------------------------------------------------
class _Call:
    """One guarded call: bound arguments, the rules that apply, provider caches and snapshots."""

    __slots__ = ("reg", "providers", "self_", "base", "rules", "snap", "pre", "post", "broken", "started")

    def __init__(self, reg: Registry, self_: Any, args: dict[str, Any]) -> None:
        self.reg, self.self_ = reg, self_
        # one registry generation per call: inv.load mid-call (a hot reload) installs new tables on reg,
        # and this call's rules must keep binding the providers they were loaded with
        self.providers: dict[str, Any] = reg.providers
        self.base: dict[str, Any] = {"args": args, "self": self_}
        self.rules: list[tuple[Rule, list[tuple[int, dict[str, Any]]]]] = []
        self.snap: dict[str, Any] = {}
        self.pre: dict[Any, Any] = {}       # provider values read before the effect
        self.post: dict[Any, Any] = {}      # provider values read after it
        self.broken: dict[tuple[str, Any], str] = {}   # (rule id, enforcer) whose provider prefetch failed
        self.started: dict[str, int] = {"pre_effect": time.time_ns()}   # when each check began (events, spans)

    def enforcers(self, timing: str) -> Iterator[tuple[Rule, int, dict[str, Any]]]:
        for rule, live in self.rules:
            for i, enf in live:
                if enf["timing"] == timing:
                    yield rule, i, enf

    def env_base(self, rule: Rule, timing: str, result: Any = None) -> dict[str, Any]:
        base = {**self.base, "old_self": self.snap.get(rule.id)}
        if timing == "post_effect":
            base["result"] = result
        return base


def guard(*keys: str, points: tuple[str, ...] = ("decorator",)) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """@inv.guard("R-1", ...) or @inv.guard("py:jobs.split_work"): pre- and post-effect checks.

    points: the enforcer points this guard evaluates (decorator, and middleware for HTTP handlers).
    Generator functions are refused: their effect runs lazily, after any check could see it.
    """
    bad_points = set(points) - GUARD_POINTS
    if bad_points or not points:
        raise ValueError(f"guard points must be from {sorted(GUARD_POINTS)}, got {list(points)}")

    def deco(fn: Callable[P, R]) -> Callable[P, R]:
        if (d := _descriptor(fn, deco)) is not None:
            return typing.cast(Callable[P, R], d)
        if inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn):
            raise TypeError(f"@inv.guard cannot wrap the generator {fn.__qualname__}: its body runs after the "
                            f"guard returns; guard a function that returns a value")
        sig = inspect.signature(fn)

        def start(a: tuple[Any, ...], kw: dict[str, Any]) -> _Call:
            reg = current()
            bound = sig.bind(*a, **kw)
            bound.apply_defaults()
            self_ = bound.arguments.get("self")
            call = _Call(reg, self_, {k: v for k, v in bound.arguments.items() if k != "self"})
            call.providers, rules = reg.generation(keys)
            for rule in rules:
                if rule.status == "retired":
                    continue
                live = [(i, e) for i, e in enumerate(rule.enforcers)
                        if e["point"] in points and reg.mode(rule, e) != "off"]
                if not live:
                    continue
                if reg.skips(rule.id):
                    for _, enf in live:
                        ablated(reg, rule, enf.get("point"))
                    continue
                call.rules.append((rule, live))
                if rule.snapshot:
                    try:
                        call.snap[rule.id] = take_snapshot(rule, self_)
                    except CelError as e:
                        call.snap[rule.id] = ProviderFailed(str(e))
                    except Exception as e:
                        call.snap[rule.id] = ProviderFailed(f"old_self snapshot raised {type(e).__name__}")
            return call

        async def aprefetch(call: _Call, timing: str, result: Any = None) -> None:
            """Every async provider the enforcers at timing read, awaited together."""
            cache = call.pre if timing == "pre_effect" else call.post
            live = list(call.enforcers(timing))
            try:
                whys = await prefetch_many(call.reg, [(rule, rule.names_for[i], call.env_base(rule, timing, result))
                                                      for rule, i, _ in live], cache, call.providers)
            except Exception as e:
                whys = [f"binding providers raised {type(e).__name__}"] * len(live)
            for (rule, i, _), why in zip(live, whys):
                if why is not None:
                    call.broken.setdefault((rule.id, i), why)

        def outcomes(call: _Call, timing: str, result: Any = None) -> Iterator[tuple[Rule, dict[str, Any], Outcome]]:
            """Each enforcer at timing, evaluated when the caller asks for it (its action may raise first)."""
            reg, cache = call.reg, call.pre if timing == "pre_effect" else call.post
            for rule, i, enf in call.enforcers(timing):
                # a failed old_self snapshot errors only the clauses that read old_self
                broken = call.broken.get((rule.id, i))
                yield rule, enf, (Outcome("rule_error", rule.id, error=broken) if broken is not None else
                                  evaluate(rule, call.env_base(rule, timing, result), i, reg, cache, call.providers))

        def check(call: _Call, timing: str, result: Any = None) -> None:
            for rule, enf, out in outcomes(call, timing, result):
                act(call.reg, rule, enf, out, started=call.started[timing])

        async def acheck(call: _Call, timing: str, result: Any = None) -> None:
            for rule, enf, out in outcomes(call, timing, result):
                await aact(call.reg, rule, enf, out, started=call.started[timing])

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*a: Any, **kw: Any) -> Any:
                call = start(a, kw)
                await aprefetch(call, "pre_effect")
                await acheck(call, "pre_effect")
                result = await typing.cast(Callable[..., Awaitable[Any]], fn)(*a, **kw)
                call.started["post_effect"] = time.time_ns()
                await aprefetch(call, "post_effect", result)
                await acheck(call, "post_effect", result)
                return result
            wrapper: Any = awrapper
        else:
            @functools.wraps(fn)
            def swrapper(*a: Any, **kw: Any) -> Any:
                call = start(a, kw)
                check(call, "pre_effect")
                result = fn(*a, **kw)
                call.started["post_effect"] = time.time_ns()
                check(call, "post_effect", result)
                return result
            wrapper = swrapper
        wrapper.__inv_rules__, wrapper.__inv_kind__ = keys, "guard"
        wrapper.__inv_points__ = tuple(points)
        WRAPPED.add(wrapper)
        return typing.cast(Callable[P, R], wrapper)
    return deco


# --- model calls and edges ----------------------------------------------------------------------
def _immutable(*_: Any, **__: Any) -> Any:
    raise TypeError("checked values are immutable")


_FREEZE_DEPTH = 256


class FrozenDict(dict[Any, Any]):
    """The value of a Checked over a {field: type} schema: a dict (equal to, and read like, the parsed
    one) whose mutating methods raise TypeError, as do those of the lists and maps inside it."""

    __slots__ = ()
    __setitem__ = __delitem__ = __ior__ = _immutable
    clear = pop = popitem = setdefault = update = _immutable

    def __init__(self, *a: Any, **k: Any) -> None:
        if a or k:          # dict.__init__ on a built FrozenDict would update it in place
            _immutable()

    def __copy__(self) -> Any:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        return _freeze(copy.deepcopy(_thaw(self), memo))

    def __reduce__(self) -> Any:
        return (_freeze, (_thaw(self),))


class FrozenList(list[Any]):
    """A list inside a frozen Checked value: equal to, and read like, a list; its mutating methods
    raise TypeError."""

    __slots__ = ()
    __setitem__ = __delitem__ = __iadd__ = __imul__ = _immutable
    append = extend = insert = pop = remove = clear = sort = reverse = _immutable

    def __init__(self, *a: Any, **k: Any) -> None:
        if a or k:          # list.__init__ on a built FrozenList would replace its items
            _immutable()

    def __copy__(self) -> Any:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        return _freeze(copy.deepcopy(_thaw(self), memo))

    def __reduce__(self) -> Any:
        return (_freeze, (_thaw(self),))


def _freeze(v: Any, depth: int = 0) -> Any:
    """Plain dicts and lists (and those inside them) as FrozenDict and FrozenList; anything else as is."""
    if depth > _FREEZE_DEPTH:
        return v
    if type(v) is dict:
        out = FrozenDict()
        for k, x in v.items():
            dict.__setitem__(out, k, _freeze(x, depth + 1))
        return out
    if type(v) is list:
        items = FrozenList()
        list.extend(items, [_freeze(x, depth + 1) for x in v])
        return items
    return v


def _thaw(v: Any, depth: int = 0) -> Any:
    """The plain dicts and lists a frozen value was made from (a copy)."""
    if depth > _FREEZE_DEPTH:
        return v
    if isinstance(v, FrozenDict):
        return {k: _thaw(x, depth + 1) for k, x in dict.items(v)}
    if isinstance(v, FrozenList):
        return [_thaw(x, depth + 1) for x in list.__iter__(v)]
    return v


class Checked:
    """Base for refined types built by inv.model_call; carries value, attempts, rule and verdict.

    Python cannot make this unforgeable (object.__new__ exists). The type is convenience;
    an @inv.edge re-validates the value against the closed schema, re-checks bad on it and calls
    its consumer with a new Checked over the re-validated copy (equal to the received one).
    copy.copy returns the same (immutable) object; copy.deepcopy and pickle give an equal
    Checked over a copy of the value. Neither adds trust: an edge re-checks a copy the same way.
    The value of a {field: type} schema is a FrozenDict (a dict whose mutating methods raise
    TypeError, as do those of the lists and maps inside it), so neither `checked.value.update(...)`
    nor `checked.update(...)` (attribute reads are forwarded to the value) changes it.
    """

    __slots__ = ("value", "attempts", "rule", "verdict")

    def __init__(self, *a: Any, **k: Any) -> None:
        raise TypeError(f"{type(self).__name__} is constructed only by inv.model_call")

    @classmethod
    def _inv_make(cls, value: Any, attempts: int, rule: str, verdict: str) -> Any:
        obj = object.__new__(cls)
        for k, v in (("value", _freeze(value)), ("attempts", attempts), ("rule", rule), ("verdict", verdict)):
            object.__setattr__(obj, k, v)
        return obj

    def __setattr__(self, k: str, v: Any) -> None:
        raise AttributeError("checked values are immutable")

    def __delattr__(self, k: str) -> None:
        raise AttributeError("checked values are immutable")

    def __getattr__(self, k: str) -> Any:
        if k.startswith("__") and k.endswith("__"):     # never borrow the value's protocols (copy, pickle)
            raise AttributeError(k)
        return getattr(object.__getattribute__(self, "value"), k)

    def _inv_state(self) -> tuple[Any, Any, Any, Any]:
        return _slot(self, "value"), _slot(self, "attempts"), _slot(self, "rule"), _slot(self, "verdict")

    def __eq__(self, other: object) -> bool:
        """Equal Checked values: the same class, rule, verdict, attempts and an equal value (an edge
        hands its consumer an equal copy, never the received object)."""
        if type(other) is not type(self):
            return NotImplemented
        return self._inv_state() == Checked._inv_state(other)

    def __hash__(self) -> int:
        _, attempts, rule, verdict = self._inv_state()
        return hash((type(self), attempts, rule, verdict))

    def __copy__(self) -> Any:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        value, attempts, rule, verdict = self._inv_state()
        return type(self)._inv_make(copy.deepcopy(value, memo), attempts, rule, verdict)

    def __reduce__(self) -> Any:
        return (_rebuild_checked, (type(self), *self._inv_state()))

    def __repr__(self) -> str:
        value, attempts, _, verdict = self._inv_state()
        return f"{type(self).__name__}({value!r}, verdict={verdict}, attempts={attempts})"


def _slot(obj: Any, k: str) -> Any:
    try:
        return object.__getattribute__(obj, k)
    except AttributeError:
        return None


def _rebuild_checked(cls: Any, value: Any, attempts: Any, rule: Any, verdict: Any) -> Any:
    """Unpickle a Checked (it carries no more trust than any other Checked: an edge re-checks it)."""
    if not (isinstance(cls, type) and issubclass(cls, Checked)):
        raise TypeError(f"{cls!r} is not an inv.Checked subclass")
    return cls._inv_make(value, attempts, rule, verdict)


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
    elif type(v) is list:
        for i, x in enumerate(v):
            _finite(x, f"{where}[{i}]", depth + 1, f"{safe}[{i}]")
    elif type(v) is dict:
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
        if type(v) is not list:
            raise _err(f"field {safe}: {_vt(v)} is not a list", f"field {where}: {_show(v)} is not a list")
        return [_check(x, args[0], f"{where}[{i}]", depth + 1, f"{safe}[{i}]") for i, x in enumerate(v)]
    if origin is dict:
        if type(v) is not dict:
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
    if type(data) is not dict:
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


class _ModelRun:
    """The state of one model_call invocation: parse -> abstain -> bad, with bounded retries.

    A rule that is retired, whose model_call enforcer is in effective mode off, or that is ablated
    is passive: the model is called once and its reply parsed into the closed schema, and nothing
    else happens (no format retries, no abstain escalation, no bad, no event but `ablated`).
    """

    def __init__(self, rule_id: str, schema: Any, refined: Any, sig: inspect.Signature,
                 a: tuple[Any, ...], kw: dict[str, Any]) -> None:
        self.started = time.time_ns()
        self.reg = reg = current()
        # one registry generation per call, as @inv.guard: a reload mid-call (a hot inv.load) must not
        # swap the providers this call's rule was loaded with
        self.providers, rules = reg.generation((rule_id,))
        self.rule = rule = rules[0]
        pol = rule.policy or {}
        i, enf = rule.enforcer("model_call")
        if i is None or enf is None:
            raise CoverageError(f"@inv.model_call({rule_id!r}): the rule has no model_call enforcer")
        self.i, self.enf = i, enf
        self.passive = rule.status == "retired" or reg.mode(rule, enf) == "off"
        if not self.passive and reg.skips(rule.id):
            ablated(reg, rule, "model_call")
            self.passive = True
        bound = sig.bind(*a, **kw)
        bound.apply_defaults()
        self.sig, self.bound, self.schema, self.refined = sig, bound, schema, refined
        self.base = {"args": {k: v for k, v in bound.arguments.items() if k not in ("inv_feedback", "self")},
                     "self": bound.arguments.get("self")}
        self.fb = pol.get("feedback", {"format": "parser_error", "semantic": "none"})
        self.prov: dict[Any, Any] = {}                 # this call's provider cache
        self.fmt_left, self.sem_left = pol.get("format_retries", 2), rule.semantic_resamples
        self.ab = pol.get("abstain")
        self.record: list[dict[str, Any]] = []
        self.feedback: str | None = None               # the full text sent to the model as inv_feedback
        self.fed_back: str | None = None               # what the attempt record says was sent (value-free)

    def call_args(self) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """(args, kwargs) for the next model call, with inv_feedback set when declared."""
        if "inv_feedback" in self.sig.parameters:
            self.bound.arguments["inv_feedback"] = self.feedback
        return self.bound.args, self.bound.kwargs

    def passive_result(self, raw: Any) -> Any:
        """The one reply of a passive run, parsed (verdict ok), or as given (verdict parse_error)."""
        try:
            reply = parse(raw, self.schema)
        except ParseError:
            return self.refined._inv_make(raw, 1, self.rule.id, "parse_error")
        return self.refined._inv_make(reply, 1, self.rule.id, "ok")

    def checked(self, out: Outcome, value: Any) -> Any:
        return self.refined._inv_make(value, len(self.record), self.rule.id, out.kind)

    def finish(self, out: Outcome, value: Any) -> Any:
        act(self.reg, self.rule, self.enf, out, self.record, started=self.started)
        return self.checked(out, value)

    async def afinish(self, out: Outcome, value: Any) -> Any:
        await aact(self.reg, self.rule, self.enf, out, self.record, started=self.started)
        return self.checked(out, value)

    def parsed(self, raw: Any) -> tuple[str, Any]:
        """('retry', None) | ('finish', (outcome, value)) | ('abstain', reply) | ('eval', reply).
        Nothing but an InvariantError escapes."""
        rule, reg = self.rule, self.reg
        try:
            reply = parse(raw, self.schema)
        except ParseError as e:
            told = parse_error_text(e, reg)         # value-free unless debug: it reaches events and logs
            self.record.append({"attempt": len(self.record) + 1, "outcome": "parse_error", "error": told,
                                "fed_back": self.fed_back})
            out = Outcome("parse_error", rule.id, error=told)
            if self.fmt_left:
                self.fmt_left -= 1
                send = self.fb.get("format", "parser_error") == "parser_error"
                self.feedback = e.feedback if send else None      # the model sees the full text
                self.fed_back = told if send else None
                report(reg, rule, self.enf, out, "retry", self.record)
                return "retry", None
            return "finish", (out, raw)
        ab = self.ab
        if ab is not None:
            try:
                abstained = _same(_field_of(reply, ab["field"]), ab["value"])
            except Exception as e:      # by contract unreachable (parse gives plain data); fail closed
                self.record.append({"attempt": len(self.record) + 1, "outcome": "rule_error",
                                    "fed_back": self.fed_back})
                return "finish", (Outcome("rule_error", rule.id, error=f"abstain check raised {type(e).__name__}"),
                                  reply)
            if abstained:
                self.record.append({"attempt": len(self.record) + 1, "outcome": "abstain",
                                    "fed_back": self.fed_back})
                return "abstain", reply
        return "eval", reply

    def handoff(self, reply: Any) -> Handoff:
        note_decision(self.rule.id)          # an abstain is the rule's decision (see --inv-ablate)
        return Handoff(self.rule.id, "escalate", None, {"reply": reply, **self.base}, list(self.record),
                       reason="abstain")

    def escalation(self, review: str) -> Escalation:
        return Escalation(f"{self.rule.id}: model abstained ({(self.ab or {}).get('value')}); sent to a person "
                          f"(opened {review})", None, self.record, review, rule=self.rule.id, point="model_call")

    def eval_base(self, reply: Any) -> dict[str, Any]:
        return {**self.base, "reply": rule_view(reply)}

    def judged(self, reply: Any) -> tuple[str, Any]:
        """('retry', None) | ('finish', (outcome, reply))."""
        rule, reg = self.rule, self.reg
        out = evaluate(rule, self.eval_base(reply), self.i, reg, self.prov, self.providers)
        self.record.append({"attempt": len(self.record) + 1, "outcome": out.kind, "messages": out.messages,
                            "fed_back": self.fed_back})
        if out.kind == "violation" and self.sem_left and reg.mode(rule, self.enf) == "enforce":
            self.sem_left -= 1
            self.feedback = self.fed_back = None     # fresh resample: never the rule text or expected value
            report(reg, rule, self.enf, out, "resample", self.record)
            return "retry", None
        return "finish", (out, reply)              # rule_error never resamples: on_error applies


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


C = TypeVar("C", bound=Checked)
C_co = TypeVar("C_co", bound=Checked, covariant=True)


class ModelCallDecorator(Protocol[C_co]):
    """What inv.model_call(rule, schema=S, returns=CheckedT) gives: a decorator for a body that
    returns the model's raw reply (JSON text, bytes or decoded data); the decorated function
    returns CheckedT (awaitable for an async def)."""

    @overload
    def __call__(self, fn: Callable[P, Coroutine[Any, Any, Any]], /) -> Callable[P, Coroutine[Any, Any, C_co]]: ...

    @overload
    def __call__(self, fn: Callable[P, Any], /) -> Callable[P, C_co]: ...


@overload
def model_call(rule_id: str, schema: Any, *, returns: type[C]) -> ModelCallDecorator[C]: ...


@overload
def model_call(rule_id: str, schema: Any, *, returns: None = None) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def model_call(rule_id: str, schema: Any, *, returns: Any = None) -> Any:
    """@inv.model_call("R-7", schema=CauseReply, returns=CheckedCause): parse -> abstain -> bad -> accept,
    with bounded retries.

    The body calls the model and returns its raw reply; annotate it with what it really returns
    (`-> str`): with returns=, the decorated function returns that inv.Checked subclass, and type
    checkers see it so. Without returns=, the body's return annotation must be the Checked
    subclass (the 0.x form; the body then needs a cast or a `type: ignore` under mypy).

    Once retries are spent, the model_call enforcer's on_violation decides. schema may be a
    dataclass, a {field: type} map or a pydantic model with extra='forbid'. On an async def,
    the wrapper is async: each reply is awaited, and async providers are awaited too.
    """
    if returns is not None and not (isinstance(returns, type) and issubclass(returns, Checked)):
        raise TypeError(f"@inv.model_call({rule_id!r}): returns must be an inv.Checked subclass, got {returns!r}")
    schema = closed_schema(schema)
    check_schema(schema, f"@inv.model_call({rule_id!r})")

    def deco(fn: Any) -> Any:
        if (d := _descriptor(fn, deco)) is not None:
            return d
        if inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn):
            raise TypeError(f"@inv.model_call cannot wrap the generator {fn.__qualname__}")
        sig = inspect.signature(fn)
        fb = sig.parameters.get("inv_feedback")
        if fb is not None and fb.kind not in (fb.POSITIONAL_OR_KEYWORD, fb.KEYWORD_ONLY):
            raise TypeError(f"@inv.model_call: {fn.__qualname__}'s inv_feedback must be a named parameter, "
                            f"not *args, **kwargs or positional-only")
        refined: Any = returns
        if refined is None:
            refined = sig.return_annotation
            if isinstance(refined, str):
                refined = _hints(fn, f"@inv.model_call({rule_id!r}) on {fn.__qualname__}").get("return")
            if not (isinstance(refined, type) and issubclass(refined, Checked)):
                raise TypeError(f"{fn.__name__} must be annotated to return an inv.Checked subclass, or pass "
                                f"returns=<the Checked subclass> to @inv.model_call")

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*a: Any, **kw: Any) -> Any:
                run = _ModelRun(rule_id, schema, refined, sig, a, kw)
                if run.passive:
                    ca, ckw = run.call_args()
                    return run.passive_result(await fn(*ca, **ckw))
                while True:
                    ca, ckw = run.call_args()
                    step, v = run.parsed(await fn(*ca, **ckw))
                    if step == "eval":
                        try:        # never raises by contract; evaluate() reports a binding failure
                            await prefetch_async(run.reg, run.rule, run.rule.names_for[run.i], run.eval_base(v),
                                                 run.prov, run.providers)
                        except Exception:
                            pass
                        step, v = run.judged(v)
                    if step == "abstain":
                        raise run.escalation(await run.reg.ahand_off(run.handoff(v)))
                    if step == "finish":
                        return await run.afinish(*v)
            wrapper: Any = awrapper
        else:
            @functools.wraps(fn)
            def swrapper(*a: Any, **kw: Any) -> Any:
                run = _ModelRun(rule_id, schema, refined, sig, a, kw)
                if run.passive:
                    ca, ckw = run.call_args()
                    return run.passive_result(fn(*ca, **ckw))
                while True:
                    ca, ckw = run.call_args()
                    step, v = run.parsed(fn(*ca, **ckw))
                    if step == "eval":
                        step, v = run.judged(v)
                    if step == "abstain":
                        raise run.escalation(run.reg.hand_off(run.handoff(v)))
                    if step == "finish":
                        return run.finish(*v)
            wrapper = swrapper

        wrapper.__inv_rules__, wrapper.__inv_kind__ = (rule_id,), "model_call"
        wrapper.__inv_schema__ = schema
        WRAPPED.add(wrapper)
        return wrapper
    return deco


def _received(value: Any) -> tuple[str | None, Any]:
    """(verdict, value) of a received Checked; verdict None when it was not built by inv.model_call
    (no verdict, or a verdict that is not a plain string)."""
    try:
        verdict, reply = object.__getattribute__(value, "verdict"), object.__getattribute__(value, "value")
    except AttributeError:
        return None, None
    return (verdict, reply) if type(verdict) is str else (None, None)


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


@dataclasses.dataclass
class _EdgeCall:
    """One edge call that is checked: what judge() needs, then the consumer's (args, kwargs)."""

    reg: Registry
    rule: Rule
    i: int
    enf: dict[str, Any]
    verdict: str | None
    perr: str | None
    base: dict[str, Any]
    providers: dict[str, Any]
    started: int
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    cache: dict[Any, Any] = dataclasses.field(default_factory=dict)


def _checked_type(t: Any) -> Any:
    """The Checked subclass an annotation names (also as X | None), else None."""
    if isinstance(t, type) and issubclass(t, Checked):
        return t
    if typing.get_origin(t) in (typing.Union, types.UnionType):
        found = [a for a in typing.get_args(t) if isinstance(a, type) and issubclass(a, Checked)]
        rest = [a for a in typing.get_args(t) if a is not type(None) and a not in found]
        if len(found) == 1 and not rest:
            return found[0]
    return None


def _state_fields(t: Any, who: str) -> dict[str, Any]:
    """Field -> annotation of a state type (TypedDict, dataclass or pydantic model), else {}."""
    if not isinstance(t, type):
        return {}
    model_fields = getattr(t, "model_fields", None)
    if isinstance(model_fields, dict) and hasattr(t, "model_copy"):
        return {n: f.annotation for n, f in model_fields.items()}
    if typing.is_typeddict(t) or dataclasses.is_dataclass(t):
        return type_hints(t, who)
    return {}


def _edge_path(sig: inspect.Signature, hints: dict[str, Any], at: str | None,
               who: str) -> tuple[tuple[str, ...], Any]:
    """(parameter, field, ...) leading to the Checked value an edge receives, and its Checked subclass.

    at=None: a parameter annotated with a Checked subclass, else the one field annotated with one in
    the state types (TypedDict, dataclass, pydantic model) the parameters are annotated with;
    none, or more than one, is a TypeError. at="state.plan": that parameter and field path, whose
    annotations must lead to a Checked subclass."""
    if at is not None:
        parts = tuple(at.split(".")) if isinstance(at, str) else ()
        if len(parts) < 2 or not all(parts) or parts[0] not in sig.parameters:
            raise TypeError(f"{who}: at={at!r} must be '<parameter>.<field>[.<field>...]' naming one of "
                            f"its parameters ({', '.join(sig.parameters)})")
        t = hints.get(parts[0])
        for f in parts[1:]:
            fields = _state_fields(t, who)
            if f not in fields:
                raise TypeError(f"{who}: at={at!r}: {getattr(t, '__name__', t)!r} has no annotated field {f!r} "
                                f"(annotate the parameter with a TypedDict, dataclass or pydantic model)")
            t = fields[f]
        refined = _checked_type(t)
        if refined is None:
            raise TypeError(f"{who}: at={at!r} is annotated {t!r}, not an inv.Checked subclass")
        return parts, refined
    direct = [(n, c) for n in sig.parameters if (c := _checked_type(hints.get(n))) is not None
              and isinstance(hints.get(n), type)]
    if direct:
        return (direct[0][0],), direct[0][1]
    found = [((n, f), c) for n in sig.parameters for f, ft in _state_fields(hints.get(n), who).items()
             if (c := _checked_type(ft)) is not None]
    if len(found) == 1:
        return found[0]
    if found:
        raise TypeError(f"{who}: more than one Checked field in its state ({', '.join('.'.join(p) for p, _ in found)}); "
                        f"name the one this edge receives with at='<parameter>.<field>'")
    raise TypeError(f"{who}: an edge needs a parameter annotated with an inv.Checked subclass, or a state "
                    f"parameter (TypedDict, dataclass or pydantic model) with one Checked field (or at=...)")


def _get_field(obj: Any, f: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(f)
    return getattr(obj, f, None)


def _with_field(obj: Any, f: str, value: Any) -> Any:
    """A shallow copy of a state object with one field replaced (the caller's object is unchanged)."""
    if isinstance(obj, dict):
        new = copy.copy(obj)
        new[f] = value
        return new
    if hasattr(obj, "model_copy") and isinstance(getattr(type(obj), "model_fields", None), dict):
        return obj.model_copy(update={f: value})
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.replace(obj, **{f: value})
    raise TypeError(f"cannot hand the consumer a copy of a {type(obj).__name__} with {f} re-checked: use a "
                    f"dict (TypedDict), a dataclass or a pydantic model for the state")


def _set_path(root: Any, path: tuple[str, ...], value: Any) -> Any:
    """root with the field at path replaced, each container on the way a shallow copy."""
    if not path:
        return value
    return _with_field(root, path[0], _set_path(_get_field(root, path[0]), path[1:], value))


def edge(rule_id: str, *, at: str | None = None) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """@inv.edge("R-7") on the consumer of a Checked value (sync or async).

    The value arrives in a parameter annotated with the Checked subclass, or in a field of a state
    parameter (a TypedDict, dataclass or pydantic model, as agent graphs pass): the one field
    annotated with a Checked subclass (X or X | None), or the one at="state.plan" names. Several
    candidates without at= is a TypeError when the decorator runs. For a state, the consumer gets
    a shallow copy of it whose field holds the new Checked (the caller's state is unchanged).

    The type gate stops honest mistakes; then the verdict must be ok, the value is re-validated
    against the closed schema of the rule's model call (extra or missing fields, out-of-enum
    values, wrong types, a value of another class: parse_error), and bad is re-evaluated on it.
    The consumer is then called with a new Checked over that re-validated copy, the exact value
    the rule judged: no other code holds it, so nothing can change it between the check and the
    consumer's reads (a received object mutated or swapped later does not reach the consumer).
    The edge enforcer used is the one whose target is this consumer (a rule may have several).
    """
    def deco(fn: Callable[P, R]) -> Callable[P, R]:
        if (d := _descriptor(fn, deco)) is not None:
            return typing.cast(Callable[P, R], d)
        if inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn):
            raise TypeError(f"@inv.edge cannot wrap the generator {fn.__qualname__}")
        sig = inspect.signature(fn)
        who = f"@inv.edge({rule_id!r}) on {fn.__qualname__}"
        hints = _hints(fn, who)
        # parameters only: the return annotation is not a parameter the Checked value arrives in
        path, refined = _edge_path(sig, hints, at, who)
        pname, fields = path[0], path[1:]
        me: list[Any] = []                                    # the wrapper, once built
        picked: dict[str, tuple[Rule, int, dict[str, Any]]] = {}
        name = f"py:{fn.__module__}.{fn.__qualname__}"

        def enforcer_for(rule: Rule) -> tuple[int, dict[str, Any]]:
            """The rule's edge enforcer whose target is this consumer."""
            got = picked.get(rule.id)
            if got is not None and got[0] is rule:
                return got[1], got[2]
            edges = [(i, e) for i, e in enumerate(rule.enforcers) if e["point"] == "edge"]
            mine = [(i, e) for i, e in edges if e.get("target") == name]
            if not mine:
                for i, e in edges:
                    try:
                        obj = resolve(e["target"])
                    except Exception:
                        continue
                    if me and _wraps(obj, me[0]):
                        mine.append((i, e))
            if not mine and len(edges) == 1:
                mine = edges                   # one edge enforcer: inv verify reports a target mismatch
            if not mine:
                raise CoverageError(f"@inv.edge({rule_id!r}): the rule has no edge enforcer" if not edges else
                                    f"@inv.edge({rule_id!r}): no edge enforcer of the rule targets {name}")
            picked[rule.id] = (rule, *mine[0])
            return mine[0]

        def prepare(a: tuple[Any, ...], kw: dict[str, Any]) -> _EdgeCall | None:
            """None to call straight through; else the checked call."""
            started = time.time_ns()
            reg = current()
            bound = sig.bind(*a, **kw)
            bound.apply_defaults()
            value = bound.arguments[pname]
            for f in fields:                                    # read once: the value checked is this one
                value = _get_field(value, f)
            if not isinstance(value, refined):
                raise TypeError(f"{fn.__name__} accepts only {refined.__name__}")
            providers, rules = reg.generation((rule_id,))       # one generation per call
            rule = rules[0]
            i, enf = enforcer_for(rule)
            if reg.mode(rule, enf) == "off":
                return None
            if reg.skips(rule.id):
                ablated(reg, rule, "edge")
                return None
            verdict, reply = _received(value)
            perr: str | None = None
            schema = getattr(rule.target, "__inv_schema__", None)
            call = (a, kw)
            if verdict == "ok" and schema is not None:
                try:                    # the closed schema again: a forged or mutated value fails here
                    reply = recheck(reply, schema)
                except ParseError as e:
                    perr = parse_error_text(e, reg)
                else:                   # the consumer gets the copy the rule judges, never the received one
                    _, attempts, rid, _ = Checked._inv_state(value)
                    fresh = type(value)._inv_make(reply, attempts, rid, verdict)
                    bound.arguments[pname] = _set_path(bound.arguments[pname], fields, fresh)
                    call = (bound.args, bound.kwargs)
            base = {"args": {k: v for k, v in bound.arguments.items() if k != "self"},
                    "self": bound.arguments.get("self"), "reply": rule_view(reply)}
            return _EdgeCall(reg, rule, i, enf, verdict, perr, base, providers, started, *call)

        def judge(c: _EdgeCall) -> tuple[Outcome, bool]:
            refusal = c.verdict != "ok"          # a received non-ok verdict is refused, never failed open
            rule = c.rule
            if c.verdict is None:
                out = Outcome("rule_error", rule.id, [f"received a {refined.__name__} that inv.model_call did not "
                                                      f"build (it has no verdict)"])
            elif c.verdict != "ok":
                out = Outcome(c.verdict if c.verdict in ("violation", "rule_error", "parse_error") else "rule_error",
                              rule.id, [f"received a {refined.__name__} with verdict {c.verdict}"])
            elif c.perr is not None:
                out = Outcome("parse_error", rule.id, error=c.perr)
            else:
                out = evaluate(rule, c.base, c.i, c.reg, c.cache, c.providers)
            return out, refusal

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*a: Any, **kw: Any) -> Any:
                c = prepare(a, kw)
                if c is not None:
                    if c.verdict == "ok" and c.perr is None:
                        try:    # never raises by contract; evaluate() reports a binding failure
                            await prefetch_async(c.reg, c.rule, c.rule.names_for[c.i], c.base, c.cache, c.providers)
                        except Exception:
                            pass
                    out, refusal = judge(c)
                    await aact(c.reg, c.rule, c.enf, out, refusal=refusal, started=c.started)
                    a, kw = c.args, c.kwargs
                return await typing.cast(Callable[..., Awaitable[Any]], fn)(*a, **kw)
            wrapper: Any = awrapper
        else:
            @functools.wraps(fn)
            def swrapper(*a: Any, **kw: Any) -> Any:
                c = prepare(a, kw)
                if c is not None:
                    out, refusal = judge(c)
                    act(c.reg, c.rule, c.enf, out, refusal=refusal, started=c.started)
                    a, kw = c.args, c.kwargs
                return fn(*a, **kw)
            wrapper = swrapper

        me.append(wrapper)
        wrapper.__inv_rules__, wrapper.__inv_kind__ = (rule_id,), "edge"
        wrapper.__inv_at__ = ".".join(path)
        WRAPPED.add(wrapper)
        return typing.cast(Callable[P, R], wrapper)
    return deco
