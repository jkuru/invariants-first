"""The boundary API: @inv.guard, @inv.model_call and @inv.edge.

Each works on sync and async functions. Provider timing: a pre_effect enforcer reads its
providers before the call; a post_effect enforcer reads them after it. Async providers are
awaited by async guards, model calls and edges. The steps of a call live in plan.py; the
wrappers here only run them, awaiting between them on an async def.
"""
from __future__ import annotations

import dataclasses
import functools
import inspect
import pathlib
import sys
import time
import types
import typing
from typing import Any, Awaitable, Callable, Coroutine, ParamSpec, Protocol, TypeVar, overload

from ..errors import CoverageError
from ..evaluate import aact, act
from ..loader import closed_schema, type_hints
from ..model import GUARD_POINTS, Rule
from ..registry import WRAPPED, _wraps
from ..resolve import resolve
from .checked import Checked
from .plan import _EdgePlan, _ModelRun, acheck, aprefetch, check, start
from .replies import check_schema

P = ParamSpec("P")
R = TypeVar("R")


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


def _is_validate_call(layer: Any) -> bool:
    """True for a pydantic.validate_call wrapper (pydantic 2: a function from pydantic's
    _validate_call module, or a ValidateCallWrapper instance, either with .raw_function)."""
    if not callable(getattr(layer, "raw_function", None)):
        return False
    if type(layer).__module__.split(".")[0] == "pydantic":
        return True
    code = getattr(layer, "__code__", None)
    return "pydantic" in pathlib.PurePath(getattr(code, "co_filename", "") or "").parts


def _refuse_validate_call_below(fn: Any, who: str) -> None:
    """TypeError if fn is (or wraps, through __wrapped__) a pydantic.validate_call wrapper: the guard
    would check the raw arguments ('yes', '1000000'), and the function would run on the coerced
    ones (True, 1000000)."""
    layer, seen = fn, 0
    while layer is not None and seen < 64:
        if _is_validate_call(layer):
            raise TypeError(f"{who} is above @pydantic.validate_call on {getattr(fn, '__qualname__', fn)!r}: it "
                            f"would check the arguments before pydantic coerces them ('yes' for True, "
                            f"'1000000' for 1000000) while the function runs on the coerced ones; put "
                            f"@inv.guard below @validate_call, so it checks what the function receives")
        layer, seen = getattr(layer, "__wrapped__", None), seen + 1


# --- guard --------------------------------------------------------------------------------------
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
        _refuse_validate_call_below(fn, "@inv.guard")
        sig = inspect.signature(fn)
        shapes: set[tuple[Any, ...]] = set()       # call shapes that bound (see plan.start)

        # no live rule: straight through; a timing no live enforcer checks at is skipped (nothing to emit)
        if inspect.iscoroutinefunction(fn):
            afn = typing.cast(Callable[..., Awaitable[Any]], fn)

            @functools.wraps(fn)
            async def awrapper(*a: Any, **kw: Any) -> Any:
                call = start(sig, keys, points, a, kw, shapes)
                if call is None:
                    return await afn(*a, **kw)
                if "pre_effect" in call.timings:
                    await aprefetch(call, "pre_effect")
                    await acheck(call, "pre_effect")
                result = await afn(*a, **kw)
                if "post_effect" in call.timings:
                    call.started["post_effect"] = time.time_ns()
                    await aprefetch(call, "post_effect", result)
                    await acheck(call, "post_effect", result)
                return result
            wrapper: Any = awrapper
        else:
            @functools.wraps(fn)
            def swrapper(*a: Any, **kw: Any) -> Any:
                call = start(sig, keys, points, a, kw, shapes)
                if call is None:
                    return fn(*a, **kw)
                if "pre_effect" in call.timings:
                    check(call, "pre_effect")
                result = fn(*a, **kw)
                if "post_effect" in call.timings:
                    if type(result) not in _PLAIN_RESULTS and inspect.isawaitable(result):
                        return _awaited_post(call, result, fn)
                    call.started["post_effect"] = time.time_ns()
                    check(call, "post_effect", result)
                return result
            wrapper = swrapper
        wrapper.__inv_rules__, wrapper.__inv_kind__ = keys, "guard"
        wrapper.__inv_points__ = tuple(points)
        WRAPPED.add(wrapper)
        return typing.cast(Callable[P, R], wrapper)
    return deco


_PLAIN_RESULTS = frozenset({type(None), bool, int, float, str, bytes, list, tuple, dict})   # never awaitable


def _awaited_post(call: Any, result: Any, fn: Any) -> Any:
    """A sync-path guarded call whose function returned an awaitable (a sync wrapper over an async def,
    say: functools.wraps without inspect.markcoroutinefunction). Its post_effect checks must see what
    the awaitable gives, not the awaitable: a coroutine is returned as a coroutine that awaits it and
    then runs them. Any other awaitable (a Future, a Task: the caller may need the object itself) is
    a TypeError, never checked as the result."""
    if not inspect.iscoroutine(result):
        raise TypeError(f"@inv.guard on {getattr(fn, '__qualname__', fn)!r}: the function returned a "
                        f"{type(result).__name__}, an awaitable that is not a coroutine, so its post_effect "
                        f"checks cannot see the real result; guard the async def itself (put @inv.guard "
                        f"below the wrapper) or return a coroutine")

    async def post() -> Any:
        got = await result
        call.started["post_effect"] = time.time_ns()
        await aprefetch(call, "post_effect", got)
        await acheck(call, "post_effect", got)
        return got
    return post()


