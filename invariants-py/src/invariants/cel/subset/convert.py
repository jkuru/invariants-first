"""CEL's type conversions on the subset's values: int(), double(), string(), bool(), dyn(),
timestamp() and duration(). Errors never quote the value (it is request data)."""
from __future__ import annotations

import datetime
import re
from typing import Any, Callable

from .. import values
from ..errors import CelError, check_result
from .data import _int_range, _ty, check_int

_INT_TEXT = re.compile(r"\s*[+-]?[0-9]+\s*")
_FLOAT_TEXT = re.compile(r"\s*[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\s*")
_BOOL_TEXT = {"1": True, "t": True, "true": True, "TRUE": True, "True": True,
              "0": False, "f": False, "false": False, "FALSE": False, "False": False}


def _convert(name: str, v: Any) -> Any:
    """CEL's type conversions on the subset's values. Errors never quote the value (it is request data).

    int(): int, bool, double (truncated toward zero; NaN, +-Infinity and values outside int64 are
    errors) and base-10 text. double(): int, double and decimal text (NaN and Infinity text are
    errors unless non-finite numbers are allowed). string(): string, int, bool, bytes (UTF-8);
    string(double) is refused because CEL engines print doubles differently (cel-expr '2', others
    '2.0'); string(timestamp) and string(duration) print as CEL does (values.format_*).
    timestamp(): RFC 3339 text or int seconds; duration(): text such as '1h30m'; int(timestamp):
    Unix seconds, rounded down. bool(): bool and the text 1, t, true, TRUE, True, 0, f, false, FALSE, False. dyn(): any value.
    """
    fn = _CONVERTERS.get(name)
    if fn is not None:
        out = fn(v)
        if out is not _NO_OVERLOAD:
            return out
    raise CelError(f"no such overload: {name}({_ty(v)})")


_NO_OVERLOAD: Any = object()        # a converter's answer for a value type it does not take


def _to_timestamp(v: Any) -> Any:
    if isinstance(v, datetime.datetime):
        return v
    if isinstance(v, str):
        return values.parse_timestamp(v)
    if isinstance(v, int) and not isinstance(v, bool):
        return values.ts_add(values.EPOCH, _seconds(v))
    return _NO_OVERLOAD


def _to_duration(v: Any) -> Any:
    if isinstance(v, datetime.timedelta):
        return v
    if isinstance(v, str):
        return values.parse_duration(v)
    return _NO_OVERLOAD


def _to_int(v: Any) -> Any:
    if isinstance(v, datetime.datetime):
        return values.unix_seconds(v)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v - v != 0.0 or not -2.0**63 < v < 2.0**63:
            raise CelError("int(): double out of int64 range")
        return check_int(int(v))
    if isinstance(v, str):
        if not _INT_TEXT.fullmatch(v):
            raise CelError("int(): cannot convert string to int")
        return _int_range(int(v))
    return _NO_OVERLOAD


def _to_double(v: Any) -> Any:
    if isinstance(v, int) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, float):
        return v
    if isinstance(v, str):
        if _FLOAT_TEXT.fullmatch(v):
            return check_result(float(v))      # '1e999' is Infinity
        if v.strip().lower().lstrip("+-") in ("nan", "inf", "infinity"):
            return check_result(float(v.strip()))
        raise CelError("double(): cannot convert string to double")
    return _NO_OVERLOAD


def _to_string(v: Any) -> Any:
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            raise CelError("string(): bytes are not valid UTF-8") from None
    if isinstance(v, datetime.datetime):
        return values.format_timestamp(v)
    if isinstance(v, datetime.timedelta):
        return values.format_duration(v)
    if isinstance(v, float):
        raise CelError("string(double) is not supported by the subset backend (CEL engines print doubles "
                       "differently); compare the number instead")
    return _NO_OVERLOAD


def _to_bool(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        if v not in _BOOL_TEXT:
            raise CelError("bool(): cannot convert string to bool")
        return _BOOL_TEXT[v]
    return _NO_OVERLOAD


_CONVERTERS: dict[str, Callable[[Any], Any]] = {
    "dyn": lambda v: v, "timestamp": _to_timestamp, "duration": _to_duration, "int": _to_int,
    "double": _to_double, "string": _to_string, "bool": _to_bool,
}


def _seconds(n: int) -> datetime.timedelta:
    try:
        return datetime.timedelta(seconds=n)
    except OverflowError:
        raise CelError("timestamp out of range") from None
