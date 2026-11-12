"""Decorators: @inv.guard, @inv.transaction, @inv.model_call, @inv.edge, and inv.Checked.

Each works on sync and async functions. Provider timing: a pre_effect enforcer reads its
providers before the call; a post_effect enforcer reads them after the effect (inside the
transaction, for a transactional target), except `snapshot` providers, which are read once
before the effect. Async providers are awaited by async guards, model calls and edges.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy
import dataclasses
import datetime
import functools
import inspect
import json
import math
import re
import reprlib
import types
import typing
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator, ParamSpec, TypeVar

from .cel import CelError, values as cel_values
from .errors import CoverageError, Escalation, InvariantError, ParseError, RolledBack
from .evaluate import ablated, act, evaluate, report
from .loader import closed_schema, literal_eq, schema_fields
from .model import GUARD_POINTS, Outcome, Rule
from .providers import ProviderFailed, fetch_providers, prefetch_async
from .registry import WRAPPED, Handoff, Registry, current
from .snapshot import take_snapshot

P = ParamSpec("P")
R = TypeVar("R")
SNAPSHOT_ONLY = frozenset({"snapshot"})

# --- transactions -------------------------------------------------------------------------------
_TXN: contextvars.ContextVar[tuple[_Frame, ...]] = contextvars.ContextVar("inv_txn", default=())


class _Frame:
    __slots__ = ("key", "owner", "rollback_only", "rule", "closed", "savepoint")

    def __init__(self, key: str, owner: Any, savepoint: bool = False) -> None:
        self.key, self.owner, self.rollback_only = key, owner, False
        self.rule: str | None = None
        self.closed = False              # set on exit: a task that copied the context no longer sees it open
        self.savepoint = savepoint       # a real savepoint: a failure inside dooms only up to here


def _doom(frame: _Frame, rule_id: str) -> None:
    """Mark frame and every enclosing open frame of the same boundary and owner rollback-only.

    The walk stops at the first savepoint frame (inclusive): its generator can undo just its part.
    """
    stack = _TXN.get()
    start = next((i for i in range(len(stack) - 1, -1, -1) if stack[i] is frame), None)
    chain = [frame] if start is None else [stack[i] for i in range(start, -1, -1)]
    for f in chain:
        if f.closed or f.key != frame.key or f.owner is not frame.owner:
            continue
        f.rollback_only = True
        f.rule = f.rule or rule_id
        if f.savepoint:
            break


@typing.overload
def transaction(fn: Callable[..., Any], *, savepoint: bool = False) -> Callable[..., Any]: ...
@typing.overload
def transaction(fn: None = None, *, savepoint: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...


def transaction(fn: Callable[..., Any] | None = None, *, savepoint: bool = False) -> Any:
    """@inv.transaction on a generator (or async generator) method that BEGINs, yields, and COMMITs.

    It becomes a context manager (or async context manager). It marks the boundary so a guard can
    confirm it runs inside it. A failed guard dooms the transaction even if the caller swallows
    the exception (RolledBack at exit).

    Nesting: each block on the same owner pushes a frame. A failed guard dooms the innermost frame
    and every enclosing frame of the same boundary and owner, so a re-entrant boundary (only the
    outermost block BEGINs and COMMITs) raises RolledBack at the outer exit even if an inner
    failure was swallowed. Use @inv.transaction(savepoint=True) for a boundary whose nested block
    is a real savepoint: the doom then stops at that block. Tasks and threads that copy the
    context see the frame only while the block is open; after it exits their guards find no
    boundary (rule_error, or WiringError under fail_closed).
    """
    if fn is None:
        return lambda f: _transaction(f, savepoint)
    return _transaction(fn, savepoint)


def _transaction(fn: Callable[..., Any], savepoint: bool) -> Callable[..., Any]:
    key = f"py:{fn.__module__}.{fn.__qualname__}"

    def rolled_back(frame: _Frame) -> RolledBack:
        return RolledBack(f"a guard failed inside {key}; the transaction was rolled back", rule=frame.rule)

    if inspect.isasyncgenfunction(fn):
        ainner = contextlib.asynccontextmanager(fn)

        @functools.wraps(fn)
        @contextlib.asynccontextmanager
        async def awrapper(*a: Any, **kw: Any) -> AsyncIterator[Any]:
            frame = _Frame(key, a[0] if a else None, savepoint)
            token = _TXN.set(_TXN.get() + (frame,))
            try:
                async with ainner(*a, **kw) as v:
                    yield v
                    if frame.rollback_only:
                        raise rolled_back(frame)
            finally:
                frame.closed = True
                _TXN.reset(token)
        awrapper.__inv_txn_key__ = key  # type: ignore[attr-defined]
        awrapper.__inv_savepoint__ = savepoint  # type: ignore[attr-defined]
        return awrapper
    if not inspect.isgeneratorfunction(fn):
        raise TypeError(f"@inv.transaction needs a generator or async generator function, not {fn.__qualname__}")
    inner = contextlib.contextmanager(fn)

    @functools.wraps(fn)
    @contextlib.contextmanager
    def wrapper(*a: Any, **kw: Any) -> Iterator[Any]:
        frame = _Frame(key, a[0] if a else None, savepoint)
        token = _TXN.set(_TXN.get() + (frame,))
        try:
            with inner(*a, **kw) as v:
                yield v
                if frame.rollback_only:
                    raise rolled_back(frame)
        finally:
            frame.closed = True
            _TXN.reset(token)

    wrapper.__inv_txn_key__ = key  # type: ignore[attr-defined]
    wrapper.__inv_savepoint__ = savepoint  # type: ignore[attr-defined]
    return wrapper


def _boundary(key: str | None, owner: Any) -> _Frame | None:
    for f in reversed(_TXN.get()):
        if not f.closed and f.key == key and (owner is None or f.owner is owner):
            return f
    return None


def _needs_boundary(providers: dict[str, Any], rule: Rule, live: list[tuple[int, dict[str, Any]]]) -> str | None:
    """Why a transactional rule needs its boundary: a post_effect enforcer or a same_txn provider."""
    if any(e["timing"] == "post_effect" for _, e in live):
        return "a post_effect check must run inside it to roll back"
    same = sorted({p.split(".")[1] for i, _ in live for p in rule.names_for.get(i, {}).values()
                   if p.startswith("provider.") and p.split(".")[1] in providers
                   and providers[p.split(".")[1]].consistency == "same_txn"})
    return f"same_txn provider(s) {', '.join(same)} must be read inside it" if same else None


def _descriptor(fn: Any, deco: Callable[[Any], Any]) -> Any:
    """Wrap the function inside a classmethod or staticmethod and re-wrap it; None for a plain function."""
    if isinstance(fn, (classmethod, staticmethod)):
        return type(fn)(deco(fn.__func__))
    return None


# --- guard --------------------------------------------------------------------------------------
class _Call:
    """One guarded call: bound arguments, the rules that apply, provider caches and snapshots."""

    __slots__ = ("reg", "providers", "self_", "base", "rules", "snap", "pre", "post", "broken")

    def __init__(self, reg: Registry, self_: Any, args: dict[str, Any]) -> None:
        self.reg, self.self_ = reg, self_
        # one registry generation per call: inv.load mid-call (a hot reload) installs new tables on reg,
        # and this call's rules must keep binding the providers they were loaded with
        self.providers: dict[str, Any] = reg.providers
        self.base: dict[str, Any] = {"args": args, "self": self_}
        self.rules: list[tuple[Rule, _Frame | None, list[tuple[int, dict[str, Any]]]]] = []
        self.snap: dict[str, Any] = {}
        self.pre: dict[Any, Any] = {}       # provider values read before the effect
        self.post: dict[Any, Any] = {}      # provider values read after it (plus snapshot providers)
        self.broken: dict[tuple[str, Any], str] = {}   # (rule id, enforcer) whose provider prefetch failed

    def enforcers(self, timing: str) -> Iterator[tuple[Rule, _Frame | None, int, dict[str, Any]]]:
        for rule, frame, live in self.rules:
            for i, enf in live:
                if enf["timing"] == timing:
                    yield rule, frame, i, enf

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
                if reg.is_ablated(rule.id):
                    for _, enf in live:
                        ablated(reg, rule, enf.get("point"))
                    continue
                frame = _boundary(rule.transaction, self_) if rule.effects == "transactional" else None
                why = _needs_boundary(call.providers, rule, live) if rule.effects == "transactional" and frame is None else None
                if why:
                    out = Outcome("rule_error", rule.id, error=f"cannot confirm {rule.target_key} runs inside "
                                  f"{rule.transaction} ({why}); the effect was not started")
                    for _, enf in live:            # before the effect: enforce + fail_closed raises WiringError
                        act(reg, rule, enf, out, wiring=True)
                    continue
                call.rules.append((rule, frame, live))
                if rule.snapshot:
                    try:
                        call.snap[rule.id] = take_snapshot(rule, self_)
                    except CelError as e:
                        call.snap[rule.id] = ProviderFailed(str(e))
                    except Exception as e:
                        call.snap[rule.id] = ProviderFailed(f"old_self snapshot raised {type(e).__name__}")
            return call

        def snapshot_providers(call: _Call) -> None:
            for rule, frame, i, _ in call.enforcers("post_effect"):
                try:
                    fetch_providers(call.reg, rule, rule.names_for[i], call.env_base(rule, "pre_effect"),
                                    call.post, frame is not None, only=SNAPSHOT_ONLY, providers=call.providers)
                except Exception as e:              # a rule defect is a rule_error, never an exception
                    call.broken[(rule.id, i)] = f"binding snapshot providers raised {type(e).__name__}"

        async def aprefetch(call: _Call, timing: str, result: Any = None,
                            only: frozenset[str] | None = None) -> None:
            cache = call.pre if timing == "pre_effect" else call.post
            when = "pre_effect" if only is not None else timing     # snapshot providers: before the effect
            for rule, frame, i, _ in call.enforcers(timing):
                try:
                    why = await prefetch_async(call.reg, rule, rule.names_for[i],
                                               call.env_base(rule, when, result), cache, frame is not None, only,
                                               call.providers)
                except Exception as e:
                    why = f"binding providers raised {type(e).__name__}"
                if why is not None:
                    call.broken.setdefault((rule.id, i), why)

        def check(call: _Call, timing: str, result: Any = None) -> None:
            reg, cache = call.reg, call.pre if timing == "pre_effect" else call.post
            for rule, frame, i, enf in call.enforcers(timing):
                # a failed old_self snapshot errors only the clauses that read old_self
                broken = call.broken.get((rule.id, i))
                out = (Outcome("rule_error", rule.id, error=broken) if broken is not None else
                       evaluate(rule, call.env_base(rule, timing, result), i, reg, cache, frame is not None,
                                call.providers))
                try:
                    act(reg, rule, enf, out)
                except InvariantError:
                    if frame is not None:
                        _doom(frame, rule.id)          # roll back on violation, out to the outermost block
                    raise

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*a: Any, **kw: Any) -> Any:
                call = start(a, kw)
                await aprefetch(call, "pre_effect")
                check(call, "pre_effect")
                await aprefetch(call, "post_effect", only=SNAPSHOT_ONLY)
                result = await typing.cast(Callable[..., Awaitable[Any]], fn)(*a, **kw)
                await aprefetch(call, "post_effect", result)
                check(call, "post_effect", result)
                return result
            wrapper: Any = awrapper
        else:
            @functools.wraps(fn)
            def swrapper(*a: Any, **kw: Any) -> Any:
                call = start(a, kw)
                check(call, "pre_effect")
                snapshot_providers(call)
                result = fn(*a, **kw)
                check(call, "post_effect", result)
                return result
            wrapper = swrapper
        wrapper.__inv_rules__, wrapper.__inv_kind__ = keys, "guard"
        wrapper.__inv_points__ = tuple(points)
        WRAPPED.add(wrapper)
        return typing.cast(Callable[P, R], wrapper)
    return deco


# --- model calls and edges ----------------------------------------------------------------------
class Checked:
    """Base for refined types built by inv.model_call; carries value, attempts, rule and verdict.

    Python cannot make this unforgeable (object.__new__ exists). The type is convenience;
    an @inv.edge re-validates the value against the closed schema, re-checks bad on it and calls
    its consumer with a new Checked over the re-validated copy (equal to the received one).
    copy.copy returns the same (immutable) object; copy.deepcopy and pickle give an equal
    Checked over a copy of the value. Neither adds trust: an edge re-checks a copy the same way.
    """

    __slots__ = ("value", "attempts", "rule", "verdict")

    def __init__(self, *a: Any, **k: Any) -> None:
        raise TypeError(f"{type(self).__name__} is constructed only by inv.model_call")

    @classmethod
    def _inv_make(cls, value: Any, attempts: int, rule: str, verdict: str) -> Any:
        obj = object.__new__(cls)
        for k, v in (("value", value), ("attempts", attempts), ("rule", rule), ("verdict", verdict)):
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
    if t is Any or t is object:
        return _finite(v, where, depth, safe)
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


def _build(data: Any, schema: Any, prefix: str, depth: int, safe: str | None = None) -> Any:
    """data (a JSON object, or for a dataclass schema an instance of exactly that class) checked
    field by field and rebuilt: a new dict for a {field: type} map, a new instance for a dataclass."""
    safe = prefix if safe is None else safe
    if isinstance(schema, type) and dataclasses.is_dataclass(schema) and type(data) is schema:
        data = {f.name: object.__getattribute__(data, f.name) for f in dataclasses.fields(schema)}
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
        if type(data) is not dict and not (isinstance(schema, type) and type(data) is schema):
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
    """The state of one model_call invocation: parse -> abstain -> bad, with bounded retries."""

    def __init__(self, rule_id: str, schema: Any, refined: Any, sig: inspect.Signature,
                 a: tuple[Any, ...], kw: dict[str, Any]) -> None:
        self.reg = reg = current()
        self.rule = rule = reg.rules_for(rule_id)[0]
        pol = rule.policy or {}
        i, enf = rule.enforcer("model_call")
        if i is None or enf is None:
            raise CoverageError(f"@inv.model_call({rule_id!r}): the rule has no model_call enforcer")
        self.i, self.enf = i, enf
        m = reg.mode(rule, enf)
        self.mode = "detect" if m == "off" else m       # off acts as detect
        bound = sig.bind(*a, **kw)
        bound.apply_defaults()
        self.sig, self.bound, self.schema, self.refined = sig, bound, schema, refined
        self.base = {"args": {k: v for k, v in bound.arguments.items() if k != "inv_feedback"}}
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

    def finish(self, out: Outcome, value: Any) -> Any:
        enf = {**self.enf, "mode": self.mode} if self.mode != self.enf["mode"] else self.enf
        act(self.reg, self.rule, enf, out, self.record)
        return self.refined._inv_make(value, len(self.record), self.rule.id, out.kind)

    def parsed(self, raw: Any) -> tuple[str, Any]:
        """('retry', None) | ('done', Checked) | ('eval', reply). Nothing but an InvariantError escapes."""
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
            return "done", self.finish(out, raw)
        ab = self.ab
        if ab is not None:
            try:
                abstained = _same(_field_of(reply, ab["field"]), ab["value"])
            except Exception as e:      # by contract unreachable (parse gives plain data); fail closed
                self.record.append({"attempt": len(self.record) + 1, "outcome": "rule_error",
                                    "fed_back": self.fed_back})
                return "done", self.finish(Outcome("rule_error", rule.id,
                                                   error=f"abstain check raised {type(e).__name__}"), reply)
            if abstained:
                self.record.append({"attempt": len(self.record) + 1, "outcome": "abstain",
                                    "fed_back": self.fed_back})
                h = Handoff(rule.id, "escalate", None, {"reply": reply, **self.base}, list(self.record),
                            reason="abstain")
                review = reg.hand_off(h)
                raise Escalation(f"{rule.id}: model abstained ({ab['value']}); sent to a person (opened {review})",
                                 None, self.record, review, rule=rule.id, point="model_call")
        return "eval", reply

    def eval_base(self, reply: Any) -> dict[str, Any]:
        return {**self.base, "reply": rule_view(reply)}

    def judged(self, reply: Any) -> tuple[str, Any]:
        """('retry', None) | ('done', Checked)."""
        rule, reg = self.rule, self.reg
        if reg.is_ablated(rule.id):
            ablated(reg, rule, "model_call")
            out = Outcome("ok", rule.id)
        else:
            out = evaluate(rule, self.eval_base(reply), self.i, reg, self.prov)
        self.record.append({"attempt": len(self.record) + 1, "outcome": out.kind, "messages": out.messages,
                            "fed_back": self.fed_back})
        if out.kind == "violation" and self.sem_left and self.mode == "enforce":
            self.sem_left -= 1
            self.feedback = self.fed_back = None     # fresh resample: never the rule text or expected value
            report(reg, rule, self.enf, out, "resample", self.record)
            return "retry", None
        return "done", self.finish(out, reply)              # rule_error never resamples: on_error applies


INSTANCE_ATTR = "_inv_model"        # on a closed pydantic reply: the validated model instance


def rule_view(reply: Any) -> Any:
    """What a rule reads as `reply`: for a closed pydantic reply, the validated model instance (its
    Python values: datetime, timedelta, UUID, Enum members, nested models), mapped to CEL by
    cel/values.py exactly as arguments are; any other reply unchanged. A closed reply's own
    attributes hold the JSON form (see integrations.pydantic), which a rule never reads."""
    try:
        return object.__getattribute__(reply, INSTANCE_ATTR)
    except Exception:
        return reply


def _field_of(reply: Any, name: str) -> Any:
    """A field of a parsed reply: a key of a {field: type} reply, an attribute of a dataclass reply."""
    if type(reply) is dict:
        return reply.get(name)
    return object.__getattribute__(reply, name)


def check_schema(schema: Any, who: str) -> None:
    """TypeError unless schema is a closed schema whose every field type parse can check."""
    if not (isinstance(schema, dict) or (isinstance(schema, type) and dataclasses.is_dataclass(schema))):
        raise TypeError(f"{who}: schema must be a dataclass, a {{field: type}} map or a pydantic model "
                        f"(got {schema!r})")
    for k, t in schema_fields(schema).items():
        if not isinstance(k, str) or not supported_field_type(t):
            raise TypeError(f"{who}: field {k!r}: unsupported field type {_tname(t)} (use plain classes, Any, "
                            f"Literal, Optional, list[T], dict[str, T] or a nested dataclass)")


def model_call(rule_id: str, schema: Any) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """@inv.model_call("R-7", schema=CauseReply): parse -> abstain -> bad -> accept, with bounded retries.

    Once retries are spent, the model_call enforcer's on_violation decides. schema may be a
    dataclass, a {field: type} map or a pydantic model with extra='forbid'. On an async def,
    the wrapper is async: each reply is awaited, and async providers are awaited too.
    """
    schema = closed_schema(schema)
    check_schema(schema, f"@inv.model_call({rule_id!r})")

    def deco(fn: Callable[P, R]) -> Callable[P, R]:
        if (d := _descriptor(fn, deco)) is not None:
            return typing.cast(Callable[P, R], d)
        if inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn):
            raise TypeError(f"@inv.model_call cannot wrap the generator {fn.__qualname__}")
        sig = inspect.signature(fn)
        fb = sig.parameters.get("inv_feedback")
        if fb is not None and fb.kind not in (fb.POSITIONAL_OR_KEYWORD, fb.KEYWORD_ONLY):
            raise TypeError(f"@inv.model_call: {fn.__qualname__}'s inv_feedback must be a named parameter, "
                            f"not *args, **kwargs or positional-only")
        refined = sig.return_annotation
        if isinstance(refined, str):
            refined = typing.get_type_hints(fn).get("return")
        if not (isinstance(refined, type) and issubclass(refined, Checked)):
            raise TypeError(f"{fn.__name__} must be annotated to return an inv.Checked subclass")

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*a: Any, **kw: Any) -> Any:
                run = _ModelRun(rule_id, schema, refined, sig, a, kw)
                while True:
                    ca, ckw = run.call_args()
                    step, v = run.parsed(await typing.cast(Callable[..., Any], fn)(*ca, **ckw))
                    if step == "eval":
                        try:        # never raises by contract; evaluate() reports a binding failure
                            await prefetch_async(run.reg, run.rule, run.rule.names_for[run.i], run.eval_base(v),
                                                 run.prov)
                        except Exception:
                            pass
                        step, v = run.judged(v)
                    if step == "done":
                        return v
            wrapper: Any = awrapper
        else:
            @functools.wraps(fn)
            def swrapper(*a: Any, **kw: Any) -> Any:
                run = _ModelRun(rule_id, schema, refined, sig, a, kw)
                while True:
                    ca, ckw = run.call_args()
                    step, v = run.parsed(typing.cast(Callable[..., Any], fn)(*ca, **ckw))
                    if step == "eval":
                        step, v = run.judged(v)
                    if step == "done":
                        return v
            wrapper = swrapper

        wrapper.__inv_rules__, wrapper.__inv_kind__ = (rule_id,), "model_call"
        wrapper.__inv_schema__ = schema
        WRAPPED.add(wrapper)
        return typing.cast(Callable[P, R], wrapper)
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

    A dataclass schema takes an instance of exactly that class, a {field: type} map a plain dict:
    extra and missing fields, out-of-enum values and wrong types fail as they do in inv.parse.
    """
    if isinstance(schema, type) and dataclasses.is_dataclass(schema):
        if type(reply) is not schema:
            got = type(reply)
            raise ParseError(f"value was not built by inv.parse from the closed schema "
                             f"{schema.__name__} (got {got.__module__}.{got.__qualname__})")
    elif type(reply) is not dict:
        raise ParseError(f"value was not built by inv.parse from the closed {{field: type}} schema "
                         f"(got {type(reply).__module__}.{type(reply).__qualname__})")
    return parse(reply, schema)


