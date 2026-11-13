"""Fixture app for the fix-round-16 tests: model calls and edges skip off, retired and ablated rules;
old_self from bind and views; self on methods; load-time scope checks; edge enforcer per target;
one generation per call; standing honesty; typed TypedDicts and closed {field: type} schemas;
snapshots of scalar values; providers (typed cache keys, gathered, only what is read, several
names); pydantic replies kept as model instances."""
from __future__ import annotations

import asyncio
import dataclasses
import datetime
import enum
import json
import time
import typing
import uuid
from typing import Any, Literal, TypedDict

import pydantic

import invariants as inv
from invariants.integrations import pydantic as inv_pd

M = "py:round16"
TESTS = ["tests/test_fix_round16.py::x"]


# --- model calls ----------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Reply:
    cause: Literal["A", "B", "CANNOT_COMPLY"]
    n: int


class C16(inv.Checked): ...


class CS(inv.Checked): ...


REPLIES: list[Any] = []
CALLS = {"n": 0}
HOOK: dict[str, Any] = {}


def _next() -> Any:
    CALLS["n"] += 1
    if "before" in HOOK:
        HOOK.pop("before")()
    return REPLIES.pop(0)


@inv.model_call("M-1", schema=Reply)
def ask(inv_feedback: str | None = None) -> C16:
    return _next()


@inv.model_call("M-A", schema=Reply)
async def aask(inv_feedback: str | None = None) -> C16:
    return _next()


@inv.edge("M-1")
def use_a(c: C16) -> str:
    return "a"


@inv.edge("M-1")
def use_b(c: C16) -> str:
    return "b"


class Agent:
    def __init__(self, limit: int) -> None:
        self.limit = limit

    @inv.model_call("M-SELF", schema=Reply)
    def ask(self, inv_feedback: str | None = None) -> CS:
        return _next()

    @inv.edge("M-SELF")
    def use(self, c: CS) -> str:
        return "used"


def reply(cause: str = "A", n: int = 1) -> str:
    return json.dumps({"cause": cause, "n": n})


def model_doc(rid: str = "M-1", bad: Any = "reply.n > 5", *, status: str = "enforce", mode: str = "enforce",
              fmt: int = 1, sem: int = 0, edges: dict[str, str] | None = None, providers: Any = None,
              target: str | None = None, view: dict[str, str] | None = None) -> dict[str, Any]:
    target = target or {"M-1": f"{M}.ask", "M-A": f"{M}.aask", "M-SELF": f"{M}.Agent.ask"}[rid]
    enf: list[dict[str, Any]] = [{"point": "model_call", "timing": "pre_effect", "mode": mode,
                                  "on_violation": "reject"}]
    if view is not None:
        enf[0]["view"] = view
    for t, m in (edges or {}).items():
        enf.append({"point": "edge", "timing": "pre_effect", "mode": m, "on_violation": "reject",
                    "target": f"{M}.{t}"})
    doc: dict[str, Any] = {
        "targets": {target: {"effects": "none"}},
        "rules": [{"id": rid, "status": status, "subject": {"target": target}, "bad": bad, "enforcers": enf,
                   "falsified_by": TESTS,
                   "model_policy": {"schema": "Reply", "format_retries": fmt, "semantic_resamples": sem,
                                    "feedback": {"format": "parser_error"},
                                    "abstain": {"field": "cause", "value": "CANNOT_COMPLY"}}}]}
    if providers:
        doc["providers"] = providers
    return doc


# --- providers ------------------------------------------------------------------------------------
@inv.provider("lim")
def lim_a() -> int:
    return 5


@inv.provider("lim")
def lim_b() -> int:
    return 100


PROVIDER_CALLS: list[tuple[str, Any]] = []


@inv.provider("known", "also_known")
def known(x: int) -> bool:
    PROVIDER_CALLS.append(("known", x))
    return x > 0


@inv.provider("flag")
def flag(x: Any) -> str:
    PROVIDER_CALLS.append(("flag", x))
    return type(x).__name__


@inv.provider("slow1")
async def slow1(x: int) -> int:
    PROVIDER_CALLS.append(("slow1-start", x))
    await asyncio.sleep(0.05)
    PROVIDER_CALLS.append(("slow1-end", x))
    return x


@inv.provider("slow2")
async def slow2(x: int) -> int:
    PROVIDER_CALLS.append(("slow2-start", x))
    await asyncio.sleep(0.05)
    PROVIDER_CALLS.append(("slow2-end", x))
    return x


@inv.provider("costly")
def costly(x: int) -> int:
    PROVIDER_CALLS.append(("costly", x))
    return x


STORE: inv.Dependency[dict[str, int]] = inv.Dependency("round16_store")


@inv.provider("stored")
def stored(key: str) -> int:
    return STORE.get()[key]


@inv.guard("G-P")
def pf(x: int, y: Any = None, key: str = "") -> None:
    return None


@inv.guard("py:round16.pf2")
def pf2(x: Any, y: Any) -> None:
    return None


@inv.guard("G-AP")
async def apf(x: int) -> None:
    return None


@inv.provider("snooze")
def snooze(x: int) -> int:
    time.sleep(0.03)
    return x


@inv.guard("G-SLOW")
def slow(x: int) -> None:
    return None


@inv.model_call("M-R", schema=Reply, returns=C16)
def ask_raw(inv_feedback: str | None = None) -> str:
    return _next()


