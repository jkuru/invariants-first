"""Values as the subset reads them: type names, the int64 and finiteness checks, field selection.

A value enters evaluation through _data (an identifier, a field, an element, a map value) or
_select (a field of a map or an object); errors never quote request data outside .detail.
"""
from __future__ import annotations

import datetime
import enum
from collections.abc import Mapping
from typing import Any, Callable

from .. import values
from ..errors import CelError, check_finite, raised
from .lexer import INT_MAX, INT_MIN


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_TY = {datetime.datetime: "timestamp", datetime.timedelta: "duration", type(None): "null"}
_TIME = (datetime.datetime, datetime.timedelta)


def _nty(v: Any) -> str:
    """CEL's name for a value's type in an arithmetic error (int and double told apart)."""
    if _num(v):
        return "double" if isinstance(v, float) else "int"
    return _ty(v)


def _ty(v: Any) -> str:
    return "num" if _num(v) else _TY.get(type(v)) or type(v).__name__


_KEY_TYPES = (str, int, bool)           # CEL map keys: int, uint, bool, string (never double or null)


def map_key(k: Any) -> Any:
    """k as a map literal key, or CelError: CEL keys are int, uint, bool or string."""
    if not isinstance(k, _KEY_TYPES):
        raise CelError(f"unsupported map key type {_nty(k) if _num(k) else _ty(k)}")
    return k


def check_int(v: int) -> int:
    """An int outside CEL's int64 range is an evaluation error on every backend."""
    if not INT_MIN <= v <= INT_MAX:
        raise CelError("integer value out of int64 range")
    return v


def _int_range(v: Any) -> Any:
    if isinstance(v, int) and not isinstance(v, bool) and not INT_MIN <= v <= INT_MAX:
        raise CelError("integer overflow")
    return v


def _finite(v: Any) -> Any:
    if isinstance(v, float):
        check_finite(v)
    return v


def _bool(v: Any, what: str) -> bool:
    if not isinstance(v, bool):
        raise CelError(f"{what} needs bool, got {_ty(v)}")
    return v


def _safe_repr(v: Any) -> str:
    try:
        return repr(v)[:80]
    except Exception:
        return "<unprintable>"


def _data(v: Any) -> Any:
    """A Python value as CEL reads it (cel/values.py): an Enum member is its value, ints must fit int64,
    a NaN or +-Infinity is an error unless allowed (check_finite), a UUID is its string, an aware
    datetime a UTC timestamp, a timedelta a duration; a naive datetime, a date, a time and a Decimal
    are errors."""
    t = type(v)
    if t is str or t is bool or t is dict or t is list or v is None:
        return v
    if isinstance(v, enum.Enum):
        v = v.value
    if isinstance(v, int) and not isinstance(v, bool):
        check_int(v)
    elif isinstance(v, float):
        check_finite(v)
    elif isinstance(v, values.SCALARS):
        return values.scalar(v)
    elif isinstance(v, (set, frozenset)):
        raise CelError(values.SET)
    return v


_NO_FIELDS = (str, bytes, int, float, list, tuple, *values.SCALARS)


def _on_map(m: Mapping[Any, Any], what: str, fn: Callable[[], Any]) -> Any:
    """fn() on a CEL map. A dict cannot fail here; any other Mapping (MappingProxyType, a custom
    Mapping: CEL maps too, as on cel-expr) that raises is a CelError, its message only in .detail."""
    if type(m) is dict:
        return fn()
    try:
        return fn()
    except CelError:
        raise
    except Exception as e:
        raise raised(f"{what} on {type(m).__name__}", e) from e


def _select(o: Any, f: str, test: bool = False) -> Any:
    if test and (o is None or isinstance(o, _NO_FIELDS)):
        # CEL has() needs a map or message: on null, a number, a string or a list it is an error (cel-expr)
        raise CelError(f"has() on {_ty(o)}: field selection on a value that has no fields")
    if isinstance(o, Mapping):
        ok, v = _on_map(o, f"reading key {f!r}", lambda: (True, o[f]) if f in o else (False, None))
    elif o is None or f.startswith("_") or isinstance(o, _NO_FIELDS) or f in values.api_names(type(o)):
        ok, v = False, None
    else:
        try:
            v = getattr(o, f)
        except AttributeError:
            ok, v = False, None
        except Exception as e:      # a property that fails is an evaluation error
            raise raised(f"reading {f!r}", e) from e
        else:
            ok = not callable(v)
    if test:
        return ok                   # CEL has(): presence only, a null value is present
    if not ok:
        raise CelError(f"no such field {f!r} on {type(o).__name__}")
    return _data(v)
