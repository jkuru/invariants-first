"""The `inv` command line: init, check, hook, validate, lock, verify, scan, status.

Exit codes: 0 ok; 1 a check failed (invalid rule file, stale lock, verify or scan failure);
2 usage error; 3 scan found nothing failing but some calls are unresolved (not a pass);
4 scan skipped an enforce rule because it is ablated (INV_ABLATE; not a pass).

The package: common (output, the rule file, with the exit codes it re-exports from invariants.project),
invariants.project (pyproject.toml, import and scan roots; shared with the MCP server), commands (the cmd_* subcommands), git_base (`inv check --base`), hooks (`inv hook`); this
module builds the parser and dispatches.
"""
from __future__ import annotations

# ruff: noqa: F401   (the names below are the package's API)
import argparse
import os
import pathlib
import sys
from typing import Any, Callable, Sequence

import invariants_scan as scanner

from ..errors import LoadError
from ..loader import read_document
from .commands import cmd_check, cmd_init, cmd_lock, cmd_mcp, cmd_scan, cmd_status, cmd_validate, cmd_verify
from .common import ABLATED, ALLOW_WEAKENING_ENV, FAILED, OK, UNRESOLVED, USAGE, Usage, _describe, _err
from .hooks import cmd_hook
from ..project import (DEFAULT_SCAN_EXCLUDE, _exclude, _pyproject, _roots, _setuptools_where, _str_list, import_roots,
                      project_root, scan_inputs, source_dirs)

_Subparser = Callable[[argparse.ArgumentParser], None]


# --- parser -------------------------------------------------------------------------------------
def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inv", description="Invariant-first rule files: check, lock, verify, scan.",
                                allow_abbrev=False,
                                epilog="exit codes: 0 ok, 1 check failed, 2 usage error, 3 scan unresolved, "
                                "4 scan skipped an ablated enforce rule")
    from .. import __version__
    p.add_argument("-V", "--version", action="version", version=f"inv (invariants) {__version__}")
    sub = p.add_subparsers(dest="command", metavar="command")

    def add(name: str, fn: Callable[[argparse.Namespace], int], help: str, imports: bool = True) -> Any:
        sp = sub.add_parser(name, help=help, description=help, allow_abbrev=False)   # no --accept-w
        sp.add_argument("rulefile", nargs="?", help="rule file (default: ./invariants.yaml|yml|json)")
        if imports:
            sp.add_argument("--path", action="append", default=[], metavar="DIR",
                            help="also import your app from DIR (repeatable); your project's src/, its "
                                 "[tool.setuptools.packages.find] where and scan roots, the rule file's "
                                 "directory and the project root are on the import path already, ahead "
                                 "of site-packages")
            sp.add_argument("--site", action="append", default=[], metavar="DIR",
                            help="import your app's dependencies from DIR too, after inv's own (repeatable): "
                                 "the site-packages of your project's environment when inv runs from an "
                                 "interpreter of its own; its .pth files are not run")
        sp.set_defaults(fn=fn)
        return sp

    for name, fn, help, imports, options in _COMMANDS:
        options(add(name, fn, help, imports))
    return p


def _no_options(sp: argparse.ArgumentParser) -> None:
    """validate takes only the rule file and --path."""


def _lock_options(lk: argparse.ArgumentParser) -> None:
    lk.add_argument("--check", action="store_true", help="compare instead of writing; exit 1 when stale")
    lk.add_argument("--update-artifacts", action="store_true",
                    help="first rewrite stale artifact sha256 digests in the rule file (review the diff)")
    lk.add_argument("--lock", metavar="FILE", help="lockfile path (default: invariants.lock beside the rule file)")
    lk.add_argument("--accept-weakening", action="store_true",
                    help="re-pin even when a change may weaken a rule (lower status or mode, removed enforcer or "
                         "clause, fail_open, a looser threshold, a changed bind, view, provider, "
                         "model_policy or artifact); without it `inv lock` refuses")
    _weakening_alias(lk, "--allow-weakening", "accept_weakening")
    lk.add_argument("--root", action="append", default=[], metavar="DIR",
                    help="pin this scan root (as `inv check --root`; prefer [tool.invariants.scan] roots)")
    lk.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="pin this scan exclude (as `inv check --exclude`; prefer [tool.invariants.scan] exclude)")


