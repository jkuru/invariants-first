"""Fixture app for the fix-round-11 tests (certification round 9): whole-value equality with a
nested NaN, model_of() and edges, pydantic API names, ASCII digits and zone names, map keys."""
from __future__ import annotations

import dataclasses
import datetime
import json
from typing import Any, Literal, Optional

import pydantic

import invariants as inv
from invariants.integrations import pydantic as inv_pd

M = "py:round11"


@dataclasses.dataclass
class Sub:
    x: float


@dataclasses.dataclass
class It:
    q: int
    x: float
    subs: list[Sub]


@inv.guard("G-WHOLE")
def whole(items: list[It], m: dict[str, It], k: str, anyv: Any, xs: list[float], mp: dict[str, float]) -> None:
    return None


@inv.guard("G-ZONE")
def zoned(t: datetime.datetime, tz: str, s: str) -> None:
    return None


@inv.guard("G-KEYS")
def keyed(m: dict[int, str], d: float, b: bool, xs: list[int]) -> None:
    return None


class Leg(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    at: datetime.datetime
    score: float = 0.0


class Rep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    cause: Literal["A", "CANNOT_COMPLY"]
    at: datetime.datetime
    legs: list[Leg]
    note: Optional[str] = None

    @property
    def first(self) -> str:             # a user property stays readable
        return "f"


@inv.guard("G-PARG")
def parg(r: Rep) -> None:
    return None


class CR(inv.Checked): ...


REPLY: dict[str, Any] = {"v": None}
SEEN: dict[str, Any] = {}


@inv_pd.model_call("R-M", schema=Rep)
def ask(inv_feedback: str | None = None) -> CR:
    return REPLY["v"]


@inv.edge("R-M")
def use(c: CR) -> str:
    SEEN["c"] = c
    SEEN["m"] = inv_pd.model_of(c)
    SEEN["at"] = c.at
    return "used"


@dataclasses.dataclass(frozen=True)
class DReply:
    cause: Literal["A", "CANNOT_COMPLY"]
    wait: datetime.timedelta


class CD(inv.Checked): ...


@inv.model_call("R-D", schema=DReply)
def ask_d(inv_feedback: str | None = None) -> CD:
    return REPLY["v"]


@inv.edge("R-D")
def use_d(c: CD) -> Any:
    SEEN["d"] = c
    return c


AT = "2024-06-01T12:00:00Z"


def reply(**over: Any) -> str:
    d: dict[str, Any] = {"cause": "A", "at": AT, "legs": [{"at": AT, "score": 1.0}], "note": None}
    d.update(over)
    return json.dumps(d)


GUARDS = {"whole": "G-WHOLE", "zoned": "G-ZONE", "keyed": "G-KEYS", "parg": "G-PARG"}


def doc(fn: str, bad: str | list[str]) -> dict[str, Any]:
    target = f"{M}.{fn}"
    return {"targets": {target: {"effects": "none"}},
            "rules": [{"id": GUARDS[fn], "status": "enforce", "subject": {"target": target}, "bad": bad,
                       "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}],
                       "falsified_by": ["tests/test_fix_round11.py::x"]}]}


def model_doc(rule: str, bad: str | list[str]) -> dict[str, Any]:
    """A model_call rule with an edge enforcer on its consumer."""
    ask_fn, use_fn, schema = {"R-M": ("ask", "use", "Rep"), "R-D": ("ask_d", "use_d", "DReply")}[rule]
    enf: list[dict[str, Any]] = [
        {"point": "model_call", "timing": "pre_effect", "mode": "enforce", "on_violation": "reject"},
        {"point": "edge", "timing": "pre_effect", "mode": "enforce", "on_violation": "reject",
         "target": f"{M}.{use_fn}"}]
    return {"targets": {f"{M}.{ask_fn}": {"effects": "none"}},
            "rules": [{"id": rule, "status": "enforce", "subject": {"target": f"{M}.{ask_fn}"}, "bad": bad,
                       "enforcers": enf, "falsified_by": ["tests/test_fix_round11.py::x"],
                       "model_policy": {"schema": schema, "format_retries": 0, "semantic_resamples": 0,
                                        "feedback": {"format": "none"},
                                        "abstain": {"field": "cause", "value": "CANNOT_COMPLY"}}}]}
