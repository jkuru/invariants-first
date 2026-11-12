"""R-1: pure target, post-effect guard (prototype checks 18-24)."""
from __future__ import annotations

import asyncio
import random

import jobs
import pytest

import invariants as inv
from support import raises


def r1_property(seed: int = 20261109, cases: int = 500):
    """Seeded property run; returns the first rejected input, or None."""
    rnd, first_bad = random.Random(seed), None
    for _ in range(cases):
        caps = [rnd.randint(0, 50) for _ in range(rnd.randint(1, 8))]
        total = rnd.randint(0, sum(caps))
        try:
            jobs.split_work(total, caps)
        except inv.Rejected as err:
            first_bad = first_bad or (total, caps, str(err))
    return first_bad


def r1_mutant_caught(monkeypatch: pytest.MonkeyPatch) -> bool:
    """Falsifier r1_mutant: the guard catches the rounding-bug mutant."""
    with monkeypatch.context() as m:
        m.setattr(jobs, "_allocate", jobs._allocate_rounding_bug)
        return r1_property() is not None


def test_property_guard_never_fires(reg):
    assert r1_property() is None


def test_reach_counter_counts_every_evaluation(reg):
    runs0 = reg.evidence["R-1"]["runs"]
    r1_property()
    assert reg.evidence["R-1"]["runs"] - runs0 == 500
    assert reg.counts("R-1")["ok"] == 500


def test_guard_catches_rounding_bug_mutant(reg, monkeypatch):
    assert r1_mutant_caught(monkeypatch)


def test_ablation_makes_falsifier_fail(reg, monkeypatch):
    with inv.ablated("R-1"):
        assert not r1_mutant_caught(monkeypatch)
    assert "R-1" not in reg.ablated
    assert r1_mutant_caught(monkeypatch)


def test_none_result_is_rule_error_not_violation(reg, monkeypatch):
    monkeypatch.setattr(jobs, "_allocate", lambda total, caps: None)
    viol0, err0 = reg.evidence["R-1"]["violation"], reg.evidence["R-1"]["rule_error"]
    e = raises(inv.Rejected, jobs.split_work, 3, [3])
    assert e is not None and e.outcome.kind == "rule_error"
    assert reg.evidence["R-1"]["violation"] == viol0 and reg.evidence["R-1"]["rule_error"] == err0 + 1


def test_clause_list_beats_zip_only_rule(reg):
    zip_r1 = inv.load({"rules": [{"id": "R-1g", "status": "candidate", "subject": {"target": "py:jobs.split_work"},
                                  "bad": "inv.sum(result) != args.total_jobs || "
                                         "inv.zip(result, args.capacities).exists(p, p[0] < 0 || p[0] > p[1])"}]},
                      install=False).rules["R-1g"]
    base = {"args": {"total_jobs": 10, "capacities": [5, 5]}, "result": [5, 4, 1]}
    out_zip, out_ours = inv.evaluate(zip_r1, base), inv.evaluate(reg.rules["R-1"], base)
    assert out_zip.kind == "rule_error" and "length mismatch" in out_zip.error
    assert out_ours.kind == "violation"


@inv.guard("R-1")
async def split_work_async(total_jobs: int, capacities: list[int]) -> list[int]:
    return [-1, total_jobs + 1]


def test_async_guard_rejects(reg):
    e = raises(inv.Rejected, asyncio.run, split_work_async(10, [5, 5]))
    assert e is not None and "negative share" in e.outcome.messages


def test_async_guard_ok(reg):
    @inv.guard("R-1")
    async def fine(total_jobs: int, capacities: list[int]) -> list[int]:
        await asyncio.sleep(0)
        return [5, 5]
    assert asyncio.run(fine(10, [5, 5])) == [5, 5]


def test_guard_preserves_metadata():
    assert jobs.split_work.__name__ == "split_work" and jobs.split_work.__inv_rules__ == ("R-1",)
    assert jobs.split_work.__inv_kind__ == "guard"
