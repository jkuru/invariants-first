"""The one mapping from Python values to CEL values, shared by every backend and the type checker.

| Python value                         | CEL value                                                  |
|--------------------------------------|------------------------------------------------------------|
| None                                 | null                                                       |
| bool                                 | bool                                                       |
| int (must fit int64)                 | int                                                        |
| float (NaN and +-Infinity refused)   | double                                                     |
| str, bytes                           | string, bytes                                              |
| Enum member                          | its value, mapped by this table                            |
| uuid.UUID                            | string (the canonical hyphenated lower-case form)          |
| datetime.datetime with a tzinfo      | timestamp (the same instant, in UTC)                       |
| datetime.timedelta                   | duration                                                   |
| list, tuple / dict, Mapping          | list / map                                                 |
| dataclass, pydantic model, object    | map of its public fields                                   |
| naive datetime.datetime              | refused: no timezone, and the library never assumes one    |
| datetime.date, datetime.time         | refused: CEL has no date or time-of-day type               |
| decimal.Decimal, Fraction, complex   | refused: CEL has no decimal, and a double loses precision  |
| set, frozenset                       | refused: CEL has no set type                               |

A refused value is a rule_error naming the type when a clause reads it, and a LoadError when
the clause reads a path whose annotation is one of the refused types (typecheck.from_py).
Timestamps and durations carry microseconds (Python's precision); CEL's range applies:
timestamps 0001-01-01T00:00:00Z to 9999-12-31T23:59:59.999999Z, durations up to about
+-10,000 years (315,576,000,000 seconds).
"""
from __future__ import annotations

import datetime
import decimal
import fractions
import re
import typing
import uuid
from typing import Any

from .errors import CelError

UTC = datetime.timezone.utc
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=UTC)
_US = datetime.timedelta(microseconds=1)
MAX_DURATION_US = 315_576_000_000 * 10**6 + 999_999      # CEL: +-(10,000 years of 365.25 days)

# Why each refused type is refused, with the conversion to make instead (used at load and run time).
NAIVE = ("a naive datetime (no tzinfo) is refused: CEL timestamps are instants and the library never "
         "assumes a timezone; pass an aware datetime, e.g. datetime.now(timezone.utc)")
DATE = ("datetime.date is not a CEL type (CEL has timestamps, not calendar dates); pass an aware datetime "
        "(e.g. midnight UTC) or an ISO date string ('2024-01-31')")
TIME = ("datetime.time is not a CEL type; pass seconds since midnight as an int or an 'HH:MM' string")
DECIMAL = ("Decimal is not a CEL type: CEL has no decimal, and converting to a double would lose money "
           "precision; pass int minor units (cents) or a str")
NUMBER = "{0} is not a CEL type; pass an int, a float or a str"
SET = "a set is not a CEL type; pass a list (sorted, if order matters)"

# The types the mapping refuses, and why (the type checker uses the same table at load).
REFUSED: tuple[tuple[type, str], ...] = (
    (datetime.date, DATE), (datetime.time, TIME), (decimal.Decimal, DECIMAL),
    (fractions.Fraction, NUMBER.format("Fraction")), (complex, NUMBER.format("complex")),
    (set, SET), (frozenset, SET),
)
# Values that are scalars to CEL even though Python objects have attributes: never read as records.
SCALARS = (datetime.date, datetime.time, datetime.timedelta, uuid.UUID, decimal.Decimal, fractions.Fraction,
           complex)


_API_NAMES: dict[type, frozenset[str]] = {}


def api_names(cls: type) -> frozenset[str]:
    """Names a record of class cls answers that are not its data: for a pydantic model, the
    BaseModel API (model_fields, model_config, copy, dict, json, ...) that is not one of its
    fields or computed fields. A rule never reads them, at load (typecheck) or at run time."""
    got = _API_NAMES.get(cls)
    if got is None:
        base = next((c for c in getattr(cls, "__mro__", ())
                     if c.__name__ == "BaseModel" and c.__module__.startswith("pydantic")), None)
        data = set(getattr(cls, "model_fields", None) or ()) | set(getattr(cls, "model_computed_fields", None) or ())
        got = frozenset() if base is None else frozenset(n for n in dir(base) if not n.startswith("_")
                                                          and n not in data)
        if len(_API_NAMES) < 4096:
            _API_NAMES[cls] = got
    return got


