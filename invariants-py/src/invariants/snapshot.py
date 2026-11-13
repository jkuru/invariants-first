"""old_self: copy only the paths a transition rule reads, and only plain data."""
from __future__ import annotations

import copy
import dataclasses
import datetime
import decimal
import enum
import fractions
import uuid
from collections.abc import Mapping
from typing import Any

from .cel import CelError
from .cel.errors import raised
from .errors import LoadError
from .model import Rule
from .providers import walk
from .registry import Registry, current

# Immutable values a snapshot keeps as they are. cel/values.py maps them when a clause reads them,
# exactly as it maps arguments: a datetime is a timestamp (a naive one is refused), a timedelta a
# duration, a UUID a string, an Enum member its value, bytes bytes; a date, time or Decimal is
# refused there with the conversion to make (CEL has no such type).
SCALARS = (bytes, datetime.date, datetime.time, datetime.timedelta, uuid.UUID, enum.Enum, decimal.Decimal,
           fractions.Fraction)


def is_plain(v: Any, depth: int = 0) -> bool:
    """Numbers, strings, bytes, None, datetimes, durations, UUIDs, Enum members, lists, maps and
    dataclasses of those."""
    if depth > 64:
        return False
    if v is None or isinstance(v, (bool, int, float, str, *SCALARS)):
        return True
    if isinstance(v, (list, tuple)):
        return all(is_plain(x, depth + 1) for x in v)
    if isinstance(v, Mapping):          # a dict, MappingProxyType or custom Mapping: a CEL map
        return all(isinstance(k, (str, int)) and is_plain(x, depth + 1) for k, x in v.items())
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return all(is_plain(getattr(v, f.name), depth + 1) for f in dataclasses.fields(v))
    return False


def _copy(v: Any) -> Any:
    """A deep copy of a plain value (is_plain) that a later change to the original never reaches. A
    Mapping that is not a dict is copied as a dict (the map the engines read): a MappingProxyType
    cannot be deep-copied, and a custom Mapping may read live state."""
    if not _holds_mapping(v):
        return copy.deepcopy(v)
    if isinstance(v, Mapping):
        return {k: _copy(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        items = [_copy(x) for x in v]
        return items if isinstance(v, list) else tuple(items)
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        c = copy.copy(v)
        for f in dataclasses.fields(v):
            object.__setattr__(c, f.name, _copy(getattr(v, f.name)))
        return c
    return copy.deepcopy(v)


def _holds_mapping(v: Any, depth: int = 0) -> bool:
    """True if a non-dict Mapping is anywhere in plain value v (deepcopy cannot be trusted with it)."""
    if depth > 64:
        return False
    if isinstance(v, Mapping):
        return not isinstance(v, dict) or any(_holds_mapping(x, depth + 1) for x in v.values())
    if isinstance(v, (list, tuple)):
        return any(_holds_mapping(x, depth + 1) for x in v)
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return any(_holds_mapping(getattr(v, f.name), depth + 1) for f in dataclasses.fields(v))
    return False


_ABSENT = object()


def _step(v: Any, p: str) -> Any:
    """v.p (a key or a public attribute), _ABSENT when v has no such field; a lookup that raises is a
    CelError (rule_error)."""
    if isinstance(v, Mapping):
        try:
            return v[p] if p in v else _ABSENT
        except Exception as e:
            raise raised(f"reading key {p!r} on {type(v).__name__}", e) from e
    if p.startswith("_") or v is None or isinstance(v, (bool, int, float, str, *SCALARS)):
        return _ABSENT
    try:
        return getattr(v, p)
    except AttributeError:
        return _ABSENT
    except Exception as e:          # a property that raises
        raise raised(f"reading {p!r} on {type(v).__name__}", e) from e


def take_snapshot(rule: Rule, obj: Any) -> dict[str, Any]:
    """Deep-copy the referenced old_self paths; never touch anything else on self.

    The snapshot has the object's shape along those paths: a field that is absent is absent from
    the snapshot too, so has(old_self.a.b) is false when b is missing (and reading it is the
    engine's own no-such-key error), as on the live object.
    """
    snap: dict[str, Any] = {}
    for path in rule.snapshot:
        v, d = obj, snap
        for k, p in enumerate(path):
            v = _step(v, p)
            if v is _ABSENT:
                break
            if k == len(path) - 1:
                if not is_plain(v):
                    raise CelError(f"old_self.{'.'.join(path)} is not plain data ({type(v).__name__})")
                d[p] = _copy(v)
            elif v is None:
                d[p] = None          # a null on the way: selecting a field of it is the engine's error
                break
            else:
                nxt = d.setdefault(p, {})
                if not isinstance(nxt, dict):
                    break
                d = nxt
    return snap


def check_snapshot(key: str, instance: Any, reg: Registry | None = None) -> list[str]:
    """Test-time check with a real instance: every old_self path is plain data, else LoadError."""
    checked = []
    for rule in (reg or current()).rules_for(key):
        for path in rule.snapshot:
            try:
                v = walk(instance, path)
            except CelError as e:
                raise LoadError(f"{rule.id}: old_self.{'.'.join(path)}: {e}") from e
            if not is_plain(v):
                raise LoadError(f"{rule.id}: old_self.{'.'.join(path)} is not plain data on a real "
                                f"{type(instance).__name__} (it is {type(v).__name__})")
            checked.append(f"{rule.id}:old_self.{'.'.join(path)}")
    return checked
