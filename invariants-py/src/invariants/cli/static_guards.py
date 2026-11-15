"""`inv check`'s guard check in the deciding process: read from the source, never imported.

The guard check `inv verify` runs imports the app, and imported code can replace whatever runs the
check in its process. So `inv check` also decides, here, in the process that never imports the
project, that each rule's target and edge targets carry their decorator: it finds each `py:` target's
module on the child's import path (importlib's PathFinder, which reads directories and runs no project
code), parses that file, walks to the `def` the target names (through classes, and `from M import
name` re-exports), and reads its decorators: an `@inv.guard(...)`, `@inv.model_call(...)` or
`@inv.edge(...)` (any import form of invariants, invariants.guard or the fastapi and pydantic
integrations) whose literal arguments name the rule's id (or, for a guard, its target key).

A target this cannot read (no source file, a name bound more than once or in a branch, a decorator
whose arguments are not literals or that is not one of those, a module alias rebound) is a FAIL that
says so: the check fails closed. `inv check` passes its guard step only when this passes and the
child's `inv verify` passes too (the child alone checks what needs the imported objects: a guard
naming a rule the file does not have, a model_call's schema, bindings, FastAPI routes).
"""
from __future__ import annotations

import ast
import importlib.machinery
import pathlib
import sys
from typing import Any, Sequence

from ..confinement import _places, _through, confine_root, inside
from ..model import GUARD_POINTS
from .common import SITE
from ..project import import_roots

# what each decorator is, by the dotted name it is imported as, and the points a guard evaluates
_GUARD = ("decorator",)
_DECORATORS: dict[str, tuple[str, tuple[str, ...]]] = {
    **{n: ("guard", _GUARD) for n in ("invariants.guard", "invariants.guard.guard",
                                       "invariants.guard.decorators.guard")},
    "invariants.integrations.fastapi.guard": ("guard", ("decorator", "middleware")),
    **{n: ("model_call", ()) for n in ("invariants.model_call", "invariants.guard.model_call",
                                        "invariants.guard.decorators.model_call",
                                        "invariants.integrations.pydantic.model_call")},
    **{n: ("edge", ()) for n in ("invariants.edge", "invariants.guard.edge", "invariants.guard.decorators.edge")},
}
_DEPTH = 8            # re-exports followed


class _Unreadable(Exception):
    """The target cannot be checked from the source: the message says why."""


