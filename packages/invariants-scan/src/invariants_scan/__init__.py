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
# ruff: noqa: F401  (the imports below are the package's API)
from __future__ import annotations

import ast
import dataclasses
import functools
import os
import pathlib
import re
import sys
import time
from typing import Any, Iterable, Iterator

from . import accept
from .checks import _canon_py, _check_rule, _declared, _merge_groups, _Targets
from .contracts import (DYNAMIC_IMPORT, KINDS, REFLECTION, SKIP_DIRS, Accept, Finding, Mediation, Report,
                        ScanConfigError,
                        _Edge, _Fan, _Module, _OnPaths, _Site)
from .astutil import _dotted
from .indexer import _Index
from .mediation import _mediation, _not_scanned
from .reachability import _entrypoints, _flat, door_only
from .sarif import to_sarif
from .sources import (SKIPPED, Found, _layout, _skipped, _Source, anywhere, holds_code, iter_sources, provides,
                      third_party)
from .text import to_text
from .walker import _Walker

# `from invariants_scan import *` (and `from invariants.scan import *`) exports these names and no submodules.
__all__ = ["DYNAMIC_IMPORT", "Finding", "Graph", "KINDS", "Mediation", "REFLECTION",
           "Report", "SKIP_DIRS", "ScanConfigError", "anywhere", "door_only", "graph",
           "iter_sources", "mediations", "scan", "to_sarif", "to_text"]


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
    _check_shared(doc, scanned)
    return scanned, skipped


def _check_shared(doc: dict[str, Any], scanned: dict[str, Mediation]) -> None:
    """fix round 31: an allow_unresolved entry that lists rules is one the file's top-level allow_unresolved
    gave each rule it lists (loader.short_forms): one written in a rule's own list naming other rules would
    say "shared by" rules that never got it. Every rule it names must hold the same entry."""
    held: dict[str, list[Any]] = {}
    for raw in doc.get("rules") or []:
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            own = (raw.get("mediation") or {}).get("allow_unresolved") if isinstance(raw.get("mediation"), dict) \
                else None
            held[raw["id"]] = own if isinstance(own, list) else []
    for rid, med in scanned.items():
        for i, a in enumerate(med.accept or []):
            for other in a.rules:
                if other != rid and a.as_dict() not in [
                        {k: v for k, v in e.items() if k in ("at", "code", "sha", "why", "rules")}
                        for e in held.get(other, []) if isinstance(e, dict)]:
                    raise ScanConfigError(
                        f"{rid}: mediation.allow_unresolved[{i}]: rules names {other}, which holds no such entry: "
                        f"a rule's own entry accepts its site under that rule alone (leave rules out); an entry "
                        f"for several rules goes in the file's top-level allow_unresolved, with rules: [...]")


@dataclasses.dataclass
class Graph:
    """The scan's view of the code: functions, call edges, sites, entrypoints."""

    idx: _Index
    calls: list[_Edge | _Fan]            # the call records; a name-matched call is one _Fan
    sites: list[_Site]
    entries: list[str]
    # the source of the files findings are in, read and parsed once for every scan of this graph (fix round
    # 29: `inv init` scans each drafted rule again and again)
    lines: accept._Lines | None = dataclasses.field(default=None, repr=False, compare=False)

    @property
    def edges(self) -> _Edges:
        """Every call edge, a name-matched call as one inferred edge per method it may be (made as read)."""
        return _Edges(self.calls)

    def functions(self) -> list[str]:
        return sorted(f for f, fn in self.idx.funcs.items() if fn.node is not None)


class _Edges:
    """The edges of a list of call records, in order, made one at a time as they are read."""

    def __init__(self, calls: list[_Edge | _Fan]) -> None:
        self._calls = calls

    def __iter__(self) -> Iterator[_Edge]:
        return _flat(self._calls)

    def __len__(self) -> int:
        return sum(len(c.callees) if isinstance(c, _Fan) else 1 for c in self._calls)



