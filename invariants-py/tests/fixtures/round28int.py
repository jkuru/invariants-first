"""Fixture app for the fix-round-28 runtime tests: a model call over a sync functools.wraps wrapper
of an async def, its edge, a monitor rule, and FastAPI handlers whose response model has a field
named like a Response attribute."""
from __future__ import annotations

import dataclasses
import functools
import typing
from typing import Any

import pydantic
from fastapi import FastAPI
from fastapi.responses import JSONResponse

import invariants as inv
from invariants.integrations import fastapi as inv_fastapi

M = "py:round28int"
REPLIES: list[Any] = []
CALLS: list[Any] = []


@dataclasses.dataclass(frozen=True)
class Reply:
    action: typing.Literal["REFUND", "UNSURE"]
    amount: int


class CheckedReply(inv.Checked):
    __slots__ = ()


def logged(fn: Any) -> Any:
    """A plain sync wrapper (no inspect.markcoroutinefunction): over an async def it returns a coroutine."""
    @functools.wraps(fn)
    def w(*a: Any, **kw: Any) -> Any:
        return fn(*a, **kw)
    return w


@inv.model_call("W-1", schema=Reply, returns=CheckedReply)
@logged
async def wplan(order: str, inv_feedback: str | None = None) -> str:
    CALLS.append(inv_feedback)
    return REPLIES.pop(0)


@inv.model_call("W-1", schema=Reply, returns=CheckedReply)
def plan(order: str, inv_feedback: str | None = None) -> Any:
    CALLS.append(inv_feedback)
    return REPLIES.pop(0)


@inv.edge("W-1")
def execute(p: CheckedReply) -> int:
    return p.amount


def model_doc(target: str = "wplan", model_mode: str = "enforce") -> dict[str, Any]:
    return {"targets": {f"{M}.{target}": {"effects": "none"}},
            "rules": [{"id": "W-1", "status": "enforce", "subject": {"target": f"{M}.{target}"},
                       "bad": "reply.amount > 100",
                       "model_policy": {"schema": "Reply", "format_retries": 1, "semantic_resamples": 1,
                                        "abstain": {"field": "action", "value": "UNSURE"}},
                       "enforcers": [{"point": "model_call", "timing": "post_effect", "mode": model_mode,
                                      "on_violation": "reject"},
                                     {"point": "edge", "target": f"{M}.execute", "timing": "pre_effect",
                                      "mode": "enforce"}],
                       "falsified_by": ["tests/test_fix_round28_int.py::test_x"]}]}


def monitor_doc() -> dict[str, Any]:
    return {"rules": [{"id": "MON-1", "status": "detect", "bad": "args.n < 0",
                       "enforcers": [{"point": "monitor", "timing": "post_effect", "mode": "detect"}]}]}


class Receipt(pydantic.BaseModel):
    total: int
    status_code: int


app = FastAPI()


@app.get("/r/{n}")
@inv_fastapi.guard(f"{M}.receipt")
def receipt(n: int) -> Receipt:
    if n < 0:
        return JSONResponse({"total": n, "status_code": 200}, status_code=200)   # type: ignore[return-value]
    return Receipt(total=n, status_code=201 if n == 7 else 200)


@app.get("/ar/{n}")
@inv_fastapi.guard(f"{M}.areceipt", response_model=Receipt)
async def areceipt(n: int) -> Any:
    if n < 0:
        return JSONResponse({"total": n, "status_code": 200}, status_code=200)
    return {"total": n, "status_code": 201 if n == 7 else 200}


def shop_doc(bad: str, on_error: str = "fail_closed") -> dict[str, Any]:
    rules = [{"id": f"SH-{fn}", "status": "enforce", "subject": {"target": f"{M}.{fn}"}, "bad": bad,
              "enforcers": [{"point": "decorator", "timing": "post_effect", "mode": "enforce", "on_error": on_error}],
              "falsified_by": ["tests/test_fix_round28_int.py::test_shop"]} for fn in ("receipt", "areceipt")]
    return {"targets": {f"{M}.receipt": {"effects": "none"}, f"{M}.areceipt": {"effects": "none"}}, "rules": rules}
