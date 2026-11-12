"""`inv scan` passes on the clean roots and flags the deliberate bypass module."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
INV = pathlib.Path(sys.executable).with_name("inv")
TOOL = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]
CFG = {"clean": TOOL["invariants"]["scan"]["roots"], "bypass": TOOL["fact-pipeline"]["scan"]["bypass"]}


def scan(roots: list[str]) -> tuple[int, dict]:
    args = [str(INV), "scan", "invariants.yaml", "--format", "json"]
    for r in roots:
        args += ["--root", r]
    p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False)
    return p.returncode, json.loads(p.stdout)


def test_clean_scan_passes() -> None:
    code, rep = scan(CFG["clean"])
    assert code == 0, rep["findings"]
    assert rep["rules"]["K-1"]["result"] == "pass" and rep["rules"]["K-12"]["result"] == "pass"


def test_scan_flags_the_bypass_module() -> None:
    code, rep = scan(CFG["clean"] + CFG["bypass"])
    assert code == 1 and rep["rules"]["K-12"]["result"] == "fail"
    kinds = {(f["kind"], f["code"]) for f in rep["findings"] if f["file"] == "fact_pipeline_backfill/backfill.py"}
    assert ("bypass", "bypass") in kinds and ("forbidden", "forbidden/raw-sql") in kinds


def test_bypass_is_not_under_the_clean_roots() -> None:
    assert all(not (ROOT / r / "fact_pipeline_backfill").exists() for r in CFG["clean"])
