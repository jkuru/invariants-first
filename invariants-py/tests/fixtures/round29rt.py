"""Fixture app for the fix-round-29 runtime tests: @inv.guard, @inv.edge and @inv.model_call over the
four shapes (sync def, async def, a sync functools.wraps wrapper of an async def, an async wrapper of
a sync def), each reading an async provider, and providers to switch between for the ablation digest."""
from __future__ import annotations

import dataclasses
import datetime
import functools
import typing
from typing import Any

import invariants as inv

M = "py:round29rt"
SHAPES = ("sync", "async", "sync_of_async", "async_of_sync")


@inv.provider("acap")
async def acap() -> int:
    return 100


@inv.provider("cap")
def cap_a() -> int:
    return 100


@inv.provider("cap")
def cap_b() -> int:
    return 10**9


@dataclasses.dataclass(frozen=True)
class Reply:
    action: typing.Literal["REFUND", "UNSURE"]
    amount: int


class CheckedReply(inv.Checked):
    __slots__ = ()


REPLIES: list[Any] = []


def sync_wrap(fn: Any) -> Any:
    """A plain sync wrapper (no inspect.markcoroutinefunction): over an async def it returns a coroutine."""
    @functools.wraps(fn)
    def w(*a: Any, **kw: Any) -> Any:
        return fn(*a, **kw)
    return w


def async_wrap(fn: Any) -> Any:
    """An async wrapper of a sync def: calling it gives a coroutine."""
    @functools.wraps(fn)
    async def w(*a: Any, **kw: Any) -> Any:
        return fn(*a, **kw)
    return w


# --- guards ------------------------------------------------------------------------------------------
@inv.guard("G-sync")
def g_sync(n: int) -> int:
    return n * 10


@inv.guard("G-async")
async def g_async(n: int) -> int:
    return n * 10


@inv.guard("G-sync_of_async")
@sync_wrap
async def g_sync_of_async(n: int) -> int:
    return n * 10


@inv.guard("G-async_of_sync")
@async_wrap
def g_async_of_sync(n: int) -> int:
    return n * 10


# --- edges -------------------------------------------------------------------------------------------
@inv.edge("E-sync")
def e_sync(p: CheckedReply) -> int:
    return typing.cast(int, p.amount)


@inv.edge("E-async")
async def e_async(p: CheckedReply) -> int:
    return typing.cast(int, p.amount)


@inv.edge("E-sync_of_async")
@sync_wrap
async def e_sync_of_async(p: CheckedReply) -> int:
    return typing.cast(int, p.amount)


@inv.edge("E-async_of_sync")
@async_wrap
def e_async_of_sync(p: CheckedReply) -> int:
    return typing.cast(int, p.amount)


# --- model calls -------------------------------------------------------------------------------------
@inv.model_call("M-sync", schema=Reply, returns=CheckedReply)
def m_sync(order: str, inv_feedback: str | None = None) -> str:
    return REPLIES.pop(0)


@inv.model_call("M-async", schema=Reply, returns=CheckedReply)
async def m_async(order: str, inv_feedback: str | None = None) -> str:
    return REPLIES.pop(0)


@inv.model_call("M-sync_of_async", schema=Reply, returns=CheckedReply)
@sync_wrap
async def m_sync_of_async(order: str, inv_feedback: str | None = None) -> str:
    return REPLIES.pop(0)


@inv.model_call("M-async_of_sync", schema=Reply, returns=CheckedReply)
@async_wrap
def m_async_of_sync(order: str, inv_feedback: str | None = None) -> str:
    return REPLIES.pop(0)


PROVIDERS = {"acap": {"from": f"{M}.acap", "type": "int", "args": {}}}


def guard_doc(shape: str, timing: str) -> dict[str, Any]:
    tk = f"{M}.g_{shape}"
    bad = "args.n > acap" if timing == "pre_effect" else "result > acap"
    return {"targets": {tk: {"effects": "none"}}, "providers": PROVIDERS,
            "rules": [{"id": f"G-{shape}", "status": "enforce", "subject": {"target": tk}, "bad": bad,
                       "falsified_by": ["t::x"],
                       "enforcers": [{"point": "decorator", "timing": timing, "mode": "enforce",
                                      "on_violation": "reject", "view": {"acap": "provider.acap"}}]}]}


def _model_rule(rid: str, target: str, bad: str, enforcers: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": rid, "status": "enforce", "subject": {"target": target}, "bad": bad,
            "model_policy": {"schema": "Reply", "format_retries": 0, "semantic_resamples": 0,
                             "abstain": {"field": "action", "value": "UNSURE"}},
            "falsified_by": ["t::x"], "enforcers": enforcers}


def edge_doc(shape: str) -> dict[str, Any]:
    src, tk = f"{M}.m_sync", f"{M}.e_{shape}"
    return {"targets": {src: {"effects": "none"}, tk: {"effects": "none"}}, "providers": PROVIDERS,
            "rules": [_model_rule(f"E-{shape}", src, "reply.amount > acap", [
                {"point": "model_call", "timing": "post_effect", "mode": "off", "on_violation": "reject"},
                {"point": "edge", "target": tk, "timing": "pre_effect", "mode": "enforce",
                 "view": {"acap": "provider.acap"}}])]}


def model_doc(shape: str) -> dict[str, Any]:
    tk = f"{M}.m_{shape}"
    return {"targets": {tk: {"effects": "none"}}, "providers": PROVIDERS,
            "rules": [_model_rule(f"M-{shape}", tk, "reply.amount > acap", [
                {"point": "model_call", "timing": "post_effect", "mode": "enforce", "on_violation": "reject",
                 "view": {"acap": "provider.acap"}}])]}


@inv.guard("py:round29rt.refund")
def refund(amount: int) -> int:
    return amount


@inv.guard("OPEN")
def opening(t: datetime.datetime, tz: str | None) -> None:
    return None


def digest_doc(provider: str = "cap_a", effects: str = "external") -> dict[str, Any]:
    return {"targets": {f"{M}.refund": {"effects": effects}},
            "providers": {"cap": {"from": f"{M}.{provider}", "type": "int", "args": {}}},
            "rules": [{"id": "R-1", "statement": "refunds are small", "guard": f"{M}.refund",
                       "bad": "args.amount > cap", "tests": ["tests/test_x.py::test_big"]}]}
