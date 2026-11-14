"""The project around a rule file: its pyproject.toml, import roots, source directories and scan settings."""
from __future__ import annotations

import argparse
import contextlib
import os
import pathlib
import sys
from typing import Any, Iterator, Sequence

from .. import lock
from ..confinement import import_module
from ..errors import LoadError
from ..loader import load, read_document
from ..registry import Registry
from .common import Usage


def project_root(rule_file: pathlib.Path) -> pathlib.Path:
    """The nearest directory at or above the rule file's that holds a pyproject.toml, else the rule
    file's own directory."""
    here = rule_file.resolve().parent
    return next((d for d in (here, *here.parents) if (d / "pyproject.toml").is_file()), here)


def _pyproject(root: pathlib.Path) -> dict[str, Any]:
    py = root / "pyproject.toml"
    if not py.is_file():
        return {}
    import tomllib
    try:
        return tomllib.loads(py.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise Usage(f"{py}: {e}") from e


def _str_list(v: Any) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _setuptools_where(tool: dict[str, Any]) -> list[str]:
    """[tool.setuptools.packages.find] where of a parsed [tool] table (the project's source directories)."""
    st = tool.get("setuptools")
    packages = st.get("packages") if isinstance(st, dict) else None
    find = packages.get("find") if isinstance(packages, dict) else None
    return _str_list(find.get("where") if isinstance(find, dict) else None)


def scan_inputs(pyproject: Any) -> dict[str, Any]:
    """The parts of a parsed pyproject.toml that the scan settings (scan_settings: the roots, exclude
    globs and so the names of the scanned files) and the import roots are derived from: all of
    [tool.invariants] and [tool.setuptools.packages] (its find.where is a default source root when
    [tool.invariants.scan] sets no roots). An edit that changes these changes what `inv check` scans."""
    tool = pyproject.get("tool") if isinstance(pyproject, dict) else None
    tool = tool if isinstance(tool, dict) else {}
    st = tool.get("setuptools")
    return {"tool.invariants": tool.get("invariants"),
            "tool.setuptools.packages": st.get("packages") if isinstance(st, dict) else None}


def import_roots(rule_file: pathlib.Path, extra: Sequence[str] = ()) -> list[pathlib.Path]:
    """Where `inv` imports the app from, ahead of site-packages: every --path, then this checkout's
    source directories (the project's src/, its setuptools `packages.find.where` and
    [tool.invariants.scan] roots), the rule file's directory and the project root. So the lock pins
    the source beside the rule file, not an installed copy or another checkout sharing the venv."""
    proj = project_root(rule_file)
    tool = _pyproject(proj).get("tool") or {}
    scan_roots = ((tool.get("invariants") or {}).get("scan") or {}).get("roots")
    srcs = [proj / r for r in [*_setuptools_where(tool), *_str_list(scan_roots), "src"]]
    out: list[pathlib.Path] = []
    for d in [*(pathlib.Path(p).resolve() for p in extra), *(s.resolve() for s in srcs if s.is_dir()),
              rule_file.resolve().parent, proj]:
        if d not in out:
            out.append(d)
    return out


def source_dirs(rule_file: pathlib.Path) -> list[pathlib.Path]:
    """The source directories of the project whose pyproject.toml sits beside the rule file (the
    one scan_config reads): setuptools `packages.find.where` and src/, if present. These are the
    directories import_roots puts first for `inv lock`; [] when the rule file has no pyproject.toml
    beside it, so a rule file nested in a bigger project never scans that project's src/."""
    proj = rule_file.resolve().parent
    if not (proj / "pyproject.toml").is_file():
        return []
    tool = _pyproject(proj).get("tool") or {}
    out: list[pathlib.Path] = []
    for r in [*_setuptools_where(tool), "src"]:
        d = (proj / r).resolve()
        if d.is_dir() and d != proj and d not in out:
            out.append(d)
    return out


@contextlib.contextmanager
def _import_path(rule_file: pathlib.Path, extra: Sequence[str]) -> Iterator[None]:
    """Put --path entries and this checkout's source roots (import_roots) first on sys.path for the block."""
    entries = [str(d) for d in import_roots(rule_file, extra)]
    saved = list(sys.path)
    sys.path[:0] = [e for e in entries if e not in sys.path]
    try:
        yield
    finally:
        sys.path[:] = saved


def _app_modules(doc: dict[str, Any]) -> list[str]:
    """The modules a rule file pins: target, provider and python-contract modules (top-level names)."""
    refs: list[str] = list((doc.get("targets") or {}).keys())
    for raw in doc.get("rules") or []:
        if isinstance(raw, dict):
            refs.append(str((raw.get("subject") or {}).get("target") or ""))
            refs += [str(e.get("target") or "") for e in raw.get("enforcers") or [] if isinstance(e, dict)]
    refs += [str(p.get("from") or "") for p in (doc.get("providers") or {}).values() if isinstance(p, dict)]
    mods = {r[3:].split(".")[0].split(":")[0] for r in refs if r.startswith("py:")}
    mods |= {m.split(".")[0] for m in _contract_modules(doc)}
    return sorted(m for m in mods if m)


def outside_roots(rule_file: pathlib.Path, doc: dict[str, Any], extra: Sequence[str] = ()) -> list[str]:
    """Warnings for each pinned module that was imported from outside this checkout (an installed
    copy, or another checkout sharing the venv): the lock would pin that copy's types, not this one's."""
    roots = [project_root(rule_file), *(pathlib.Path(p).resolve() for p in extra)]
    out = []
    for name in _app_modules(doc):
        mod = sys.modules.get(name)
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        where = pathlib.Path(f).resolve()
        if not any(where.is_relative_to(r) for r in roots):
            out.append(f"warning: module {name} was imported from {where}, outside this checkout "
                       f"({roots[0]}); the pins describe that copy. Pass --path DIR to import this "
                       f"checkout's source.")
    return out


def _contract_modules(doc: dict[str, Any]) -> list[str]:
    mods = []
    for c in (doc.get("contracts") or {}).values():
        if not isinstance(c, dict):
            continue
        ref = c.get("ref")
        # a python contract, or a schema contract naming a class (module:Class) as `inv lock` pins it;
        # a schema contract's other refs are files
        if isinstance(ref, str) and (c.get("type") in lock.PY_CONTRACTS
                                     or (c.get("type") in lock.SCHEMA_CONTRACTS and ":" in ref)):
            mods.append(ref.partition(":")[0])
        elif c.get("type") == "openapi" and isinstance(c.get("app"), str):
            mods.append(c["app"].partition(":")[0])
    return mods


def _load_app(rule_file: pathlib.Path, imports: Sequence[str]) -> Registry:
    """Load the rule file into the current (isolated) registry and import the app's modules."""
    reg = load(rule_file)
    for mod in [*_contract_modules(read_document(rule_file)), *imports]:
        try:
            import_module(mod, "contract module")
        except ImportError as e:
            raise LoadError(f"cannot import {mod}: {e}") from e
    return reg


def scan_config(path: pathlib.Path) -> dict[str, Any]:
    """[tool.invariants.scan] (roots, exclude) from the pyproject.toml beside the rule file, if any."""
    py = path.resolve().parent / "pyproject.toml"
    if not py.is_file():
        return {}
    import tomllib
    try:
        cfg = tomllib.loads(py.read_text(encoding="utf-8")).get("tool", {}).get("invariants", {}).get("scan", {})
    except tomllib.TOMLDecodeError as e:
        raise Usage(f"{py}: {e}") from e
    for k in ("roots", "exclude"):
        if k in cfg and not (isinstance(cfg[k], list) and all(isinstance(x, str) for x in cfg[k])):
            raise Usage(f"{py}: [tool.invariants.scan] {k} must be a list of strings")
    return cfg


DEFAULT_SCAN_EXCLUDE = lock.DEFAULT_SCAN_EXCLUDE


def _exclude(a: argparse.Namespace, path: pathlib.Path) -> list[str]:
    cfg = scan_config(path)
    return [*cfg.get("exclude", DEFAULT_SCAN_EXCLUDE), *a.exclude]


def _roots(a: argparse.Namespace, path: pathlib.Path) -> list[pathlib.Path]:
    base = path.resolve().parent
    # By default: the project's source directories (src/, setuptools `where`), as `inv lock` imports
    # from, and the rule file's directory for everything else; a file is named by its deepest root.
    roots = ([pathlib.Path(r) for r in a.root] or [base / r for r in scan_config(path).get("roots", [])]
             or [*source_dirs(path), base])
    for r in roots:
        if not r.exists():
            raise Usage(f"source root {r} does not exist")
    return roots


def scan_settings(a: argparse.Namespace, path: pathlib.Path) -> dict[str, list[str]]:
    """The roots (relative to the rule file) and excludes `inv check` scans with, as the lock pins them."""
    base = path.resolve().parent
    roots = [pathlib.Path(os.path.relpath(r.resolve(), base)).as_posix() for r in _roots(a, path)]
    return lock.scan_pin(roots, _exclude(a, path))