class _Progress:
    """A line on stderr now and then while a long scan runs: nothing in the first PROGRESS_AFTER seconds
    (a normal scan says nothing), then at most one line every PROGRESS_EVERY seconds. INV_PROGRESS=0
    turns it off; stdout (the report) is never touched."""

    AFTER = 5.0
    EVERY = 5.0

    def __init__(self) -> None:
        self.start = self.last = time.monotonic()
        self.on = os.environ.get("INV_PROGRESS", "1") != "0"

    def __call__(self, what: str, done: int, total: int) -> None:
        if not self.on:
            return
        now = time.monotonic()
        if now - self.start < self.AFTER or now - self.last < self.EVERY:
            return
        self.last = now
        print(f"inv scan: {what} {done}/{total} ({now - self.start:.0f} s)", file=sys.stderr, flush=True)


_NO_SOURCE_WHY = {"extension": "<an extension module, with no Python source the scan can read>",
                  "bytecode": "<a compiled .pyc with no Python source the scan can read>"}


def _index(roots: Iterable[str | pathlib.Path], exclude: Iterable[str],
           progress: _Progress | None = None) -> _Index:
    """The first pass over the roots: scanned modules indexed, excluded ones noted (not read)."""
    idx = _Index()
    progress = progress or _Progress()
    skipped: list[_Source] = []
    walked = Found()
    paths = [pathlib.Path(r) for r in roots]
    idx.roots = [r.resolve() for r in paths]
    found = _layout(paths, exclude, walked)
    names = {s.name for s in found}
    for s in found:                                    # what a file under the roots is named, from any root
        for n in (s.name, *s.aliases):
            idx.files_named.add(n)
            idx.module_files.add(n)
            if s.form == "package":
                idx.package_names.add(n)
            parts = n.split(".")
            for i in range(1, len(parts)):
                idx.files_named.add(".".join(parts[:i]))
                idx.package_names.add(".".join(parts[:i]))
    for s in found:                                    # every other root's name for a file is an alias
        if s.shadowed_by is None:
            for a in s.aliases:
                if a not in names:
                    idx.module_alias.setdefault(a, s.name)
    for k, s in enumerate(found):
        progress("reading files", k, len(found))
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
        if s.form in ("source", "package") and not s.globbed:
            idx.skipped_files.append((s.root, s.file, pattern))
    idx.scanned_real = {m.file.resolve() for m in idx.modules.values()}
    for top, link in walked.outside:
        rel = link.relative_to(top).as_posix()
        idx.notes.append(f"{rel} is a symlink to a directory outside the source roots: it is scanned under "
                         f"the name of the package that holds the link")
    for top, link in walked.refused:                  # confined (inv mcp): never followed, so never read
        rel = link.relative_to(top).as_posix()
        idx.problems.append((top, link, 0, "unresolved/outside-root",
                             f"{rel} is a symlink that resolves outside the directory `inv mcp` confines reads "
                             f"to (its --root): it is not followed, so the code it holds is not checked; run "
                             f"`inv scan` yourself, or start the server with a wider --root"))
    for top, arc, globbed in walked.archives:
        rel = arc.relative_to(top).as_posix()
        if globbed is not None or third_party(top, arc, idx.third_party) is not None or not (count := holds_code(arc)):
            continue                                   # excluded, a virtualenv's own egg, or no code in it
        idx.problems.append((top, arc, 1, "unresolved/no-source",
                             f"{rel} is an archive holding {count} Python module(s), which Python imports once it "
                             f"is on sys.path (zipimport): what they run and call is not checked; keep their "
                             f"source in the root, or delete it"))
    idx.deep_imports()
    idx.unresolved_imports()
    idx.import_path_writes()
    return idx


# calls that import a module named by their first argument; and calls that run code named at run time (the
# builtins only as plain names: the methods `re.compile` and `model.eval` are not them)
_IMPORTS_NAMED = frozenset({"import_module", "__import__", "run_module"})
_RUNS_NAMED = frozenset({"run_path", "spec_from_file_location", "SourceFileLoader", "zipimporter", "load_module",
                         "exec_module"})
