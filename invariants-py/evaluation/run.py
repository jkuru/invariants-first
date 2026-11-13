"""Run the 24-change evaluation against the example apps and compare with the published results.

For each change: apply it to a working copy of its app, run the app's own ci.sh (and, if it stopped
at `inv lock --check`, the reflex re-pin `inv lock` and ci.sh again), run baseline B1 (the same
tests with every rule ablated), then put the copy back and check it matches the pristine copy.

Usage (from anywhere; nothing is written to the repository unless --results points into it):

    python evaluation/run.py                      # all 24 changes
    python evaluation/run.py P02 F12              # a subset
    python evaluation/run.py --apps ../apps --work /tmp/inv-eval --python .venv/bin/python

--python is the interpreter ci.sh runs (PYTHON); `inv` must sit next to it, with the invariants
library and both apps' test dependencies installed (`pip install -e 'invariants-py[dev]'`). A
PYTHONPATH in the environment is kept (after each app's src/), so a library checkout can be tested
without installing it.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from changes import CHANGES, REBASED  # noqa: E402

APPS = ("pool-service", "fact-pipeline")
IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", ".hypothesis", "build", "*.db", ".venv")
EXCL = ["-x", "__pycache__", "-x", ".hypothesis", "-x", "build", "-x", ".pytest_cache", "-x", "*.db", "-x", ".venv"]


class Run:
    def __init__(self, a: argparse.Namespace) -> None:
        self.apps = pathlib.Path(a.apps).resolve()
        self.work = pathlib.Path(a.work).resolve() if a.work else pathlib.Path(tempfile.mkdtemp(prefix="inv-eval-"))
        self.results = pathlib.Path(a.results).resolve() if a.results else self.work / "results"
        self.py = str(pathlib.Path(a.python).absolute()) if a.python else sys.executable
        self.inv = str(pathlib.Path(self.py).parent / "inv")
        # The published runs predate the apps' replay tests (tests/test_replay.py). Those match every
        # model request exactly, so any change to what reaches the model fails them, a defect (F06)
        # or a benign edit (F12) alike; they are left out unless --with-replay.
        self.addopts = "" if a.with_replay else "--ignore=tests/test_replay.py"
        self.results.mkdir(parents=True, exist_ok=True)

    # --- environment ----------------------------------------------------------------------------
    def env(self, app: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        e = {k: v for k, v in os.environ.items() if not k.startswith("INV_")}
        if self.addopts:
            e["PYTEST_ADDOPTS"] = " ".join(filter(None, [e.get("PYTEST_ADDOPTS"), self.addopts]))
        path = os.pathsep.join(filter(None, [str(self.work / app / "src"), os.environ.get("PYTHONPATH")]))
        e.update({"PYTHON": self.py, "PYTHONPATH": path, "PYTHONDONTWRITEBYTECODE": "1"}, **(extra or {}))
        return e

    def sh(self, cmd: list[str], app: str, extra: dict[str, str] | None = None,
           timeout: int = 900) -> tuple[int, str, float]:
        t = time.monotonic()
        p = subprocess.run(cmd, cwd=self.work / app, env=self.env(app, extra), capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout + p.stderr, round(time.monotonic() - t, 2)

    def setup(self) -> None:
        """Copy each app twice (a working copy and a pristine one), then find B1's deselect list."""
        for app in APPS:
            for dst in (self.work / app, self.work / "pristine" / app):
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(self.apps / app, dst, ignore=IGNORE)
            control = self.ci(app, "control", "pristine")
            if control["rc"] != 0:
                raise SystemExit(f"{app}: ci.sh fails on the unchanged app ({control['step']}); see "
                                 f"{self.results / ('pristine-' + app + '.control.log')}")
            desel = self.results / f"deselect-{app}.txt"
            if not desel.exists():
                rc, log, _ = self.sh([self.py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE"], app,
                                     self.ablate_all(app))
                desel.write_text("\n".join(failures(log)) + "\n")
            print(f"{app}: ci.sh green in {control['secs']}s; B1 drops {len(desel.read_text().split())} test(s) "
                  f"that fail with every rule ablated on the unchanged app", flush=True)

    def ablate_all(self, app: str) -> dict[str, str]:
        """B1's environment: every rule of the app ablated (INV_ABLATE, armed as the plugin arms it)."""
        import yaml
        doc = yaml.safe_load((self.work / "pristine" / app / "invariants.yaml").read_text())
        ids = [r["id"] for r in doc.get("rules") or [] if isinstance(r, dict) and r.get("status") != "retired"]
        return {"INV_ABLATE": ",".join(ids), "INV_ABLATE_TESTING": "1"}

    # --- one change -----------------------------------------------------------------------------
    def ci(self, app: str, tag: str, cid: str) -> dict[str, Any]:
        rc, log, t = self.sh(["bash", "ci.sh"], app)
        (self.results / f"{cid}-{app}.{tag}.log" if cid == "pristine" else self.results / f"{cid}.{tag}.log").write_text(log)
        return {"rc": rc, "secs": t, "step": "ok" if rc == 0 else failed_step(log), "failed_tests": failures(log),
                "not_load_bearing": re.findall(r"(\S+).*not load-bearing", log)}

    def baseline(self, app: str, cid: str) -> dict[str, Any]:
        desel = (self.results / f"deselect-{app}.txt").read_text().split()
        rc, log, _ = self.sh([self.py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE",
                              *[f"--deselect={d}" for d in desel]], app, self.ablate_all(app))
        (self.results / f"{cid}.baseline.log").write_text(log)
        return {"rc": rc, "failed_tests": failures(log)}

    def apply(self, cid: str, app: str) -> str:
        """Apply the change: the published patch, else its text edits, else the re-based edits."""
        patch = HERE / "patches" / f"{cid}.patch"
        dry = subprocess.run(["patch", "--dry-run", "-s", "-p2", "-d", str(self.work / app), "-i", str(patch)],
                             capture_output=True, text=True)
        if dry.returncode == 0:
            subprocess.run(["patch", "-s", "-p2", "-d", str(self.work / app), "-i", str(patch)], check=True,
                           capture_output=True)
            self.drop_backups(app)
            return "patch"
        for how, table in (("edits", CHANGES), ("rebased", REBASED)):
            if cid in table and edit(self.work / app, table[cid][2], dry_run=True):
                edit(self.work / app, table[cid][2])
                if "openapi" in table[cid][3:]:
                    self.sh([self.py, "scripts/export_openapi.py"], app)
                return how
        return "not applicable"

    def drop_backups(self, app: str) -> None:
        """Delete the .orig backups `patch` leaves when a hunk applies at an offset (the published run's
        P05 to P12 tripped on one, see docs/evaluation.md)."""
        for f in (self.work / app).rglob("*.orig"):
            if not (self.work / "pristine" / app / f.relative_to(self.work / app)).exists():
                f.unlink()

    def restore(self, app: str, applied_by: str, cid: str) -> bool:
        """Undo the change and report whether the copy matched the pristine copy afterwards."""
        if applied_by == "patch":
            subprocess.run(["patch", "-s", "-R", "-p2", "-d", str(self.work / app), "-i",
                            str(HERE / "patches" / f"{cid}.patch")], capture_output=True)
            self.drop_backups(app)
            shutil.copy(self.work / "pristine" / app / "invariants.lock", self.work / app / "invariants.lock")
            d = subprocess.run(["diff", "-rq", *EXCL, str(self.work / "pristine" / app), str(self.work / app)],
                               capture_output=True, text=True).stdout.strip()
        else:
            d = ""
        shutil.rmtree(self.work / app)                      # always start the next change from pristine
        shutil.copytree(self.work / "pristine" / app, self.work / app, ignore=IGNORE)
        if d:
            print(f"{cid}: not clean after patch -R (restored from pristine):\n{d}", file=sys.stderr)
        return not d

    def one(self, cid: str) -> dict[str, Any]:
        app, cat = CHANGES[cid][0], CHANGES[cid][1]
        r: dict[str, Any] = {"id": cid, "app": app, "cat": cat}
        r["applied_by"] = how = self.apply(cid, app)
        try:
            if how != "not applicable":
                r["ci"] = self.ci(app, "ci", cid)
                if r["ci"]["step"].startswith("inv lock"):
                    rc, log, _ = self.sh([self.inv, "lock", "invariants.yaml"], app)
                    r["repin"] = {"rc": rc, "output": log.strip().splitlines()[-3:]}
                    r["ci_after_repin"] = self.ci(app, "ci2", cid)
                r["baseline"] = self.baseline(app, cid)
        finally:
            r["reversed_clean"] = self.restore(app, how, cid)
        (self.results / f"{cid}.json").write_text(json.dumps(r, indent=2) + "\n")
        return r


# --- helpers --------------------------------------------------------------------------------------
def edit(root: pathlib.Path, edits: list[tuple[str, str, str]], dry_run: bool = False) -> bool:
    """Apply exact text edits (each old text must occur once); with dry_run, only check they would apply."""
    texts: dict[str, str] = {}
    for rel, old, new in edits:
        f = root / rel
        text = texts.get(rel, f.read_text() if f.is_file() else "")
        if text.count(old) != 1:
            return False
        texts[rel] = text.replace(old, new)
    if not dry_run:
        for rel, text in texts.items():
            (root / rel).write_text(text)
    return True


def failed_step(log: str) -> str:
    steps = re.findall(r"^== (.+)$", log, re.M)
    return steps[-1].strip() if steps else "?"


def failures(log: str) -> list[str]:
    return sorted(set(re.findall(r"^(?:FAILED|ERROR) (\S+)", log, re.M)))


def shape(r: dict[str, Any]) -> dict[str, Any]:
    """What a result is compared on: where CI stopped, how many tests failed, the re-pin and B1."""
    if "ci" not in r:
        return {"applied": False}
    c2 = r.get("ci_after_repin")
    return {"ci": r["ci"]["step"], "ci_fails": len(r["ci"]["failed_tests"]),
            "repin": c2["step"] if c2 else None, "b1_fails": len(r["baseline"]["failed_tests"])}


def compare(results: list[dict[str, Any]]) -> list[str]:
    def fmt(s: dict[str, Any]) -> str:
        if not s:
            return "-"
        if not s.get("applied", True):
            return "not applied"
        step = s["ci"].split(" (")[0]                     # "pytest (R-23's bypass scan ...)" -> "pytest"
        repin = f", re-pin: {s['repin'].split(' (')[0]}" if s.get("repin") else ""
        return f"{step}{repin}; {s['ci_fails']} failing, B1 {s['b1_fails']}"

    rows = []
    for r in results:
        pub_file = HERE / "published" / f"{r['id']}.json"
        pub = shape(json.loads(pub_file.read_text())) if pub_file.exists() else {}
        rows.append((r, fmt(shape(r)), fmt(pub), shape(r) == pub))
    w = max([len("published"), *(max(len(a), len(b)) for _, a, b, _ in rows)])
    lines = [f"{'id':<4} {'cat':<3} {'applied by':<10} {'now':<{w}} {'published':<{w}} same"]
    same = sum(ok for *_, ok in rows)
    for r, now, pub, ok in rows:
        lines.append(f"{r['id']:<4} {r['cat']:<3} {r['applied_by']:<10} {now:<{w}} {pub:<{w}} {'yes' if ok else 'NO'}")
    lines.append(f"{same} of {len(results)} match the published results (where CI stopped, the number of "
                 f"failing tests, the re-pin and B1)")
    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("ids", nargs="*", help="change ids (default: all 24)")
    p.add_argument("--apps", default=str(HERE.parent.parent / "apps"), help="the example apps (default: ../../apps)")
    p.add_argument("--work", help="working directory for the copies (default: a new temporary directory)")
    p.add_argument("--results", help="where results go (default: WORK/results)")
    p.add_argument("--python", help="interpreter for ci.sh, with inv next to it (default: this one)")
    p.add_argument("--with-replay", action="store_true",
                   help="also run the apps' replay tests (not in the published runs; see README)")
    a = p.parse_args(argv)
    unknown = [i for i in a.ids if i not in CHANGES]
    if unknown:
        p.error(f"unknown change id(s): {', '.join(unknown)}")
    run = Run(a)
    print(f"working copies in {run.work}; results in {run.results}", flush=True)
    run.setup()
    results = []
    for cid in a.ids or list(CHANGES):
        r = run.one(cid)
        results.append(r)
        s = shape(r)
        print(f"{cid} {r['cat']} {r['applied_by']}: " + (
            "not applied" if not s.get("applied", True) else
            f"ci={s['ci']}({r['ci']['secs']}s) " + (f"repin->{s['repin']} " if s["repin"] else "")
            + f"fails={s['ci_fails']} baseline_fails={s['b1_fails']} reversed={r['reversed_clean']}"), flush=True)
    table = compare(results)
    (run.results / "summary.txt").write_text("\n".join(table) + "\n")
    print("\n".join(table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
