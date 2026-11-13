"""Fix round 20 (guard): each test marked "failed before" failed before its fix; the others pin
what the fix must not change.

1. @inv.guard calls straight through when no rule is live (every enforcer off, ablated or
   retired): a call shape that bound once is not bound again, and no check runs. A timing no live
   enforcer checks at (post_effect of a pre-only rule) is skipped. Events, ablated events, the
   TypeError of a call that does not bind and its precedence over a LoadError are unchanged.
2. _ModelRun and _EdgeCall have __slots__.
3. An async @inv.model_call or @inv.edge whose provider prefetch fails records the rule_error
   @inv.guard records ("binding providers raised ..."), instead of dropping why and evaluating.
4. @inv.edge accepts a direct parameter annotated CheckedT | None (Optional[CheckedT]), as a state
   field: a None received there is the TypeError of any value that is not a CheckedT.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Optional

import pytest

import invariants as inv
import round19 as r19
from invariants import providers as inv_providers
from invariants.guard import decorators as inv_decorators
from invariants.guard.plan import _EdgeCall, _ModelRun

M = "py:test_fix_round20_guard"
SPEC = "invariant/v0.1"
WHY = "binding providers raised RuntimeError"


@pytest.fixture
def iso():
    with inv.isolated() as r:
        yield r


def events(reg: inv.Registry, rule_id: str) -> list[dict[str, Any]]:
    return [{k: v for k, v in e.items() if k not in ("start_ns", "end_ns")}
            for e in reg.sink if e.get("rule") == rule_id]


# --- 1. guards with nothing live ------------------------------------------------------------------
@inv.guard("Q-G")
def door(n: int, k: int = 0) -> int:
    return n + k


@inv.guard("Q-G")
async def adoor(n: int, k: int = 0) -> int:
    return n + k


def guards() -> tuple[Any, Any]:
    """A fresh sync and async guard of Q-G (each guard remembers the call shapes that bound)."""
    @inv.guard("Q-G")
    def door(n: int, k: int = 0) -> int:
        return n + k

    @inv.guard("Q-G")
    async def adoor(n: int, k: int = 0) -> int:
        return n + k
    return door, adoor


def guard_doc(mode: str = "enforce", timing: str = "pre_effect", status: str = "enforce",
              bad: str = "args.n > 5") -> dict[str, Any]:
    enf = [{"point": "decorator", "timing": timing, "mode": mode, "on_violation": "reject"}]
    return {"spec": SPEC, "targets": {f"{M}.door": {"effects": "none"}, f"{M}.adoor": {"effects": "none"}},
            "rules": [{"id": "Q-G", "status": status, "subject": {"target": f"{M}.door"}, "bad": bad,
                       "enforcers": enf, "falsified_by": ["t"]}]}


@pytest.fixture
def binds(monkeypatch):
    """How many times a signature was bound."""
    seen: list[inspect.Signature] = []
    real = inspect.Signature.bind

    def spy(self: inspect.Signature, *a: Any, **kw: Any) -> inspect.BoundArguments:
        seen.append(self)
        return real(self, *a, **kw)
    monkeypatch.setattr(inspect.Signature, "bind", spy)
    return seen


@pytest.mark.parametrize("doc", [guard_doc(mode="off", status="candidate"), guard_doc(status="retired")], ids=["off", "retired"])
def test_a_guard_with_no_live_rule_binds_a_call_shape_once(iso, binds, doc):   # failed before
    door, adoor = guards()
    inv.load(doc)
    assert [door(9), door(9), door(1, 2), door(n=9), door(n=9)] == [9, 9, 3, 9, 9]
    assert asyncio.run(adoor(9)) == 9 and asyncio.run(adoor(9)) == 9
    assert len(binds) == 4                   # (1,), (2,), (0, 'n') for door; (1,) for adoor: was 7
    assert events(iso, "Q-G") == []


def test_an_ablated_guard_emits_its_ablated_events_and_binds_once(iso, binds):   # failed before
    door, adoor = guards()
    inv.load(guard_doc())
    with inv.ablated("Q-G"):
        assert door(9) == 9 and door(9) == 9 and asyncio.run(adoor(9)) == 9
    assert len(binds) == 2                   # was 3
    ev = {"rule": "Q-G", "rev": None, "outcome": "ablated", "mode": "off", "point": "decorator",
          "timing": None, "messages": [], "error": None, "attempts": None, "action": "proceed"}
    assert events(iso, "Q-G") == [ev, ev, ev]
    assert iso.counts("Q-G") == {"ablated": 3}
    with pytest.raises(inv.Rejected):        # live again: the cached shape still binds the arguments
        door(9)


def test_a_call_that_does_not_bind_is_still_a_type_error_before_anything_else(iso):
    door, _ = guards()
    inv.load(guard_doc())
    with inv.ablated("Q-G"):
        door(1)
        for bad in (lambda: door(), lambda: door(1, 2, 3), lambda: door(1, x=2)):
            with pytest.raises(TypeError, match="argument"):
                bad()
    assert [e["outcome"] for e in iso.sink] == ["ablated"]          # none for the calls that did not bind

    @inv.guard("NOT-LOADED")
    def lone(x: int) -> int:
        return x
    with pytest.raises(TypeError, match="missing a required argument"):
        lone()                               # binding comes first, as before
    with pytest.raises(inv.LoadError):
        lone(1)


def test_a_live_guard_after_a_cached_shape_still_judges_the_arguments(iso):
    door, _ = guards()
    inv.load(guard_doc(mode="off", status="candidate"))
    assert door(9) == 9 and door(n=9, k=1) == 10
    inv.load(guard_doc())
    with pytest.raises(inv.Rejected):
        door(9)
    with pytest.raises(inv.Rejected):
        door(n=9, k=1)
    assert door(1) == 1
    assert [e["outcome"] for e in iso.sink] == ["violation", "violation", "ok"]


def test_a_guard_skips_a_timing_no_live_enforcer_checks_at(iso, monkeypatch):   # failed before
    ran: list[str] = []
    for name in ("check", "acheck", "aprefetch"):
        real = getattr(inv_decorators, name)

        def spy(call: Any, timing: str, *rest: Any, _real: Any = real, _name: str = name) -> Any:
            ran.append(f"{_name}:{timing}")
            return _real(call, timing, *rest)
        monkeypatch.setattr(inv_decorators, name, spy)
    door, adoor = guards()
    inv.load(guard_doc())
    assert door(1) == 1 and asyncio.run(adoor(1)) == 1
    assert ran == ["check:pre_effect", "aprefetch:pre_effect", "acheck:pre_effect"]
    ran.clear()
    inv.load(guard_doc(timing="post_effect", bad="result > 5"))
    assert door(1) == 1 and asyncio.run(adoor(1)) == 1
    assert ran == ["check:post_effect", "aprefetch:post_effect", "acheck:post_effect"]
    assert [e["timing"] for e in iso.sink] == ["pre_effect"] * 2 + ["post_effect"] * 2


# --- 2. __slots__ ---------------------------------------------------------------------------------
def test_model_run_and_edge_call_have_slots():   # failed before
    assert _ModelRun.__dictoffset__ == 0 and _EdgeCall.__dictoffset__ == 0
    assert "__slots__" in vars(_ModelRun) and "__slots__" in vars(_EdgeCall)


# --- 3. a failed async prefetch in a model call or an edge ------------------------------------------
class CQ(inv.Checked): ...


REPLIES: list[Any] = []


@inv.provider("lim")
async def alim() -> int:
    return 5


@inv.model_call("Q-1", schema={"n": int}, returns=CQ)
async def aask(inv_feedback: str | None = None) -> str:
    return REPLIES.pop(0)


@inv.edge("Q-1")
async def aconsume(p: CQ) -> int:
    return p.value["n"]


def model_doc() -> dict[str, Any]:
    enf = [{"point": "model_call", "timing": "pre_effect", "mode": "enforce", "on_violation": "reject"},
           {"point": "edge", "target": f"{M}.aconsume", "timing": "pre_effect", "mode": "enforce",
            "on_violation": "reject"}]
    return {"spec": SPEC, "targets": {f"{M}.aask": {"effects": "none"}, f"{M}.adoor": {"effects": "none"}},
            "providers": {"lim": {"type": "int", "from": f"{M}.alim", "args": {}}},
            "rules": [{"id": "Q-1", "status": "enforce", "subject": {"target": f"{M}.aask"},
                       "bad": "reply.n > lim", "enforcers": enf, "falsified_by": ["t"],
                       "model_policy": {"format_retries": 0, "semantic_resamples": 0,
                                        "feedback": {"format": "none"}}},
                      {"id": "Q-G", "status": "enforce", "subject": {"target": f"{M}.adoor"},
                       "bad": "args.n > lim", "falsified_by": ["t"],
                       "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce",
                                      "on_violation": "reject"}]}]}


def fail_async_prefetch(monkeypatch) -> None:
    """Binding the async providers raises (the sync binding evaluate() does still works)."""
    real = inv_providers.fetch_providers

    def boom(*a: Any, **kw: Any) -> Any:
        if kw.get("pending", a[5] if len(a) > 5 else None) is not None:
            raise RuntimeError("no")
        return real(*a, **kw)
    monkeypatch.setattr(inv_providers, "fetch_providers", boom)


def test_a_failed_prefetch_is_the_same_rule_error_in_a_guard_a_model_call_and_an_edge(iso, monkeypatch):
    # failed before: the model call and the edge dropped why, then evaluated (lim "is async" there)
    inv.load(model_doc())
    REPLIES[:] = ['{"n": 1}']
    c = asyncio.run(aask())
    assert c.verdict == "ok" and asyncio.run(aconsume(c)) == 1
    iso.sink.clear()
    fail_async_prefetch(monkeypatch)
    with pytest.raises(inv.Rejected):
        asyncio.run(adoor(1))
    REPLIES[:] = ['{"n": 1}']
    with pytest.raises(inv.Rejected):
        asyncio.run(aask())
    with pytest.raises(inv.Rejected):
        asyncio.run(aconsume(c))
    got = [(e["rule"], e["point"], e["outcome"], e["error"]) for e in iso.sink]
    assert got == [("Q-G", "decorator", "rule_error", WHY), ("Q-1", "model_call", "rule_error", WHY),
                   ("Q-1", "edge", "rule_error", WHY)]


def test_an_async_model_call_still_retries_abstains_and_hands_off(iso):
    doc = r19.model_doc()
    doc["rules"][0]["model_policy"].update(format_retries=1, semantic_resamples=1)
    inv.load(doc)
    r19.REPLIES[:] = ["not json", r19.reply(n=9), r19.reply(n=2)]
    c = asyncio.run(r19.aplan())
    assert c.verdict == "ok" and c.attempts == 3
    assert [e["action"] for e in iso.sink] == ["retry", "resample", "proceed"]
    r19.REPLIES[:] = ["not json", "still not"]
    with pytest.raises(inv.Rejected):
        r19.plan()
    assert r19.REPLIES == []


# --- 4. an Optional Checked parameter on an edge ------------------------------------------------------
def test_an_edge_accepts_an_optional_checked_parameter(iso):   # failed before
    @inv.edge("Q-1")
    async def opt(p: CQ | None, note: str = "") -> int:
        assert p is not None
        return p.value["n"]

    @inv.edge("Q-1")
    def opt2(p: Optional[CQ]) -> int:
        assert p is not None
        return p.value["n"]
    assert opt.__inv_at__ == "p" and opt2.__inv_at__ == "p"     # type: ignore[attr-defined]
    doc = model_doc()
    doc["rules"][0]["enforcers"][1]["target"] = f"{M}.door"   # the one edge enforcer: any consumer uses it
    doc["rules"][0]["bad"] = "reply.n > 5"
    inv.load(doc)
    REPLIES[:] = ['{"n": 1}']
    c = asyncio.run(aask())
    assert asyncio.run(opt(c)) == 1 and opt2(c) == 1
    with pytest.raises(TypeError, match="accepts only CQ"):
        asyncio.run(opt(None))
    with pytest.raises(TypeError, match="accepts only CQ"):
        opt2(None)
    inv.load(r19.model_doc())                 # a None state field: the same TypeError
    with pytest.raises(TypeError, match="accepts only CPlan"):
        asyncio.run(r19.act_dstate(r19.DState(None)))
