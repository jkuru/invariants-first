"""Fixture app for the fix-round-10 tests (certification round 8): typed model replies, timezones,
numeric arithmetic, element-field projection."""
from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import json
import uuid
from typing import Any, Literal, Optional

import pydantic

import invariants as inv
from invariants.integrations import pydantic as inv_pd

M = "py:round10"


class Color(enum.Enum):
    RED = "r"
    BLUE = "b"


class Leg(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    at: datetime.datetime
    wait: datetime.timedelta


class PReply(pydantic.BaseModel):
    """A pydantic reply with time, duration, UUID, Enum, Optional, nested, list and dict fields."""
    model_config = pydantic.ConfigDict(extra="forbid")
    cause: Literal["A", "CANNOT_COMPLY"]
    at: datetime.datetime
    wait: datetime.timedelta
    uid: uuid.UUID
    col: Color
    maybe: Optional[datetime.datetime]
    leg: Leg
    legs: list[Leg]
    waits: dict[str, datetime.timedelta]


class NaiveReply(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    cause: Literal["A", "CANNOT_COMPLY"]
    at: pydantic.NaiveDatetime
    price: decimal.Decimal
    day: datetime.date
    note: str


@dataclasses.dataclass(frozen=True)
class DLeg:
    at: datetime.datetime
    wait: datetime.timedelta


@dataclasses.dataclass(frozen=True)
class DReply:
    """A dataclass reply with the same kinds of fields."""
    cause: Literal["A", "CANNOT_COMPLY"]
    at: datetime.datetime
    wait: datetime.timedelta
    uid: uuid.UUID
    maybe: Optional[datetime.datetime]
    leg: DLeg
    legs: list[DLeg]


class CP(inv.Checked): ...


class CD(inv.Checked): ...


class CN(inv.Checked): ...


REPLY: dict[str, Any] = {"v": None}


@inv_pd.model_call("R-P", schema=PReply)
def ask_p(inv_feedback: str | None = None) -> CP:
    return REPLY["v"]


@inv.edge("R-P")
def use_p(c: CP) -> str:
    return "used"


@inv.model_call("R-D", schema=DReply)
def ask_d(inv_feedback: str | None = None) -> CD:
    return REPLY["v"]


@inv.edge("R-D")
def use_d(c: CD) -> str:
    return "used"


@inv_pd.model_call("R-N", schema=NaiveReply)
def ask_n(inv_feedback: str | None = None) -> CN:
    return REPLY["v"]


AT = "2024-06-01T12:00:00Z"
LEG = {"at": AT, "wait": "PT5M"}


def p_reply(**over: Any) -> str:
    d: dict[str, Any] = {"cause": "A", "at": AT, "wait": "PT5M", "uid": str(uuid.UUID(int=7)), "col": "r",
                         "maybe": None, "leg": LEG, "legs": [LEG, {"at": "2024-06-02T00:00:00Z", "wait": "PT1H"}],
                         "waits": {"x": "PT30S"}}
    d.update(over)
    return json.dumps(d)


def d_reply(**over: Any) -> str:
    d: dict[str, Any] = {"cause": "A", "at": AT, "wait": "300s", "uid": str(uuid.UUID(int=7)), "maybe": None,
                         "leg": LEG, "legs": [LEG, {"at": "2024-06-02T00:00:00Z", "wait": "1h"}]}
    d.update(over)
    return json.dumps(d)


def model_doc(rule: str, bad: str | list[str]) -> dict[str, Any]:
    """One rule with a model_call enforcer and, for R-P and R-D, an edge enforcer."""
    ask, use = {"R-P": ("ask_p", "use_p"), "R-D": ("ask_d", "use_d"), "R-N": ("ask_n", None)}[rule]
    enf: list[dict[str, Any]] = [{"point": "model_call", "timing": "pre_effect", "mode": "enforce",
                                  "on_violation": "reject"}]
    if use:
        enf.append({"point": "edge", "timing": "pre_effect", "mode": "enforce", "on_violation": "reject",
                    "target": f"{M}.{use}"})
    schema = {"R-P": "PReply", "R-D": "DReply", "R-N": "NaiveReply"}[rule]
    return {"targets": {f"{M}.{ask}": {"effects": "none"}},
            "rules": [{"id": rule, "status": "enforce", "subject": {"target": f"{M}.{ask}"}, "bad": bad,
                       "enforcers": enf, "falsified_by": ["tests/test_fix_round10.py::x"],
                       "model_policy": {"schema": schema, "format_retries": 0, "semantic_resamples": 0,
                                        "feedback": {"format": "none"},
                                        "abstain": {"field": "cause", "value": "CANNOT_COMPLY"}}}]}


@dataclasses.dataclass
class Item:
    qty: int
    price: decimal.Decimal


@dataclasses.dataclass
class Order:
    id: str
    items: list[Item]


@inv.guard("G-ZONE")
def zoned(t: datetime.datetime, tz: str) -> None:
    return None


@inv.guard("G-MIX")
def mixed(price: float, qty: int, n: int, x: float) -> None:
    return None


@inv.guard("G-CART")
def cart(items: list[Item], orders: list[Order], m: dict[str, Item]) -> None:
    return None


GUARDS = {"zoned": "G-ZONE", "mixed": "G-MIX", "cart": "G-CART"}


def doc(fn: str, bad: str | list[str]) -> dict[str, Any]:
    target = f"{M}.{fn}"
    return {"targets": {target: {"effects": "none"}},
            "rules": [{"id": GUARDS[fn], "status": "enforce", "subject": {"target": target}, "bad": bad,
                       "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}],
                       "falsified_by": ["tests/test_fix_round10.py::x"]}]}