_RUNS_BUILTIN = frozenset({"exec", "eval", "compile", "execfile", "builtins.exec", "builtins.eval",
                           "builtins.compile"})
_PATH_STATE = ("sys.path.", "sys.meta_path.", "sys.path_hooks.")


def _skipped_imports(idx: _Index, root: pathlib.Path, file: pathlib.Path) -> list[tuple[int, str]]:
    """(line, what) for every scanned module a file in an always-skipped directory imports, statically or by
    a literal dynamic import, and for every call in it that runs or imports code it names at run time,
    read once. None when the file is a scanned module itself (through a symlink) or an exact copy of one
    (build/lib); in a recognised third-party tree (a virtualenv, a node package), an import that the tree
    itself provides and a computed import stay quiet, unless the file changes sys.path."""
    if file in idx.skipped_imports:
        return idx.skipped_imports[file]
    got: list[tuple[int, str]] = []
    idx.skipped_imports[file] = got
    rel = file.relative_to(root)
    parts = [*rel.parts[:-1], rel.stem]
    if parts[-1] == "__init__":
        parts.pop()
    try:
        data = file.read_bytes()
    except OSError:
        data = b""
    if idx.skipped_words is None:                      # a file naming none of these cannot reach scanned code
        words = {n.split(".")[0] for n in (*idx.modules, *idx.module_alias)} | _IMPORTS_NAMED | _RUNS_NAMED | {
            w.rpartition(".")[2] for w in _RUNS_BUILTIN}
        idx.skipped_words = re.compile(rb"\b(?:" + b"|".join(re.escape(w.encode()) for w in sorted(words)) + rb")\b")
    if not idx.skipped_words.search(data) or file.resolve() in idx.scanned_real or any(
            (m := idx.modules.get(".".join(parts[i:]))) is not None and _same(m.file, data) for i in range(len(parts))):
        return got
    try:
        tree = ast.parse(data, filename=str(file))
    except (SyntaxError, ValueError):
        tree = ast.Module(body=[], type_ignores=[])
    package = ".".join(parts if rel.stem == "__init__" else parts[:-1])
    mod = _Module(f"<skipped {rel.as_posix()}>", file, root, tree, package)
    tops = {n.split(".")[0] for n in (*idx.modules, *idx.module_alias)}
    names: list[tuple[int, str]] = []
    runs: list[tuple[int, str]] = []
    paths = False
    # a name bound to sys.path (p = sys.path; p.insert(0, d)) changes it too
    held = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) and f"{_dotted(n.value)}.".startswith(_PATH_STATE)
            for t in n.targets if isinstance(t, ast.Name)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [(node.lineno, idx.imported(mod, a.name)) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            src = idx._absolute(mod, node)
            names += [(node.lineno, src), *((node.lineno, f"{src}.{a.name}") for a in node.names)]
        elif isinstance(node, ast.Call):
            d = _dotted(node.func) or ""
            last = d.rpartition(".")[2]
            first = node.args[0] if node.args else None
            if last in _IMPORTS_NAMED and isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.append((node.lineno, idx.real(first.value.lstrip("."))))
            elif last in _IMPORTS_NAMED or last in _RUNS_NAMED or d in _RUNS_BUILTIN:
                runs.append((node.lineno, f"{d}()"))
            paths = paths or d.startswith(_PATH_STATE) or (d.partition(".")[0] in held and "." in d)
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            paths = paths or any(f"{_dotted(t.value if isinstance(t, ast.Subscript) else t)}.".startswith(_PATH_STATE)
                                 for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))
    base = third_party(root, file, idx.third_party)
    for line, name in names:
        top = name.split(".")[0]
        if top in tops and (base is None or paths or not provides(base, file, top)):
            got.append((line, f"imports {name}, which the scan reads,"))
    if base is None:
        got += [(line, f"runs or imports code it names at run time ({call}), which may be scanned code,")
                for line, call in runs]
    got.sort()
    return got


