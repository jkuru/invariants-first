"""Fixture app for the fix-round-8 tests (certification round 6): strict pydantic replies, non-finite
arithmetic, call arity, registry ablation, value-free parse errors."""
from __future__ import annotations

import dataclasses
from typing import Any, Literal

import pydantic

import invariants as inv

M = "py:round8"
SPEC = "invariant/v0.1"
TESTS = ["tests/test_fix_round8.py"]


class Top(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    cause: Literal["A", "B"]
    confidence: float


class Nested(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    inner: Top
    xs: list[float]
    m: dict[str, float]


class Lax(pydantic.BaseModel):
    """Opts out of strict reply validation (pydantic's lax coercion)."""
    model_config = pydantic.ConfigDict(extra="forbid", strict=False)
    cause: Literal["A", "B"]
    confidence: float


DictReply: dict[str, Any] = {"cause": Literal["A", "B"], "confidence": float, "tags": dict[str, float]}


@dataclasses.dataclass(frozen=True)
class Ratio:
    a: float
    b: float


class OutT(inv.Checked):
    __slots__ = ()


class OutN(inv.Checked):
    __slots__ = ()


class OutD(inv.Checked):
    __slots__ = ()


class OutR(inv.Checked):
    __slots__ = ()


REPLIES: list[Any] = []
FEEDBACK: list[Any] = []        # inv_feedback values the model functions received


@inv.model_call("P-T", schema=Top)
def tmodel(prompt: str, inv_feedback: str | None = None) -> OutT:
    FEEDBACK.append(inv_feedback)
    return REPLIES.pop(0)


@inv.model_call("P-N", schema=Nested)
def nmodel(prompt: str) -> OutN:
    return REPLIES.pop(0)


@inv.model_call("P-D", schema=DictReply)
def dmodel(prompt: str, inv_feedback: str | None = None) -> OutD:
    FEEDBACK.append(inv_feedback)
    return REPLIES.pop(0)


@inv.model_call("P-R", schema=Ratio)
def rmodel(prompt: str) -> OutR:
    return REPLIES.pop(0)


@inv.edge("P-T")
def tuse(c: OutT) -> Any:
    return c.value


@inv.edge("P-N")
def nuse(c: OutN) -> Any:
    return c.value


@inv.guard("G-A")
def arith(x: float, y: float) -> float:
    return x


def model_rule(rid: str, target: str, bad: Any, edge: str | None = None, retries: int = 0) -> dict[str, Any]:
    enf = [{"point": "model_call", "timing": "post_effect", "mode": "enforce"}]
    if edge:
        enf.append({"point": "edge", "target": f"{M}.{edge}", "timing": "pre_effect", "mode": "enforce"})
    return {"id": rid, "status": "enforce", "subject": {"target": f"{M}.{target}"}, "bad": bad,
            "model_policy": {"schema": "S", "format_retries": retries, "semantic_resamples": 0,
                             "feedback": {"format": "parser_error" if retries else "none", "semantic": "none"}},
            "enforcers": enf, "falsified_by": TESTS}


def doc(arith_bad: Any = "args.x / args.y > 1.0") -> dict[str, Any]:
    return {"spec": SPEC,
            "targets": {f"{M}.{t}": {"effects": "none"} for t in ("tmodel", "nmodel", "dmodel", "rmodel", "arith")},
            "rules": [model_rule("P-T", "tmodel", "reply.cause == 'B'", "tuse", retries=1),
                      model_rule("P-N", "nmodel", "reply.inner.cause == 'B'", "nuse"),
                      model_rule("P-D", "dmodel", "reply.cause == 'B'", retries=1),
                      model_rule("P-R", "rmodel", "reply.a / reply.b < 0.0 || reply.a / reply.b > 1.0"),
                      {"id": "G-A", "status": "enforce", "subject": {"target": f"{M}.arith"}, "bad": arith_bad,
                       "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}],
                       "falsified_by": TESTS}]}


def one_rule(bad: str) -> dict[str, Any]:
    """A rule file with only G-A, its bad replaced."""
    return {"spec": SPEC, "targets": {f"{M}.arith": {"effects": "none"}},
            "rules": [doc(bad)["rules"][-1]]}
