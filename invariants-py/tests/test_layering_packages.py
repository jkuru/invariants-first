"""Layering of the guard, registry, cli and mcp packages, read from their import statements (lazy ones too).

The direction, from the imports as they are: registry (rules, verify, standing, review stores) is the
bottom of the runtime and imports neither the guards that use it nor the static layers (scan, lock)
nor the CLI; guard (the decorators) builds on registry but not on scan, lock or the CLI; scan and lock
never import the CLI (test_layering_scan.py, test_layering_lock.py), and nothing outside cli/ but the
MCP server does. Inside each package the modules import one another in one direction only."""
from __future__ import annotations

import ast
import pathlib

import pytest

import invariants

SRC = pathlib.Path(invariants.__file__).parent
FORBIDDEN = {
    "registry": ("invariants.guard", "invariants.cli", "invariants.scan", "invariants.lock", "invariants.lockfile",
                 "invariants.integrations", "invariants.mcp", "invariants.status_report"),
    "guard": ("invariants.cli", "invariants.scan", "invariants.lock", "invariants.lockfile",
              "invariants.integrations", "invariants.mcp", "invariants.status_report"),
    # the CLI reads the lock through invariants.lock, never the compatibility name
    "cli": ("invariants.lockfile",),
    # the MCP server reads through the CLI and the scan; it never imports the runtime guards
    "mcp": ("invariants.lockfile", "invariants.guard", "invariants.integrations", "invariants.pytest_plugin"),
}
# the modules of its own package each module may import (the package __init__ composes them all)
STAGES = {
    # coverage and ablation: Registry for typing; context: Registry and observe, which __init__ defines first
    "registry": {"reviews": set(), "session": set(), "events": {"reviews"}, "ablation": {"__init__", "events", "session"},
                 "coverage": {"__init__"}, "context": {"__init__", "events", "reviews"}},
    "guard": {"checked": set(), "replies": {"checked"}, "plan": {"checked", "replies"},
              "decorators": {"checked", "plan", "replies"}},
    "cli": {"common": set(), "project": {"common"}, "git_base": {"common", "project"},
            "commands": {"common", "git_base", "project"}, "hooks": {"commands", "common", "project"},
            "__main__": {"__init__"}},
    "mcp": {"confine": set(), "tools": {"confine"}, "propose": {"confine", "tools"},
            "protocol": {"confine", "propose", "tools"}, "__main__": {"__init__"}},
}


def _imports(path: pathlib.Path, pkg: str) -> set[str]:
    """Every module a file of the package invariants.<pkg> imports ("" for a top-level module), at any
    depth, as an absolute name: `.x` is invariants.<pkg>.x, `..x` invariants.x."""
    parts = ["invariants", *pkg.split(".")] if pkg else ["invariants"]
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = (node.module or "") if node.level == 0 else ".".join(
                [*parts[:len(parts) - node.level + 1], *([node.module] if node.module else [])])
            out |= {base} | {f"{base}.{a.name}" for a in node.names}
    return out


def _files(pkg: str) -> list[pathlib.Path]:
    files = sorted((SRC / pkg).glob("*.py"))
    assert {f.stem for f in files} >= set(STAGES[pkg]), pkg
    return files


@pytest.mark.parametrize("pkg, path", [(p, f) for p in FORBIDDEN for f in _files(p)],
                         ids=lambda v: v if isinstance(v, str) else v.name)
def test_package_never_imports_a_layer_above(pkg: str, path: pathlib.Path) -> None:
    bad = sorted(m for m in _imports(path, pkg) for f in FORBIDDEN[pkg] if m == f or m.startswith(f + "."))
    assert not bad, f"{pkg}/{path.name} imports {bad}"


@pytest.mark.parametrize("pkg, stage", [(p, s) for p in STAGES for s in sorted(STAGES[p])])
def test_modules_import_in_one_direction(pkg: str, stage: str) -> None:
    mods = _imports(SRC / pkg / f"{stage}.py", pkg)
    prefix = f"invariants.{pkg}."
    used = {m[len(prefix):].split(".")[0] for m in mods if m.startswith(prefix)}
    used = {u for u in used if (SRC / pkg / f"{u}.py").is_file()}
    if f"invariants.{pkg}" in mods:
        used.add("__init__")
    assert used <= STAGES[pkg][stage], f"{pkg}/{stage}.py imports {pkg}.{sorted(used - STAGES[pkg][stage])}"


def test_only_the_cli_and_the_mcp_server_import_the_cli() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 40
    for path in files:
        rel = path.relative_to(SRC)
        if rel.parts[0] in ("cli", "mcp"):
            continue
        mods = _imports(path, ".".join(rel.parts[:-1]))
        bad = sorted(m for m in mods if m == "invariants.cli" or m.startswith("invariants.cli."))
        assert not bad, f"{rel} imports {bad}"


def test_the_import_reader_resolves_relative_imports(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("from ..cli import main\ndef f():\n    from .. import scan\n    import invariants.lock\n"
                   "from .plan import x\nfrom . import y\n")
    got = _imports(bad, "guard")
    assert {"invariants.cli", "invariants.scan", "invariants.lock", "invariants.guard.plan",
            "invariants.guard"} <= got
    top = tmp_path / "top.py"
    top.write_text("from . import cli\nfrom .cli import x\n")
    assert {"invariants.cli", "invariants.cli.x"} <= _imports(top, "")
    deep = tmp_path / "deep.py"
    deep.write_text("from ... import cli\n")
    assert "invariants.cli" in _imports(deep, "integrations.x")
