"""Layering of the guard, registry, cli and mcp packages, read from their import statements (lazy ones too).

The direction, from the imports as they are: registry (rules, verify, standing, review stores) is the
bottom of the runtime and imports neither the loader above it, the guards that use it, the static
layers (scan, lock) nor the CLI; the loader imports neither the lock, the integrations, the guards nor
the CLI; guard (the decorators) builds on registry but not on scan, lock or the CLI; scan and lock
never import the CLI, and nothing outside cli/ does: the MCP server shares the project helpers through
invariants.project. The top-level packages and modules import one another without a cycle (the package
__init__ wires what a lower layer reads from a higher one). Inside each package the modules import one
another in one direction only.

Three libraries are distributions of their own (packages/ of the monorepo) and import nothing of
invariants, lazily or not: invariants-cel (invariants_cel: the CEL engine, typeinfo, typecheck),
invariants-scan (invariants_scan, with the hooks the package wires) and invariants-schema-diff
(invariants_schema_diff: the JSON Schema and CEL weakening judges over canonical JSON and Change),
which may import invariants_cel. Each is standard library only (cel-expr, the optional second CEL
backend, excepted). invariants imports them by their own names; invariants.cel and invariants.scan
are thin re-exports of their public API."""
from __future__ import annotations

import ast
import pathlib
import re
import sys
import tomllib

import pytest

import invariants

SRC = pathlib.Path(invariants.__file__).parent
FORBIDDEN = {
    "registry": ("invariants.loader", "invariants.guard", "invariants.cli", "invariants.scan", "invariants.lock",
                 "invariants.integrations", "invariants.mcp", "invariants.status_report", "invariants_scan",
                 "invariants_schema_diff"),
    # the loader is below the lock (which pins what it loads) and the integrations (which build on it)
    "loader": ("invariants.lock", "invariants.integrations", "invariants.guard", "invariants.cli", "invariants.mcp",
               "invariants.pytest_plugin", "invariants.status_report"),
    "guard": ("invariants.cli", "invariants.scan", "invariants.lock",
              "invariants.integrations", "invariants.mcp", "invariants.status_report", "invariants_scan",
              "invariants_schema_diff"),
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
            "scan_gate": {"common", "git_base"},
            "commands": {"common", "git_base", "scan_gate", "static_guards", "worker"}, "hooks": {"commands", "common"},
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


# --- the libraries: three distributions of their own, under packages/ of the monorepo ----------------
# packages/<dist>/src/<import name>: each imports only itself, the standard library, the libraries it
# depends on and its optional engine; none imports invariants (invariants-first depends on them)
PACKAGES = pathlib.Path(__file__).resolve().parents[2] / "packages"
LIBS = {"invariants_cel": "invariants-cel", "invariants_scan": "invariants-scan",
        "invariants_schema_diff": "invariants-schema-diff"}
# the libraries each library may import (its own included), as its pyproject.toml declares them
LIB_DEPS = {"invariants_cel": {"invariants_cel"}, "invariants_scan": {"invariants_scan"},
            "invariants_schema_diff": {"invariants_schema_diff", "invariants_cel"}}
# the third-party modules a library may import: cel-expr, the optional backend (and the protobuf it needs)
LIB_EXTRAS = {"invariants_cel": {"cel_expr_python", "google"}, "invariants_scan": set(), "invariants_schema_diff": set()}


def _lib_src(lib: str) -> pathlib.Path:
    return PACKAGES / LIBS[lib] / "src" / lib


def _module_file(name: str, src: pathlib.Path | None = None) -> pathlib.Path | None:
    """The source file of module `name` under src (the package's directory, invariants/ by default):
    its package's __init__.py or its .py file, or None."""
    base = src or SRC
    if name.split(".")[0] != base.name:
        return None
    rel = pathlib.Path(*name.split(".")[1:])
    for f in (base / rel / "__init__.py", (base / rel).with_suffix(".py")):
        if f.is_file():
            return f
    return None


def _module_name(path: pathlib.Path, src: pathlib.Path) -> str:
    rel = path.relative_to(src).with_suffix("")
    parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
    return ".".join((src.name, *parts))


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
                out.add((sub if _module_file(sub, src) else base, node.lineno))
    return out


def _lib_problems(lib: str, path: pathlib.Path, src: pathlib.Path | None = None) -> list[str]:
    src = src or _lib_src(lib)
    out = []
    for name, line in sorted(_targets(path, src), key=lambda t: (t[1], t[0])):
        top = name.split(".")[0]
        if top in LIB_DEPS[lib]:
            continue
        where = f"{src.name}/{path.relative_to(src)}:{line} imports {name}"
        if top == "invariants" or top in LIBS:
            out.append(f"{where}: {lib} is a library of its own and may import only "
                       f"{', '.join(sorted(LIB_DEPS[lib]))}; pass what it needs in as a parameter or a hook "
                       f"the host wires (invariants/__init__.py wires invariants_scan.hooks)")
        elif top not in sys.stdlib_module_names and top not in LIB_EXTRAS[lib]:
            out.append(f"{where}: {lib} is standard library only")
    return out


@pytest.mark.parametrize("lib, path", [(lib, f) for lib in LIBS for f in sorted(_lib_src(lib).rglob("*.py"))],
                         ids=lambda v: v if isinstance(v, str) else str(v.relative_to(PACKAGES)))
def test_a_library_imports_only_itself_and_its_dependencies(lib: str, path: pathlib.Path) -> None:
    problems = _lib_problems(lib, path)
    assert not problems, "\n".join(problems)


def test_every_library_is_checked_and_declares_what_it_imports() -> None:
    assert sorted(p.name for p in PACKAGES.iterdir() if (p / "pyproject.toml").is_file()) == sorted(LIBS.values())
    for lib, dist in LIBS.items():
        assert len(list(_lib_src(lib).rglob("*.py"))) >= 3, lib
        project = tomllib.loads((PACKAGES / dist / "pyproject.toml").read_text())["project"]
        declared = {re.split(r"[\[=<>~! ]", d)[0].replace("-", "_") for d in project["dependencies"]}
        assert declared == LIB_DEPS[lib] - {lib}, (dist, project["dependencies"])


def test_invariants_imports_the_libraries_by_their_own_names() -> None:
    """Nothing in invariants/ imports a module that moved into a library under its old name (a thin
    re-export, invariants/cel.py and invariants/scan.py, keeps the public paths `invariants.cel` and
    `invariants.scan`; nothing inside the package imports them)."""
    gone = ("invariants._typeinfo", "invariants.typecheck", "invariants._scan_hooks", "invariants.lock.schema_diff",
            "invariants.lock.cel_canon", "invariants.lock.cel_sites", "invariants.lock.cel_mono")
    shims = ("invariants.cel", "invariants.scan")
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC)
        for name, line in _targets(path):
            assert not any(name == g or name.startswith(g + ".") for g in gone), f"{rel}:{line} imports {name}"
            if rel != pathlib.Path("__init__.py"):          # the package exposes invariants.cel as inv.cel
                assert not any(name == g or name.startswith(g + ".") for g in shims), f"{rel}:{line} imports {name}"