def guard_doc(rid: str, fn: str, bad: Any, *, providers: Any = None, bind: Any = None, timing: str = "pre_effect",
              point: str = "decorator", view: Any = None, mediation: Any = None, status: str = "enforce",
              effects: str = "none", extra_enf: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    enf: dict[str, Any] = {"point": point, "timing": timing, "mode": "enforce"}
    if view is not None:
        enf["view"] = view
    r: dict[str, Any] = {"id": rid, "status": status, "subject": {"target": f"{M}.{fn}"}, "bad": bad,
                         "enforcers": [enf, *(extra_enf or [])], "falsified_by": TESTS}
    if bind is not None:
        r["bind"] = bind
    if mediation is not None:
        r["mediation"] = mediation
    doc: dict[str, Any] = {"targets": {f"{M}.{fn}": {"effects": effects}}, "rules": [r]}
    if providers:
        doc["providers"] = providers
    return doc


# --- old_self -------------------------------------------------------------------------------------
class Color(enum.Enum):
    RED = "r"
    BLUE = "b"


@dataclasses.dataclass
class Account:
    balance: int = 500
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)
    blob: bytes = b"zz"
    at: datetime.datetime = datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc)
    day: datetime.date = datetime.date(2024, 6, 1)
    tod: datetime.time = datetime.time(9, 30)
    wait: datetime.timedelta = datetime.timedelta(minutes=5)
    uid: uuid.UUID = uuid.UUID(int=7)
    color: Color = Color.RED
    limit: int = 10

    @inv.guard("G-OLD")
    def withdraw(self, amount: int) -> int:
        self.balance -= amount
        return self.balance

    @inv.guard("G-SNAP")
    def touch(self) -> None:
        return None

    @inv.guard("G-SELF")
    def spend(self, amount: int) -> None:
        return None


# --- typecheck ------------------------------------------------------------------------------------
class TD(TypedDict):
    n: str
    k: int


@inv.guard("G-TD")
def td(t: TD) -> None:
    return None


@inv.model_call("M-MAP", schema={"cause": Literal["A", "CANNOT_COMPLY"], "n": int})
def ask_map(inv_feedback: str | None = None) -> C16:
    return _next()


# --- verify ---------------------------------------------------------------------------------------
def elsewhere(x: int) -> None:
    return None


@inv.guard("py:round16.elsewhere")
def misplaced(x: int) -> None:
    return None


def backdoor(x: int) -> None:
    return None


# --- pydantic replies -----------------------------------------------------------------------------
class Fl(enum.Enum):
    LOW = 1.5
    HIGH = 2.5


class Pair(enum.Enum):
    ONE = (1, 2)
    TWO = (3, 4)


class Inner(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    x: int


class PR(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    cause: Literal["A", "CANNOT_COMPLY"]
    at: datetime.datetime
    level: Fl
    inner: Inner
    blob: Any
    secret: str = pydantic.Field(default="s", exclude=True)


class PT(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    cause: Literal["A", "CANNOT_COMPLY"]
    pair: Pair


class Many(enum.Enum):
    SOME = [1, 2]
    MORE = [3]


class PL(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    cause: Literal["A", "CANNOT_COMPLY"]
    many: Many


class CP(inv.Checked): ...


@inv_pd.model_call("M-PD", schema=PR)
def ask_pd(inv_feedback: str | None = None) -> CP:
    return _next()


@inv.edge("M-PD")
def use_pd(c: CP) -> Any:
    return c.value


@dataclasses.dataclass(frozen=True)
class AnyReply:
    cause: Literal["A", "CANNOT_COMPLY"]
    blob: Any


class CA(inv.Checked): ...


@inv.model_call("M-ANY", schema=AnyReply)
def ask_any(inv_feedback: str | None = None) -> CA:
    return _next()


SEEN: dict[str, Any] = {}


@inv.edge("M-ANY")
def use_any(c: CA) -> Any:
    SEEN["before"] = json.dumps(c.value.blob)
    if "during" in HOOK:
        HOOK.pop("during")()
    SEEN["after"] = json.dumps(c.value.blob)
    return c.value


def pd_reply(**over: Any) -> str:
    d: dict[str, Any] = {"cause": "A", "at": "2024-06-01T12:00:00Z", "level": 1.5, "inner": {"x": 1},
                         "blob": {"k": [1]}, "secret": "x"}
    d.update(over)
    return json.dumps(d)


def pd_doc(rid: str, target: str, schema: str, bad: Any, edge: str | None = None, fmt: int = 0) -> dict[str, Any]:
    enf: list[dict[str, Any]] = [{"point": "model_call", "timing": "pre_effect", "mode": "enforce",
                                  "on_violation": "reject"}]
    if edge:
        enf.append({"point": "edge", "timing": "pre_effect", "mode": "enforce", "on_violation": "reject",
                    "target": f"{M}.{edge}"})
    return {"targets": {f"{M}.{target}": {"effects": "none"}},
            "rules": [{"id": rid, "status": "enforce", "subject": {"target": f"{M}.{target}"}, "bad": bad,
                       "enforcers": enf, "falsified_by": TESTS,
                       "model_policy": {"schema": schema, "format_retries": fmt, "semantic_resamples": 0,
                                        "feedback": {"format": "parser_error"},
                                        "abstain": {"field": "cause", "value": "CANNOT_COMPLY"}}}]}


__all__ = ["typing"]
