"""`inv scan`: a static, read-only call-graph check that every path to a protected sink passes its door.

The rule file declares, per rule, under `mediation`:

    sink:   "py:pkg.store.set_size"          # protected API(s): a string or a list
    door:   "py:pkg.service.resize"          # the API(s) every call must pass; defaults to subject.target
    allow:  ["py:pkg.jobs.backfill"]         # optional: known back doors, frozen

The source is parsed with `ast` and never imported or run. Findings have four kinds:
bypass (a path of extracted call edges from an entrypoint to the sink that avoids every door),
forbidden (a bypass idiom), unresolved (something the scan cannot follow; never counted as passed)
and missing (a declared sink or door that does not exist in the scanned code).

The pipeline: sources (which files) -> indexer (first pass: modules, functions, classes) -> walker with
inference (second pass: call edges and sites) -> reachability and checks (findings per rule) -> Report.
"""
# ruff: noqa: F401  (the imports below re-export the namespace scan.py had: public and private names)
from __future__ import annotations

import ast
import dataclasses
import pathlib
from typing import Any, Iterable, Iterator

from .astutil import (_HARMLESS_DUNDERS, _add_leaves, _docstrings, _dotted, _dunder, _fold_str, _locals,
                      _own_nodes, _pairs, _single_types)
from .checks import _canon_py, _check_rule, _finding, _holds, _loc
from .contracts import (DYNAMIC_IMPORT, KINDS, REFLECTION, SKIP_DIRS, Finding, Mediation, Report,
                        ScanConfigError, SqlWrite, _Edge, _Func, _Module, _Site)
from .indexer import (_BUILTINS, _DEFINING_MODULES, _DEFINING_NAMES, _KNOWN_BASE_MODULES, _TYPE_ALIAS, _Index,
                      _defines_only, _runs_code)
from .inference import (_ANNOTATED, _CM, _CONTAINERS, _LITERALS, _OPTIONAL, _PROPERTY, _SELF, _UNION, _YIELDS,
                        _Inference)
from .mediation import _mediation, _not_scanned, _str_list
from .reachability import _entrypoints, _paths, _reach, _shortest, door_only
from .sarif import _LEVEL, to_sarif
from .sources import SKIPPED, _excluded, _layout, _skipped, _Source, _walk_sources, anywhere, iter_sources
from .sql import _KEYWORDS, _SQL_COMMENT, _SQL_FORMS, _TABLE, sql_writes
from .text import to_text
from .walker import _REFLECTIVE, _Walker

# `from invariants.scan import *` exports exactly the public names scan.py's star import gave (it had no
# __all__), and no modules: without this list it would bind the submodules (text, sql, checks, ...) over
# a caller's own names. The stdlib modules scan.py leaked (re, sys, ast, ...) are not re-exported.
__all__ = ["Any", "DYNAMIC_IMPORT", "Finding", "Graph", "Iterable", "Iterator", "KINDS", "Mediation", "REFLECTION",
           "Report", "SKIP_DIRS", "ScanConfigError", "SqlWrite", "annotations", "anywhere", "door_only", "graph",
           "iter_sources", "mediations", "scan", "sql_writes", "to_sarif", "to_text"]


# defined here, not in .mediation: `inv validate` labels its errors by this function's module ("scan:")
def mediations(doc: dict[str, Any]) -> tuple[dict[str, Mediation], dict[str, str]]:
    """Scan fields per rule, and the reason each other rule is not scanned."""
    scanned: dict[str, Mediation] = {}
    skipped: dict[str, str] = {}
    for raw in doc.get("rules") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            continue
        rid, med = raw["id"], raw.get("mediation") or {}
        why = _not_scanned(rid, raw, med)
        if why is not None:
            skipped[rid] = why
            continue
        scanned[rid] = _mediation(rid, raw, med)
    return scanned, skipped


@dataclasses.dataclass
class Graph:
    """The scan's view of the code: functions, call edges, sites (raw SQL and the like), entrypoints."""

    idx: _Index
    edges: list[_Edge]
    sites: list[_Site]
    entries: list[str]

    def functions(self) -> list[str]:
        return sorted(f for f, fn in self.idx.funcs.items() if fn.node is not None)

    def sql_writes(self) -> list[tuple[str, SqlWrite, int]]:
        """(owner function, write, line) for every raw SQL write string; `inv init` reads it to find
        repositories (a function that writes raw SQL is one). The scan's checks never read it."""
        return [(s.owner, w, s.line) for s in self.sites if s.what == "sql" for w in sql_writes(s.detail)]


