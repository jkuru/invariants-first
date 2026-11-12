"""Fixture app for the fix-round tests: provider timing, async paths and failure modes."""
from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any

import invariants as inv


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    c.execute("CREATE TABLE t (x INTEGER)")
    return c


class Svc:
    """A door: door(n) inserts n rows; post-effect rules read the row count after it."""

    def __init__(self) -> None:
        self.conn = _conn()

    def count(self) -> int:
        return self.conn.execute("SELECT count(*) FROM t").fetchone()[0]

    @inv.guard("T-1")
    def door(self, n: int) -> int:
        self.conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(n)])
        return n

    @inv.guard("T-2")
    def door2(self, n: int) -> int:
        self.conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(n)])
        return n


@inv.provider("rows")
def rows(svc: Svc) -> int:
    return svc.count()


@inv.provider("rows_before")
def rows_before(svc: Svc) -> int:
    return svc.count()


class ASvc(Svc):
    """The same door, async, with an async provider."""

    @inv.guard("A-1")
    async def adoor(self, n: int) -> int:
        self.conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(n)])
        return n


@inv.provider("arows")
async def arows(svc: ASvc) -> int:
    return svc.count()


# --- model calls ----------------------------------------------------------------------------------
@dataclasses.dataclass
class Reply:
    v: int
    cause: str


class CheckedReply(inv.Checked): ...


REPLIES: list[Any] = []
CONSUMED: list[Any] = []


@inv.provider("alimit")
async def alimit() -> int:
    return 10


@inv.model_call("M-1", schema=Reply)
async def amodel(prompt: str) -> CheckedReply:
    return REPLIES.pop(0)


@inv.edge("M-1")
async def aconsume(r: CheckedReply) -> None:
    CONSUMED.append(r)


@inv.model_call("M-2", schema=Reply)
def smodel(prompt: str) -> CheckedReply:
    return REPLIES.pop(0)


@inv.edge("M-2")
def sconsume(r: CheckedReply) -> None:
    CONSUMED.append(r)


# --- failure modes --------------------------------------------------------------------------------
class Boom:
    """An object whose property raises."""

    @property
    def boom(self) -> int:
        raise RuntimeError("property boom")


@inv.provider("echo")
def echo(v: int) -> int:
    return v


CALLS: list[Any] = []


@inv.guard("D-1")
def probe(obj: Any, x: Any = 0) -> None:
    CALLS.append(obj)


@inv.guard("D-2")
def probe2(obj: Any, x: Any = 0) -> None:
    CALLS.append(x)


def plain(n: int) -> int:
    return n


@inv.guard("D-3")
def elsewhere(n: int) -> int:     # D-3's target is plain, not this
    return n


SPEC = "invariant/v0.1"


def doc(*rules: dict[str, Any]) -> dict[str, Any]:
    """The fixture rule file with the given rules."""
    m = "py:fixround"
    return {
        "spec": SPEC,
        "targets": {
            f"{m}.Svc.door": {"effects": "external"},
            f"{m}.Svc.door2": {"effects": "external"},
            f"{m}.ASvc.adoor": {"effects": "external"},
            f"{m}.probe": {"effects": "none"}, f"{m}.probe2": {"effects": "none"},
            f"{m}.plain": {"effects": "none"},
        },
        "providers": {
            "rows": {"type": "int", "from": f"{m}.rows", "args": {"svc": "self"}},
            "rows_before": {"type": "int", "from": f"{m}.rows_before", "args": {"svc": "self"}},
            "arows": {"type": "int", "from": f"{m}.arows", "args": {"svc": "self"}},
            "alimit": {"type": "int", "from": f"{m}.alimit", "args": {}},
            "echo": {"type": "int", "from": f"{m}.echo", "args": {"v": "args.obj.boom"}},
        },
        "rules": list(rules),
    }


def enf(point: str = "decorator", timing: str = "pre_effect", mode: str = "enforce", **kw: Any) -> dict[str, Any]:
    return {"point": point, "timing": timing, "mode": mode, **kw}


def rule(rid: str, target: str, bad: Any, *enforcers: dict[str, Any], status: str = "enforce",
         **kw: Any) -> dict[str, Any]:
    return {"id": rid, "status": status, "subject": {"target": f"py:fixround.{target}"}, "bad": bad,
            "enforcers": list(enforcers), "falsified_by": ["tests/test_fix_round.py"], **kw}


# a post_effect check on an external effect only reports (detect): the effect has happened
T1 = rule("T-1", "Svc.door", "rows > 3", enf(timing="post_effect", mode="detect"), status="detect")
T2 = rule("T-2", "Svc.door2", "rows != args.n", enf(timing="post_effect", mode="detect"), status="detect")
A1 = rule("A-1", "ASvc.adoor", "arows > 3", enf(timing="post_effect", mode="detect"), status="detect")
POLICY = {"schema": "Reply", "format_retries": 0, "semantic_resamples": 0, "feedback": {"format": "none"},
          "abstain": {"field": "cause", "value": "CANNOT_COMPLY"}}
M1 = rule("M-1", "amodel", "reply.v > alimit", enf("model_call", on_violation="reject"),
          enf("edge", target="py:fixround.aconsume"), model_policy=POLICY)
M2 = rule("M-2", "smodel", "reply.v > 10", enf("model_call", on_violation="escalate"),
          enf("edge", target="py:fixround.sconsume"), model_policy=POLICY)