# `inv lock` and `inv check` each take both spellings: --accept-weakening is the documented one, and
# --allow-weakening (the name `inv check` used first) is a hidden alias of it. `inv hook pretool`
# blocks both.
WEAKENING_FLAGS = ("--accept-weakening", "--allow-weakening")


def _weakening_alias(sp: argparse.ArgumentParser, flag: str, dest: str) -> None:
    sp.add_argument(flag, dest=dest, action="store_true", help=argparse.SUPPRESS)


def _verify_options(vf: argparse.ArgumentParser) -> None:
    vf.add_argument("--import", dest="imports", action="append", default=[], metavar="MODULE",
                    help="also import MODULE (contracts of type python are imported already)")
    vf.add_argument("--json", action="store_true")


def _scan_options(sc: argparse.ArgumentParser) -> None:
    sc.add_argument("--root", action="append", default=[], metavar="DIR",
                    help="source root to scan (repeatable; default: the rule file's directory)")
    sc.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="skip files matching GLOB, relative to each root (repeatable; also "
                         "[tool.invariants.scan] exclude in pyproject.toml)")
    sc.add_argument("--format", choices=("text", "json", "sarif"), default="text")
    sc.add_argument("--output", metavar="FILE", help="write the report to FILE")


def _init_options(it: argparse.ArgumentParser) -> None:
    it.add_argument("--root", action="append", default=[], metavar="DIR",
                    help="source root to read (repeatable; default: src/ or the current directory)")
    it.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="skip files matching GLOB (tests, docs, examples and scripts are skipped already)")
    it.add_argument("--no-lock", action="store_true", help="write the rule file only; do not run `inv lock`")
    it.add_argument("--no-doors", action="store_true",
                    help="do not look for doors (the service functions every call to a repository's writes "
                         "goes through)")
    it.add_argument("--graphify", "--graph", metavar="GRAPH",
                    help="read a Graphify graph.json for door hints (default: graphify-out/graph.json when it "
                         "exists); every suggested door is still checked by the scan")
    it.add_argument("--no-graphify", action="store_true", help="do not read a Graphify graph")
    it.add_argument("--max-doors", type=int, default=5, metavar="N",
                    help="write at most N door rules, the most important first (default 5); the rest are listed")


def _check_options(ck: argparse.ArgumentParser) -> None:
    ck.add_argument("--relock", action="store_true",
                    help="re-pin a stale lock when no change weakens a rule (for pre-commit); a weakening "
                         "still fails")
    ck.add_argument("--fail-on-repin", action="store_true",
                    help="with --relock: exit 1 after re-pinning, so the new lock is reviewed and staged (the "
                         "pre-commit hook)")
    ck.add_argument("--base", metavar="REF",
                    help="also fail when the rules weaken since the lock committed at git REF (e.g. origin/main), "
                         "even if the change re-pinned the lock with --accept-weakening")
    ck.add_argument("--accept-weakening", dest="allow_weakening", action="store_true",
                    help=f"with --base: report weakenings but pass (also {ALLOW_WEAKENING_ENV}=1); for a "
                         f"reviewer to set, not the change's author")
    _weakening_alias(ck, "--allow-weakening", "allow_weakening")
    ck.add_argument("--root", action="append", default=[], metavar="DIR",
                    help="scan this source root (repeatable; as `inv scan --root`, also [tool.invariants.scan] roots)")
    ck.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="scan: skip files matching GLOB (as `inv scan --exclude`)")
    ck.add_argument("--import", dest="imports", action="append", default=[], metavar="MODULE",
                    help="guards: also import MODULE and check its guards (repeatable; as `inv verify "
                         "--import`); a module nothing imports is not checked")


HOOK_EVENTS = ("pretool", "stop")


