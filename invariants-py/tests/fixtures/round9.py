"""Fixture app for the fix-round-9 tests (certification round 7): Python values to CEL types, nesting."""
from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid
from typing import Any

import pydantic

import invariants as inv

M = "py:round9"


@dataclasses.dataclass
class Order:
    total: float
    placed: datetime.datetime
    closes: datetime.datetime | None = None


@dataclasses.dataclass
class Item:
    price: decimal.Decimal
    qty: int


@inv.guard("T-SHIP")
def ship(deadline: datetime.datetime, wait: datetime.timedelta) -> None:
    return None


@inv.guard("T-PLACE")
def place(order: Order) -> None:
    return None


@inv.guard("T-WHEN")
def when(at: datetime.datetime | None, lease: datetime.timedelta | None = None) -> None:
    return None


@inv.guard("T-ID")
def ident(u: uuid.UUID, us: list[uuid.UUID]) -> None:
    return None


@inv.guard("T-MONEY")
def money(v: decimal.Decimal) -> None:
    return None


@inv.guard("T-DAY")
def day(d: datetime.date) -> None:
    return None


@inv.guard("T-NAIVE")
def naive(t: pydantic.NaiveDatetime) -> None:
    return None


@inv.guard("T-AWARE")
def aware(t: pydantic.AwareDatetime) -> None:
    return None


@inv.guard("T-CART")
def cart(items: list[Item]) -> None:
    return None


@inv.guard("T-ANY")
def untyped(x: Any) -> None:
    return None


@inv.guard("T-SUM")
def sums(a: float, b: float) -> None:
    return None


RULES = {"ship": "T-SHIP", "place": "T-PLACE", "when": "T-WHEN", "ident": "T-ID", "money": "T-MONEY",
         "day": "T-DAY", "naive": "T-NAIVE", "aware": "T-AWARE", "cart": "T-CART", "untyped": "T-ANY",
         "sums": "T-SUM"}


def doc(fn: str, bad: str | list[str]) -> dict[str, Any]:
    """A rule file with one enforcing rule on round9.<fn>."""
    target = f"{M}.{fn}"
    return {"targets": {target: {"effects": "none"}},
            "rules": [{"id": RULES[fn], "status": "enforce", "subject": {"target": target}, "bad": bad,
                       "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}],
                       "falsified_by": ["tests/test_fix_round9.py::x"]}]}