_NO_SOURCE_WHY = {"extension": "<an extension module, with no Python source the scan can read>",
                  "bytecode": "<a compiled .pyc with no Python source the scan can read>"}


def _index(roots: Iterable[str | pathlib.Path], exclude: Iterable[str]) -> _Index:
    """The first pass over the roots: scanned modules indexed, excluded ones noted (not read)."""
    idx = _Index()
    skipped: list[_Source] = []
    found = _layout((pathlib.Path(r) for r in roots), exclude)
    names = {s.name for s in found}
    for s in found:                                    # every other root's name for a file is an alias
        if s.shadowed_by is None:
            for a in s.aliases:
                if a not in names:
                    idx.module_alias.setdefault(a, s.name)
    for s in found:
        rel = s.file.relative_to(s.root).as_posix()
        if s.shadowed_by is not None:
            _shadowed(idx, s)
            continue
        idx.long_package[s.name] = s.long if s.form == "package" else s.long.rpartition(".")[0]
        if s.pattern is not None and s.skip:
            idx.skipped_dirs[_skipped(pathlib.PurePath(rel)) or s.pattern] += 1
        if s.form in ("extension", "bytecode") and not (s.pattern is not None and s.skip):
            if s.pattern is None:                      # importable code the scan cannot read: noted, never empty
                idx.problems.append((s.root, s.file, 1, "unresolved/no-source",
                                     f"module {s.name} is {_NO_SOURCE_WHY[s.form][1:-1]}: what it runs and "
                                     f"calls is not checked; keep its source in the root, or delete it"))
            idx.excluded[s.name] = (rel, s.pattern or _NO_SOURCE_WHY[s.form])
            idx.no_source[s.name] = s.file
            continue
        if s.pattern is None:
            idx.add(s.root, s.file, s.name)
            for glob, root in s.partial:
                idx.notes.append(f"exclude {glob!r} matches {rel} only from some roots (not from every root "
                                 f"that holds it, such as {root}): it is scanned")
        elif s.skip:
            skipped.append(s)
        else:                                          # not scanned; a reference into it from scanned code is noted
            idx.excluded[s.name] = (rel, s.pattern)
            idx.excluded_files[s.name] = s.file
    # a skipped directory (build/, dist/, venv/, ...) counts only where its modules are importable under the
    # project's own package names (app/build/jobs.py is app.build.jobs); a virtualenv's site-packages or a
    # build/lib copy is named from the skipped directory down, so third-party code stays quiet
    tops = {m.split(".")[0] for m in idx.modules}
    for s in skipped:
        name, pattern = s.name, s.pattern or ""
        if name.split(".")[0] in tops and all(p.isidentifier() for p in name.split(".")) \
                and name not in idx.modules and name not in idx.excluded:
            idx.excluded[name] = (s.file.relative_to(s.root).as_posix(), pattern)
            if s.form in ("extension", "bytecode"):
                idx.no_source[name] = s.file
            else:
                idx.excluded_files[name] = s.file
        elif s.form in ("source", "package") and not s.globbed:
            idx.skipped_files.append((s.root, s.file, pattern))
    idx.unresolved_imports()
    return idx


_THIRD_PARTY = frozenset({"site-packages", "node_modules", "__pycache__"})


