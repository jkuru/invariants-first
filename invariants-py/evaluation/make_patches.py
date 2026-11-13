"""Rebuild patches/<ID>.patch from the text edits in changes.py, against a copy of the current apps.

Usage: python evaluation/make_patches.py [--apps DIR] [--out DIR] [ID ...]

The patches in patches/ are the record of the published runs; by default this writes new ones to a
temporary directory, so they can be compared with that record before replacing it (--out patches).
Changes flagged "openapi" regenerate the app's openapi.json with scripts/export_openapi.py.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from changes import CHANGES, REBASED  # noqa: E402

IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", ".hypothesis", "build", "*.db", ".venv")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("ids", nargs="*")
    p.add_argument("--apps", default=str(HERE.parent.parent / "apps"))
    p.add_argument("--out", help="where the patches go (default: a new temporary directory)")
    a = p.parse_args(argv)
    out = pathlib.Path(a.out) if a.out else pathlib.Path(tempfile.mkdtemp(prefix="inv-patches-"))
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="inv-make-patches-") as d:
        base = pathlib.Path(d)
        for cid in a.ids or list(CHANGES):
            spec = REBASED.get(cid) or CHANGES[cid]          # the current apps: re-based edits first
            app, edits, flags = spec[0], spec[2], spec[3:]
            for sub in ("pristine", "tmp"):
                shutil.rmtree(base / sub, ignore_errors=True)
                shutil.copytree(pathlib.Path(a.apps) / app, base / sub / app, ignore=IGNORE)
            tmp = base / "tmp" / app
            for rel, old, new in edits:
                text = (tmp / rel).read_text()
                if text.count(old) != 1:
                    raise SystemExit(f"{cid}: {rel}: expected 1 match, found {text.count(old)}: {old[:60]!r}")
                (tmp / rel).write_text(text.replace(old, new))
            if "openapi" in flags:
                env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(tmp / "src"),
                                                                                  os.environ.get("PYTHONPATH")]))}
                subprocess.run([sys.executable, "scripts/export_openapi.py"], cwd=tmp, env=env, check=True,
                               capture_output=True)
            diff = subprocess.run(["diff", "-ruN", "-x", "__pycache__", "-x", ".hypothesis", "-x", "build",
                                   "-x", ".pytest_cache", f"pristine/{app}", f"tmp/{app}"], cwd=base,
                                  capture_output=True, text=True).stdout
            (out / f"{cid}.patch").write_text(diff)
            added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
            removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
            print(f"{cid} {app:14} +{added} -{removed}")
    print(f"patches written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
