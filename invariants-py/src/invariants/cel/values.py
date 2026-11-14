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
| pydantic RootModel                   | its .root, mapped by this table                            |
| dataclass, pydantic model, object    | map of its public fields                                   |
| other sequence or iterable           | refused: UserList, deque, range, generators, any class     |
|                                      | with __iter__/__len__/__getitem__ (pass a list)            |
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
import os
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


# A str holding a surrogate code point (U+D800..U+DFFF: json.loads('"\\ud800"'), a FastAPI or
# pydantic python-mode body, text re-dumped with ensure_ascii=False) is not Unicode text and has no
# UTF-8 form: cel-cpp crashes converting one, and its length and comparisons would differ from a
# real character's. A clause that reads one is a rule_error on every backend (the value itself is
# never in the message).
NOT_TEXT = ("a string holding a surrogate code point (U+D800 to U+DFFF, e.g. from json.loads of a '\\ud800' "
            "escape) is not valid Unicode text and cannot be read by a rule")
_SURROGATE = re.compile("[\ud800-\udfff]")


def text(s: str) -> str:
    """s, or CelError(NOT_TEXT) if it holds a surrogate code point (an ASCII str is O(1) to check)."""
    if s.isascii() or _SURROGATE.search(s) is None:
        return s
    raise CelError(NOT_TEXT)


# The string-size limit: contains(), startsWith(), endsWith() and matches() refuse an operand longer
# than this many characters (a rule_error, on every backend alike), so no call's work grows without
# bound with the request. INV_CEL_STRING_LIMIT sets it at import; cel.set_string_limit() at run time.
STRING_LIMIT_ENV = "INV_CEL_STRING_LIMIT"
DEFAULT_STRING_LIMIT = 1_000_000


def _limit_from_env() -> int:
    raw = os.environ.get(STRING_LIMIT_ENV, "")
    try:
        n = int(raw) if raw.strip() else DEFAULT_STRING_LIMIT
    except ValueError:
        n = DEFAULT_STRING_LIMIT
    return n if n > 0 else DEFAULT_STRING_LIMIT


_string_limit = [_limit_from_env()]


def string_limit() -> int:
    """The current string-size limit (characters)."""
    return _string_limit[0]


def set_string_limit(n: int) -> int:
    """Set the string-size limit (a positive int of characters); returns the previous one."""
    if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        raise ValueError("the string-size limit is a positive int (characters)")
    old, _string_limit[0] = _string_limit[0], n
    return old


def sized(name: str, s: str) -> str:
    """s, or CelError if it is longer than the string-size limit (its length only in .detail)."""
    if len(s) > _string_limit[0]:
        err = CelError(f"{name}(): a string operand is longer than the string-size limit "
                       f"({_string_limit[0]:,} characters; {STRING_LIMIT_ENV} or cel.set_string_limit)")
        err.detail = f"{len(s):,} characters"
        raise err
    return s


# ---- lists that are not list or tuple --------------------------------------------------------
# A pydantic RootModel is the value it wraps (.root): RootModel[list[Line]] is the list the API
# received. Any other sequence or iterable that is not a list or tuple (collections.UserList, a
# deque, a range, a generator, a class with __iter__, __len__ or __getitem__) is refused on every
# backend: never read as the map of its fields (size() would count its attributes), and never
# iterated (a generator would be used up, a custom sequence could answer differently each time).

LISTLIKE = ("a {0} is a sequence or iterable but not a list or tuple, and a rule reads only a list or "
            "tuple as a list; pass list(...) (or a pydantic RootModel, which is read as its .root)")


_ROOT_MODEL: dict[type, bool] = {}


def is_root_model(v: Any) -> bool:
    """True for a pydantic RootModel instance (read as its .root). Cached per class."""
    t = type(v)
    got = _ROOT_MODEL.get(t)
    if got is None:
        got = getattr(t, "__pydantic_root_model__", False) is True
        if len(_ROOT_MODEL) < 4096:
            _ROOT_MODEL[t] = got
    return got


