"""`inv verify`: import the app, then every target guarded and every guard named."""
from __future__ import annotations

import json

import yaml

from cli_support import BYPASS, CLEAN, run_inv


def test_apps_pass():
    for app in (CLEAN, BYPASS):
        proc = run_inv("verify", app / "invariants.yaml")
        assert proc.returncode == 0, proc.stderr
        assert "verify: pass" in proc.stdout


def test_import_flag_brings_in_unnamed_guard():
    proc = run_inv("verify", CLEAN / "invariants.yaml", "--import", "ledger.extra")
    assert proc.returncode == 1
    assert "audit: guards L-404, which is not in the rule file" in proc.stderr


def test_unguarded_target(tmp_path):
    doc = yaml.safe_load((CLEAN / "invariants.yaml").read_text())
    doc["targets"]["py:ledger.api.handle_withdraw"] = {"effects": "external"}
    doc["rules"].append({"id": "L-9", "status": "detect", "subject": {"target": "py:ledger.api.handle_withdraw"},
                         "bad": "args.amount < 0",
                         "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}]})
    p = tmp_path / "invariants.yaml"
    p.write_text(yaml.safe_dump(doc))
    proc = run_inv("verify", p, "--path", CLEAN, "--json")
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["passed"] is False
    assert data["problems"] == ["L-9: py:ledger.api.handle_withdraw is not wrapped by its guard"]


def test_bad_import_fails():
    proc = run_inv("verify", CLEAN / "invariants.yaml", "--import", "ledger.nothing_here")
    assert proc.returncode == 1 and "cannot import ledger.nothing_here" in proc.stderr
