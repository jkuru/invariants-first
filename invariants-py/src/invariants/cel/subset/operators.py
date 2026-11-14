"""The binary operators on evaluated operands: arithmetic (numbers, strings, lists, timestamps and
durations), ordering and membership. && || == != are evaluated in evaluator.py."""
from __future__ import annotations

import datetime
import math
from collections.abc import Mapping
from typing import Any

from .. import values
from ..errors import ALLOW_NON_FINITE, CelError, check_result
from .data import UINT_MAX, _TIME, Uint, _finite, _int_range, _nty, _num, _on_map, _ty, as_container
from .equality import _eq, lookup, whole

_TIME_ARITH = {("+", "timestamp", "duration"), ("+", "duration", "timestamp"), ("+", "duration", "duration"),
               ("-", "timestamp", "timestamp"), ("-", "timestamp", "duration"), ("-", "duration", "duration")}


def _time_arith(op: str, a: Any, b: Any) -> Any:
    """timestamp and duration arithmetic (CEL: ts + dur, dur + ts, dur +- dur, ts - ts, ts - dur), range-checked."""
    if (op, _ty(a), _ty(b)) not in _TIME_ARITH:
        raise CelError(f"no such overload: {_ty(a)} {op} {_ty(b)}")
    if isinstance(a, datetime.datetime) and isinstance(b, datetime.datetime):
        return values.duration(a - b)
    if isinstance(b, datetime.datetime):
        return values.ts_add(b, a)
    if isinstance(a, datetime.datetime):
        return values.ts_add(a, b if op == "+" else -b)
    return values.duration(a + b if op == "+" else a - b)


def _arith(op: str, a: Any, b: Any) -> Any:
    if type(a) in _TIME or type(b) in _TIME:
        return _time_arith(op, a, b)
    if op == "+" and isinstance(a, str) and isinstance(b, str):
        return a + b
    if op == "+" and isinstance(a, bytes) and isinstance(b, bytes):
        return a + b                    # bytes concatenation, as in CEL
    if op == "+" and isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return list(a) + list(b)
    if not (_num(a) and _num(b)) or type(a) is not type(b) or (op == "%" and type(a) is float):
        raise CelError(f"no such overload: {_nty(a)} {op} {_nty(b)}")   # CEL: no int-double mix, no double %
    if op in "/%" and b == 0:
        if op == "/" and isinstance(a, float) and ALLOW_NON_FINITE.get():
            return ieee_div_zero(a, b)
        raise CelError("division by zero")
    if type(a) is Uint:                         # uint arithmetic: in [0, 2^64), / and % on naturals
        r = a + b if op == "+" else a - b if op == "-" else a * b if op == "*" else a // b if op == "/" else a % b
        if not 0 <= r <= UINT_MAX:
            raise CelError("uint overflow")
        return Uint(r)
    if op == "/" and isinstance(a, int):
        q = abs(a) // abs(b)
        return _int_range(q if (a >= 0) == (b > 0) else -q)
    if op == "%" and isinstance(a, int):
        return a - b * _arith("/", a, b)
    if op == "+":
        r = a + b
    elif op == "-":
        r = a - b
    elif op == "*":
        r = a * b
    else:
        r = a / b if op == "/" else a % b
    return check_result(_int_range(r))       # a double overflow (1e200 * 1e200) is an error


def ieee_div_zero(a: float, b: float) -> float:
    """IEEE a / b for a zero double b (Python raises): +-Infinity by the signs, NaN for 0/0 or NaN/0."""
    return math.copysign(math.inf, a) * math.copysign(1.0, b) if a == a and a != 0.0 else math.nan


_ORDER = ("<", "<=", ">", ">=")


def _order(op: str, a: Any, b: Any) -> bool:
    """a < b, a <= b, a > b or a >= b: numbers, or two strings, bytes (lexicographic by byte), bools,
    timestamps or durations."""
    if not ((_num(a) and _num(b)) or _ty(a) == _ty(b) in ("str", "bytes", "bool", "timestamp", "duration")):
        raise CelError(f"no such overload: {_ty(a)} {op} {_ty(b)}")
    _finite(a)
    _finite(b)
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def _in(a: Any, b: Any) -> bool:
    """a in b. The list or map is read whole, as on the engines (a record as the map of its fields)."""
    b = as_container(b)
    if isinstance(b, (list, tuple)):
        b = whole(b)
        return any(_eq(a, x) for x in b)
    if isinstance(b, Mapping):
        return lookup(_on_map(b, "reading", lambda: whole(b)), whole(a))[0]
    raise CelError(f"no such overload: in {_ty(b)}")
