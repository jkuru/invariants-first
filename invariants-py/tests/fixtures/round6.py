"""Fixture app for the fix-round-6 tests: hostile model replies, abstain and edges for every schema kind."""
from __future__ import annotations

import dataclasses
import typing
from typing import Any, Literal

import pydantic

import invariants as inv

M = "py:round6"
SPEC = "invariant/v0.1"
Cause = Literal["OK", "BAD", "CANNOT_COMPLY"]

DictReply: dict[str, Any] = {"cause": Cause, "v": int, "note": str}


@dataclasses.dataclass(frozen=True)
class DcReply:
    cause: Cause
    v: int
    note: str


class PdReply(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    cause: Cause
    v: int
    note: str
    tags: list[str]


class CheckedD(inv.Checked):
    __slots__ = ()


class CheckedC(inv.Checked):
    __slots__ = ()


class CheckedP(inv.Checked):
    __slots__ = ()


REPLIES: list[Any] = []
CONSUMED: list[Any] = []


def _next() -> Any:
    return REPLIES.pop(0)


@inv.model_call("D-1", schema=DictReply)
def dmodel(prompt: str) -> CheckedD:
    return _next()


@inv.model_call("D-2", schema=DcReply)
def cmodel(prompt: str) -> CheckedC:
    return _next()


@inv.model_call("D-3", schema=PdReply)
def pmodel(prompt: str) -> CheckedP:
    return _next()


@inv.model_call("D-1", schema=DictReply)
async def admodel(prompt: str) -> CheckedD:
    return _next()


@inv.edge("D-1")
def dconsume(r: CheckedD) -> Any:
    CONSUMED.append(r)
    return r


@inv.edge("D-2")
def cconsume(r: CheckedC) -> Any:
    CONSUMED.append(r)
    return r


@inv.edge("D-3")
def pconsume(r: CheckedP) -> Any:
    CONSUMED.append(r)
    return r


MODELS = {"dict": (dmodel, dconsume, CheckedD, "D-1"), "dataclass": (cmodel, cconsume, CheckedC, "D-2"),
          "pydantic": (pmodel, pconsume, CheckedP, "D-3")}


def rule(rid: str, target: str, edge_target: str, retries: int = 1, on_violation: str = "reject",
         abstain: Any = None, bad: str = "reply.v > 100") -> dict[str, Any]:
    pol: dict[str, Any] = {"format_retries": retries, "semantic_resamples": 0, "feedback": {"format": "none"},
                           "abstain": abstain or {"field": "cause", "value": "CANNOT_COMPLY"}}
    return {"id": rid, "status": "enforce", "subject": {"target": f"{M}.{target}"}, "bad": bad,
            "enforcers": [{"point": "model_call", "timing": "pre_effect", "mode": "enforce",
                           "on_violation": on_violation},
                          {"point": "edge", "target": f"{M}.{edge_target}", "timing": "pre_effect",
                           "mode": "enforce", "on_violation": "reject"}],
            "model_policy": pol, "falsified_by": ["tests/test_fix_round6.py"]}


def doc(retries: int = 1, on_violation: str = "reject", **over: Any) -> dict[str, Any]:
    rules = [rule("D-1", "dmodel", "dconsume", retries, on_violation),
             rule("D-2", "cmodel", "cconsume", retries, on_violation),
             rule("D-3", "pmodel", "pconsume", retries, on_violation)]
    for r in rules:
        r.update(over.get(r["id"], {}))
    return {"spec": SPEC, "targets": {f"{M}.{t}": {"effects": "none"} for t in ("dmodel", "cmodel", "pmodel")},
            "rules": rules}


# --- a property whose exception message holds a secret ------------------------------------------
SECRET = "sk-live-0123456789"


class Leaky:
    @property
    def boom(self) -> int:
        raise RuntimeError(f"token={SECRET}")


@inv.guard("L-1")
def leaky(obj: Any) -> int:
    return 1


def leaky_doc() -> dict[str, Any]:
    return {"spec": SPEC, "targets": {f"{M}.leaky": {"effects": "none"}},
            "rules": [{"id": "L-1", "status": "enforce", "subject": {"target": f"{M}.leaky"},
                       "bad": "args.obj.boom > 1",
                       "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}],
                       "falsified_by": ["tests/test_fix_round6.py"]}]}


