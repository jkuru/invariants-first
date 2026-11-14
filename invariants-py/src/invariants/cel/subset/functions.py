"""The built-in functions on evaluated arguments: size, the string tests, matches, inv.sum, inv.zip,
the conversions and the timestamp getters. The macros and has() are in evaluator.py: they evaluate
their arguments themselves."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from .. import values
from ..errors import CelError, check_result
from .catalog import _GETTERS, CONVERSIONS
from .convert import _convert
from .cost import _matches, charge
from .data import _int_range, _num, _on_map, as_container, has_enum_key
from .equality import whole

# name -> fn(name, has_receiver, vals, budget); vals holds the receiver first, if there is one
_Builtin = Callable[[str, bool, list[Any], list[int]], Any]


def _size(name: str, recv: bool, vals: list[Any], budget: list[int]) -> Any:
    sized = as_container(vals[0]) if len(vals) == 1 else None
    if not isinstance(sized, (str, list, tuple, Mapping)):
        raise CelError("size() needs one string, list or map")
    if isinstance(sized, Mapping):
        return _on_map(sized, "size()", lambda: len(whole(sized)) if has_enum_key(sized) else len(sized))
    return len(sized)


def _string_test(name: str, recv: bool, vals: list[Any], budget: list[int]) -> bool:
    if len(vals) != 2 or not all(isinstance(v, str) for v in vals):
        raise CelError(f"{name}() needs strings")
    s, t = values.sized(name, vals[0]), values.sized(name, vals[1])
    charge(budget, len(s) + len(t) if name == "contains" else min(len(s), len(t)))
    if name == "contains":
        return t in s
    return s.startswith(t) if name == "startsWith" else s.endswith(t)


def _inv_sum(name: str, recv: bool, vals: list[Any], budget: list[int]) -> Any:
    if len(vals) != 1 or not isinstance(vals[0], (list, tuple)) or not all(_num(v) for v in vals[0]):
        raise CelError("inv.sum() needs a list of numbers")
    return check_result(_int_range(sum(vals[0])))


def _match(name: str, recv: bool, vals: list[Any], budget: list[int]) -> bool:
    if len(vals) != 2 or not all(isinstance(v, str) for v in vals):
        raise CelError("matches() needs a string and a pattern")
    return _matches(values.sized(name, vals[0]), values.sized(name, vals[1]), budget)


def _inv_zip(name: str, recv: bool, vals: list[Any], budget: list[int]) -> list[list[Any]]:
    if len(vals) != 2 or not all(isinstance(v, (list, tuple)) for v in vals):
        raise CelError("inv.zip() needs two lists")
    if len(vals[0]) != len(vals[1]):
        err = CelError("inv.zip(): length mismatch")      # the lengths are request data: only in .detail
        err.detail = f"{len(vals[0])} vs {len(vals[1])}"
        raise err
    return [[a, b] for a, b in zip(*vals)]


def _conversion(name: str, recv: bool, vals: list[Any], budget: list[int]) -> Any:
    if recv or len(vals) != 1:
        raise CelError(f"{name}() takes one argument")
    return _convert(name, vals[0])


def _getter(name: str, recv: bool, vals: list[Any], budget: list[int]) -> Any:
    if not recv or len(vals) not in (1, 2):
        raise CelError(f"{name}() takes a receiver and an optional timezone")
    return values.getter(name, *vals)


_BUILTINS: dict[str, _Builtin] = {
    "size": _size, "contains": _string_test, "startsWith": _string_test, "endsWith": _string_test,
    "inv.sum": _inv_sum, "matches": _match, "inv.zip": _inv_zip,
    **{n: _conversion for n in CONVERSIONS}, **{n: _getter for n in _GETTERS},
    # uint(), bytes() and type(): only in the exact evaluation of a cel-expr clause (expr_python;
    # evaluator.evaluate_short); on the subset backend they are unknown (the loader refuses them)
    "uint": _conversion, "bytes": _conversion, "type": _conversion,
}
ENGINE_ONLY = frozenset({"uint", "bytes", "type"})