def timestamp(v: datetime.datetime) -> datetime.datetime:
    """An aware datetime as a plain UTC datetime; a naive one is a CelError."""
    if v.tzinfo is None or v.utcoffset() is None:
        raise CelError(NAIVE)
    try:
        u = v.astimezone(UTC)
    except (OverflowError, ValueError):
        raise CelError("timestamp out of range") from None
    return datetime.datetime(u.year, u.month, u.day, u.hour, u.minute, u.second, u.microsecond, tzinfo=UTC)


def duration(v: datetime.timedelta) -> datetime.timedelta:
    """A timedelta as a plain timedelta inside CEL's duration range."""
    if abs(v // _US) > MAX_DURATION_US:
        raise CelError("duration out of range")
    return datetime.timedelta(days=v.days, seconds=v.seconds, microseconds=v.microseconds)


def scalar(v: Any) -> Any:
    """The CEL value of a datetime, date, time, timedelta, UUID or refused number (see the table)."""
    if isinstance(v, datetime.datetime):
        return timestamp(v)
    if isinstance(v, datetime.timedelta):
        return duration(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    for t, why in REFUSED:
        if isinstance(v, t):
            raise CelError(why)
    return v


# ---- CEL timestamp and duration text ------------------------------------------------------
# One parser and one printer for every backend, so timestamp(), duration() and string() agree.

# re.ASCII: \d is 0-9 only, never another script's digits ('PT٥M', '+٠٥:٣٠' are not durations or offsets)
_TS = re.compile(r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(?:([Zz])|([+-])(\d{2}):?(\d{2}))",
                 re.ASCII)
_DUR_PART = re.compile(r"(\d+(?:\.\d*)?|\.\d+)(ms|us|ns|h|m|s)", re.ASCII)
_OFFSET = re.compile(r"([+-])?(\d{2}):(\d{2})", re.ASCII)
_ZONE_NAME = re.compile(r"[A-Za-z0-9_+\-/]+", re.ASCII)   # IANA names: no NUL, control, space or non-ASCII
_UNIT_NS = {"h": 3600 * 10**9, "m": 60 * 10**9, "s": 10**9, "ms": 10**6, "us": 10**3, "ns": 1}


def parse_timestamp(s: str) -> datetime.datetime:
    """timestamp(string): RFC 3339 (e.g. '2024-01-31T09:30:00Z', '...+05:30'), in UTC; CelError otherwise.

    Fractions past microseconds must be zero (Python datetimes have microsecond precision)."""
    m = _TS.fullmatch(s)
    if not m:
        raise CelError("timestamp(): not an RFC 3339 timestamp (e.g. 2024-01-31T09:30:00Z)")
    y, mo, d, h, mi, sec, frac, z, sign, oh, om = m.groups()
    frac = (frac or "").ljust(9, "0")
    if frac[6:] != "000":
        raise CelError("timestamp(): sub-microsecond precision is not supported")
    try:
        tz = UTC if z else datetime.timezone((-1 if sign == "-" else 1)
                                             * datetime.timedelta(hours=int(oh), minutes=int(om)))
        return timestamp(datetime.datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec), int(frac[:6]), tz))
    except (ValueError, OverflowError):
        raise CelError("timestamp(): out of range or not a valid date and time") from None


