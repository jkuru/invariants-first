"""CEL equality and map lookup: heterogeneous equality, lists and maps read whole, CEL key matching."""
from __future__ import annotations

import datetime
from typing import Any

from .data import _data, _finite, _num, _ty

_SCALAR_TYPES = (str, bytes, bool, int, float, datetime.datetime, datetime.timedelta, type(None))


def _eq(a: Any, b: Any) -> bool:
    """CEL equality. A list, map or record compared whole is first read whole, as the engines'
    conversion reads it (cel/values.py): a NaN or +-Infinity anywhere in it is an error (unless
    allowed), a record is the map of its fields, and nested values compare by CEL rules, never by
    Python's == (which calls a shared NaN object equal to itself)."""
    a, b = _data(a), _data(b)           # list elements and map values are read here too (UUID -> string, ...)
    if type(a) is float or type(b) is float:
        _finite(a)
        _finite(b)
    if _num(a) and _num(b):
        return a == b
    if not isinstance(a, _SCALAR_TYPES) or not isinstance(b, _SCALAR_TYPES):
        return _eq_plain(whole(a), whole(b))
    if _ty(a) != _ty(b):
        return False            # CEL heterogeneous equality is false, not error
    return a == b


def whole(v: Any) -> Any:
    """v read whole as plain CEL data (the engines' conversion, cel/_engine.to_plain)."""
    from .._engine import to_plain  # _engine imports this package
    return to_plain(v)


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


def lookup(m: Any, k: Any) -> tuple[bool, Any]:
    """CEL map lookup: (found, value). Keys compare as CEL values: an int key and an integral double
    index are the same key ({1: 'a'}[1.0] == 'a'), a bool is never a number ({1: 'a'}[true] is no
    such key), whatever Python's dict thinks of 1 == 1.0 == True."""
    if type(k) is str:
        return (k in m, m.get(k))
    kk = _kind(k)
    if kk not in ("bool", "num") or k not in m:
        return (False, None)
    for key in m:                       # Python merges 1, 1.0 and True: find the stored key's kind
        if _kind(key) == kk and key == k:
            return (True, m[key])
    return (False, None)
