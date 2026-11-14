"""Helpers for the scan's tests: a small source tree on disk, and the excludes invariants-first's
`inv scan` passes by default (invariants.lock.DEFAULT_SCAN_EXCLUDE; invariants-py's
tests/test_layering_scan.py checks the two agree)."""
from __future__ import annotations

import pathlib

from invariants_scan import anywhere

# test code and migrations are not production paths: skipped at any depth
DEFAULT_EXCLUDE = anywhere("tests", "test", "testing", "test_*.py", "*_test.py", "conftest.py",
                           "migrations", "alembic")


def write_project(root: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    """Write a small source tree for scan tests."""
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root
