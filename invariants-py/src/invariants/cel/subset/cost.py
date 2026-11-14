"""The cost budget: one unit per evaluated node, plus the work of matches() (its regex steps) and of
contains(), startsWith() and endsWith() (the characters they scan).

A budget is a one-item list [units left], shared by every node of one evaluation.
"""
from __future__ import annotations

from .. import _regex
from ..errors import CelError
from .data import _safe_repr

DEFAULT_BUDGET = 10_000


class CostExceeded(CelError):
    """The cost budget is spent. Never absorbed: && || all exists exists_one re-raise it at once, so a
    clause that runs out of budget is a rule_error whatever its other branches say."""


CHARS_PER_COST = 4096       # characters contains(), startsWith() and endsWith() scan per unit of the budget


def charge(budget: list[int] | None, chars: int) -> None:
    """Charge a string function's scan of chars characters to the budget (one unit per
    CHARS_PER_COST, on top of its node's own unit); CostExceeded once it is spent."""
    if budget is not None and chars >= CHARS_PER_COST:
        budget[0] -= chars // CHARS_PER_COST
        if budget[0] < 0:
            raise CostExceeded("cost limit exceeded")


_RE_CACHE: dict[str, list[list]] = {}
_RE_CACHE_MAX = 256
STEPS_PER_COST = 16          # regex VM steps (or pattern characters and instructions compiled) per unit


def _spend(budget: list[int] | None, steps: int) -> None:
    if budget is not None and steps >= STEPS_PER_COST:
        budget[0] -= steps // STEPS_PER_COST
        if budget[0] < 0:
            raise CostExceeded("cost limit exceeded (matches)")


def _matches(text: str, pattern: str, budget: list[int] | None = None) -> bool:
    """CEL matches(): an unanchored search with RE2 syntax, in linear time (see _regex).

    Its steps are charged to the cost budget, so no pattern or input can run past it. That is
    stricter than cel-expr: an input past about STEPS_PER_COST * DEFAULT_BUDGET VM
    steps (some tens of thousands of characters for a small pattern) is a rule_error here and a
    result there. The bound is deliberate: free steps per input character would let a caller
    buy CPU time with input size, once per matches() in a loop.
    """
    code = _RE_CACHE.get(pattern)
    if code is None:
        try:
            code = _regex.compile_pattern(pattern)
        except _regex.PatternLimit as e:
            err = CelError(_regex.PATTERN_LIMIT)
            err.detail = str(e)
            raise err from None
        except _regex.RegexError as e:
            err = CelError("matches(): bad pattern")   # the pattern may be request data: only in .detail
            err.detail = f"{e} in {_safe_repr(pattern)}"
            raise err from None
        if len(_RE_CACHE) >= _RE_CACHE_MAX:     # bounded: the oldest entry goes
            _RE_CACHE.pop(next(iter(_RE_CACHE)))
        _RE_CACHE[pattern] = code
    _spend(budget, len(pattern) + len(code))    # its compile (bounded by the pattern limit), charged on
    #                                             every call, cached or not: the verdict never depends on the cache
    left = budget[0] if budget is not None else DEFAULT_BUDGET
    try:
        found, steps = _regex.search(code, text, max(0, left) * STEPS_PER_COST)
    except _regex.StepLimit:
        if budget is not None:
            budget[0] = -1
        raise CostExceeded("cost limit exceeded (matches)") from None
    if budget is not None:
        budget[0] -= steps // STEPS_PER_COST
    return found
