"""The child process that imports the app for `inv validate`, `inv lock`, `inv check` and `inv init`.

Pinning a target's signature, a model's JSON Schema or a live app's OpenAPI document means importing
the project's code, and imported code can do anything in the process that imports it: set
INV_ALLOW_WEAKENING, or replace `invariants.lock.changes` with one that drops the weakened changes. So
the process that compares the pins, decides WEAKENED and writes the lock never imports the project:
one child per command (`python -I`, the parent's sys.path) loads the rule file, takes the pins, runs
the guard check, and hands back JSON. The pins themselves still come from the imported code and are
only as trustworthy as it is (docs/lockfile.md, "What the lock trusts").

The parent registers what a command needs (Imported.want_*), and the first read runs the child once
with everything registered; a read the run did not cover runs it again.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from typing import Any, Callable, Sequence

from .. import lock
from ..errors import LoadError
from ..scan.text import safe_output
from .common import ALLOW_WEAKENING_ENV, SITE, Usage

_BOOT = ("import json, sys\n"
         "req = json.loads(sys.stdin.buffer.read())\n"
         "sys.path[:] = req['sys_path']\n"
         "sys.dont_write_bytecode = req['dont_write_bytecode']\n"
         "from invariants.cli.child import serve\n"
         "serve(req)\n")


class Reported(LoadError):
    """An error the child raised, as the text `inv` prints for it (its type and the module whose
    import raised it already in the text)."""


class Imported:
    """What one command needs from the code it imports, computed in a child process, and what this
    process read before the child ran (the rule document, the stored lock, the scan): the child can
    write files, so nothing the verdict reads is read after it."""

    def __init__(self, path: pathlib.Path, extra: Sequence[str], doc: dict[str, Any] | None = None) -> None:
        self.path, self.extra, self.doc = path, list(extra), doc
        self.want: dict[str, Any] = {"load": False, "build": None, "olds": [], "verify": None, "contracts": None}
        self.got: dict[str, Any] | None = None
        self.ran = False                              # a child has imported the app: files may have changed
        self._early: dict[Any, tuple[bool, Any]] = {}

    def before(self, key: Any, fn: Callable[[], Any]) -> Any:
        """fn(), computed the first time `key` is asked for (a command asks before the child runs)
        and replayed after, its exception too."""
        if key not in self._early:
            try:
                self._early[key] = (True, fn())
            except Exception as e:
                self._early[key] = (False, e)
        ok, v = self._early[key]
        if not ok:
            raise v
        return v

    # --- what a command will read ---------------------------------------------------------------
    def want_load(self) -> None:
        self._want("load", True)

    def want_build(self, scan: dict[str, Any] | None, scan_error: bool = False) -> None:
        """Register the pins; the ones that need no import are taken here, now (lock.static_pins).
        Once a child has run, only the scan settings registered before it are accepted: settings read
        after it come from files the imported app could have rewritten."""
        wanted = {"scan": scan, "scan_error": scan_error}
        if self.ran and self.want["build"] != wanted:
            raise LoadError("internal error: scan settings that were not read before the app was imported "
                            "cannot be pinned (the app could have rewritten pyproject.toml)")
        with contextlib.suppress(Exception):          # raised by build(), after the child's own errors
            self._static(scan)
        self._want("build", wanted)

    def _static(self, scan: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, str]]:
        got: tuple[dict[str, Any], dict[str, str]] = self.before(
            ("static", json.dumps(scan, sort_keys=True)), lambda: _static(_doc(self), self.path, scan))
        return got

    def want_old(self, old: dict[str, Any]) -> None:
        rules = _rules(old)
        if rules not in self.want["olds"]:
            self.want = {**self.want, "olds": [*self.want["olds"], rules]}
            self.got = None

    def want_verify(self, imports: Sequence[str]) -> None:
        self._want("verify", list(imports))

    def want_contracts(self, records: list[tuple[str, Any]]) -> None:
        self._want("contracts", [[n, r] for n, r in records])

    def _want(self, key: str, value: Any) -> None:
        if self.want[key] != value:
            self.want = {**self.want, key: value}
            self.got = None

    # --- reading ------------------------------------------------------------------------------
    def _result(self) -> dict[str, Any]:
        if self.got is None:
            self.ran = True
            self.got = run_child(self.path, self.extra, self.want)
        return self.got

    def load(self) -> dict[str, Any]:
        """{"notes": [...]} when the rule file loads, {"problem": "load: ..."} when the loader refuses
        it; any other error is raised. "output" is what the import printed."""
        self.want_load()
        got: dict[str, Any] = self._result()["load"]
        return got

    def build(self, scan: dict[str, Any] | None, scan_error: bool = False) -> dict[str, Any]:
        """{"fresh": the lock, "warnings": [...]}: the child's pins with every part that needs no
        import taken from this process (lock.with_static); the error `inv lock` would raise is raised."""
        self.want_build(scan, scan_error)
        got: dict[str, Any] = self._result()["build"]
        if not got.get("shown"):                  # what the import printed, once
            emit(got.get("output", ""))
            got["shown"] = True
        if "raised" in got:
            reraise(got["raised"])
        if "fresh" not in got:                    # stopped at a scan setting the parent raises
            return got
        static, errors = self._static(scan)
        if errors:
            raise LoadError(errors[sorted(errors)[0]])
        return {**got, "fresh": lock.with_static(got["fresh"], static)}

    def types(self, old: dict[str, Any]) -> dict[str, lock.Typer | None]:
        """The clause types of the rule file, replayed for a comparison with `old` (registered first)."""
        self.want_old(old)
        tables = self._result()["build"].get("types") or {}
        return {rid: (None if t is None else _replay(t)) for rid, t in tables.items()}

    def verify(self) -> tuple[int, str]:
        code, text = self._result()["verify"]
        return int(code), str(text)

    def contracts(self) -> list[str | None]:
        return list(self._result()["contracts"])


def _static(doc: dict[str, Any], path: pathlib.Path, scan: dict[str, Any] | None) -> Any:
    return lock.static_pins(doc, path.resolve().parent, scan)


def _doc(imp: Imported) -> dict[str, Any]:
    from ..loader import read_document
    return imp.doc if imp.doc is not None else read_document(imp.path)


def imported(a: argparse.Namespace, path: pathlib.Path, doc: dict[str, Any] | None = None) -> Imported:
    """The Imported a command shares with the commands it runs (`inv check` runs validate, lock and
    verify on one, with the rule document it read first), else a new one over `doc`."""
    got = getattr(a, "imported", None)
    if isinstance(got, Imported):
        return got
    return Imported(path, getattr(a, "path", []) or [], doc)


def _rules(old: dict[str, Any]) -> Any:
    return json.loads(json.dumps(old.get("rules") or {}, default=str))


def _replay(table: list[Any]) -> lock.Typer:
    kinds = {k: (None if v is None else frozenset(v)) for k, v in table}

    def typer(raw: Any) -> frozenset[str] | None:
        return kinds.get(repr(raw))
    return typer


def emit(text: str) -> None:
    if text:
        text = safe_output(text)
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        sys.stdout.flush()


def reraise(err: dict[str, str]) -> None:
    if err["cls"] == "usage":
        raise Usage(err["text"])
    raise Reported(err["text"])


def child_env() -> dict[str, str]:
    """The child's environment: the parent's (an app reads its settings from it at import), less
    the reviewer's switch, which only the parent reads."""
    env = dict(os.environ)
    env.pop(ALLOW_WEAKENING_ENV, None)
    return env