def _same(file: pathlib.Path, data: bytes) -> bool:
    try:
        return file.read_bytes() == data
    except OSError:
        return False


def _skipped_importers(idx: _Index, med: Mediation) -> list[Finding]:
    """A file in an always-skipped directory (build/, dist/, a dot-directory, a hand-made site-packages/, ...)
    that imports scanned code, or runs code it names at run time: if it runs, as a standalone script or
    through sys.path, its calls are not checked, so no path through it to a sink is seen."""
    out: list[Finding] = []
    for root, file, where in idx.skipped_files:
        hits = _skipped_imports(idx, root, file)
        if hits:
            line, what = hits[0]
            out.append(Finding("unresolved", med.rule, "unresolved/skipped-importer",
                               f"{what} from a {where!r} directory, which the scan always skips: if this file "
                               f"runs, its calls are not checked; move it out of {where}/ or delete it",
                               file.relative_to(root).as_posix(), line, 1, str(root)))
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
    progress = _Progress()
    idx = _index(roots, exclude, progress)
    edges: list[_Edge | _Fan] = []
    sites: list[_Site] = []
    for k, mod in enumerate(idx.modules.values()):
        progress("following calls in module", k, len(idx.modules))
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
    idx, edges, sites = g.idx, g.calls, g.sites
    report = Report([], scanned, skipped, files=len(idx.modules) + len(idx.parse_errors),
                    functions=sum(1 for f in idx.funcs.values() if f.node is not None))
    report.skipped = dict(sorted(idx.skipped_dirs.items()))
    report.notes = list(idx.notes)
    entries = g.entries
    progress = _Progress()
    guarded = _guarded(idx, doc)
    if g.lines is None:
        g.lines = accept._Lines({m.file: m.tree for m in idx.modules.values()})
    lines = g.lines
    for k, med in enumerate(scanned.values()):
        progress("checking rule", k, len(scanned))
        on = _OnPaths()                                # worked out when read (inv init), not here
        dict[str, Any].__setitem__(report.on_paths, med.rule, on)
        if med.pairs is not None:                      # a sink mapping: each set of doors with its own sinks
            report.findings += _check_paired(idx, med, edges, lines, sites, entries, on, guarded, report)
            continue
        found = _check_rule(idx, med, edges, sites, entries, on, guarded=guarded)
        found += [Finding("unresolved", med.rule, code, msg, f.relative_to(r).as_posix(), line, 1,
                          str(r)) for r, f, line, code, msg in idx.problems]
        found += _skipped_importers(idx, med)
        if med.accept is not None:                     # reviewed sites: shown as accepted, never failing
            accept.fill_sha(found, lines, _inferred_basis(idx, med, edges, entries, found), old=bool(med.accept))
            found, ok = accept.apply(med.rule, med.accept, found)
            report.accepted += ok
        else:                                          # hashed only when asked (JSON, SARIF, the API)
            accept.defer_sha(found, lines, functools.partial(_inferred_basis, idx, med, edges, entries, found))
        report.findings += found
    order = {k: i for i, k in enumerate(KINDS)}
    report.findings.sort(key=lambda f: (f.rule, order[f.kind], f.file or "", f.line, f.col, f.code))
    report.accepted.sort(key=lambda f: (f.rule, f.file or "", f.line, f.col, f.code))
    return report