def edge(rule_id: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """@inv.edge("R-7") on the consumer of a Checked value (sync or async).

    The type gate stops honest mistakes; then the verdict must be ok, the value is re-validated
    against the closed schema of the rule's model call (extra or missing fields, out-of-enum
    values, wrong types, a value of another class: parse_error), and bad is re-evaluated on it.
    The consumer is then called with a new Checked over that re-validated copy, the exact value
    the rule judged: no other code holds it, so nothing can change it between the check and the
    consumer's reads (a received object mutated or swapped later does not reach the consumer).
    """
    def deco(fn: Callable[P, R]) -> Callable[P, R]:
        if (d := _descriptor(fn, deco)) is not None:
            return typing.cast(Callable[P, R], d)
        if inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn):
            raise TypeError(f"@inv.edge cannot wrap the generator {fn.__qualname__}")
        sig = inspect.signature(fn)
        hints = typing.get_type_hints(fn)
        pname, refined = next(((n, t) for n, t in hints.items() if isinstance(t, type) and issubclass(t, Checked)),
                              (None, None))
        if pname is None or refined is None:
            raise TypeError(f"{fn.__name__}: an edge needs a parameter annotated with an inv.Checked subclass")

        def prepare(a: tuple[Any, ...], kw: dict[str, Any]) -> tuple[Any, ...] | None:
            """None to call straight through; else what judge() needs, then the consumer's (args, kwargs)."""
            reg = current()
            bound = sig.bind(*a, **kw)
            bound.apply_defaults()
            value = bound.arguments[pname]
            if not isinstance(value, refined):
                raise TypeError(f"{fn.__name__} accepts only {refined.__name__}")
            rule = reg.rules_for(rule_id)[0]
            i, enf = rule.enforcer("edge")
            if i is None or enf is None:
                raise CoverageError(f"@inv.edge({rule_id!r}): the rule has no edge enforcer")
            if reg.mode(rule, enf) == "off":
                return None
            if reg.is_ablated(rule.id):
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
                    bound.arguments[pname] = type(value)._inv_make(reply, attempts, rid, verdict)
                    call = (bound.args, bound.kwargs)
            base = {"args": dict(bound.arguments), "reply": rule_view(reply)}
            return reg, rule, i, enf, verdict, perr, base, {}, call

        def judge(reg: Registry, rule: Rule, i: int, enf: dict[str, Any], verdict: str | None, perr: str | None,
                  base: dict[str, Any], cache: dict[Any, Any]) -> None:
            refusal = verdict != "ok"            # a received non-ok verdict is refused, never failed open
            if verdict is None:
                out = Outcome("rule_error", rule.id, [f"received a {refined.__name__} that inv.model_call did not "
                                                      f"build (it has no verdict)"])
            elif verdict != "ok":
                out = Outcome(verdict if verdict in ("violation", "rule_error", "parse_error") else "rule_error",
                              rule.id, [f"received a {refined.__name__} with verdict {verdict}"])
            elif perr is not None:
                out = Outcome("parse_error", rule.id, error=perr)
            else:
                out = evaluate(rule, base, i, reg, cache)
            act(reg, rule, enf, out, refusal=refusal)

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*a: Any, **kw: Any) -> Any:
                p = prepare(a, kw)
                if p is not None:
                    reg, rule, i, _, verdict, perr, base, cache, (a, kw) = p
                    if verdict == "ok" and perr is None:
                        try:    # never raises by contract; evaluate() reports a binding failure
                            await prefetch_async(reg, rule, rule.names_for[i], base, cache)
                        except Exception:
                            pass
                    judge(*p[:-1])
                return await typing.cast(Callable[..., Awaitable[Any]], fn)(*a, **kw)
            wrapper: Any = awrapper
        else:
            @functools.wraps(fn)
            def swrapper(*a: Any, **kw: Any) -> Any:
                p = prepare(a, kw)
                if p is not None:
                    judge(*p[:-1])
                    a, kw = p[-1]
                return fn(*a, **kw)
            wrapper = swrapper

        wrapper.__inv_rules__, wrapper.__inv_kind__ = (rule_id,), "edge"
        WRAPPED.add(wrapper)
        return typing.cast(Callable[P, R], wrapper)
    return deco