def unroot(v: Any) -> Any:
    """v, or the value a pydantic RootModel wraps (nested RootModels unwrapped too)."""
    for _ in range(64):
        if not is_root_model(v):
            return v
        v = v.root
    raise CelError("value is cyclic or nested too deeply")


_LISTLIKE: dict[type, bool] = {}


def listlike(o: Any) -> bool:
    """True for an object that is a sequence or iterable (its class has __iter__, __len__ or
    __getitem__) but not a record (a dataclass or pydantic model instance: is_record); the caller has
    ruled out lists, tuples, strings, bytes, Mappings and scalars. See LISTLIKE. Cached per class."""
    t = type(o)
    got = _LISTLIKE.get(t)
    if got is None:
        got = (hasattr(t, "__iter__") or hasattr(t, "__len__") or hasattr(t, "__getitem__")) and not is_record(o)
        if len(_LISTLIKE) < 4096:
            _LISTLIKE[t] = got
    return got


def listlike_error(o: Any) -> CelError:
    return CelError(LISTLIKE.format(type(o).__name__))


def is_record(o: Any) -> bool:
    """A dataclass instance or a pydantic model instance: a record, read as the map of its fields
    even if it is iterable (a pydantic model is)."""
    return (hasattr(type(o), "__dataclass_fields__") and not isinstance(o, type)) \
        or isinstance(getattr(type(o), "model_fields", None), dict)


_API_NAMES: dict[type, frozenset[str]] = {}


_RECORD_NAMES: dict[type, frozenset[str] | None] = {}


def record_names(o: Any) -> frozenset[str]:
    """api_names(type(o)) for an object read by field name, or CelError if it is list-like
    (listlike: a sequence or iterable is never the map of its attributes). Cached per class."""
    t = type(o)
    if t not in _RECORD_NAMES:
        got = None if listlike(o) else api_names(t)
        if len(_RECORD_NAMES) >= 4096:
            if got is None:
                raise listlike_error(o)
            return got
        _RECORD_NAMES[t] = got
    names = _RECORD_NAMES[t]
    if names is None:
        raise listlike_error(o)
    return names


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


NOT_GIVEN: Any = type("NotGiven", (), {"__repr__": lambda self: "NOT_GIVEN"})()   # a getter called without a zone


def getter(name: str, v: Any, tz: Any = NOT_GIVEN) -> int:
    """The CEL timestamp and duration getters (getFullYear, getMonth, ..., getMilliseconds). Without tz,
    a timestamp is read in UTC; a tz that is given must be a zone string: null is no zone, but an error
    (no such overload), as in cel-expr, never UTC."""
    if isinstance(v, datetime.timedelta):
        if tz is not NOT_GIVEN:
            raise CelError(f"no such overload: duration.{name}({'null' if tz is None else 'string'})")
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
    try:                # a zone can move an instant near CEL's range ends past year 1 or 9999
        t = v.astimezone(UTC if tz is NOT_GIVEN else zone(tz))
    except (OverflowError, ValueError):
        raise CelError("timestamp out of range") from None
    return {
        "getFullYear": lambda: t.year, "getMonth": lambda: t.month - 1, "getDate": lambda: t.day,
        "getDayOfMonth": lambda: t.day - 1, "getDayOfYear": lambda: t.timetuple().tm_yday - 1,
        "getDayOfWeek": lambda: (t.weekday() + 1) % 7, "getHours": lambda: t.hour, "getMinutes": lambda: t.minute,
        "getSeconds": lambda: t.second, "getMilliseconds": lambda: t.microsecond // 1000,
    }[name]()


__all__ = ["api_names", "UTC", "NAIVE", "DATE", "TIME", "DECIMAL", "REFUSED", "SCALARS", "scalar", "timestamp", "duration",
           "parse_timestamp", "parse_duration", "format_duration", "format_timestamp", "unix_seconds", "getter",
           "ts_add", "zone", "check_zone"]
