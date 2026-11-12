"""Fixture app for the fix-round-7 tests (certification round 5): NaN, function names, edges, ablation."""
from __future__ import annotations

import dataclasses
from typing import Any, Literal

import pydantic

import invariants as inv

M = "py:round7"
SPEC = "invariant/v0.1"
TESTS = ["tests/test_fix_round7.py"]

DictReply: dict[str, Any] = {"cause": Literal["A", "B"], "confidence": float}


@dataclasses.dataclass(frozen=True)
class DcReply:
    cause: Literal["A", "B"]
    confidence: float


class PdReply(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    cause: Literal["A", "B"]
    confidence: float


class OutD(inv.Checked):
    __slots__ = ()


class OutC(inv.Checked):
    __slots__ = ()


class OutP(inv.Checked):
    __slots__ = ()


REPLIES: list[Any] = []


@inv.model_call("N-D", schema=DictReply)
def dmodel(prompt: str) -> OutD:
    return REPLIES.pop(0)


@inv.model_call("N-C", schema=DcReply)
def cmodel(prompt: str) -> OutC:
    return REPLIES.pop(0)


@inv.model_call("N-P", schema=PdReply)
def pmodel(prompt: str) -> OutP:
    return REPLIES.pop(0)


@inv.edge("N-D")
def duse(c: OutD) -> Any:
    return c.value


@inv.edge("N-C")
def cuse(c: OutC) -> Any:
    return c.value


@inv.edge("N-P")
def puse(c: OutP) -> Any:
    return c.value


MODELS = {"dict": (dmodel, duse, OutD, "N-D"), "dataclass": (cmodel, cuse, OutC, "N-C"),
          "pydantic": (pmodel, puse, OutP, "N-P")}

RANGE = "reply.confidence < 0.0 || reply.confidence > 1.0"     # the usual bound check, NaN-unsafe


def model_rule(rid: str, target: str, edge_target: str) -> dict[str, Any]:
    return {"id": rid, "status": "enforce", "subject": {"target": f"{M}.{target}"}, "bad": RANGE,
            "model_policy": {"schema": "S", "format_retries": 0, "semantic_resamples": 0,
                             "feedback": {"format": "none", "semantic": "none"}},
            "enforcers": [{"point": "model_call", "timing": "post_effect", "mode": "enforce"},
                          {"point": "edge", "target": f"{M}.{edge_target}", "timing": "pre_effect",
                           "mode": "enforce"}],
            "falsified_by": TESTS}


# --- guarded functions ---------------------------------------------------------------------------
@inv.guard("G-1")
def score(x: float, xs: list[float]) -> float:
    return x


@inv.guard("G-2")
def compute(x: float) -> float:
    return x * 2


@inv.guard("G-3")
def lookup(m: Any, k: Any, s: Any, p: Any) -> int:
    return 1


def guard_rule(rid: str, target: str, bad: Any, timing: str = "pre_effect") -> dict[str, Any]:
    return {"id": rid, "status": "enforce", "subject": {"target": f"{M}.{target}"}, "bad": bad,
            "enforcers": [{"point": "decorator", "timing": timing, "mode": "enforce"}],
            "falsified_by": TESTS}


def doc(g1: Any = "args.x < 0.0 || args.x > 1.0", g3: Any = "args.m[args.k] > 0") -> dict[str, Any]:
    return {"spec": SPEC,
            "targets": {f"{M}.{t}": {"effects": "none"} for t in
                        ("dmodel", "cmodel", "pmodel", "score", "compute", "lookup")},
            "rules": [model_rule("N-D", "dmodel", "duse"), model_rule("N-C", "cmodel", "cuse"),
                      model_rule("N-P", "pmodel", "puse"),
                      guard_rule("G-1", "score", g1),
                      guard_rule("G-2", "compute", "result > 10.0", timing="post_effect"),
                      guard_rule("G-3", "lookup", g3)]}


def one_rule(bad: str) -> dict[str, Any]:
    """A rule file with only G-1, its bad replaced."""
    return {"spec": SPEC, "targets": {f"{M}.score": {"effects": "none"}},
            "rules": [guard_rule("G-1", "score", bad)]}


# --- an edge-only rule (refused at load) ----------------------------------------------------------
@inv.edge("E-1")
def edge_only(c: OutD) -> Any:
    return c.value


EDGE_ONLY = {"spec": SPEC, "rules": [
    {"id": "E-1", "status": "enforce", "subject": {"target": f"{M}.edge_only"}, "bad": "reply.cause == 'B'",
     "enforcers": [{"point": "edge", "target": f"{M}.edge_only", "timing": "pre_effect", "mode": "enforce"}],
     "falsified_by": TESTS}]}


# --- FastAPI: a NaN body ----------------------------------------------------------------------------
def shop_app() -> Any:
    from fastapi import FastAPI
    from invariants.integrations import fastapi as inv_fastapi

    app = FastAPI()

    @app.post("/orders", operation_id="createOrder")
    @inv_fastapi.guard("py:round7.create_order")
    def create_order(order: Order) -> dict[str, Any]:
        return {"ok": True}
    inv_fastapi.install(app)
    return app


class Order(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    sku: str
    quantity: float


@inv.guard("S-1")
def create_order(order: Order) -> dict[str, Any]:       # the key the route guard names
    return {"ok": True}


SHOP = {"spec": SPEC, "targets": {f"{M}.create_order": {"effects": "external"}},
        "rules": [{"id": "S-1", "status": "enforce", "subject": {"target": f"{M}.create_order"},
                   "bad": [{"when": "args.order.quantity > 10.0", "message": "over stock"},
                           {"when": "args.order.quantity <= 0.0", "message": "non-positive"}],
                   "enforcers": [{"point": "middleware", "timing": "pre_effect", "mode": "enforce"}],
                   "falsified_by": TESTS}]}
