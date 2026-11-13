"""Falsifiers that run `inv scan`, under --inv-ablate.

While the plugin ablates a scan rule, `inv scan` skips it and exits 4 (ABLATED): the scan refused to
judge the rule, which is neither a pass nor a fail. A falsifier that reads the exit code loosely
(`returncode != 0`) then passes for the wrong reason. The honest pattern asserts the rule's findings
(and, for a raw subprocess, the exact exit code 1); the inv_scan fixture makes the exit code and a
"pass" of an ablated rule unreadable during the ablated re-run, so the plugin reports such a
falsifier as inconclusive instead of crediting or blaming the rule.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytest_plugins = ["pytester"]

RULES = {"spec": "invariant/v0.1", "rules": [{
    "id": "R-9", "status": "enforce", "subject": {"target": "py:app.store.write"}, "bad": "false",
    "enforcers": [{"point": "structure", "timing": "pre_effect", "mode": "enforce"}],
    "mediation": {"chokepoint": "every call to store.write passes svc.door", "residual_paths": [],
                  "sink": "py:app.store.write", "door": "py:app.svc.door"},
    "falsified_by": ["test_scan.py::test_falsifier"]}]}

FILES = {
    "app_src/app/__init__.py": "",
    "app_src/app/store.py": "def write(conn, v):\n    conn.append(v)\n",
    "app_src/app/svc.py": "from app import store\n\ndef door(conn, v):\n    store.write(conn, v)\n",
    "bypass/rogue.py": "from app import store\n\ndef rogue(conn):\n    store.write(conn, 1)\n",
}

RAW = '''
import json, subprocess, sys

def scan(*roots):
    args = [sys.executable, "-m", "invariants.cli", "scan", "rules.json", "--format", "json"]
    p = subprocess.run(args + [a for r in roots for a in ("--root", r)], capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout)
'''

FALSIFIERS = {
    # the documented pattern for a raw subprocess: the exact exit code and the rule's finding
    "raw_exact": RAW + '''
def test_falsifier():
    rc, report = scan("app_src", "bypass")
    assert rc == 1
    assert any(f["rule"] == "R-9" and f["kind"] == "bypass" for f in report["findings"])
''',
    # the trap: exit 4 is also non-zero, so this passes with the rule ablated
    "raw_nonzero": RAW + '''
def test_falsifier():
    rc, _ = scan("app_src", "bypass")
    assert rc != 0
''',
    "fixture_failed": '''
def test_falsifier(inv_scan):
    r = inv_scan("rules.json", "app_src", "bypass")
    assert r.failed("R-9")
    assert [f["kind"] for f in r.findings("R-9")] == ["bypass"]
''',
    "fixture_code": '''
def test_falsifier(inv_scan):
    assert inv_scan("rules.json", "app_src", "bypass").code != 0
''',
    # a clean-scan test named as a falsifier: under ablation it must not count as caught
    "fixture_clean_pass": '''
def test_falsifier(inv_scan):
    assert inv_scan("rules.json", "app_src").passed("R-9")
''',
}


@pytest.fixture
def scan_project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeini("[pytest]\npythonpath = app_src\n")
    for rel, text in FILES.items():
        path = pytester.path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (pytester.path / "rules.json").write_text(json.dumps(RULES))
    return pytester


def _ablate(p: pytest.Pytester, falsifier: str) -> tuple[pytest.RunResult, dict]:
    p.makepyfile(test_scan=FALSIFIERS[falsifier])
    r = p.runpytest_subprocess("-p", "no:cacheprovider", "--inv-rules", "rules.json", "--inv-ablate",
                               "--inv-report-json", "rep.json")
    return r, json.loads((p.path / "rep.json").read_text())["ablation"]


def _result(rep: dict) -> str:
    return rep["rules"]["R-9"][0]["result"]


@pytest.mark.parametrize("falsifier", ["raw_exact", "fixture_failed"])
def test_findings_falsifier_is_caught(scan_project, falsifier):
    r, rep = _ablate(scan_project, falsifier)
    assert _result(rep) == "caught", r.stdout.str()
    assert r.ret == 0


def test_nonzero_exit_falsifier_still_passes_and_fails_the_session(scan_project):
    """`returncode != 0` cannot tell a found bypass (1) from a skipped rule (4): not evidence, and loud."""
    r, rep = _ablate(scan_project, "raw_nonzero")
    assert _result(rep) == "still passes"
    assert rep["not_load_bearing"] == ["R-9"] and r.ret == pytest.ExitCode.TESTS_FAILED


@pytest.mark.parametrize("falsifier", ["fixture_code", "fixture_clean_pass"])
def test_reading_an_ablated_scan_verdict_is_inconclusive(scan_project, falsifier):
    r, rep = _ablate(scan_project, falsifier)
    assert _result(rep) == "inconclusive (read an ablated scan's exit code or pass; assert its findings)"
    assert rep["unverified"] == ["R-9"] and r.ret == pytest.ExitCode.TESTS_FAILED
    r.assert_outcomes(passed=1)                 # the test itself passed with the rule on


def test_inv_scan_without_ablation_reads_normally(scan_project):
    scan_project.makepyfile(test_scan='''
def test_clean(inv_scan):
    r = inv_scan("rules.json", "app_src")
    assert r.code == 0 and r.passed("R-9") and not r.failed("R-9") and r.ablated == []

def test_bypass(inv_scan):
    r = inv_scan("rules.json", "app_src", "bypass")
    assert r.code == 1 and r.failed("R-9") and not r.passed("R-9")
    assert r.findings(kind="bypass")[0]["path"][-1] == "app.store.write"
''')
    scan_project.runpytest_subprocess("-p", "no:cacheprovider").assert_outcomes(passed=2)


def test_inv_scan_exit_4_outside_an_ablation_run_fails_the_test(scan_project, monkeypatch):
    """INV_ABLATE set by hand (not by --inv-ablate): exit 4 is never read as a pass."""
    scan_project.makepyfile(test_scan='''
def test_code(inv_scan):
    inv_scan("rules.json", "app_src").code

def test_pass(inv_scan):
    inv_scan("rules.json", "app_src").passed("R-9")
''')
    monkeypatch.setenv("INV_ABLATE", "R-9")
    monkeypatch.setenv("INV_ABLATE_TESTING", "1")
    r = scan_project.runpytest_subprocess("-p", "no:cacheprovider")
    r.assert_outcomes(failed=2)
    r.stdout.fnmatch_lines(["*inv scan exited 4*neither a pass nor a fail*", "*skipped R-9 because it is ablated*"])


def test_cli_exit_code_under_ablation_is_4(scan_project):
    """The contract the fixture relies on: an ablated enforce rule makes `inv scan` exit 4."""
    env = {**os.environ, "INV_ABLATE": "R-9", "INV_ABLATE_TESTING": "1"}
    p = subprocess.run([sys.executable, "-m", "invariants.cli", "scan", "rules.json", "--root", "app_src",
                        "--root", "bypass"], cwd=scan_project.path, env=env, capture_output=True, text=True)
    assert p.returncode == 4, p.stdout + p.stderr
