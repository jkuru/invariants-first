"""Helpers for the `inv` CLI tests. Commands that import an app run in a subprocess, so the
fixture apps never enter the test process (their guards would otherwise join every verify)."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

CLI = pathlib.Path(__file__).resolve().parent / "fixtures" / "cli"
CLEAN = CLI / "clean"
BYPASS = CLI / "bypass"


def run_inv(*args: str | os.PathLike[str], cwd: str | os.PathLike[str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run `inv` in a fresh interpreter."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("INV_ABLATE", None)
    return subprocess.run([sys.executable, "-m", "invariants.cli", *map(str, args)], capture_output=True,
                          text=True, cwd=cwd, env=env, timeout=60)


def copy_app(src: pathlib.Path, dst: pathlib.Path) -> pathlib.Path:
    """A writable copy of a fixture app."""
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
    return dst


def write_project(root: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    """Write a small source tree for scan tests."""
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root
