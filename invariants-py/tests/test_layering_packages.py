"""Layering of the guard, registry, cli and mcp packages, read from their import statements (lazy ones too).

The direction, from the imports as they are: registry (rules, verify, standing, review stores) is the
bottom of the runtime and imports neither the loader above it, the guards that use it, the static
layers (scan, lock) nor the CLI; the loader imports neither the lock, the integrations, the guards nor
the CLI; guard (the decorators) builds on registry but not on scan, lock or the CLI; scan and lock
never import the CLI (test_layering_scan.py, test_layering_lock.py), and nothing outside cli/ does:
the MCP server shares the project helpers through invariants.project. The top-level packages and
modules import one another without a cycle (the package __init__ wires what a lower layer reads from
a higher one). Inside each package the modules import one another in one direction only.

Three groups are the libraries that can be taken out on their own, and import nothing else of
invariants (lazily or not): the CEL engine (cel/, _typeinfo, typecheck), the scan (scan/, _scan_hooks)
and the schema-diff core of the lock (schema_diff and the CEL weakening judge cel_canon, cel_sites,
cel_mono, over the pure tables in contracts), which may import the CEL engine. Each is standard
library only (cel-expr, the optional second CEL backend, excepted)."""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

import invariants

SRC = pathlib.Path(invariants.__file__).parent
FORBIDDEN = {
    "registry": ("invariants.loader", "invariants.guard", "invariants.cli", "invariants.scan", "invariants.lock",
                 "invariants.integrations", "invariants.mcp", "invariants.status_report"),
    # the loader is below the lock (which pins what it loads) and the integrations (which build on it)
    "loader": ("invariants.lock", "invariants.integrations", "invariants.guard", "invariants.cli", "invariants.mcp",
               "invariants.pytest_plugin", "invariants.status_report"),
    "guard": ("invariants.cli", "invariants.scan", "invariants.lock",
              "invariants.integrations", "invariants.mcp", "invariants.status_report"),
    # the MCP server reads through invariants.project and the scan; it never imports the CLI or the runtime guards
    "mcp": ("invariants.cli", "invariants.guard", "invariants.integrations", "invariants.pytest_plugin"),
}
# the modules of its own package each module may import (the package __init__ composes them all)
STAGES = {
    # coverage and ablation: Registry for typing; context: Registry and observe, which __init__ defines first
    "registry": {"reviews": set(), "session": set(), "events": {"reviews"}, "ablation": {"__init__", "events", "session"},
                 "coverage": {"__init__"}, "context": {"__init__", "events", "reviews"}},
    "guard": {"checked": set(), "replies": {"checked"}, "plan": {"checked", "replies"},
              "decorators": {"checked", "plan", "replies"}},
    "cli": {"common": set(), "worker": {"common"}, "git_base": {"common", "worker"},
            "static_guards": {"common"},
            "commands": {"common", "git_base", "static_guards", "worker"}, "hooks": {"commands", "common"},
            "child": {"commands", "common"},
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
    assert files and {f.stem for f in files} >= set(STAGES.get(pkg, ())), pkg
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


def test_only_the_cli_imports_the_cli() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 40
    for path in files:
        rel = path.relative_to(SRC)
        if rel.parts[0] == "cli":
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


# --- the target DAG: three groups that import nothing else of invariants ------------------------------
GROUPS = {
    "cel": ("invariants.cel", "invariants._typeinfo", "invariants.typecheck"),
    "scan": ("invariants.scan", "invariants._scan_hooks"),
    "schema_diff": ("invariants.lock.schema_diff", "invariants.lock.cel_canon", "invariants.lock.cel_sites",
                    "invariants.lock.cel_mono", "invariants.lock.contracts"),
}
# the groups whose modules each group may import (its own included)
GROUP_DEPS = {"cel": {"cel"}, "scan": {"scan"}, "schema_diff": {"schema_diff", "cel"}}
# the third-party modules a group may import: cel-expr, the optional backend (and the protobuf it needs)
GROUP_EXTRAS = {"cel": {"cel_expr_python", "google"}, "scan": set(), "schema_diff": set()}


def _module_file(name: str, src: pathlib.Path | None = None) -> pathlib.Path | None:
    """The source file of invariants module `name` (its package's __init__.py), or None."""
    rel = pathlib.Path(*name.split(".")[1:])
    base = src or SRC
    for f in (base / rel / "__init__.py", (base / rel).with_suffix(".py")):
        if f.is_file():
            return f
    return None


def _module_name(path: pathlib.Path, src: pathlib.Path) -> str:
    rel = path.relative_to(src).with_suffix("")
    parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
    return ".".join(("invariants", *parts))


def _targets(path: pathlib.Path, src: pathlib.Path | None = None) -> set[tuple[str, int]]:
    """The modules a file under src (the package's directory) imports, at any depth (in a function,
    under TYPE_CHECKING, in a try), with the line: `from X import y` is the module X.y when y is a
    module of the package, else X; a relative import resolves against the file's own package."""
    src = src or SRC
    own = _module_name(path, src)
    pkg = own if path.name == "__init__.py" else own.rpartition(".")[0]
    out: set[tuple[str, int]] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {(a.name, node.lineno) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = pkg.split(".")
                base = ".".join([*parts[:len(parts) - node.level + 1], *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            for a in node.names:
                sub = f"{base}.{a.name}"
                out.add((sub if base.startswith("invariants") and _module_file(sub, src) else base, node.lineno))
    return out


def _group_of(name: str) -> str | None:
    return next((g for g, mods in GROUPS.items() if any(name == m or name.startswith(m + ".") for m in mods)), None)


def _group_files(group: str) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for m in GROUPS[group]:
        f = _module_file(m)
        assert f is not None, f"{m} (group {group}) has no source file"
        out += sorted(f.parent.rglob("*.py")) if f.name == "__init__.py" else [f]
    return out


def _group_problems(group: str, path: pathlib.Path, src: pathlib.Path | None = None) -> list[str]:
    src = src or SRC
    out = []
    for name, line in sorted(_targets(path, src), key=lambda t: (t[1], t[0])):
        top = name.split(".")[0]
        if top == "invariants":
            if _group_of(name) not in GROUP_DEPS[group]:
                out.append(f"{path.relative_to(src)}:{line} imports {name}, outside the {group} group and the groups "
                           f"it may import ({', '.join(sorted(GROUP_DEPS[group]))}): {group} is a library of its own; "
                           f"pass what it needs in as a parameter or a hook the package wires (invariants/__init__.py)")
        elif top not in sys.stdlib_module_names and top not in GROUP_EXTRAS[group]:
            out.append(f"{path.relative_to(src)}:{line} imports {name}: the {group} group is standard library only")
    return out


@pytest.mark.parametrize("group, path", [(g, f) for g in GROUPS for f in _group_files(g)],
                         ids=lambda v: v if isinstance(v, str) else str(v.relative_to(SRC)))
def test_a_library_group_imports_only_itself_and_its_dependencies(group: str, path: pathlib.Path) -> None:
    problems = _group_problems(group, path)
    assert not problems, "\n".join(problems)


def test_the_group_check_catches_a_bad_import(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "invariants"
    for m in ("__init__.py", "typecheck.py", "loader/__init__.py", "cel/__init__.py", "cel/subset/__init__.py",
              "lock/__init__.py", "lock/contracts.py", "lock/pinning.py"):
        (src / m).parent.mkdir(parents=True, exist_ok=True)
        (src / m).touch()
    bad = src / "lock" / "cel_canon.py"
    bad.write_text("import json\nfrom .. import typecheck\nfrom ..cel import subset\nfrom .contracts import same\n"
                   "def f():\n    from ..loader import _type_env\n    import yaml\n    from . import pinning\n")
    got = _group_problems("schema_diff", bad, src)
    assert [p.split(" imports ")[1].split(",")[0].split(":")[0] for p in got] == [
        "invariants.loader", "yaml", "invariants.lock.pinning"], got
    assert got[0].startswith("lock/cel_canon.py:6 imports invariants.loader, outside the schema_diff group")


def test_the_top_level_packages_import_one_another_without_a_cycle() -> None:
    """Over every top-level package and module of invariants but the package itself, which composes and
    wires them all."""
    edges: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC)
        if rel == pathlib.Path("__init__.py"):
            continue
        a = rel.parts[0].removesuffix(".py")
        for name, _ in _targets(path):
            parts = name.split(".")
            if parts[0] == "invariants" and len(parts) > 1 and parts[1] != a and _module_file(".".join(parts[:2])):
                edges.setdefault(a, set()).add(parts[1])
    assert {"loader", "registry", "lock", "scan", "cli", "mcp", "integrations"} <= set(edges)
    left = set(edges) | {m for ms in edges.values() for m in ms}
    while left:                  # peel the nodes that import nothing left, or that nothing left imports
        ready = {n for n in left if not (edges.get(n, set()) & left)
                 or not any(n in edges.get(m, set()) for m in left)}
        assert ready, "a cycle among the top-level packages: " + "; ".join(
            f"{n} -> {', '.join(sorted(edges[n] & left))}" for n in sorted(left) if edges.get(n, set()) & left)
        left -= ready