class _HookEvent(argparse.Action):
    """`inv hook`'s event, before or after the rule file: `inv hook stop invariants.yaml` and
    `inv hook invariants.yaml stop` are the same call (argparse fills the optional rule file first)."""

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: Any,
                 option_string: str | None = None) -> None:
        first = getattr(namespace, "rulefile", None)
        if values not in HOOK_EVENTS and first in HOOK_EVENTS:
            namespace.rulefile, values = values, first
        if values not in HOOK_EVENTS:
            raise argparse.ArgumentError(self, f"invalid choice: {values!r} (choose from "
                                               f"{', '.join(map(repr, HOOK_EVENTS))})")
        setattr(namespace, self.dest, values)


def _hook_options(hk: argparse.ArgumentParser) -> None:
    hk.add_argument("event", action=_HookEvent, metavar="{pretool,stop}",
                    help="pretool (before a tool call) or stop (before the agent finishes); before or "
                         "after the rule file")


def _status_options(st: argparse.ArgumentParser) -> None:
    st.add_argument("--root", action="append", default=[], metavar="DIR",
                    help="scan this source root (repeatable; as `inv scan --root`, also [tool.invariants.scan] roots)")
    st.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="scan: skip files matching GLOB (as `inv scan --exclude`)")
    st.add_argument("--import", dest="imports", action="append", default=[], metavar="MODULE",
                    help="also import MODULE, so its guards count (repeatable; as `inv verify --import`)")
    st.add_argument("--json", action="store_true", help="print the report as JSON")
    st.add_argument("--ablation", metavar="JSON",
                    help="the pytest --inv-ablate --inv-report-json file; without it no rule is reported held")


def _mcp_options(mc: argparse.ArgumentParser) -> None:
    mc.add_argument("--root", default=".", metavar="DIR", help="project root (default: the current directory)")


# name, implementation, help, whether it takes --path, its other options; in `inv --help` order
_COMMANDS: tuple[tuple[str, Callable[[argparse.Namespace], int], str, bool, _Subparser], ...] = (
    ("validate", cmd_validate, "schema and every load-time check; exit 1 with readable errors", True, _no_options),
    ("lock", cmd_lock, "write invariants.lock; with --check, fail when it is stale", True, _lock_options),
    ("verify", cmd_verify, "import the app and check every target is guarded and every guard named", True,
     _verify_options),
    ("scan", cmd_scan, "static call-graph check: no path to a protected sink avoids its door", False, _scan_options),
    ("init", cmd_init, "draft invariants.yaml from the pydantic constraints, doors and function "
     "preconditions the code already has, then lock it", True, _init_options),
    ("check", cmd_check, "validate, lock --check, verify and scan with one exit code: for CI, "
     "pre-commit and agent hooks", True, _check_options),
    ("hook", cmd_hook, "a hook for AI coding agents (reads Claude Code's hook JSON on stdin; exit 2 "
     "blocks): pretool refuses commands that accept a weakening, stop refuses to finish while "
     "`inv check` fails", True, _hook_options),
    ("status", cmd_status, "standing per rule from verify and scan", True, _status_options),
    ("mcp", cmd_mcp, "read-only MCP server over stdio for coding agents (docs/agents.md)", False, _mcp_options),
)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `inv`."""
    p = parser()
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if not getattr(a, "fn", None):
        p.print_usage(sys.stderr)
        _err(f"inv: choose a command: {', '.join(c[0] for c in _COMMANDS)}")
        return USAGE
    # the reviewer's switch is read here, before anything could import the app (which never happens
    # in this process: the imports run in a child, worker.py)
    a.allow_weakening_env = os.environ.get(ALLOW_WEAKENING_ENV) == "1"
    from . import common
    common.SITE[:] = [str(pathlib.Path(d).resolve()) for d in getattr(a, "site", None) or []]
    try:
        return a.fn(a)
    except Usage as e:
        _err(f"inv {a.command}: {e}")
        return USAGE
    except (LoadError, scanner.ScanConfigError) as e:
        _err(f"inv {a.command}: {_describe(e)}")
        return FAILED
    except Exception as e:          # an app module failing at import, for example
        _err(f"inv {a.command}: {_describe(e, typed=True)}")
        return FAILED