class _Source:
    """Modules found on one import path, parsed once each."""

    def __init__(self, path: list[str]) -> None:
        self.path = path
        self._mods: dict[str, tuple[pathlib.Path, bool] | None] = {}
        self._trees: dict[pathlib.Path, ast.Module] = {}
        self.top = confine_root()       # under `inv mcp` (INV_CONFINE_ROOT): read only what the child imports
        self._inside: set[str] = set()  # modules found inside top (every place)
        self._project: set[str] = set()  # top-level packages of the project: a place inside top

    def _confine(self, name: str, spec: importlib.machinery.ModuleSpec) -> None:
        """Under INV_CONFINE_ROOT, as the child imports (confinement.import_module, ConfinedFinder): a
        module of the project that resolves outside the root (a symlink inside it leads out, or a
        submodule of a package found inside it) is not read; _Unreadable before the file is opened."""
        if self.top is None:
            return
        places = _places(spec)
        out = [p for p in places if not inside(p, self.top)]
        if "." not in name and len(out) < len(places):
            self._project.add(name)
        if not out:
            if places:
                self._inside.add(name)
            return
        if any(_through(p, self.top) for p in out) or ("." in name and name.split(".")[0] in self._project):
            raise _Unreadable(f"module {name} resolves outside {self.top}, the --root `inv mcp` confines "
                              f"reads to; not read")

    def confine_head(self, name: str) -> None:
        """Under INV_CONFINE_ROOT, a target's top-level module is a file inside the root (as the child's
        import_module requires: the standard library and installed packages are outside it)."""
        if self.top is not None and self.find(name) is not None and name not in self._inside:
            raise _Unreadable(f"module {name} is not a file inside {self.top}, the --root `inv mcp` "
                              f"confines reads to; not read")

    def find(self, name: str) -> tuple[pathlib.Path, bool] | None:
        """(source file, is a package) of an importable module, None if there is none. A module
        that exists but has no .py source raises _Unreadable."""
        if name in self._mods:
            return self._mods[name]
        parent, _, last = name.rpartition(".")
        where: Sequence[str] | None = self.path
        if parent:
            up = self.find(parent)
            if up is None or not up[1]:
                self._mods[name] = None
                return None
            where = self._locations(parent)
        try:
            spec = importlib.machinery.PathFinder.find_spec(name if not parent else name, list(where or []))
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            self._mods[name] = None
            return None
        self._confine(name, spec)
        is_pkg = spec.submodule_search_locations is not None
        origin = spec.origin
        if origin is None or origin == "namespace" or not origin.endswith(".py"):
            if is_pkg and (origin is None or origin == "namespace"):
                got: tuple[pathlib.Path, bool] | None = (pathlib.Path(""), True)   # a namespace package
                self._mods[name] = got
                self._ns = getattr(self, "_ns", {})
                self._ns[name] = list(spec.submodule_search_locations or [])
                return got
            raise _Unreadable(f"module {name} has no Python source to read ({origin})")
        got = (pathlib.Path(origin), is_pkg)
        self._mods[name] = got
        return got

    def _locations(self, pkg: str) -> list[str]:
        ns = getattr(self, "_ns", {})
        if pkg in ns:
            return list(ns[pkg])
        got = self.find(pkg)
        return [] if got is None else [str(got[0].parent)]

    def tree(self, name: str) -> ast.Module | None:
        """The parsed module (None for a namespace package, which has no source)."""
        got = self.find(name)
        if got is None:
            raise _Unreadable(f"module {name} is not found on the import path")
        file = got[0]
        if file == pathlib.Path(""):
            return None
        if file not in self._trees:
            try:
                self._trees[file] = ast.parse(file.read_bytes(), filename=str(file))
            except (OSError, SyntaxError, ValueError) as e:
                raise _Unreadable(f"{file.name} cannot be parsed ({type(e).__name__})") from e
        return self._trees[file]


def _search_path(rule_file: pathlib.Path, extra: Sequence[str]) -> list[str]:
    """The child's import path: the --path entries and the checkout's roots first, sys.path, --site last."""
    first = [str(d) for d in import_roots(rule_file, extra)]
    return [*first, *(p for p in sys.path if p not in first), *(s for s in SITE if s not in first and s not in sys.path)]


# --- bindings ---------------------------------------------------------------------------------
def _flat(body: list[ast.stmt]) -> list[tuple[ast.stmt, bool]]:
    """Every statement of a body, with the ones in if/try/with/for/while/match blocks (not in a def or
    class): (statement, at the top of the body)."""
    out: list[tuple[ast.stmt, bool]] = []

    def walk(stmts: list[ast.stmt], top: bool) -> None:
        for s in stmts:
            out.append((s, top))
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(s, field, None)
                if isinstance(sub, list):
                    walk(sub, False)
            for h in getattr(s, "handlers", []) or []:
                walk(h.body, False)
            for c in getattr(s, "cases", []) or []:
                walk(c.body, False)
    walk(body, True)
    return out


def _target_names(t: ast.AST) -> list[str]:
    return [n.id for n in ast.walk(t) if isinstance(n, ast.Name)]


