"""`inv status`: standing per rule from the registry, verify and scan."""
from __future__ import annotations

import json

from invariants import scan, status_report
from cli_support import BYPASS, CLEAN, run_inv
from support import RULES


def _rows(proc):
    assert proc.returncode == 0, proc.stderr
    return {r["id"]: r for r in json.loads(proc.stdout)["rules"]}


def test_clean_app_standing():
    rows = _rows(run_inv("status", CLEAN / "invariants.yaml", "--json"))
    assert rows["L-1"]["standing"] == "held" and rows["L-1"]["reasons"] == [] and rows["L-1"]["scan"] == "pass"
    assert rows["L-2"]["standing"] == "monitored"
    assert rows["L-3"]["standing"] == "candidate"


def test_bypass_app_is_known_callers():
    rows = _rows(run_inv("status", BYPASS / "invariants.yaml", "--json"))
    f1 = rows["F-1"]
    assert f1["standing"] == "enforce(known-callers)" and f1["scan"] == "fail"
    assert f1["reasons"] == ["inv scan fail: 1 bypass, 2 forbidden, 2 unresolved"]
    assert f1["scan_counts"] == {"bypass": 1, "forbidden": 2, "unresolved": 2}


def test_verify_failure_downgrades_held():
    proc = run_inv("status", CLEAN / "invariants.yaml", "--import", "ledger.extra", "--json")
    data = json.loads(proc.stdout)
    assert data["verify"]["passed"] is False
    l1 = {r["id"]: r for r in data["rules"]}["L-1"]
    assert l1["standing"] == "enforce(known-callers)" and any("verify fails" in w for w in l1["reasons"])


def test_text_output():
    proc = run_inv("status", BYPASS / "invariants.yaml")
    assert proc.returncode == 0
    assert "F-1" in proc.stdout and "enforce(known-callers)" in proc.stdout and "verify: pass" in proc.stdout


def test_invalid_rule_file_exits_1(tmp_path):
    (tmp_path / "invariants.json").write_text('{"rules": [{"id": "X"}]}')
    assert run_inv("status", tmp_path / "invariants.json").returncode == 1


def test_unscanned_rule_is_not_held(reg):
    """In-process on the core fixture registry: no rule there declares mediation.sink."""
    report = scan.scan(RULES, [])
    row = status_report.rule_status(reg, report, "R-20")
    assert row["standing"] == "enforce(known-callers)" and row["scan"] == "not_scanned"
    assert any("add mediation.sink" in w for w in row["reasons"])
    # R-1's target is effects: none, so there is no sink to scan for (fix round 1)
    assert status_report.rule_status(reg, report, "R-1")["standing"] == "held"
