"""R-23 is checked by inv scan. Its falsifier runs the scan on src plus the deliberate bypass module.

The scan runs in a subprocess, the way CI runs it. Under `pytest --inv-ablate` the plugin also
sets INV_ABLATE=R-23, so the subprocess skips the rule and this test fails: the ablation is caught.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

APP_ROOT = pathlib.Path(__file__).resolve().parents[1]


def scan(*roots: str) -> tuple[int, dict]:
    r = subprocess.run([sys.executable, "-m", "invariants.cli", "scan", "invariants.yaml", "--format", "json",
                        *[a for root in roots for a in ("--root", root)]],
                       cwd=APP_ROOT, capture_output=True, text=True, timeout=120)
    return r.returncode, json.loads(r.stdout)


def test_r23_scan_finds_the_bypass_module() -> None:
    """Falsifier for R-23: bypass/pool_service/rogue.py writes pools.size around the doors."""
    rc, report = scan("src", "bypass")
    assert rc == 1
    assert report["rules"]["R-23"]["result"] == "fail"
    codes = {f["code"] for f in report["findings"] if f["rule"] == "R-23"}
    assert "bypass" in codes or "forbidden/raw-sql" in codes


def test_clean_scan_passes_for_r23() -> None:
    rc, report = scan("src")
    assert report["rules"]["R-23"]["result"] == "pass"