def test_the_library_check_catches_a_bad_import(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "invariants_schema_diff"
    for m in ("__init__.py", "changes.py", "sub/__init__.py"):
        (src / m).parent.mkdir(parents=True, exist_ok=True)
        (src / m).touch()
    bad = src / "cel_canon.py"
    bad.write_text("import json\nfrom invariants_cel import subset\nfrom .changes import same\n"
                   "def f():\n    from invariants.loader import _type_env\n    import yaml\n"
                   "    from invariants_scan import scan\n    from . import sub\n")
    got = _lib_problems("invariants_schema_diff", bad, src)
    assert [p.split(" imports ")[1].split(":")[0] for p in got] == [
        "invariants.loader", "yaml", "invariants_scan"], got
    assert got[0].startswith("invariants_schema_diff/cel_canon.py:5 imports invariants.loader: ")
    # the same file read as part of invariants-cel, which depends on no other library
    assert [p.split(" imports ")[1].split(":")[0] for p in _lib_problems("invariants_cel", bad, src)] == [
        "invariants_schema_diff.changes", "invariants.loader", "yaml", "invariants_scan",
        "invariants_schema_diff.sub"]


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
    assert {"loader", "registry", "lock", "cli", "mcp", "integrations"} <= set(edges)
    left = set(edges) | {m for ms in edges.values() for m in ms}
    while left:                  # peel the nodes that import nothing left, or that nothing left imports
        ready = {n for n in left if not (edges.get(n, set()) & left)
                 or not any(n in edges.get(m, set()) for m in left)}
        assert ready, "a cycle among the top-level packages: " + "; ".join(
            f"{n} -> {', '.join(sorted(edges[n] & left))}" for n in sorted(left) if edges.get(n, set()) & left)
        left -= ready
