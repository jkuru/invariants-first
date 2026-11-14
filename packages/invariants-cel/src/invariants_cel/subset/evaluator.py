"""The tree-walking evaluator: one cost unit per node; CEL's error absorption in && || all exists.

evaluate() reads lit, id and call nodes itself and hands every other node kind to its entry in
_NODES. A handler evaluates its children with evaluate() directly, so one tree level costs two
Python frames (three for a call), as it did when evaluate() was one function. The work at a node
(an operator, a field read) runs one frame deeper than it did then, so a tree about 500 levels
deep can reach the recursion limit one level sooner.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from ..errors import CelError
from .catalog import _MACROS
from .cost import DEFAULT_BUDGET, CostExceeded
from .data import (Uint, _bool, _data, _int_range, _nty, _num, _on_map, _safe_repr, _select, _ty, as_container,
                   has_enum_key, map_key)
from .equality import _eq, lookup, whole
from .functions import _BUILTINS, ENGINE_ONLY
from .operators import _ORDER, _arith, _in, _order
from .parser import Node


def evaluate(n: Node, env: dict[str, Any], budget: list[int] | None = None) -> Any:
    """Evaluate a tree against env. Raises CelError on any evaluation error.

    budget: [units left], or [units left, SHORT] to short-circuit && and || at a deciding left side
    and all/exists at a deciding element, as cel-cpp does (the same result; the work, and so the cost,
    follows cel-cpp's: the exact evaluation of a cel-expr clause, expr_python)."""
    budget = budget if budget is not None else [DEFAULT_BUDGET]
    budget[0] -= 1
    if budget[0] < 0:
        raise CostExceeded("cost limit exceeded")
    k = n[0]
    if k == "lit":
        return n[1]
    if k == "id":
        if n[1] not in env:
            raise CelError(f"undeclared reference {n[1]!r}")
        return _data(env[n[1]])
    if k == "call":
        return _call(n[1], n[2], n[3], env, budget)
    try:
        handler = _NODES.get(k)
    except TypeError:                   # an unhashable node kind in a hand-built tree
        handler = None
    if handler is None:
        raise CelError(f"unknown node {k}")
    return handler(n, env, budget)


SHORT = 1           # the second budget item that turns short-circuiting on (see evaluate)


def evaluate_short(prog: Any, env: dict[str, Any]) -> Any:
    """prog (a SubsetProgram) evaluated with short-circuiting (see evaluate) against env."""
    try:
        return evaluate(prog.ast, env, [prog.budget, SHORT])
    except RecursionError:
        raise CelError("value or expression nested too deeply") from None


def _list(n: Node, env: dict[str, Any], budget: list[int]) -> list[Any]:
    return [evaluate(x, env, budget) for x in n[1]]


def _map(n: Node, env: dict[str, Any], budget: list[int]) -> dict[Any, Any]:
    out: dict[Any, Any] = {}
    for a, b in n[1]:
        key = map_key(evaluate(a, env, budget))
        if key in out:              # a repeated key, or a bool next to the number 1 or 0
            raise CelError("repeated map key" if lookup(out, key)[0] else
                           "a map literal cannot hold both a bool key and the number key it equals in Python")
        out[key] = evaluate(b, env, budget)
    return out


def _sel(n: Node, env: dict[str, Any], budget: list[int]) -> Any:
    return _select(evaluate(n[1], env, budget), n[2])


def _idx(n: Node, env: dict[str, Any], budget: list[int]) -> Any:
    o, i = as_container(evaluate(n[1], env, budget)), evaluate(n[2], env, budget)
    if isinstance(o, Mapping):
        found, v = _on_map(o, "reading a key", lambda: lookup(o, i))
        if not found:
            err = CelError("no such key")          # the key is request data: only in .detail
            err.detail = _safe_repr(i)
            raise err
        return _data(v)
    if type(i) is float and i.is_integer() and isinstance(o, (list, tuple)):
        i = int(i)                  # CEL: a list index may be an integral double ([7, 8][1.0] == 8)
    if not isinstance(o, (list, tuple)) or not isinstance(i, int) or isinstance(i, bool):
        raise CelError(f"cannot index {_ty(o)} with {_nty(i) if _num(i) else _ty(i)}")
    if not 0 <= i < len(o):
        err = CelError("index out of range")
        err.detail = f"index {i}, size {len(o)}"
        raise err
    return _data(o[i])


def _cond(n: Node, env: dict[str, Any], budget: list[int]) -> Any:
    return evaluate(n[2], env, budget) if _bool(evaluate(n[1], env, budget), "?:") else evaluate(n[3], env, budget)


def _un(n: Node, env: dict[str, Any], budget: list[int]) -> Any:
    v = evaluate(n[2], env, budget)
    if n[1] == "!":
        return not _bool(v, "!")
    if not _num(v) or type(v) is Uint:
        raise CelError(f"no such overload: -{'uint' if type(v) is Uint else _ty(v)}")
    return _int_range(-v)


def _bin(n: Node, env: dict[str, Any], budget: list[int]) -> Any:
    op = n[1]
    if op in ("&&", "||"):                     # CEL: error absorbed if the other side decides
        decider = op == "||"
        left: Any
        try:
            left = _bool(evaluate(n[2], env, budget), op)
        except CostExceeded:
            raise
        except CelError as e:
            left = e
        if left is decider and len(budget) > 1:
            return decider                      # short-circuit (SHORT): the right side is never evaluated
        right: Any
        try:
            right = _bool(evaluate(n[3], env, budget), op)
        except CostExceeded:
            raise
        except CelError as e:
            right = e
        if left is decider or right is decider:
            return decider
        if isinstance(left, CelError):
            raise left
        if isinstance(right, CelError):
            raise right
        return not decider
    a, b = evaluate(n[2], env, budget), evaluate(n[3], env, budget)
    if op == "==":
        return _eq(a, b)
    if op == "!=":
        return not _eq(a, b)
    if op == "in":
        return _in(a, b)
    if op in _ORDER:
        return _order(op, a, b)
    return _arith(op, a, b)


_NODES: dict[str, Callable[[Node, dict[str, Any], list[int]], Any]] = {
    "list": _list, "map": _map, "sel": _sel, "idx": _idx, "cond": _cond, "un": _un, "bin": _bin,
}


def _call(name: str, tgt: Node | None, args: list[Node], env: dict[str, Any], budget: list[int]) -> Any:
    """A call node: a macro, has(), or a built-in function on its evaluated receiver and arguments."""
    if name in _MACROS or name == "inv.unique_by":
        return _macro(name, tgt, args, env, budget)
    if name == "has":
        return _has(args[0], env, budget)
    vals = _arguments(tgt, args, env, budget)
    fn = _BUILTINS.get(name)
    if fn is None or (name in ENGINE_ONLY and len(budget) == 1):
        raise CelError(f"unknown function {name}()")
    return fn(name, tgt is not None, vals, budget)


def _has(sel: Node, env: dict[str, Any], budget: list[int]) -> bool:
    """has(e.f): whether e has the field f (a frame of its own, as a call level always had)."""
    return _select(evaluate(sel[1], env, budget), sel[2], test=True)


def _arguments(tgt: Node | None, args: list[Node], env: dict[str, Any], budget: list[int]) -> list[Any]:
    """The receiver, if any, then the arguments, evaluated in order (a frame of its own, as a call
    level always had: see the module docstring)."""
    return ([evaluate(tgt, env, budget)] if tgt is not None else []) + [evaluate(a, env, budget) for a in args]


def _iter(v: Any, name: str) -> list[Any]:
    v = as_container(v)
    if isinstance(v, Mapping):
        return _on_map(v, f"{name}() over the keys", lambda: list(whole(v)) if has_enum_key(v) else list(v))
    if isinstance(v, (list, tuple)):
        return list(v)
    raise CelError(f"{name}() on {_ty(v)}")


def _macro(name: str, tgt: Node | None, args: list[Node], env: dict[str, Any], budget: list[int]) -> Any:
    """A comprehension (all, exists, exists_one, map, filter) or inv.unique_by over a list or map's keys."""
    if name == "inv.unique_by":
        tgt, args = args[0], args[1:]
    if tgt is None:                   # the parser refuses this form; never evaluate it
        raise CelError(f"{name}() needs a receiver list")
    items, var, body = _iter(evaluate(tgt, env, budget), name), args[0][1], args[1]
    if name in ("map", "inv.unique_by"):
        vals = [evaluate(body, {**env, var: x}, budget) for x in items]
        return vals if name == "map" else _unique(vals)
    if name == "filter":
        return [x for x in items if _bool(evaluate(body, {**env, var: x}, budget), name)]
    return _quantify(name, items, var, body, env, budget)


def _unique(vals: list[Any]) -> bool:
    """inv.unique_by: no two keys are CEL-equal."""
    keys: list[Any] = []
    for v in vals:
        if any(_eq(v, key) for key in keys):
            return False
        keys.append(v)
    return True


_STOPS = {"exists": True, "all": False}       # where cel-cpp's loop condition ends all and exists


def _quantify(name: str, items: list[Any], var: str, body: Node, env: dict[str, Any], budget: list[int]) -> bool:
    """all, exists or exists_one. CEL folds exists with ||, all with &&: an element's error is
    absorbed when another element decides; a spent budget never is."""
    hits: list[Any] = []
    stop = _STOPS.get(name) if len(budget) > 1 else None
    for x in items:
        try:
            hits.append(_bool(evaluate(body, {**env, var: x}, budget), name))
        except CostExceeded:
            raise
        except CelError as e:
            hits.append(e)
        if stop is not None and hits[-1] is stop:
            break                           # short-circuit (SHORT), as cel-cpp's loop condition
    errs = [h for h in hits if isinstance(h, CelError)]
    if name == "exists" and True in hits:       # CEL folds exists with ||, all with &&
        return True
    if name == "all" and False in hits:
        return False
    if errs:
        raise errs[0]
    return {"all": True, "exists": False, "exists_one": hits.count(True) == 1}[name]
