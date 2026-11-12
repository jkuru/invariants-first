"""The benchmark script runs and reports p50/p99 for each case."""
from __future__ import annotations

import json
import subprocess
import sys

from conftest import APP_ROOT


def test_bench_runs() -> None:
    r = subprocess.run([sys.executable, "bench/bench_guards.py", "--n", "60", "--json"], cwd=APP_ROOT,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    assert len(rows) == 5
    assert all({"p50_us", "p99_us"} <= set(row["with"]) for row in rows)
