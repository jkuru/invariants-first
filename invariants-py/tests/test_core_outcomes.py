"""Outcomes and clause lists."""
from __future__ import annotations

import jobs
import pytest

import invariants as inv
from support import raises


@pytest.fixture(autouse=True)
def _clear_restarted():
    jobs.RESTARTED.clear()
    yield
    jobs.RESTARTED.clear()


def p2_empty_id_caught() -> bool:
    """Falsifier p2_empty_id: an empty job id is rejected before the effect."""
    before = list(jobs.RESTARTED)
    e = raises(inv.Rejected, jobs.restart_job, "")
    return e is not None and jobs.RESTARTED == before


def test_fail_open_and_page_proceeds_and_pages(reg):
    jobs.restart_job(None)                               # size(null): rule_error
    ev = reg.sink[-1]
    assert jobs.RESTARTED == [None] and ev["outcome"] == "rule_error" and ev["action"] == "page"


def test_violation_rejects_before_effect(reg):
    assert p2_empty_id_caught()
    assert jobs.RESTARTED == []


def test_ok_proceeds(reg):
    jobs.restart_job("J-1")
    assert jobs.RESTARTED == ["J-1"] and reg.sink[-1]["action"] == "proceed"


def test_clause_list_combination():
    clauses = inv.load({"rules": [{"id": "C-1", "status": "candidate", "bad": [
        {"when": "args.x > 1", "message": "x > 1"}, {"when": "args.y.z", "message": "reads a missing field"},
        {"when": "args.x > 2", "message": "x > 2"}]}]}, install=False).rules["C-1"]
    o_v = inv.evaluate(clauses, {"args": {"x": 5, "y": {}}})
    o_e = inv.evaluate(clauses, {"args": {"x": 0, "y": {}}})
    o_k = inv.evaluate(clauses, {"args": {"x": 0, "y": {"z": False}}})
    assert o_v.kind == "violation" and o_v.messages == ["x > 1", "x > 2"]
    assert o_e.kind == "rule_error" and "z" in o_e.error
    assert o_k.kind == "ok"


def test_non_bool_clause_is_rule_error():
    r = inv.load({"rules": [{"id": "C-2", "status": "candidate", "bad": "args.x + 1"}]}, install=False).rules["C-2"]
    out = inv.evaluate(r, {"args": {"x": 1}})
    assert out.kind == "rule_error" and "not bool" in out.error


@pytest.mark.parametrize("on_violation,exc", [("reject", inv.Rejected), ("halt", inv.Halted),
                                              ("escalate", inv.Escalation), ("route", inv.Routed)])
def test_on_violation_maps_to_exception(on_violation, exc):
    @inv.guard("py:test_core_outcomes.target_fn")
    def target_fn(x: int) -> None: ...
    globals()["target_fn"] = target_fn
    try:
        r = inv.load({"targets": {"py:test_core_outcomes.target_fn": {"effects": "none"}},
                      "rules": [{"id": "O-1", "status": "enforce", "subject": {"target": "py:test_core_outcomes.target_fn"},
                                 "bad": "args.x > 1", "falsified_by": ["t"],
                                 "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce",
                                                "on_violation": on_violation}]}]}, install=False)
        with inv.using(r):
            target_fn(1)
            e = raises(exc, target_fn, 2)
            assert e is not None and type(e) is exc and e.outcome.kind == "violation"
            assert (e.review_id is not None) == (on_violation in ("route", "escalate"))   # both hand off
    finally:
        del globals()["target_fn"]
