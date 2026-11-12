"""The monitor job and the detect-mode rule K-16."""
from __future__ import annotations

import importlib
import pathlib
import sys

import invariants as inv
import pytest

from fact_pipeline import monitor
from fact_pipeline.pipeline import Pipeline

from .conftest import S1, S2

BYPASS = pathlib.Path(__file__).resolve().parents[1] / "bypass"


def _believed(pipe: Pipeline) -> str:
    pipe.process("test://s1", S1, "newswire-a")
    pipe.process("test://s2", S2, "wire-b")
    (fid,) = pipe.promote_all()
    return fid


def test_clean_store_has_no_findings(pipe: Pipeline) -> None:
    _believed(pipe)
    assert monitor.run(pipe.store) == []


def test_monitor_counts_its_evaluations(pipe: Pipeline) -> None:
    _believed(pipe)
    before = inv.current().counts("K-12").get("runs", 0)
    monitor.run(pipe.store)
    assert inv.current().counts("K-12")["runs"] == before + 1


def test_k16_detect_reports_but_promotes(pipe: Pipeline) -> None:
    pipe.process("test://s1", S1, "newswire-a")
    pipe.process("test://s2", S2, "wire-b")
    pipe.intake("test://s1", S1 + " Acme Corp is headquartered in Springfield.", "newswire-a")   # S1 edited
    with inv.capture() as seen:
        promoted = pipe.promote_all()
    assert promoted                                          # detect never blocks
    events = [e for e in seen if e["rule"] == "K-16" and e["outcome"] == "violation"]
    assert events and events[0]["action"] == "proceed"


def test_monitor_flags_stale_believed_fact(pipe: Pipeline) -> None:
    fid = _believed(pipe)
    pipe.intake("test://s2", S2 + " Beta Ltd is headquartered in Leeds.", "wire-b")
    found = monitor.run(pipe.store)
    assert [(f.rule, f.key, f.outcome) for f in found] == [("K-16", fid, "violation")]


def test_monitor_catches_a_write_that_bypassed_the_door(pipe: Pipeline) -> None:
    pipe.process("test://s1", S1, "newswire-a")
    fid = pipe.store.facts()[0]["id"]
    sys.path.insert(0, str(BYPASS))
    try:
        backfill = importlib.import_module("fact_pipeline_backfill.backfill")
        backfill.force_believe(pipe.store.conn, fid)
    finally:
        sys.path.remove(str(BYPASS))
    found = monitor.run(pipe.store)
    assert ("K-12", fid) in {(f.rule, f.key) for f in found}


def test_reconciler_runs_over_deleted_sources(pipe: Pipeline) -> None:
    rep = pipe.process("test://s1", S1, "newswire-a")
    assert rep.source_id
    pipe.delete_source(rep.source_id)
    before = inv.current().counts("K-15").get("runs", 0)
    assert monitor.run(pipe.store) == []
    assert inv.current().counts("K-15")["runs"] == before + 1


def test_monitor_skips_an_ablated_rule(pipe: Pipeline) -> None:
    fid = _believed(pipe)
    pipe.intake("test://s2", S2 + " Beta Ltd is headquartered in Leeds.", "wire-b")
    with inv.ablated("K-16"):
        assert all(f.rule != "K-16" for f in monitor.run(pipe.store))
    assert fid


@pytest.mark.parametrize("rule", monitor.MONITORED)
def test_every_monitored_rule_declares_a_monitor_enforcer(rule: str) -> None:
    assert any(e["point"] == "monitor" for e in inv.current().rules[rule].enforcers)
