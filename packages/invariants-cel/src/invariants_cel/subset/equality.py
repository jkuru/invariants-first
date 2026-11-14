"""CEL equality and map lookup: heterogeneous equality, lists and maps read whole, CEL key matching."""
from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Any

from ..errors import CelError, raised
from .cost import charge_whole
from .data import CelType, _data, _finite, _num, _ty, enum_key, has_enum_key

_SCALAR_TYPES = (str, bytes, bool, int, float, datetime.datetime, datetime.timedelta, type(None), CelType)


def _eq(a: Any, b: Any, budget: list[int] | None = None) -> bool:
    """CEL equality. A list, map or record compared whole is first read whole, as the engines'
    conversion reads it (values.py): a NaN or +-Infinity anywhere in it is an error (unless
    allowed), a record is the map of its fields, and nested values compare by CEL rules, never by
    Python's == (which calls a shared NaN object equal to itself). Reading them is charged to
    budget (cost.charge_whole)."""
    a, b = _data(a), _data(b)           # list elements and map values are read here too (UUID -> string, ...)
    if type(a) is float or type(b) is float:
        _finite(a)
        _finite(b)
    if _num(a) and _num(b):
        return a == b
    if not isinstance(a, _SCALAR_TYPES) or not isinstance(b, _SCALAR_TYPES):
        return _eq_plain(whole(a, budget), whole(b, budget))
    if _ty(a) != _ty(b):
        return False            # CEL heterogeneous equality is false, not error
    return a == b


def whole(v: Any, budget: list[int] | None = None) -> Any:
    """v read whole as plain CEL data (the engines' conversion, _engine.to_plain), its values charged
    to budget, if given (cost.charge_whole)."""
    from .._engine import to_plain  # _engine imports this package
    try:
        out = to_plain(v)
    except CelError:
        raise
    except Exception as e:              # a custom Mapping or record that raises while it is read
        raise raised(f"reading {type(v).__name__}", e) from e
    charge_whole(budget, out)
    return out


def cel_keys(m: Any) -> list[Any]:
    """The keys of the Mapping m as CEL map keys, in order: each read as the engines read a key
    (to_plain: an Enum member is its value, an int must fit int64, a string must be text), and a
    CelError for one CEL cannot hold (a double, null, bytes, a list, a timestamp: "unsupported map
    key type", as cel-expr says) or that repeats another once read (an Enum key next to its value).
    The macros and size() read a map's keys this way; its values are not read."""
    from .._engine import to_plain  # _engine imports this package
    out: list[Any] = []
    seen: set[Any] = set()
    for k in m:
        p = k if type(k) is str and k.isascii() else to_plain(k)
        if not isinstance(p, (str, int)):           # (a bool is an int)
            raise CelError(f"unsupported map key type {type(p).__name__}")
        if p in seen:                   # Python's dict merges 1 and True, as to_plain does
            raise CelError("repeated map key")
        seen.add(p)
        out.append(p)
    return out


class _Unequal:
    """The hash key of a NaN (when non-finite numbers are allowed): equal to nothing, itself included."""

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        return False

    def __hash__(self) -> int:
        return 0


def _key(p: Any) -> Any:
    """A hashable key of the plain value p (to_plain output, or a scalar as _data reads it) such that
    two values are CEL-equal (_eq_plain) exactly when their keys are equal: numbers by value across
    int, uint and double, a bool never a number, lists in order, maps by their entries."""
    t = type(p)
    if t is list:
        return ("list", tuple(_key(x) for x in p))
    if t is dict:
        return ("dict", frozenset((_key(k), _key(v)) for k, v in p.items()))
    if t is bool:
        return ("bool", p)
    if isinstance(p, (int, float)):
        return ("num", _Unequal() if p != p else p)
    return (t, p)


def unique(vals: Sequence[Any], budget: list[int] | None = None) -> bool:
    """True if no two of vals are CEL-equal (inv.unique_by, on every backend): each value is hashed by
    its CEL value, so the work is linear; a list, map or record is read whole for that, charged to
    budget. Errors are those comparing each value with the earlier ones (_eq) would raise: a value
    that cannot be read is an error once a second value is reached, unless a repeat came first."""
    seen: set[Any] = set()
    pending: CelError | None = None         # the first value's error: raised when a second one comes
    for i, v in enumerate(vals):
        try:
            d = _data(v)
            if type(d) is float:
                _finite(d)
            k = _key(d if isinstance(d, _SCALAR_TYPES) else whole(d, budget))
        except CelError as e:
            if i:
                raise
            pending = e
            continue
        if pending is not None:
            raise pending
        if k in seen:
            return False
        seen.add(k)
    return True


def _kind(v: Any) -> str:
    """The equality class of a plain CEL value: numbers form one (CEL compares int and double)."""
    if isinstance(v, bool):
        return "bool"
    return "num" if isinstance(v, (int, float)) else type(v).__name__


def _eq_plain(a: Any, b: Any) -> bool:
    """CEL equality of two plain values (to_plain output): no Python identity shortcut, NaN != NaN."""
    ka, kb = _kind(a), _kind(b)
    if ka != kb:
        return False
    if ka == "list":
        return len(a) == len(b) and all(map(_eq_plain, a, b))
    if ka == "dict":
        if len(a) != len(b):
            return False
        for k, x in a.items():
            ok, y = lookup(b, k)
            if not ok or not _eq_plain(x, y):
                return False
        return True
    return bool(a == b)


def lookup(m: Any, k: Any, budget: list[int] | None = None) -> tuple[bool, Any]:
    """CEL map lookup: (found, value). Keys compare as CEL values: an int key and an integral double
    index are the same key ({1: 'a'}[1.0] == 'a'), a bool is never a number ({1: 'a'}[true] is no
    such key), whatever Python's dict thinks of 1 == 1.0 == True."""
    if type(k) is str:
        if k in m:
            return (True, m.get(k))
        return enum_key(m, k)
    kk = _kind(k)
    if kk not in ("bool", "num"):       # bytes, null, a list, a map, a timestamp: never a map key (as cel-expr)
        raise CelError(f"no such overload: a {_ty(k)} as a map key (a key is an int, uint, bool or string)")
    if k not in m:
        if has_enum_key(m):             # an Enum key is its value (values.py)
            return lookup(whole(m, budget), k)
        return (False, None)
    for key in m:                       # Python merges 1, 1.0 and True: find the stored key's kind
        if _kind(key) == kk and key == k:
            return (True, m[key])
    return (False, None)
