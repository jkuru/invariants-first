"""Fixture app for the fix-round-26 runtime tests: a guarded target taking a str subclass, a model
call that can abstain (sync and async) with its edge, and FastAPI handlers that return a Response."""
from __future__ import annotations

import dataclasses
import typing
from typing import Any

import invariants as inv

M = "py:round26rt"
DONE: list[Any] = []


class Role(str):
    """A str subclass, as an app's own string type (or a library's) arrives in a guarded call."""


@inv.guard("py:round26rt.delete_account")
def delete_account(user: str, role: str) -> str:
    DONE.append(("delete", user))
    return "deleted"


@dataclasses.dataclass(frozen=True)
class Reply:
    action: typing.Literal["REFUND", "ESCALATE_TO_HUMAN"]
    amount: int


class CheckedReply(inv.Checked):
    __slots__ = ()


REPLIES: list[str] = []


@inv.model_call("M-1", schema=Reply, returns=CheckedReply)
def plan(order: str, inv_feedback: str | None = None) -> str:
    return REPLIES.pop(0)


@inv.model_call("M-1", schema=Reply, returns=CheckedReply)
async def aplan(order: str, inv_feedback: str | None = None) -> str:
    return REPLIES.pop(0)


@inv.edge("M-1")
def execute(p: CheckedReply, order: str) -> int:
    DONE.append(("execute", p.amount))
    return p.amount


def abstain_doc(status: str, model_mode: str = "enforce", edge_mode: str = "enforce") -> dict[str, Any]:
    rule: dict[str, Any] = {
        "id": "M-1", "status": status, "subject": {"target": f"{M}.plan"}, "bad": "reply.amount > 100",
        "model_policy": {"schema": "Reply", "format_retries": 1, "semantic_resamples": 1,
                         "abstain": {"field": "action", "value": "ESCALATE_TO_HUMAN"}},
        "enforcers": [{"point": "model_call", "timing": "post_effect", "mode": model_mode, "on_violation": "reject"},
                      {"point": "edge", "target": f"{M}.execute", "timing": "pre_effect", "mode": edge_mode}]}
    if status == "enforce":
        rule["falsified_by"] = ["tests/test_fix_round26_runtime.py::test_abstain"]
    return {"targets": {f"{M}.plan": {"effects": "none"}}, "rules": [rule]}


def role_doc() -> dict[str, Any]:
    return {"rules": [{"id": "ROLE-1", "status": "enforce", "subject": {"target": f"{M}.delete_account"},
                       "bad": "args.role == 'guest'",
                       "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}],
                       "falsified_by": ["tests/test_fix_round26_runtime.py::test_role"]}]}
