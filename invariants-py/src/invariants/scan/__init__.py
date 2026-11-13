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
from .sources import _excluded, _walk_sources, anywhere, iter_sources
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


def _index(roots: Iterable[str | pathlib.Path], exclude: Iterable[str]) -> _Index:
    """The first pass over the roots: scanned modules indexed, excluded ones noted (not read)."""
    idx = _Index()
    skipped: list[tuple[pathlib.Path, pathlib.Path, str, str]] = []
    for root, f, name, pattern, skip in _walk_sources((pathlib.Path(r) for r in roots), exclude):
        if pattern is None:
            idx.add(root, f, name)
        elif skip:
            skipped.append((root, f, name, pattern))
        else:                                          # not scanned; a reference into it from scanned code is noted
            idx.excluded[name] = (f.relative_to(root).as_posix(), pattern)
            idx.excluded_files[name] = f
    # a skipped directory (build/, dist/, venv/, ...) counts only where its modules are importable under the
    # project's own package names (app/build/jobs.py is app.build.jobs); a virtualenv's site-packages or a
    # build/lib copy is named from the skipped directory down, so third-party code stays quiet
    tops = {m.split(".")[0] for m in idx.modules}
    for root, f, name, pattern in skipped:
        if name.split(".")[0] in tops and all(p.isidentifier() for p in name.split(".")) \
                and name not in idx.modules and name not in idx.excluded:
            idx.excluded[name] = (f.relative_to(root).as_posix(), pattern)
            idx.excluded_files[name] = f
    return idx


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
    entries = g.entries
    for med in scanned.values():
        report.on_paths[med.rule] = set()
        report.findings += _check_rule(idx, med, edges, sites, entries, report.on_paths[med.rule])
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
