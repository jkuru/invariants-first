"""Fixture app for the fix-round-24 runtime tests: guarded targets read by several clauses, list-like
arguments, a sync wrapper over an async def, and edges that receive a Checked in a state."""
from __future__ import annotations

import asyncio
import collections.abc as abc
import dataclasses
import datetime
import functools
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, RootModel

import invariants as inv

M = "py:round24"
SPEC = "invariant/v0.1"
DONE: list[Any] = []


@inv.guard("T-PAY")
def pay(amount: int, note: str) -> str:
    DONE.append(("pay", amount))
    return "paid"


@inv.guard("T-ORDER")
def order(items: list[dict[str, Any]], ref: int) -> str:
    DONE.append(("order", len(items)))
    return "placed"


class Line(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str
    qty: int


class Lines(RootModel[list[Line]]): ...


class Batch(abc.Sequence):              # a list-like class that is not a list
    def __init__(self, items: Any) -> None:
        self.items, self.source = list(items), "api"

    def __getitem__(self, i: Any) -> Any:
        return self.items[i]

    def __len__(self) -> int:
        return len(self.items)


@inv.guard("T-BULK")
def bulk(lines: Lines) -> int:
    DONE.append(("bulk", len(lines.root)))
    return len(lines.root)


@inv.guard("T-ANY")
def bulk_any(xs: Any) -> str:
    DONE.append(("any", len(xs)))
    return "ok"


@inv.guard("T-BATCH")
def bulk_batch(xs: Batch) -> str:
    return "ok"


@inv.guard("T-WHEN")
def when(t: datetime.datetime) -> str:
    DONE.append(("when", t))
    return "done"


# --- a sync wrapper over an async def (functools.wraps, no markcoroutinefunction) ------------------
def retry(fn: Any) -> Any:
    @functools.wraps(fn)
    def w(*a: Any, **k: Any) -> Any:
        return fn(*a, **k)
    return w


@dataclasses.dataclass
class Refund:
    amount: int


@inv.guard("T-REFUND")
@retry
async def refund(amount: int) -> Refund:
    DONE.append(("refund", amount))
    return Refund(amount)


@inv.guard("T-FUT")
def refund_future(amount: int) -> Any:
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(Refund(amount))
    return fut


# --- edges --------------------------------------------------------------------------------------
class CPlan(inv.Checked): ...


REPLY = ['{"action": "read", "n": 1}']


@inv.model_call("P", schema={"action": str, "n": int}, returns=CPlan)
def plan(inv_feedback: str | None = None) -> str:
    return REPLY[0]


class State(TypedDict):
    plan: CPlan


@dataclasses.dataclass
class DState:
    plan: CPlan


SEEN: list[Any] = []


@inv.edge("P")
def act(p: CPlan) -> None:
    SEEN.append((type(p), dict(p.value)))


@inv.edge("P")
def act_state(state: State) -> None:
    SEEN.append((type(state), dict(state["plan"].value)))


@inv.edge("P")
def act_dc(state: DState) -> None:
    SEEN.append((type(state), dict(state.plan.value)))


def doc(fn: str, bad: str | list[dict[str, str]], **enforcer: Any) -> dict[str, Any]:
    """A one-rule document guarding round24.<fn> (rule id T-<FN>)."""
    rid = "T-" + {"bulk_any": "ANY", "bulk_batch": "BATCH", "refund_future": "FUT"}.get(fn, fn.upper())
    timing = enforcer.pop("timing", "pre_effect")
    enf = {"point": "decorator", "timing": timing, "mode": "enforce", "on_violation": "reject", **enforcer}
    return {"spec": SPEC, "targets": {f"{M}.{fn}": {"effects": "none"}},
            "rules": [{"id": rid, "status": "enforce", "bad": bad, "falsified_by": ["t"],
                       "subject": {"target": f"{M}.{fn}"}, "enforcers": [enf]}]}


def plan_doc(bad: str = "reply.action == 'delete' || reply.n > 10") -> dict[str, Any]:
    me = {"point": "model_call", "timing": "pre_effect", "mode": "enforce", "on_violation": "reject",
          "on_error": "fail_closed"}
    edges = [{"point": "edge", "timing": "pre_effect", "mode": "enforce", "on_violation": "reject",
              "on_error": "fail_closed", "target": f"{M}.{fn}"} for fn in ("act", "act_state", "act_dc")]
    return {"spec": SPEC, "targets": {f"{M}.plan": {"effects": "none"},
                                      **{f"{M}.{fn}": {"effects": "external"} for fn in ("act", "act_state", "act_dc")}},
            "rules": [{"id": "P", "status": "enforce", "bad": bad, "falsified_by": ["t"],
                       "subject": {"target": f"{M}.plan"}, "enforcers": [me, *edges],
                       "model_policy": {"format_retries": 0}}]}