def _skipped_imports(idx: _Index, root: pathlib.Path, file: pathlib.Path) -> list[tuple[int, str]]:
    """(line, module) for every import in a file in an always-skipped directory, read once; none when the
    file is third-party code (under site-packages/node_modules) or a copy of a scanned module (build/lib)."""
    if file in idx.skipped_imports:
        return idx.skipped_imports[file]
    rel = file.relative_to(root)
    parts = [*rel.parts[:-1], rel.stem]
    if parts[-1] == "__init__":
        parts.pop()
    known = {*idx.modules, *idx.excluded}
    copy = any(".".join(parts[i:]) in known for i in range(1, len(parts)))
    got: list[tuple[int, str]] = []
    if not (_THIRD_PARTY & set(rel.parts) or copy):
        try:
            tree = ast.parse(file.read_bytes(), filename=str(file))
        except (SyntaxError, ValueError, OSError):
            tree = ast.Module(body=[], type_ignores=[])
        package = ".".join(parts if rel.stem == "__init__" else parts[:-1])
        mod = _Module(f"<skipped {rel.as_posix()}>", file, root, tree, package)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                got += [(node.lineno, idx.real(a.name)) for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                src = idx._absolute(mod, node)
                got += [(node.lineno, src), *((node.lineno, f"{src}.{a.name}") for a in node.names)]
    idx.skipped_imports[file] = got
    return got


def _skipped_importers(idx: _Index, med: Mediation) -> list[Finding]:
    """A file in an always-skipped directory (build/, dist/, a dot-directory, ...) that is not importable
    under the project's package names, so no scanned code can reach it, but that imports a module holding
    one of the rule's sinks: a standalone script there runs unchecked."""
    homes = {idx.funcs[c].module for p in med.sinks if (c := _canon_py(idx, p)) in idx.funcs}
    out: list[Finding] = []
    for root, file, where in idx.skipped_files:
        for line, name in _skipped_imports(idx, root, file):
            home = next((h for h in homes if name == h or name.startswith(h + ".")), None)
            if home is not None:
                out.append(Finding("unresolved", med.rule, "unresolved/skipped-importer",
                                   f"imports {name} (where the sink lives) from a {where!r} directory, which the "
                                   f"scan always skips: if this file runs, its calls are not checked; move it "
                                   f"out of {where}/ or delete it", file.relative_to(root).as_posix(), line, 1,
                                   str(root)))
                break
    return out


def _shadowed(idx: _Index, s: _Source) -> None:
    """A file Python never imports under its module name, since another file has it: noted, since
    someone reading it may think it runs."""
    win = s.shadowed_by
    if win is None or s.form == "bytecode" or s.pattern is not None:
        return                                         # a stale x.pyc is never loaded; excluded code is not read
    same_dir = win.root == s.root and s.file.parent in (win.file.parent, win.file.parent.parent)
    why = "in the same directory, Python imports it instead" if same_dir else "from an earlier source root"
    idx.problems.append((s.root, s.file, 1, "unresolved/shadowed-module",
                         f"{s.file.relative_to(s.root).as_posix()} is never imported as {s.name}: "
                         f"{win.file.relative_to(win.root).as_posix()} has the same module name ({why}), so "
                         f"the scan reads that one; rename or delete one of them"))


def graph(roots: Iterable[str | pathlib.Path], exclude: Iterable[str] = ()) -> Graph:
    """Index and walk the source roots once (no imports); scan() and `inv init` read the result."""
    idx = _index(roots, exclude)
    edges: list[_Edge] = []
    sites: list[_Site] = []
    for mod in idx.modules.values():
        w = _Walker(idx, mod)
        w.run()
        edges += w.edges
        sites += w.sites
    return Graph(idx, edges, sites, _entrypoints(idx, edges))


def scan(doc: dict[str, Any], roots: Iterable[str | pathlib.Path], exclude: Iterable[str] = (),
         g: Graph | None = None) -> Report:
    """Scan the source roots (minus exclude globs) against every rule that declares mediation.sink."""
    scanned, skipped = mediations(doc)
    g = g or graph(roots, exclude)
    idx, edges, sites = g.idx, g.edges, g.sites
    report = Report([], scanned, skipped, files=len(idx.modules) + len(idx.parse_errors),
                    functions=sum(1 for f in idx.funcs.values() if f.node is not None))
    report.skipped = dict(sorted(idx.skipped_dirs.items()))
    report.notes = list(idx.notes)
    entries = g.entries
    for med in scanned.values():
        report.on_paths[med.rule] = set()
        report.findings += _check_rule(idx, med, edges, sites, entries, report.on_paths[med.rule])
        report.findings += [Finding("unresolved", med.rule, code, msg, f.relative_to(r).as_posix(), line, 1,
                                    str(r)) for r, f, line, code, msg in idx.problems]
        report.findings += _skipped_importers(idx, med)
    order = {k: i for i, k in enumerate(KINDS)}
    report.findings.sort(key=lambda f: (f.rule, order[f.kind], f.file or "", f.line, f.col, f.code))
    return report

# Classes keep the module name they had before the split: a pinned signature (`invariants.lock`) names
# an annotation's class by its module, and pickles name it too, so a moved __module__ would make an
# upgrade alone re-pin the lock and make new pickles unreadable by older versions.
for _obj in list(globals().values()):
    if isinstance(_obj, type) and (_obj.__module__ == __name__ or _obj.__module__.startswith(__name__ + ".")):
        _obj.__module__ = 'invariants.scan'
del _obj