def _bindings(body: list[ast.stmt], name: str, module: bool) -> list[tuple[ast.stmt, bool]]:
    """The statements of a body that bind name, and (for a module) any def that declares it global."""
    out: list[tuple[ast.stmt, bool]] = []
    for s, top in _flat(body):
        names: list[str] = []
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [s.name]
        elif isinstance(s, (ast.Import, ast.ImportFrom)):
            names = [a.asname or (a.name.split(".")[0] if isinstance(s, ast.Import) else a.name) for a in s.names]
            if any(a.name == "*" for a in s.names):
                names.append(name)                     # a star import may bind anything
        elif isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
            for t in (s.targets if isinstance(s, (ast.Assign, ast.Delete)) else [s.target]):
                names.extend(_target_names(t))
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            names = _target_names(s.target)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            names = [n for i in s.items if i.optional_vars is not None for n in _target_names(i.optional_vars)]
        elif isinstance(s, ast.Try):
            names = [h.name for h in s.handlers if h.name]
        if name in names:
            out.append((s, top))
        # walrus anywhere in the statement's expressions (not in nested defs)
        if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for n in ast.walk(s):
                if isinstance(n, ast.NamedExpr) and n.target.id == name and (s, top) not in out:
                    out.append((s, top))
    if module:
        tree = ast.Module(body=body, type_ignores=[])
        for n in ast.walk(tree):
            if isinstance(n, (ast.Global, ast.Nonlocal)) and name in n.names:
                out.append((n, False))
    return out


def _is_overload(d: ast.expr) -> bool:
    return (isinstance(d, ast.Name) and d.id == "overload") or (isinstance(d, ast.Attribute) and d.attr == "overload")


def _only(body: list[ast.stmt], name: str, module: bool, where: str) -> ast.stmt:
    """The one statement that binds name in body (typing.overload stubs before it aside)."""
    found = _bindings(body, name, module)
    real = [(s, top) for s, top in found
            if not (isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(map(_is_overload, s.decorator_list)))]
    if not found:
        raise _Unreadable(f"{where} has no {name!r} in its source")
    if len(real) != 1:
        lines = ", ".join(str(getattr(s, "lineno", "?")) for s, _ in found)
        raise _Unreadable(f"{name!r} is bound {len(real)} times in {where} (lines {lines}): the source does not "
                          f"say which one runs")
    s, top = real[0]
    if not top:
        raise _Unreadable(f"{name!r} is bound inside a block in {where} (line {s.lineno}): the source does not "
                          f"say whether it runs")
    return s


# --- walking to the def -----------------------------------------------------------------------
def _absolute(mod: str, is_pkg: bool, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    base = mod.split(".") if is_pkg else mod.split(".")[:-1]
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, *([node.module] if node.module else [])])


def _find_def(src: _Source, mod: str, rest: list[str], depth: int = 0
              ) -> tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """(module the def is in, the def) for the attribute path rest of module mod."""
    if depth > _DEPTH:
        raise _Unreadable(f"more than {_DEPTH} re-exports to follow")
    tree = src.tree(mod)
    found = src.find(mod)
    is_pkg = bool(found and found[1])
    if tree is None:                               # a namespace package: only its submodules
        if src.find(f"{mod}.{rest[0]}") is not None and len(rest) > 1:
            return _find_def(src, f"{mod}.{rest[0]}", rest[1:], depth + 1)
        raise _Unreadable(f"{mod} is a namespace package with no {rest[0]!r} to read")
    body: list[ast.stmt] = tree.body
    where = mod
    for i, part in enumerate(rest):
        try:
            s = _only(body, part, i == 0, where)
        except _Unreadable:
            if i == 0 and is_pkg and len(rest) > 1 and not _bindings(body, part, True) \
                    and src.find(f"{mod}.{part}") is not None:
                return _find_def(src, f"{mod}.{part}", rest[1:], depth + 1)     # a submodule
            raise
        last = i == len(rest) - 1
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and last:
            return where.partition(":")[0], s
        if isinstance(s, ast.ClassDef) and not last:
            body, where = s.body, f"{mod}:{'.'.join(rest[:i + 1])}"
            continue
        if isinstance(s, ast.ImportFrom) and i == 0:
            alias = next(a for a in s.names if (a.asname or a.name) == part)
            base = _absolute(mod, is_pkg, s)
            if src.find(f"{base}.{alias.name}") is not None and not _defines(src, base, alias.name):
                if last:
                    raise _Unreadable(f"{part!r} in {mod} is a module, not a function")
                return _find_def(src, f"{base}.{alias.name}", rest[1:], depth + 1)
            return _find_def(src, base, [alias.name, *rest[1:]], depth + 1)
        if isinstance(s, ast.Import) and i == 0 and not last:
            alias = next(a for a in s.names if (a.asname or a.name.split(".")[0]) == part)
            return _find_def(src, alias.name if alias.asname else part, rest[1:], depth + 1)
        what = type(s).__name__
        raise _Unreadable(f"{'.'.join(rest[:i + 1])} in {mod} is bound by a {what} statement (line {s.lineno}), "
                          f"not a def{'' if last else ' or class'} the source can be read for")
    raise _Unreadable(f"no function in {mod}")