def parse_duration(s: str) -> datetime.timedelta:
    """duration(string): a sign, then number-unit parts with units h, m, s, ms, us, ns ('1h30m', '-1.5s', '0')."""
    body, sign = (s[1:], -1) if s[:1] == "-" else (s[1:], 1) if s[:1] == "+" else (s, 1)
    if body == "0":
        return datetime.timedelta(0)
    pos, total = 0, fractions.Fraction(0)
    while pos < len(body):
        m = _DUR_PART.match(body, pos)
        if not m:
            raise CelError("duration(): not a duration (e.g. '1h30m', '90s', '1.5s')")
        total += fractions.Fraction(m.group(1)) * _UNIT_NS[m.group(2)]
        pos = m.end()
    if pos == 0:
        raise CelError("duration(): not a duration (e.g. '1h30m', '90s', '1.5s')")
    if total.denominator != 1 or total.numerator % 1000:
        raise CelError("duration(): sub-microsecond precision is not supported")
    us = sign * (total.numerator // 1000)
    if abs(us) > MAX_DURATION_US:
        raise CelError("duration out of range")
    return datetime.timedelta(microseconds=us)


def _frac(us: int) -> str:
    return "" if us == 0 else f".{us // 1000:03d}" if us % 1000 == 0 else f".{us:06d}"


def format_duration(v: datetime.timedelta) -> str:
    """string(duration): seconds with 0, 3 or 6 fraction digits, as CEL prints it ('5400s', '-1.500s')."""
    us = v // _US
    s, rest = divmod(abs(us), 10**6)
    return f"{'-' if us < 0 else ''}{s}{_frac(rest)}s"


def format_timestamp(v: datetime.datetime) -> str:
    """string(timestamp): RFC 3339 in UTC with 0, 3 or 6 fraction digits ('2024-01-31T09:30:00.500Z')."""
    u = v.astimezone(UTC)
    return (f"{u.year:04d}-{u.month:02d}-{u.day:02d}T{u.hour:02d}:{u.minute:02d}:{u.second:02d}"
            f"{_frac(u.microsecond)}Z")


def unix_seconds(v: datetime.datetime) -> int:
    """int(timestamp): whole seconds since the Unix epoch, rounded down (as cel-cpp does)."""
    return (v - EPOCH) // datetime.timedelta(seconds=1)


def ts_add(a: datetime.datetime, d: datetime.timedelta) -> datetime.datetime:
    """timestamp + duration, a CelError outside CEL's timestamp range."""
    try:
        return a + d
    except OverflowError:
        raise CelError("timestamp out of range") from None


def zone(tz: Any) -> datetime.tzinfo:
    """A getter's timezone argument: an IANA name ('America/New_York', 'UTC') or an offset ('+05:30', '-08:00')."""
    if not isinstance(tz, str):
        raise CelError("timezone must be a string")
    m = _OFFSET.fullmatch(tz)
    if m:
        off = datetime.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        try:                                # an offset must be under 24 hours, its minutes under 60
            if int(m.group(3)) >= 60:
                raise ValueError(tz)
            return datetime.timezone(-off if m.group(1) == "-" else off)
        except (ValueError, OverflowError):
            raise CelError("unknown timezone (an offset must be between -23:59 and +23:59)") from None
    if not _ZONE_NAME.fullmatch(tz):         # a NUL ends the name in C engines: refused before any engine sees it
        raise CelError("unknown timezone")
    import zoneinfo
    try:
        return zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        raise CelError("unknown timezone") from None


def check_zone(tz: Any) -> str:
    """tz unchanged if zone() accepts it, else CelError: engines that read zones themselves get only
    the zones every backend accepts (cel-expr would take '' as UTC and wrap '+99:00')."""
    zone(tz)
    return typing.cast(str, tz)


def getter(name: str, v: Any, tz: Any = None) -> int:
    """The CEL timestamp and duration getters (getFullYear, getMonth, ..., getMilliseconds)."""
    if isinstance(v, datetime.timedelta):
        if tz is not None:
            raise CelError(f"no such overload: duration.{name}(string)")
        us = v // _US
        total = abs(us)
        sign = -1 if us < 0 else 1
        if name == "getHours":
            return sign * (total // (3600 * 10**6))
        if name == "getMinutes":
            return sign * (total // (60 * 10**6))
        if name == "getSeconds":
            return sign * (total // 10**6)
        if name == "getMilliseconds":
            return sign * ((total % 10**6) // 1000)
        raise CelError(f"no such overload: duration.{name}()")
    if not isinstance(v, datetime.datetime):
        raise CelError(f"no such overload: {name}() on a value that is not a timestamp or duration")
    t = v.astimezone(UTC if tz is None else zone(tz))
    return {
        "getFullYear": lambda: t.year, "getMonth": lambda: t.month - 1, "getDate": lambda: t.day,
        "getDayOfMonth": lambda: t.day - 1, "getDayOfYear": lambda: t.timetuple().tm_yday - 1,
        "getDayOfWeek": lambda: (t.weekday() + 1) % 7, "getHours": lambda: t.hour, "getMinutes": lambda: t.minute,
        "getSeconds": lambda: t.second, "getMilliseconds": lambda: t.microsecond // 1000,
    }[name]()


__all__ = ["api_names", "UTC", "NAIVE", "DATE", "TIME", "DECIMAL", "REFUSED", "SCALARS", "scalar", "timestamp", "duration",
           "parse_timestamp", "parse_duration", "format_duration", "format_timestamp", "unix_seconds", "getter",
           "ts_add", "zone", "check_zone"]