# --- model calls --------------------------------------------------------------------------------
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
    subclass (the body then needs a cast or a `type: ignore` under mypy).

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
        refined = _refined(fn, sig, returns, rule_id)

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*a: Any, **kw: Any) -> Any:
                run = _ModelRun(rule_id, schema, refined, sig, a, kw)
                if run.passive:
                    ca, ckw = run.call_args()
                    return run.passive_result(await fn(*ca, **ckw))
                steps = run.steps(prefetch=True)     # the retry loop (plan.py); this only calls and awaits
                want, v = next(steps)
                while want != "finish":
                    if want == "call":
                        got = await fn(*v[0], **v[1])
                    elif want == "prefetch":
                        got = await run.aprefetch(v)
                    else:
                        got = await run.reg.ahand_off(v)
                    want, v = steps.send(got)
                return await run.afinish(*v)
            wrapper: Any = awrapper
        else:
            @functools.wraps(fn)
            def swrapper(*a: Any, **kw: Any) -> Any:
                run = _ModelRun(rule_id, schema, refined, sig, a, kw)
                if run.passive:
                    ca, ckw = run.call_args()
                    return run.passive_result(fn(*ca, **ckw))
                steps = run.steps(prefetch=False)    # the retry loop (plan.py); this only calls
                want, v = next(steps)
                while want != "finish":
                    got = fn(*v[0], **v[1]) if want == "call" else run.reg.hand_off(v)
                    want, v = steps.send(got)
                return run.finish(*v)
            wrapper = swrapper

        wrapper.__inv_rules__, wrapper.__inv_kind__ = (rule_id,), "model_call"
        wrapper.__inv_schema__ = schema
        WRAPPED.add(wrapper)
        return wrapper
    return deco


def _refined(fn: Any, sig: inspect.Signature, returns: Any, rule_id: str) -> Any:
    """The Checked subclass a model call returns: returns=, else the body's return annotation."""
    refined: Any = returns
    if refined is None:
        refined = sig.return_annotation
        if isinstance(refined, str):
            refined = _hints(fn, f"@inv.model_call({rule_id!r}) on {fn.__qualname__}").get("return")
        if not (isinstance(refined, type) and issubclass(refined, Checked)):
            raise TypeError(f"{fn.__name__} must be annotated to return an inv.Checked subclass, or pass "
                            f"returns=<the Checked subclass> to @inv.model_call")
    return refined


# --- edges --------------------------------------------------------------------------------------
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

    at=None: the one parameter annotated with a Checked subclass (X or X | None), else the one field
    annotated with one in the state types (TypedDict, dataclass, pydantic model) the parameters are
    annotated with; none, or more than one, is a TypeError. at="plan": that parameter; at="state.plan":
    that parameter and field path, whose annotations must lead to a Checked subclass."""
    if at is not None:
        parts = tuple(at.split(".")) if isinstance(at, str) else ()
        if not parts or not all(parts) or parts[0] not in sig.parameters:
            raise TypeError(f"{who}: at={at!r} must be '<parameter>.<field>[.<field>...]' or '<parameter>' "
                            f"naming one of its parameters ({', '.join(sig.parameters)})")
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
    # X or X | None, as a state field: a None received there is the TypeError of any non-X value
    direct = [(n, c) for n in sig.parameters if (c := _checked_type(hints.get(n))) is not None]
    if len(direct) == 1:
        return (direct[0][0],), direct[0][1]
    if direct:
        raise TypeError(f"{who}: more than one parameter annotated with an inv.Checked subclass "
                        f"({', '.join(n for n, _ in direct)}); name the one this edge receives with at='<parameter>'")
    found = [((n, f), c) for n in sig.parameters for f, ft in _state_fields(hints.get(n), who).items()
             if (c := _checked_type(ft)) is not None]
    if len(found) == 1:
        return found[0]
    if found:
        raise TypeError(f"{who}: more than one Checked field in its state ({', '.join('.'.join(p) for p, _ in found)}); "
                        f"name the one this edge receives with at='<parameter>.<field>'")
    raise TypeError(f"{who}: an edge needs a parameter annotated with an inv.Checked subclass, or a state "
                    f"parameter (TypedDict, dataclass or pydantic model) with one Checked field (or at=...)")


def _path_states(hints: dict[str, Any], path: tuple[str, ...], who: str) -> tuple[Any, ...]:
    """The annotated type of each state container on an edge path (the parameter's, then each field's
    but the last), as _edge_path walked them."""
    out: list[Any] = []
    t = hints.get(path[0])
    for f in path[1:]:
        out.append(t)
        t = _state_fields(t, who).get(f)
    return tuple(out)


def edge(rule_id: str, *, at: str | None = None) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """@inv.edge("R-7") on the consumer of a Checked value (sync or async).

    The value arrives in a parameter annotated with the Checked subclass (X or X | None), or in a
    field of a state parameter (a TypedDict, dataclass or pydantic model, as agent graphs pass): the
    one field annotated with a Checked subclass (X or X | None), or the one at="state.plan" names
    (at="plan" names a parameter). Several candidates without at= is a TypeError when the decorator runs. For a state, the consumer gets
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

        steps = _EdgePlan(rule_id, sig, path, refined, fn, enforcer_for, _path_states(hints, path, who))
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*a: Any, **kw: Any) -> Any:
                c = steps.prepare(a, kw)
                if c is not None:
                    await steps.aprefetch(c)
                    out, refusal = steps.judge(c)
                    await aact(c.reg, c.rule, c.enf, out, refusal=refusal, started=c.started)
                    a, kw = c.args, c.kwargs
                return await typing.cast(Callable[..., Awaitable[Any]], fn)(*a, **kw)
            wrapper: Any = awrapper
        else:
            @functools.wraps(fn)
            def swrapper(*a: Any, **kw: Any) -> Any:
                c = steps.prepare(a, kw)
                if c is not None:
                    out, refusal = steps.judge(c)
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