def _defines(src: _Source, mod: str, name: str) -> bool:
    try:
        tree = src.tree(mod)
    except _Unreadable:
        return False
    return tree is not None and bool(_bindings(tree.body, name, True))


def _locate(src: _Source, key: str) -> tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    parts = key[3:].split(".")
    if not key.startswith("py:") or not all(p.isidentifier() for p in parts):
        raise _Unreadable("not a py:module.function name")
    src.confine_head(parts[0])
    for i in range(len(parts), 0, -1):
        if src.find(".".join(parts[:i])) is not None:
            if i == len(parts):
                raise _Unreadable("it names a module, not a function")
            return _find_def(src, ".".join(parts[:i]), parts[i:])
    raise _Unreadable(f"no module {parts[0]!r} on the import path")


# --- the decorators ---------------------------------------------------------------------------
def _aliases(src: _Source, mod: str) -> dict[str, str]:
    """Module-level import names -> what they import, for the names invariants is imported as. A
    name also bound some other way (or imported twice, as different things) is left out."""
    tree = src.tree(mod)
    found = src.find(mod)
    if tree is None or found is None:
        return {}
    out: dict[str, str] = {}
    bad: set[str] = set()
    for s, _ in _flat(tree.body):
        pairs: list[tuple[str, str]] = []
        if isinstance(s, ast.Import):
            pairs = [(a.asname, a.name) if a.asname else (a.name.split(".")[0], a.name.split(".")[0]) for a in s.names]
        elif isinstance(s, ast.ImportFrom):
            base = _absolute(mod, found[1], s)
            pairs = [(a.asname or a.name, f"{base}.{a.name}") for a in s.names if a.name != "*"]
        for n, full in pairs:
            if full.split(".")[0] == "invariants":
                if out.get(n, full) != full:
                    bad.add(n)
                out[n] = full
    for n in list(out):
        others = [s for s, _ in _bindings(tree.body, n, True) if not isinstance(s, (ast.Import, ast.ImportFrom))]
        if others or n in bad:
            del out[n]
    return out


def _dotted(e: ast.expr) -> str | None:
    if isinstance(e, ast.Name):
        return e.id
    if isinstance(e, ast.Attribute):
        head = _dotted(e.value)
        return None if head is None else f"{head}.{e.attr}"
    return None


