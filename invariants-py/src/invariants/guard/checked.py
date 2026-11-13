"""inv.Checked, the refined type inv.model_call builds, and the frozen containers its value is held in.

A {field: type} reply's value is a FrozenDict (its lists FrozenList): read like the parsed data,
but every mutating method raises TypeError. _freeze makes one, _thaw gives back the plain data.
"""
from __future__ import annotations

import copy
from typing import Any


def _immutable(*_: Any, **__: Any) -> Any:
    raise TypeError("checked values are immutable")


_FREEZE_DEPTH = 256


class FrozenDict(dict[Any, Any]):
    """The value of a Checked over a {field: type} schema: a dict (equal to, and read like, the parsed
    one) whose mutating methods raise TypeError, as do those of the lists and maps inside it."""

    __slots__ = ()
    __setitem__ = __delitem__ = __ior__ = _immutable
    clear = pop = popitem = setdefault = update = _immutable

    def __init__(self, *a: Any, **k: Any) -> None:
        if a or k:          # dict.__init__ on a built FrozenDict would update it in place
            _immutable()

    def __copy__(self) -> Any:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        return _freeze(copy.deepcopy(_thaw(self), memo))

    def __reduce__(self) -> Any:
        return (_freeze, (_thaw(self),))


class FrozenList(list[Any]):
    """A list inside a frozen Checked value: equal to, and read like, a list; its mutating methods
    raise TypeError."""

    __slots__ = ()
    __setitem__ = __delitem__ = __iadd__ = __imul__ = _immutable
    append = extend = insert = pop = remove = clear = sort = reverse = _immutable

    def __init__(self, *a: Any, **k: Any) -> None:
        if a or k:          # list.__init__ on a built FrozenList would replace its items
            _immutable()

    def __copy__(self) -> Any:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        return _freeze(copy.deepcopy(_thaw(self), memo))

    def __reduce__(self) -> Any:
        return (_freeze, (_thaw(self),))


def _freeze(v: Any, depth: int = 0) -> Any:
    """Plain dicts and lists (and those inside them) as FrozenDict and FrozenList; anything else as is."""
    if depth > _FREEZE_DEPTH:
        return v
    if type(v) is dict:
        out = FrozenDict()
        for k, x in v.items():
            dict.__setitem__(out, k, _freeze(x, depth + 1))
        return out
    if type(v) is list:
        items = FrozenList()
        list.extend(items, [_freeze(x, depth + 1) for x in v])
        return items
    return v


# a frozen Checked value (or a part of one) parses like the plain data it was made from
_LISTS = (list, FrozenList)
_DICTS = (dict, FrozenDict)


def _thaw(v: Any, depth: int = 0) -> Any:
    """The plain dicts and lists a frozen value was made from (a copy)."""
    if depth > _FREEZE_DEPTH:
        return v
    if isinstance(v, FrozenDict):
        return {k: _thaw(x, depth + 1) for k, x in dict.items(v)}
    if isinstance(v, FrozenList):
        return [_thaw(x, depth + 1) for x in list.__iter__(v)]
    return v


class Checked:
    """Base for refined types built by inv.model_call; carries value, attempts, rule and verdict.

    Python cannot make this unforgeable (object.__new__ exists). The type is convenience;
    an @inv.edge re-validates the value against the closed schema, re-checks bad on it and calls
    its consumer with a new Checked over the re-validated copy (equal to the received one).
    copy.copy returns the same (immutable) object; copy.deepcopy and pickle give an equal
    Checked over a copy of the value. Neither adds trust: an edge re-checks a copy the same way.
    The value of a {field: type} schema is a FrozenDict (a dict whose mutating methods raise
    TypeError, as do those of the lists and maps inside it), so neither `checked.value.update(...)`
    nor `checked.update(...)` (attribute reads are forwarded to the value) changes it.
    """

    __slots__ = ("value", "attempts", "rule", "verdict")

    def __init__(self, *a: Any, **k: Any) -> None:
        raise TypeError(f"{type(self).__name__} is constructed only by inv.model_call")

    @classmethod
    def _inv_make(cls, value: Any, attempts: int, rule: str, verdict: str) -> Any:
        obj = object.__new__(cls)
        for k, v in (("value", _freeze(value)), ("attempts", attempts), ("rule", rule), ("verdict", verdict)):
            object.__setattr__(obj, k, v)
        return obj

    def __setattr__(self, k: str, v: Any) -> None:
        raise AttributeError("checked values are immutable")

    def __delattr__(self, k: str) -> None:
        raise AttributeError("checked values are immutable")

    def __getattr__(self, k: str) -> Any:
        if k.startswith("__") and k.endswith("__"):     # never borrow the value's protocols (copy, pickle)
            raise AttributeError(k)
        return getattr(object.__getattribute__(self, "value"), k)

    def _inv_state(self) -> tuple[Any, Any, Any, Any]:
        return _slot(self, "value"), _slot(self, "attempts"), _slot(self, "rule"), _slot(self, "verdict")

    def __eq__(self, other: object) -> bool:
        """Equal Checked values: the same class, rule, verdict, attempts and an equal value (an edge
        hands its consumer an equal copy, never the received object)."""
        if type(other) is not type(self):
            return NotImplemented
        return self._inv_state() == Checked._inv_state(other)

    def __hash__(self) -> int:
        _, attempts, rule, verdict = self._inv_state()
        return hash((type(self), attempts, rule, verdict))

    def __copy__(self) -> Any:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        value, attempts, rule, verdict = self._inv_state()
        return type(self)._inv_make(copy.deepcopy(value, memo), attempts, rule, verdict)

    def __reduce__(self) -> Any:
        return (_rebuild_checked, (type(self), *self._inv_state()))

    def __repr__(self) -> str:
        value, attempts, _, verdict = self._inv_state()
        return f"{type(self).__name__}({value!r}, verdict={verdict}, attempts={attempts})"


def _slot(obj: Any, k: str) -> Any:
    try:
        return object.__getattribute__(obj, k)
    except AttributeError:
        return None


def _rebuild_checked(cls: Any, value: Any, attempts: Any, rule: Any, verdict: Any) -> Any:
    """Unpickle a Checked (it carries no more trust than any other Checked: an edge re-checks it)."""
    if not (isinstance(cls, type) and issubclass(cls, Checked)):
        raise TypeError(f"{cls!r} is not an inv.Checked subclass")
    return cls._inv_make(value, attempts, rule, verdict)
