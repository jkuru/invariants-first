"""Fix round 1: scan excludes and config, scan ablation, lock --update-artifacts, standing of pure targets."""
from __future__ import annotations

import hashlib
import json

import yaml

from cli_support import run_inv, write_project
import invariants_scan as scan
from invariants import cli
from invariants.registry import ablated_everywhere

DOC = {"rules": [{"id": "R-9", "status": "enforce", "subject": {"target": "py:app.svc.door"}, "bad": "false",
                  "mediation": {"chokepoint": "door", "residual_paths": [], "sink": "py:app.store.write",
                                "door": "py:app.svc.door"}}]}
FILES = {
    "app/__init__.py": "",
    "app/store.py": "def write(conn, v):\n    conn.execute('UPDATE things SET v = ?', (v,))\n",
    "app/svc.py": "from app import store\n\ndef door(conn, v):\n    store.write(conn, v)\n",
    "app/rogue.py": "from app import store\n\ndef sneak(conn):\n    store.write(conn, 1)\n",
}


def test_scan_exclude_glob_drops_files(tmp_path):
    root = write_project(tmp_path, FILES)
    assert scan.scan(DOC, [root]).rule_result("R-9") == "fail"
    assert scan.scan(DOC, [root], exclude=["app/rogue.py"]).rule_result("R-9") == "pass"
    assert scan.scan(DOC, [root], exclude=["app/rog*"]).rule_result("R-9") == "pass"


def test_scan_reads_exclude_and_roots_from_pyproject(tmp_path):
    write_project(tmp_path / "src", FILES)
    (tmp_path / "rules.json").write_text(json.dumps(DOC))
    r = run_inv("scan", tmp_path / "rules.json", "--root", tmp_path / "src")
    assert r.returncode == 1
    (tmp_path / "pyproject.toml").write_text('[tool.invariants.scan]\nroots = ["src"]\nexclude = ["app/rogue.py"]\n')
    r = run_inv("scan", tmp_path / "rules.json", cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    r = run_inv("scan", tmp_path / "rules.json", "--exclude", "app/svc.py", cwd=tmp_path)
    assert r.returncode == 1                         # the door is gone: missing/door


def test_scan_skips_an_ablated_rule(tmp_path):
    root = write_project(tmp_path, FILES)
    with ablated_everywhere("R-9"):
        report = scan.scan(DOC, [root])
    assert report.rule_result("R-9") == "not_scanned" and "ablated" in report.not_scanned["R-9"]


def test_lock_update_artifacts_rewrites_a_stale_digest(tmp_path):
    (tmp_path / "gateway.yaml").write_text("routes: []\n")
    doc = {"spec": "invariant/v0.1", "rules": [{"id": "S-1", "status": "candidate", "bad": "false",
           "enforcers": [{"point": "gateway", "timing": "pre_effect", "mode": "detect",
                          "artifact": "gateway.yaml#sha256:" + "0" * 64}]}]}
    rf = tmp_path / "invariants.yaml"
    rf.write_text(yaml.safe_dump(doc, sort_keys=False))
    assert run_inv("lock", rf).returncode == 1                       # stale digest: the load fails
    r = run_inv("lock", rf, "--update-artifacts")
    assert r.returncode == 0 and "updated S-1" in r.stdout, r.stderr
    digest = hashlib.sha256((tmp_path / "gateway.yaml").read_bytes()).hexdigest()
    assert digest in rf.read_text() and run_inv("lock", rf, "--check").returncode == 0
    assert cli.main(["lock", str(rf), "--update-artifacts", "--check"]) == cli.USAGE


def test_pure_target_needs_no_sink_for_held(reg):
    from invariants import status_report
    report = scan.scan({"rules": [r.raw for r in reg.rules.values()]}, [])
    rows = {r["id"]: r for r in status_report.build(reg, report)["rules"]}
    assert not any("add mediation.sink" in w for w in rows["R-1"]["reasons"])     # R-1: effects none