def _decorators(src: _Source, mod: str, fn: ast.FunctionDef | ast.AsyncFunctionDef
                ) -> list[tuple[str, list[Any] | None, tuple[str, ...] | None, int]]:
    """(kind, rule keys or None when not all literal, points or None when not literal, line) of each
    invariants decorator on fn."""
    names = _aliases(src, mod)
    out = []
    for d in fn.decorator_list:
        if not isinstance(d, ast.Call):
            continue
        dotted = _dotted(d.func)
        if dotted is None:
            continue
        head, _, tail = dotted.partition(".")
        full = names.get(head)
        if full is None:
            continue
        full = f"{full}.{tail}" if tail else full
        if full not in _DECORATORS:
            continue
        kind, points = _DECORATORS[full]
        args: list[ast.expr] = list(d.args) if kind == "guard" else list(d.args[:1])
        if kind != "guard":
            args += [k.value for k in d.keywords if k.arg == "rule_id"]
        keys: list[Any] | None = [a.value for a in args if isinstance(a, ast.Constant)]
        if any(not (isinstance(a, ast.Constant) and isinstance(a.value, str)) for a in args) \
                or any(isinstance(a, ast.Starred) for a in d.args):
            keys = None
        pts: tuple[str, ...] | None = points
        for k in d.keywords:
            if k.arg == "points" and kind == "guard" and full != "invariants.integrations.fastapi.guard":
                v = k.value
                if isinstance(v, (ast.Tuple, ast.List)) and all(
                        isinstance(x, ast.Constant) and isinstance(x.value, str) for x in v.elts):
                    pts = tuple(str(x.value) for x in v.elts if isinstance(x, ast.Constant))
                else:
                    pts = None
            elif k.arg is None and kind == "guard":
                pts = None                          # **kw could pass points
        out.append((kind, keys, pts, d.lineno))
    return out


def _carries(src: _Source, key: str, rid: str, kind: str, points: set[str]) -> str | None:
    """Why the def key names does not carry @inv.<kind> naming rid (or key, for a guard), None if it does."""
    try:
        mod, fn = _locate(src, key)
        decos = _decorators(src, mod, fn)
    except _Unreadable as e:
        return f"{rid}: {key} cannot be checked from its source ({e}); `inv check` fails closed"
    names = {rid} | ({key} if kind != "edge" else set())
    deco = {"guard": "@inv.guard", "model_call": "@inv.model_call", "edge": "@inv.edge"}[kind]
    unread = False
    for k, keys, pts, _ in decos:
        if keys is None:
            unread = True
            continue
        if k == kind and names & set(keys):
            if kind == "guard" and pts is None:
                return (f"{rid}: the guard on {key} passes points that are not literals; `inv check` cannot "
                        f"read them from the source")
            if kind == "guard" and pts is not None and points - set(pts):
                return f"{rid}: the guard on {key} does not evaluate point {', '.join(sorted(points - set(pts)))}"
            return None
    if unread:
        return (f"{rid}: an invariants decorator on {key} names its rules with expressions, not literal "
                f"strings, so the source does not show it names {rid}; `inv check` fails closed")
    return f"{rid}: {key} is not decorated with {deco}({rid!r}) in its source (line {fn.lineno})"


def guard_problems(rule_file: pathlib.Path, doc: dict[str, Any], extra: Sequence[str]) -> tuple[int, list[str]]:
    """(the number of targets checked, why any does not carry its decorator in the source)."""
    src = _Source(_search_path(rule_file, extra))
    n, problems = 0, []
    for r in doc.get("rules") or []:
        if not isinstance(r, dict) or r.get("status") == "retired":
            continue
        rid = str(r.get("id"))
        subject = r.get("subject")
        target = subject.get("target") if isinstance(subject, dict) else None
        if not isinstance(target, str):
            continue                                 # a targetless guard: only the child checks it
        enforcers = [e for e in r.get("enforcers") or [] if isinstance(e, dict)]
        points = {e.get("point") for e in enforcers} & GUARD_POINTS
        if r.get("model_policy") or points:
            n += 1
            why = _carries(src, target, rid, "model_call" if r.get("model_policy") else "guard",
                           set() if r.get("model_policy") else {str(p) for p in points})
            if why:
                problems.append(why)
        for e in enforcers:
            if e.get("point") == "edge" and isinstance(e.get("target"), str):
                n += 1
                why = _carries(src, e["target"], rid, "edge", set())
                if why:
                    problems.append(why)
    return n, problems