def _check_paired(idx: _Index, med: Mediation, edges: list[_Edge | _Fan], lines: accept._Lines, sites: list[_Site],
                  entries: list[str], on: _OnPaths, guarded: frozenset[str], report: Report) -> list[Finding]:
    """The findings of a rule whose mediation.sink maps each sink to its doors: each set of doors is checked
    with the sinks it may reach (Mediation.groups), so a path to a sink through another sink's door only is a
    bypass. The same finding from two parts is one; an allow entry is judged in the part of each sink it lists; each
    part's inferred paths are hashed in its own door-free graph. Accepted findings go to report.accepted."""
    parts = [(sub, _check_rule(idx, sub, edges, sites, entries, on, guarded=guarded)) for sub in med.groups()]
    found = _merge_groups([got for _, got in parts])
    rest = [Finding("unresolved", med.rule, code, msg, f.relative_to(r).as_posix(), line, 1,
                    str(r)) for r, f, line, code, msg in idx.problems]
    rest += _skipped_importers(idx, med)
    for sub, got in parts:
        if med.accept is not None:
            accept.fill_sha(got, lines, _inferred_basis(idx, sub, edges, entries, got), old=bool(med.accept))
        else:
            accept.defer_sha(got, lines, functools.partial(_inferred_basis, idx, sub, edges, entries, got))
    if med.accept is not None:
        accept.fill_sha(rest, lines, None, old=bool(med.accept))
        found, ok = accept.apply(med.rule, med.accept, found + rest)
        report.accepted += ok
        return found
    accept.defer_sha(rest, lines, lambda: None)
    return found + rest


def _inferred_basis(idx: _Index, med: Mediation, edges: list[_Edge | _Fan], entries: list[str],
                    found: list[Finding]) -> accept.InferredBasis | None:
    """What an unresolved/inferred-path finding's sha covers besides its site (accept.InferredBasis), from
    the rule's door-free graph with name-matched calls; None when the rule has no such finding."""
    if not any(f.code == "unresolved/inferred-path" for f in found):
        return None
    t = _Targets(_declared(idx, med.rule, med.sinks, "sink", []), _declared(idx, med.rule, med.doors, "door", []),
                 _declared(idx, med.rule, med.allow, "allow", []))
    goals = t.sinks - t.doors
    ps = t.passes(edges)[1]
    return accept.InferredBasis(ps.succ, [s for s in entries if s not in t.sinks and s not in t.doors], goals,
                                found)


# decorators that put an inv guard on a function (it checks the rule's values before or after it runs)
_INV_GUARDS = frozenset({"guard", "model_call", "edge"})


def _guarded(idx: _Index, doc: dict[str, Any]) -> frozenset[str]:
    """The scanned functions that are guarded doors: the doors and targets of the rules in the file, and
    every function decorated with an inv guard (`@inv.guard`, `@inv.model_call`, `@inv.edge`)."""
    from .inference import _Inference
    named: set[str] = set()
    for raw in doc.get("rules") or []:
        if not isinstance(raw, dict):
            continue
        med = raw.get("mediation")
        doors = med.get("door") if isinstance(med, dict) else None
        sink = med.get("sink") if isinstance(med, dict) else None
        if isinstance(sink, dict):                     # a sink mapping names its doors (fix round 30)
            doors = [d for ds in sink.values() for d in ([ds] if isinstance(ds, str) else
                                                         ds if isinstance(ds, list) else [])]
        subject = raw.get("subject")
        target = subject.get("target") if isinstance(subject, dict) else None
        for p in [*([doors] if isinstance(doors, str) else doors if isinstance(doors, list) else []),
                  *([target] if isinstance(target, str) else [])]:
            if isinstance(p, str) and p.startswith("py:"):
                named.add(idx.canon(p[3:]))
    out: set[str] = {n for n in named if n in idx.funcs and idx.funcs[n].node is not None}
    homes: dict[str, _Inference] = {}
    # only a module that imports from invariants (or star-imports) can name an inv guard
    may = {m for m, mod in idx.modules.items() if mod.star or any(
        v.split(".")[0] == "invariants" for v in [*mod.aliases.values(), *mod.nested.values()])}
    for fid, f in idx.funcs.items():
        node = f.node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.decorator_list or fid in out \
                or f.module not in may:
            continue
        inf = homes.get(f.module) or homes.setdefault(f.module, _Inference(idx, idx.modules[f.module]))
        for d in node.decorator_list:
            fn = d.func if isinstance(d, ast.Call) else d
            fq = inf._resolve(fn, set(), None) if _dotted(fn) else None
            if fq and fq.split(".")[0] == "invariants" and fq.rpartition(".")[2] in _INV_GUARDS:
                out.add(fid)
                break
    return frozenset(out)
