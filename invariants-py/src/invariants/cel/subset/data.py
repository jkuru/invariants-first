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
UINT_MAX = 2**64 - 1


class Uint(int):
    """A CEL uint (from uint() or a 5u literal). The subset evaluates it for the exact evaluation of a
    cel-expr clause (expr_python: an int past 2^53 may meet a double); the loader refuses uint() on the
    subset backend. An int to Python, so numbers compare across int, uint and double exactly."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"{int(self)}u"


def check_uint(v: int) -> Uint:
    if not 0 <= v <= UINT_MAX:
        raise CelError("uint value out of range")
    return Uint(v)


class CelType:
    """A CEL type value (what type(x) gives, and what int, uint, double, ... name); for the exact
    evaluation of a cel-expr clause, as Uint."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other: object) -> bool:
        return type(other) is CelType and other.name == self.name

    def __hash__(self) -> int:
        return hash(self.name)

    def __repr__(self) -> str:
        return self.name


# the type names a clause may compare type(x) with, as cel-expr names them
TYPE_NAMES: dict[str, Any] = {n: CelType(n) for n in ("int", "uint", "double", "string", "bool", "bytes", "list",
                                                       "map", "null_type", "type")}
TYPE_NAMES["google"] = {"protobuf": {"Timestamp": CelType("google.protobuf.Timestamp"),
                                     "Duration": CelType("google.protobuf.Duration")}}


def type_of(v: Any) -> CelType:
    """type(v): the CEL type of an evaluated value."""
    if v is None:
        return TYPE_NAMES["null_type"]
    if isinstance(v, bool):
        return TYPE_NAMES["bool"]
    if isinstance(v, Uint):
        return TYPE_NAMES["uint"]
    if isinstance(v, int):
        return TYPE_NAMES["int"]
    for t, name in ((float, "double"), (str, "string"), (bytes, "bytes"), ((list, tuple), "list"),
                    (CelType, "type")):
        if isinstance(v, t):
            return TYPE_NAMES[name]
    if isinstance(v, datetime.datetime):
        return TYPE_NAMES["google"]["protobuf"]["Timestamp"]
    if isinstance(v, datetime.timedelta):
        return TYPE_NAMES["google"]["protobuf"]["Duration"]
    return TYPE_NAMES["map"]            # a Mapping, or a record (the map of its fields)


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
    datetime a UTC timestamp, a timedelta a duration; a naive datetime, a date, a time, a Decimal
    and a str holding a surrogate code point are errors."""
    t = type(v)
    if t is str:
        return v if v.isascii() else values.text(v)     # a surrogate code point is not text (values.text)
    if t is bool or t is dict or t is list or v is None:
        return v
    if t is int:
        return check_int(v)
    if t in _AS_IS:                     # a class seen before that none of the cases below converts
        return v
    if isinstance(v, enum.Enum):
        v = v.value
    if isinstance(v, int) and not isinstance(v, bool):
        if type(v) is not Uint:         # an int subclass (or an IntEnum's value) is a plain int, as on cel-expr
            return check_int(int(v))
    elif isinstance(v, float):          # a float subclass (numpy.float64) is a plain double
        return check_finite(v if type(v) is float else float(v))
    elif isinstance(v, values.SCALARS):
        return values.scalar(v)
    elif isinstance(v, (set, frozenset)):
        raise CelError(values.SET)
    elif isinstance(v, str):            # a str Enum value or a str subclass: a plain string (as to_plain)
        return values.text(v if type(v) is str else str(v))
    elif isinstance(v, bytes):          # a bytes subclass: plain bytes
        return v if type(v) is bytes else bytes(v)
    elif values.is_root_model(v):       # a pydantic RootModel is the value it wraps
        return _data(values.unroot(v))
    elif t is type(v) and len(_AS_IS) < 4096:   # a record or other object (not an Enum): as it is from now on
        _AS_IS.add(t)
    return v


_AS_IS: set[type] = {CelType}


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


def enum_key(m: Mapping[Any, Any], key: str) -> tuple[bool, Any]:
    """(found, value) for the field key among m's Enum keys, each read as its value (an Enum member
    is its value, as a map key too)."""
    for k in m:
        if isinstance(k, enum.Enum) and isinstance(k.value, str) and k.value == key:
            return (True, m[k])
    return (False, None)


def has_enum_key(m: Mapping[Any, Any]) -> bool:
    """True if a key of m is an Enum member (read as its value: see as_cel_map)."""
    return any(isinstance(k, enum.Enum) for k in m)


def as_container(v: Any) -> Any:
    """v as size(), in, [] and the macros read it: a list, tuple, string or Mapping as it is; a record
    (a dataclass, pydantic model or other object: cel/values.py) as the map of its public fields, as
    the engines read it; a sequence or iterable that is not a list or tuple a CelError
    (values.LISTLIKE); anything else as it is (the caller's overload error)."""
    t = type(v)
    if t is list or t is dict or t is str or t is tuple or v is None or isinstance(v, _CONTAINERS):
        return v
    from .equality import whole     # (equality imports this module)
    return whole(v)


_CONTAINERS = (str, bytes, list, tuple, Mapping, int, float, *values.SCALARS)


def _select(o: Any, f: str, test: bool = False) -> Any:
    if test and (o is None or isinstance(o, _NO_FIELDS)):
        # CEL has() needs a map or message: on null, a number, a string or a list it is an error (cel-expr)
        raise CelError(f"has() on {_ty(o)}: field selection on a value that has no fields")
    if isinstance(o, Mapping):
        ok, v = _on_map(o, f"reading key {f!r}", lambda: (True, o[f]) if f in o else enum_key(o, f))
    elif o is None or f.startswith("_") or isinstance(o, _NO_FIELDS) or f in values.record_names(o):
        ok, v = False, None             # (record_names: a list-like object is never the map of its attributes)
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
