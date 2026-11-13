"""Fixture app for the fix-round-23 runtime tests (cel-expr): guarded targets, a model call wrapped by a
decorator without functools.wraps, and Enum fields in closed schemas."""
from __future__ import annotations

import dataclasses
import enum
import json
from typing import Any

import invariants as inv

M = "py:round23"
SPEC = "invariant/v0.1"
SHIPPED: list[Any] = []


@inv.guard("T-SHIP")
def ship(qtys: list[int], total: int, fee: int) -> str:
    SHIPPED.append((qtys, total, fee))
    return "shipped"


@inv.guard("T-PAY")
def pay(cents: int, limit: float) -> int:
    return cents


@inv.guard("T-TEXT")
def text(s: Any, t: Any = "") -> None:
    return None


def doc(fn: str, bad: str | list[dict[str, str]], **enforcer: Any) -> dict[str, Any]:
    """A one-rule document guarding round23.<fn> (rule id T-<FN>)."""
    rid = {"ship": "T-SHIP", "pay": "T-PAY", "text": "T-TEXT"}[fn]
    enf = {"point": "decorator", "timing": "pre_effect", "mode": "enforce", "on_violation": "reject", **enforcer}
    return {"spec": SPEC, "targets": {f"{M}.{fn}": {"effects": "none"}},
            "rules": [{"id": rid, "status": "enforce", "bad": bad, "falsified_by": ["t"],
                       "subject": {"target": f"{M}.{fn}"}, "enforcers": [enf]}]}


# --- M4: an edge whose model_call target is wrapped by a decorator without functools.wraps ----------
class CPlan(inv.Checked): ...


REPLIES: list[Any] = []
SEEN: list[Any] = []


def traced(fn: Any) -> Any:                 # a hand-rolled decorator: no functools.wraps
    def inner(*a: Any, **k: Any) -> Any:
        return fn(*a, **k)
    return inner


@traced
@inv.model_call("P-W", schema={"action": str, "n": int}, returns=CPlan)
def wrapped_plan(inv_feedback: str | None = None) -> str:
    return REPLIES.pop(0)


@inv.model_call("P-W", schema={"action": str, "n": int}, returns=CPlan)
def plain_plan(inv_feedback: str | None = None) -> str:
    return REPLIES.pop(0)


@inv.edge("P-W")
def act(p: CPlan) -> str:
    SEEN.append(p)
    return "acted"


def edge_doc(target: str = "wrapped_plan") -> dict[str, Any]:
    return {"spec": SPEC, "targets": {f"{M}.{target}": {"effects": "none"}},
            "rules": [{"id": "P-W", "status": "enforce", "subject": {"target": f"{M}.{target}"},
                       "bad": "reply.n > 5", "falsified_by": ["t"],
                       "enforcers": [{"point": "model_call", "timing": "pre_effect", "mode": "enforce",
                                      "on_violation": "reject"},
                                     {"point": "edge", "target": f"{M}.act", "timing": "pre_effect",
                                      "mode": "enforce", "on_violation": "reject", "on_error": "fail_open_and_page"}],
                       "model_policy": {"format_retries": 0, "semantic_resamples": 0,
                                        "feedback": {"format": "none"}}}]}


# --- L1: Enum fields in a {field: type} and a dataclass schema --------------------------------------
class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


@dataclasses.dataclass
class Paint:
    color: Color
    level: Level


class CMix(inv.Checked): ...


class CPaint(inv.Checked): ...


MIX: list[Any] = []


@inv.model_call("E-MIX", schema={"color": Color, "level": Level}, returns=CMix)
def mix(inv_feedback: str | None = None) -> str:
    return MIX.pop(0)


@inv.model_call("E-PAINT", schema=Paint, returns=CPaint)
def paint(inv_feedback: str | None = None) -> str:
    return MIX.pop(0)


@inv.edge("E-MIX")
def use_mix(c: CMix) -> Any:
    return c.value


def enum_doc() -> dict[str, Any]:
    def rule(rid: str, target: str, bad: str, edge: str | None = None) -> dict[str, Any]:
        enf = [{"point": "model_call", "timing": "pre_effect", "mode": "enforce", "on_violation": "reject"}]
        if edge:
            enf.append({"point": "edge", "target": f"{M}.{edge}", "timing": "pre_effect", "mode": "enforce",
                        "on_violation": "reject"})
        return {"id": rid, "status": "enforce", "subject": {"target": f"{M}.{target}"}, "bad": bad,
                "falsified_by": ["t"], "enforcers": enf,
                "model_policy": {"format_retries": 0, "semantic_resamples": 0, "feedback": {"format": "none"}}}
    return {"spec": SPEC, "targets": {f"{M}.mix": {"effects": "none"}, f"{M}.paint": {"effects": "none"}},
            "rules": [rule("E-MIX", "mix", "reply.color == 'blue' && reply.level > 1", edge="use_mix"),
                      rule("E-PAINT", "paint", "reply.color == 'blue'")]}


def reply(**fields: Any) -> str:
    return json.dumps(fields)