def _child_path() -> list[str]:
    """The child's sys.path: this process's, then the --site directories (common.SITE)."""
    own = [p for p in sys.path if isinstance(p, str)]
    return [*own, *(s for s in SITE if s not in own)]


def run_child(path: pathlib.Path, extra: Sequence[str], want: dict[str, Any]) -> dict[str, Any]:
    """Run the child for `want` and return what it computed."""
    flags = ["-I", *(["-O"] * min(sys.flags.optimize, 2))]
    with tempfile.TemporaryDirectory(prefix="inv-worker-") as tmp:
        out = pathlib.Path(tmp) / "result.json"
        req = {"rulefile": str(path), "extra": list(extra), "want": want, "out": str(out),
               "sys_path": _child_path(),
               "dont_write_bytecode": sys.dont_write_bytecode}
        p = subprocess.run([sys.executable, *flags, "-c", _BOOT], input=json.dumps(req).encode(),
                           capture_output=True, env=child_env())
        stray = (p.stdout + p.stderr).decode("utf-8", "replace").strip()
        try:
            got: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise LoadError(f"the process that imports the app stopped (exit {p.returncode}) before it "
                            f"finished" + (f": {stray.splitlines()[-1]}" if stray else "")) from None
    if stray:
        print(safe_output(stray), file=sys.stderr)
    return got
