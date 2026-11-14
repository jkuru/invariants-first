"""The function catalogue: every function name the subset knows and the call shapes CEL allows.

The loader reads it at load (FUNCTIONS, ARITY, arity_error, CONVERSIONS) on every backend; the
parser reads the macro names and the inv.* arities.
"""
from __future__ import annotations

INV_ARITY = {"inv.sum": 1, "inv.zip": 2, "inv.unique_by": 3}
_MACROS = {"all", "exists", "exists_one", "map", "filter"}
CONVERSIONS = frozenset({"int", "double", "string", "bool", "dyn", "timestamp", "duration"})
_GETTERS = ("getFullYear", "getMonth", "getDayOfYear", "getDayOfMonth", "getDate", "getDayOfWeek",
            "getHours", "getMinutes", "getSeconds", "getMilliseconds")
# every function name the subset evaluates (global or receiver form); the loader refuses others
FUNCTIONS = frozenset({"size", "contains", "startsWith", "endsWith", "matches", "has", *_MACROS, *CONVERSIONS,
                       *_GETTERS, *INV_ARITY})
# name -> (argument counts in global form f(...), argument counts in receiver form x.f(...)), per
# the CEL spec. An empty set: CEL has no such form. The loader refuses any other call shape of a
# known function at load, on every backend (it would be a rule_error on every evaluation).
ARITY: dict[str, tuple[frozenset[int], frozenset[int]]] = {
    "size": (frozenset({1}), frozenset({0})),
    **{n: (frozenset({2}), frozenset({1})) for n in ("contains", "startsWith", "endsWith", "matches")},
    "has": (frozenset({1}), frozenset()),
    **{n: (frozenset(), frozenset({2})) for n in _MACROS},
    **{n: (frozenset({1}), frozenset()) for n in ("int", "uint", "double", "string", "bytes", "bool", "dyn",
                                                   "type", "timestamp", "duration")},
    **{n: (frozenset(), frozenset({0, 1})) for n in _GETTERS},
    **{n: (frozenset({k}), frozenset()) for n, k in INV_ARITY.items()},
}


def arity_error(name: str, method: bool, n: int) -> str | None:
    """Why a call of a known function with n arguments (on a receiver if method) cannot evaluate, else None."""
    if name not in ARITY:
        return None
    glob, recv = ARITY[name]
    if (n in recv) if method else (n in glob):
        return None
    form = f"x.{name}()" if method else f"{name}()"
    if name in _MACROS and not method:
        return f"{name}() is a macro: write list.{name}(var, predicate), not {name}(list, var, predicate)"
    if method and not recv:
        return f"{form}: {name} is not a method; write {name}(x)"
    if not method and not glob:
        return f"{form}: {name} is a method; write x.{name}(...)"
    counts = sorted(recv if method else glob)
    words = {0: "no arguments", 1: "one argument", 2: "two arguments", 3: "three arguments"}
    return f"{form} takes {' or '.join(words.get(c, f'{c} arguments') for c in counts)}, got {n}"
