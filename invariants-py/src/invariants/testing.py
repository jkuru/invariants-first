"""Test helpers. Everything here forges values on purpose; never use it in production code."""
from __future__ import annotations

from typing import Any, TypeVar

from .guard import Checked
from .registry import capture

C = TypeVar("C", bound=Checked)


def make_checked(cls: type[C], value: Any, verdict: str = "ok", attempts: int = 1, rule: str | None = None) -> C:
    """A cls instance carrying value and verdict, as inv.model_call would build it. A deliberate forgery for tests."""
    if not (isinstance(cls, type) and issubclass(cls, Checked)):
        raise TypeError("make_checked needs an inv.Checked subclass")
    obj: C = cls._inv_make(value, attempts, rule or "", verdict)
    return obj


def make_unbuilt(cls: type[C]) -> C:
    """A cls instance with no verdict or value at all (what object.__new__ gives). For edge tests."""
    return object.__new__(cls)


__all__ = ["make_checked", "make_unbuilt", "capture"]
