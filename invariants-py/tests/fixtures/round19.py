"""Fixture app for the fix-round-19 runtime tests: a {field: type} model call, its edges (direct and
through agent-graph state), guards and a rule-error route."""
from __future__ import annotations

import dataclasses
import datetime
import json
from typing import Any, Literal, Optional, TypedDict

import pydantic

import invariants as inv

M = "py:round19"
SPEC = "invariant/v0.1"

Plan: dict[str, Any] = {"action": Literal["go", "stop"], "n": int, "tags": list[str]}


class CPlan(inv.Checked): ...


REPLIES: list[Any] = []
SEEN: dict[str, Any] = {}


@inv.model_call("P-1", schema=Plan)
def plan(inv_feedback: str | None = None) -> CPlan:
    return REPLIES.pop(0)


@inv.model_call("P-1", schema=Plan)
async def aplan(inv_feedback: str | None = None) -> CPlan:
    return REPLIES.pop(0)


@inv.edge("P-1")
def act(p: CPlan) -> str:
    SEEN["p"] = p
    return "acted"


class State(TypedDict):
    plan: CPlan
    note: str


@inv.edge("P-1")
def act_state(state: State) -> int:
    SEEN["state"] = state
    return state["plan"].value["n"]


@dataclasses.dataclass
class DState:
    plan: Optional[CPlan]
    note: str = ""


@inv.edge("P-1")
async def act_dstate(state: DState) -> int:
    SEEN["dstate"] = state
    assert state.plan is not None
    return state.plan.value["n"]


class PState(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True)
    plan: CPlan
    other: Optional[CPlan] = None


@inv.edge("P-1", at="state.plan")
def act_pstate(state: PState) -> int:
    SEEN["pstate"] = state
    return state.plan.value["n"]


EDGES = ("act", "act_state", "act_dstate", "act_pstate")


def reply(**over: Any) -> str:
    d: dict[str, Any] = {"action": "go", "n": 1, "tags": ["a"]}
    d.update(over)
    return json.dumps(d)


def model_doc(bad: str = "reply.n > 5") -> dict[str, Any]:
    edges = [{"point": "edge", "target": f"{M}.{e}", "timing": "pre_effect", "mode": "enforce",
              "on_violation": "reject"} for e in EDGES]
    return {"spec": SPEC, "targets": {f"{M}.plan": {"effects": "none"}, f"{M}.aplan": {"effects": "none"}},
            "rules": [{"id": "P-1", "status": "enforce", "subject": {"target": f"{M}.plan"}, "bad": bad,
                       "enforcers": [{"point": "model_call", "timing": "pre_effect", "mode": "enforce",
                                      "on_violation": "reject"}, *edges],
                       "model_policy": {"format_retries": 0, "semantic_resamples": 0,
                                        "feedback": {"format": "none"}},
                       "falsified_by": ["tests/test_fix_round19_runtime.py"]}]}


# --- guards --------------------------------------------------------------------------------------
@inv.guard("G-1")
def spend(amount: int) -> int:
    return amount


@inv.provider("count")
def count(v: int) -> int:
    return v


class Box:
    def __init__(self) -> None:
        self.size = 1

    @inv.guard("G-OLD")
    def grow(self, by: int) -> int:
        self.size += by
        return self.size


def guard_doc(rid: str, fn: str, bad: str, *, target: bool = True, timing: str = "pre_effect",
              on_violation: str = "reject", bind: Any = None, providers: Any = None,
              point: str = "decorator") -> dict[str, Any]:
    r: dict[str, Any] = {"id": rid, "status": "enforce", "bad": bad, "falsified_by": ["t"],
                         "enforcers": [{"point": point, "timing": timing, "mode": "enforce",
                                        "on_violation": on_violation}]}
    if target:
        r["subject"] = {"target": f"{M}.{fn}"}
    if bind is not None:
        r["bind"] = bind
    doc: dict[str, Any] = {"spec": SPEC, "targets": {f"{M}.{fn}": {"effects": "none"}}, "rules": [r]}
    if providers:
        doc["providers"] = providers
    return doc


AWARE = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
