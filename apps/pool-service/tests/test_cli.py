"""The inv CLI on this project, and proof that the scan catches the deliberate bypass module."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import subprocess
import sys

import pytest

import invariants as inv
from pool_service import service, store
from conftest import APP_ROOT

INV = str(pathlib.Path(sys.executable).with_name("inv"))
ROGUE = APP_ROOT / "bypass" / "pool_service" / "rogue.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([INV, *args], cwd=APP_ROOT, capture_output=True, text=True, timeout=120)


def test_validate() -> None:
    r = run("validate")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("ok: invariants.yaml: 9 rule(s)")


def test_lock_is_fresh() -> None:
    r = run("lock", "--check")
    assert r.returncode == 0, r.stderr


def test_verify() -> None:
    r = run("verify", "--import", "pool_service.api", "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"passed": True, "problems": [], "targeted_rules": 9}


def test_clean_scan_passes() -> None:
    r = run("scan", "--root", "src", "--format", "json")
    assert r.returncode == 0, r.stdout
    report = json.loads(r.stdout)
    assert report["findings"] == []
    assert set(report["rules"]) == {"R-7", "R-20", "R-21", "R-22", "R-23"}


def test_scan_flags_the_bypass_module() -> None:
    r = run("scan", "--root", "src", "--root", "bypass", "--format", "json")
    assert r.returncode == 1
    found = {(f["rule"], f["code"], f["path"][0] if f["path"] else None) for f in json.loads(r.stdout)["findings"]}
    codes = {(rule, code) for rule, code, _ in found}
    assert ("R-23", "bypass", "pool_service.rogue.emergency_resize") in found
    assert ("R-20", "bypass", "pool_service.rogue.overbook") in found
    assert {("R-23", "forbidden/unwrap"), ("R-23", "forbidden/getattr-sink")} <= codes
    assert not any("sql" in code for _, code in codes)         # raw SQL is below the API: not a finding


def test_sarif_output(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "scan.sarif"
    r = run("scan", "--root", "src", "--root", "bypass", "--format", "sarif", "--output", str(out))
    assert r.returncode == 1
    assert json.loads(out.read_text())["runs"][0]["results"]


def test_status() -> None:
    r = run("status", "--root", "src", "--import", "pool_service.api", "--json")
    assert r.returncode == 0, r.stderr
    rows = {row["id"]: row for row in json.loads(r.stdout)["rules"]}
    assert rows["R-21"]["standing"] == "held"
    assert rows["R-20"]["standing"] == "enforce(known-callers)"          # the concurrent-writers residual path
    assert any("residual paths" in why for why in rows["R-20"]["reasons"])


def _rogue():
    spec = importlib.util.spec_from_file_location("pool_service_rogue", ROGUE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_bypass_really_bypasses(conn: sqlite3.Connection) -> None:
    """Why the scan matters: the rogue resize skips R-21 at run time (only the store's own trigger still
    refuses an over-lease, which no rule relies on)."""
    rogue = _rogue()
    service.create_pool(conn, "p1", "P1", 10)
    rogue.emergency_resize(conn, "p1", 1000)            # no approval, 100x growth: nothing at run time stops it
    assert store.size(conn, "p1") == 1000
    with pytest.raises(sqlite3.IntegrityError, match="R-20"):
        rogue.overbook(conn, "p1") or rogue.overbook(conn, "p1")
    with pytest.raises(inv.Rejected):                   # the door itself still works
        service.lease_slots(conn, "p1", 1, "x")
