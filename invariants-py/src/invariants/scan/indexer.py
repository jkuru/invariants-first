"""The first pass: modules, functions, classes and aliases across every source root (never imported)."""
from __future__ import annotations

import ast
import builtins
import collections
import importlib.machinery
import pathlib
import re
import sys
from typing import Any

from .astutil import DEFS, IMPORTS, _dotted, _number, _own_nodes, _walk_of
from .contracts import _Func, _Module

_BUILTINS = frozenset(dir(builtins))
# top-level modules whose classes are known bases: a base from one of them is not project code
_KNOWN_BASE_MODULES = frozenset({*sys.stdlib_module_names, "typing_extensions"})
_TYPE_ALIAS = getattr(ast, "TypeAlias", None)            # `type X = ...` (Python 3.12+)
_NOWHERE = "<above the source roots>"                    # a relative import above every root: no module
# where Python finds modules: a write sends later imports to code outside the roots
_IMPORT_STATE = frozenset({"sys.path", "sys.meta_path", "sys.path_hooks", "sys.path_importer_cache"})
_IMPORT_STATE_CALLS = frozenset({"importlib.invalidate_caches", "site.addsitedir", "zipimport.zipimporter",
                                 "importlib.machinery.PathFinder.invalidate_caches"})
# calls that let a package's submodules come from other directories (they extend its __path__)
_PACKAGE_PATH_CALLS = frozenset({"pkgutil.extend_path", "pkg_resources.declare_namespace",
                                 "pkg_resources.fixup_namespace_packages"})
_READS = frozenset({"index", "count", "copy", "get", "keys", "values", "items", "__contains__", "__len__",
                    "__iter__", "__getitem__"})


class _Index:
    """Modules, functions, classes and aliases across every source root."""

    def __init__(self) -> None:
        self.modules: dict[str, _Module] = {}
        self.funcs: dict[str, _Func] = {}
        self.classes: dict[str, dict[str, Any]] = {}     # id -> {methods: {name: id}, bases: [expr], module}
        self.methods_by_name: collections.defaultdict[str, set[str]] = collections.defaultdict(set)
        self.parse_errors: list[tuple[pathlib.Path, pathlib.Path, int, str]] = []
        self.attr_types: dict[str, dict[str, str]] = {}  # class -> {attribute: class}, shared by every walker
        self.attr_seen: dict[str, dict[str, set[str | None]]] = {}   # class -> its own self.attr bindings
        self.foreign: set[str] | None = None             # attribute names set from outside their object
        self.returns: dict[str, str | None] = {}         # function -> the scanned class its return annotation names
        self._values: dict[str, dict[str, ast.expr]] = {}  # module -> its names bound once (module_values)
        self._subs: dict[str, list[str]] | None = None     # class -> its scanned subclasses (subclasses)
        self._short: set[str] | None = None                # the last name of every scanned function
        self._bases: dict[str, list[str | None]] = {}      # class -> _base_names (once the index is built)
        self._methods: dict[tuple[str, str], str | None] = {}   # (class, name) -> method()
        self._mros: dict[tuple[str, bool], list[str] | None] = {}  # (class, strict) -> its C3 order
        self._fans: dict[str, tuple[str, ...]] = {}         # name -> every scanned method of that name, sorted
        # fix round 26: module-level functions by name (a call on an unknown receiver may be a module's
        # function), the classes that bind a method name (a def or a class-body alias), and each class body's
        # bindings in order (def, alias, other), linked into the methods once every module is read
        self.module_funcs_by_name: collections.defaultdict[str, set[str]] = collections.defaultdict(set)
        self.owners_by_name: collections.defaultdict[str, set[str]] = collections.defaultdict(set)
        self._class_events: dict[str, list[tuple[str, str, str]]] = {}   # class -> (kind, name, fid | dotted)
        self._linked = True
        self._ctors: dict[str, tuple[str, ...]] = {}
        self._members: dict[str, tuple[str, ...]] = {}
        self.getters: dict[str, tuple[str, ...]] = {}       # name -> the scanned property getters of that name
        self._class_names: collections.defaultdict[str, list[str]] | None = None   # last name -> classes
        self._tails: set[str] | None = None                # every dotted tail of a module name under the roots
        self.excluded: dict[str, tuple[str, str]] = {}   # excluded module -> (root-relative file, its glob)
        self.excluded_files: dict[str, pathlib.Path] = {}  # excluded module -> its file (read only for classes)
        self._excluded_bases: list[tuple[str, set[str]]] | None = None   # (class, its base names), lazily
        self._excluded_trees: dict[str, tuple[ast.Module, dict[str, str]] | None] = {}
        self._effects: dict[str, bool] = {}               # excluded module -> runs code at import time
        # (first parameter name, class) -> how the methods naming it so use it: cm, plain, static
        self.first_params: collections.defaultdict[tuple[str, str], set[str]] = collections.defaultdict(set)
        # files and roots (fix round 23): a file several roots hold has one name and aliases; a relative
        # import resolves by directory; what the scan cannot read or what Python never imports is noted
        self.module_alias: dict[str, str] = {}           # another root's name for a module -> its name
        self.long_package: dict[str, str] = {}           # module -> its package named from the shallowest root
        self.no_source: dict[str, pathlib.Path] = {}     # importable module with no source (.so, .pyd, .pyc)
        # (root, file, line, code, message) reported under every scanned rule, like a parse error
        self.problems: list[tuple[pathlib.Path, pathlib.Path, int, str, str]] = []
        self.skipped_dirs: collections.Counter[str] = collections.Counter()   # always-skipped dir -> files
        self.skipped_files: list[tuple[pathlib.Path, pathlib.Path, str]] = []  # (root, file, its directory)
        self.skipped_imports: dict[pathlib.Path, list[tuple[int, str]]] = {}   # skipped file -> its imports
        self.notes: list[str] = []
        # every module name a file under the roots has (scanned or not, any root's name, and each package
        # above one), and which of them are packages: an import of anything else under them is unknown
        self.files_named: set[str] = set()
        self.module_files: set[str] = set()              # names with a file of their own (not namespace)
        self.package_names: set[str] = set()
        self.roots: list[pathlib.Path] = []              # the source roots, resolved
        self._installed: dict[str, bool] = {}            # namespace portion -> installed outside the roots
        self.scanned_real: set[pathlib.Path] = set()     # the real path of every scanned module's file
        self.skipped_words: re.Pattern[bytes] | None = None   # a word a skipped file reaching scanned code has
        self.third_party: dict[pathlib.Path, pathlib.Path | None] = {}   # directory -> its third-party tree

    def fan(self, name: str) -> tuple[str, ...]:
        """Every scanned method named name, sorted: what a call of .name on an unknown receiver may be (one
        tuple per name, shared by every such call)."""
        got = self._fans.get(name)
        if got is None:
            self.link()
            # a module's function too (`m._send()` where m is a module value the scan cannot type), and the
            # constructors of a class of that name (`m.Sender(...)`)
            if self._class_names is None:
                self._class_names = collections.defaultdict(list)
                for k in self.classes:
                    self._class_names[k.rpartition(".")[2]].append(k)
            ctors = {c for k in self._class_names.get(name, ()) for c in self.constructors(k)}
            got = self._fans[name] = tuple(sorted({*self.methods_by_name.get(name, ()),
                                                   *self.module_funcs_by_name.get(name, ()), *ctors}))
        return got

    def short_names(self) -> set[str]:
        """The last name of every scanned function (`save` for app.repo.Repo.save): what a sink may be named."""
        if self._short is None:
            self.link()
            self._short = {f.rpartition(".")[2] for f in self.funcs} | set(self.methods_by_name)
        return self._short

    def class_param(self, me: tuple[str, str] | None) -> bool:
        """me's name is the class itself (a @classmethod's `cls`), never an instance, in every method of
        its class that names its first parameter so: `cls()` builds an instance of it or of a subclass."""
        return me is not None and self.first_params.get(me) == {"cm"}

    def outside(self, fq: str) -> bool:
        """fq names an object outside the scanned code: no prefix of it is a scanned, excluded or unreadable
        module, a file under the roots, or a scanned class or function; so it is the standard library, a
        builtin or a third-party package (an import of a module a scanned package lacks is reported on
        its own, as unresolved/unknown-module)."""
        if not fq or fq.startswith(("self:", "?")) or self.excluded_module(fq) is not None:
            return False
        if self._tails is None:                        # a file under the roots named from a deeper directory
            self._tails = {".".join(p[i:]) for n in self.module_files for p in [n.split(".")]
                           for i in range(len(p))}
        parts = self.real(fq).split(".")
        for i in range(1, len(parts) + 1):
            head = ".".join(parts[:i])
            if head in self.modules or head in self.classes or head in self.funcs or head in self.module_files \
                    or head in self.no_source or head in self.excluded or head in self._tails:
                return False                           # (`fleet.store` may be app/fleet/store.py on sys.path)
        return True

    def real(self, fq: str) -> str:
        """fq with a module name another root gives a file (`src.app.x` with the roots `src` and `.`)
        replaced by the name the scan indexes it under (`app.x`)."""
        if not self.module_alias or fq in self.modules:
            return fq
        parts = fq.split(".")
        for i in range(len(parts), 0, -1):
            head = ".".join(parts[:i])
            if head in self.modules:
                return fq
            if head in self.module_alias:
                return ".".join([self.module_alias[head], *parts[i:]])
        return fq

    def excluded_module(self, fq: str) -> str | None:
        """The excluded module fq names or lies in (a scanned module of the same name wins), else None."""
        parts = self.real(fq).split(".")
        for i in range(len(parts), 0, -1):
            head = ".".join(parts[:i])
            if head in self.modules or head in self.classes or head in self.funcs:
                return None
            if head in self.excluded:
                return head
        return None

    def add(self, root: pathlib.Path, file: pathlib.Path, name: str) -> None:
        try:
            tree = ast.parse(file.read_bytes(), filename=str(file))
        except (SyntaxError, ValueError) as e:
            self.parse_errors.append((root, file, getattr(e, "lineno", 0) or 0, str(e)))
            return
        package = name if file.name == "__init__.py" else name.rpartition(".")[0]
        _number(tree)
        mod = _Module(name, file, root, tree, package)
        self.modules[name] = mod
        self._bases.clear()                            # a new module may change what a base name names
        self._methods.clear()
        self._mros.clear()
        self._linked = False
        self._class_names = None
        self.funcs[f"{name}.<module>"] = _Func(f"{name}.<module>", name, file, root, 1, 0, None)
        # an import binds the scope it is in: one in a function or class body is not a module attribute
        # (`from ops import go` sees the module's own go), so it may not shadow a module-scope binding
        top_level = {id(n) for n in _own_nodes(tree) if isinstance(n, IMPORTS)}
        for node in _walk_of(tree, *IMPORTS):
            into = mod.aliases if id(node) in top_level else mod.nested
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        into[a.asname] = self.real(a.name)
                    else:
                        top = a.name.split(".")[0]
                        into[top] = top
            elif isinstance(node, ast.ImportFrom):
                src = self._absolute(mod, node)
                for a in node.names:
                    if a.name == "*":
                        mod.star.append(src)
                    else:
                        into[a.asname or a.name] = f"{src}.{a.name}" if src else a.name
        self._defs(mod, tree.body, prefix=name, cls=None)

    def _absolute(self, mod: _Module, node: ast.ImportFrom) -> str:
        """The module an import from names. A relative one resolves by the file's directory: from its name
        under the shallowest root that holds it, so `from ..payments import x` in src/app/sub/ops.py is
        app.payments whichever root names the file; one that climbs above every root names nothing."""
        if not node.level:
            return self.imported(mod, node.module or "")
        got = self._relative(mod, node)
        return self.real(got) if got is not None else _NOWHERE

    def imported(self, mod: _Module, name: str) -> str:
        """The module an absolute import of name in mod names, as the scan indexes it (deep_imports())."""
        if mod.rebase and (new := mod.rebase.get(name.partition(".")[0])):
            name = ".".join(filter(None, [new, name.partition(".")[2]]))
        return self.real(name)

    def deep_imports(self) -> None:
        """An absolute import whose first name is no top-level module under the roots, nor the standard
        library or an installed package, is looked for as Python finds it on sys.path: first in the
        importing file's own directory (sys.path[0] for a script: ops/run.py's `from pay import sink` is
        ops/pay.py), then under any directory below a root (a deeper sys.path entry: `import fleet.store`
        is cli/fleet/store.py). A unique match aliases the import to that module; a name several modules
        match is unresolved/unknown-module: never dropped (an import resolved to nothing hides its calls).
        The whole dotted name must match (a module or package): `import mysql.connector` beside a project
        package named mysql that has no connector is the installed driver, as it is for Python."""
        tops = {n.split(".")[0] for n in self.files_named}
        tails: collections.defaultdict[str, set[str]] = collections.defaultdict(set)
        for n in {*self.modules, *self.excluded}:        # (not a skipped tree's: a virtualenv's packages)
            p = n.split(".")
            for i in range(1, len(p)):
                for j in range(i + 1, len(p) + 1):
                    tails[".".join(p[i:j])].add(".".join(p[:i]))
        known = {".".join(n.split(".")[:i]) for n in {*self.modules, *self.excluded} for i in range(1, n.count(".") + 2)}
        changed = False

        def fits(mod: _Module, node: ast.stmt, prefix: str, name: str) -> bool:
            """prefix.name can be what the import names: not the package the importing file is in (a
            module never imports its own package by a bare name: hypothesis/extra/django/_fields.py's
            `from django import forms` is the installed django), and holding every name a from-import
            takes from it (a submodule, or a name its source binds)."""
            pkg = f"{prefix}.{name.split('.')[0]}"
            if mod.name == pkg or mod.name.startswith(pkg + "."):
                return False
            target = f"{prefix}.{name}"
            if not isinstance(node, ast.ImportFrom) or target in self.excluded:
                return True
            bound = self._bound(target) if target in self.modules else set()
            return all(a.name == "*" or f"{target}.{a.name}" in known or bound is None or a.name in bound
                       for a in node.names)
        for mod in self.modules.values():
            own = mod.name if mod.file.name == "__init__.py" else mod.name.rpartition(".")[0]
            for node in _walk_of(mod.tree, *IMPORTS):
                if isinstance(node, ast.ImportFrom):
                    if node.level or not node.module:
                        continue
                    names = [node.module]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                else:
                    continue
                for name in names:
                    name = self.real(name)
                    head = name.split(".")[0]
                    if head in tops or head in mod.rebase or head in sys.stdlib_module_names or head not in tails:
                        continue
                    if own and f"{own}.{name}" in known and fits(mod, node, own, name):
                        mod.rebase[head] = f"{own}.{head}"     # the script's own directory: sys.path[0]
                        changed = True
                        continue
                    if self.installed_portion(head):
                        continue                         # an installed package of that name: third-party
                    found = {p for p in tails.get(name, ()) if fits(mod, node, p, name)}
                    if not found:
                        continue                         # (`mysql.connector` beside a package named mysql)
                    if len(found) == 1:
                        mod.rebase[head] = f"{next(iter(found))}.{head}"
                        changed = True
                        continue
                    shown = ", ".join(sorted(f"{p}.{name}" for p in found)[:4])
                    self.problems.append((mod.root, mod.file, node.lineno, "unresolved/unknown-module",
                                          f"imports {name}, which is no top-level module under the source roots "
                                          f"but which several modules below a root may be ({shown}): which one "
                                          f"Python imports depends on sys.path, so calls through it cannot be "
                                          f"traced; import it by its full name"))
            if mod.rebase:
                for into in (mod.aliases, mod.nested):
                    for k, v in into.items():
                        if v.partition(".")[0] in mod.rebase:
                            into[k] = self.imported(mod, v)
                mod.star[:] = [self.imported(mod, s) if s.partition(".")[0] in mod.rebase else s for s in mod.star]
        if changed:
            self._bases.clear()
            self._methods.clear()
            self._mros.clear()
            self._class_names = None

    def _relative(self, mod: _Module, node: ast.ImportFrom) -> str | None:
        if not node.level:
            return node.module or ""
        package = self.long_package.get(mod.name, mod.package)
        base = package.split(".") if package else []
        if node.level - 1 > len(base):
            return None                                  # above the top of every root
        base = base[: len(base) - (node.level - 1)]
        return ".".join([*base, *([node.module] if node.module else [])])

    def unresolved_imports(self) -> None:
        """A relative import in a scanned module that names no module under the roots (above the top of
        every root, or a file that is not there) is a problem: its names would silently resolve to nothing.
        So is any import of a module a scanned package does not hold (`import pkg.admin` with no
        pkg/admin under the roots): it is not third-party code, and what it runs is not read."""
        known = {*self.modules, *self.excluded, *self.no_source}
        heads = {".".join(m.split(".")[:i]) for m in known for i in range(1, m.count(".") + 1)}

        def found(name: str) -> bool:
            r = self.real(name)
            return r in known or r in heads
        for mod in self.modules.values():
            for node in _walk_of(mod.tree, *IMPORTS):
                if isinstance(node, ast.Import):
                    self._unknown_at(mod, node, next(filter(None, (self._unknown(self.imported(mod, a.name))
                                                                   for a in node.names)), None))
                if not isinstance(node, ast.ImportFrom):
                    continue
                if not node.level:
                    self._unknown_at(mod, node, self._unknown_from(self._absolute(mod, node), node))
                    continue
                got = self._relative(mod, node)
                text = "." * node.level + (node.module or "")
                if got is None:
                    why = f"from {text} import ... climbs above the top of every source root"
                elif not (got and found(got)) and not any(
                        found(f"{got}.{a.name}" if got else a.name) for a in node.names if a.name != "*"):
                    why = f"from {text} import ... names no module under the source roots ({self._shown(mod, node, got)})"
                else:
                    self._unknown_at(mod, node, self._unknown_from(self.real(got), node) if got else None)
                    continue
                self.problems.append((mod.root, mod.file, node.lineno, "unresolved/relative-import",
                                      f"{why}: the names it imports are not followed, so calls through them "
                                      f"cannot be traced; fix the import or add the missing root"))

    def _shown(self, mod: _Module, node: ast.ImportFrom, got: str) -> str:
        """The module a relative import names, as the scan names the importing file's package (fix round 28:
        `google.adk.platform.internal`, not the deeper root's `src.google.adk.platform.internal`)."""
        base = mod.package.split(".") if mod.package else []
        if node.level - 1 > len(base):
            return got or "."
        base = base[: len(base) - (node.level - 1)]
        return ".".join([*base, *([node.module] if node.module else [])]) or "."

    def _unknown(self, name: str) -> str | None:
        """The first module along a dotted name that lies in a package under the roots but that no file
        under the roots is (`pkg.admin` in `pkg.admin.tasks`), else None."""
        parts = self.real(name).split(".")
        for i in range(1, len(parts)):
            parent, child = ".".join(parts[:i]), ".".join(parts[:i + 1])
            if parent not in self.package_names:
                return None
            if child not in self.files_named:
                if parent not in self.module_files and self.installed_portion(child):
                    return None                          # a namespace package's portion installed elsewhere
                return child
        return None

    def installed_portion(self, name: str) -> bool:
        """name, a submodule of a namespace package under the roots (one with no __init__.py), is a module
        installed outside the roots: a file or directory for it on sys.path that lies outside every root,
        or in a site-packages or dist-packages directory (`google.genai` next to an app's own `google.adk`).
        It is third-party code, like any other installed import. Read from the file system only: nothing
        is imported. A file for it under a root that the scan did not read (a dangling symlink, an
        excluded tree) is not installed, so it stays unknown."""
        got = self._installed.get(name)
        if got is None:
            got = self._installed[name] = any(self._portion_at(pathlib.Path(e or "."), name.split("."))
                                              for e in sys.path)
        return got

    def _portion_at(self, entry: pathlib.Path, parts: list[str]) -> bool:
        base = entry.joinpath(*parts)
        try:
            hits = [base] if base.is_dir() else []
            hits += [base.with_name(base.name + sfx) for sfx in (".py", *importlib.machinery.EXTENSION_SUFFIXES)
                     if base.with_name(base.name + sfx).is_file()]
        except OSError:
            return False
        for hit in hits:
            real = hit.resolve()
            if not any(real.is_relative_to(r) for r in self.roots) or \
                    any(d in ("site-packages", "dist-packages") for d in real.parts):
                return True
        return False

    def _unknown_from(self, src: str, node: ast.ImportFrom) -> str | None:
        """_unknown() for `from src import a, b`: src itself, or a name that is neither a module under the
        roots nor bound in src's own `__init__.py` (a namespace package binds nothing)."""
        hit = self._unknown(src) if src else None
        if hit or src not in self.package_names:
            return hit
        if src in self.module_files and src not in self.modules:
            return None                                  # an excluded or unreadable package: its names are unknown
        bound = self._bound(src) if src in self.modules else set()
        if bound is None:
            return None
        return next((f"{src}.{a.name}" for a in node.names if a.name != "*"
                     and f"{src}.{a.name}" not in self.files_named and a.name not in bound
                     and not (src not in self.module_files and self.installed_portion(f"{src}.{a.name}"))), None)

    def _bound(self, module: str) -> set[str] | None:
        """Every name a scanned module binds anywhere (an over-approximation), or None when it may bind
        names the scan cannot list (a star import, a module __getattr__, a globals() write)."""
        mod = self.modules[module]
        if mod.star:
            return None
        out: set[str] = set()
        for n in _walk_of(mod.tree, ast.Name, *DEFS, *IMPORTS, ast.Global, ast.Nonlocal):
            if isinstance(n, ast.Name):
                if n.id in ("globals", "vars", "setattr", "exec") and isinstance(n.ctx, ast.Load):
                    return None
                out.add(n.id)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(n.name)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                out.update((a.asname or a.name).split(".")[0] for a in n.names)
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                out.update(n.names)
        return None if "__getattr__" in out else out

    def _unknown_at(self, mod: _Module, node: ast.stmt, name: str | None) -> None:
        if name is None:
            return
        parent = name.rpartition(".")[0]
        self.problems.append((mod.root, mod.file, node.lineno, "unresolved/unknown-module",
                              f"imports {name}, but no file under the source roots is {name} although {parent} "
                              f"is a package there (a symlink to nothing, a file outside the roots, a module "
                              f"made at build time): what it runs is not read, so calls through it cannot be "
                              f"traced; add its source to the roots or fix the import"))

    def import_path_writes(self) -> None:
        """Scanned code that changes where Python finds modules (sys.path, sys.meta_path, sys.path_hooks,
        sys.path_importer_cache, importlib.invalidate_caches, site.addsitedir, zipimport), directly or
        through a name bound to one of them (`p = sys.path; p.insert(0, d)`), is a problem: an import
        after it may load code from outside the roots, such as a zip archive. So is a write of a
        package's `__path__` (`__path__.append(d)`, `__path__ = extend_path(__path__, __name__)`,
        `pkg.__path__ += [d]`, pkgutil.extend_path): its submodules may then come from any directory."""
        for mod in self.modules.values():
            names = _state_aliases(mod)
            seen: set[int] = set()                     # one __path__ finding per line

            def full(node: ast.AST) -> str:
                f = _full(mod, node) or ""
                head, _, rest = f.partition(".")
                return ".".join(filter(None, [names[head], rest])) if head in names else f
            for node in _walk_of(mod.tree, ast.Call, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete):
                what, line, package = None, getattr(node, "lineno", 1), False
                if isinstance(node, ast.Call):
                    f = full(node.func)
                    owner, _, attr = f.rpartition(".")
                    if f in _IMPORT_STATE_CALLS or (owner in _IMPORT_STATE and attr not in _READS):
                        what = f"{f}()"
                    elif f in _PACKAGE_PATH_CALLS or (_package_path(owner) and attr not in _READS):
                        what, package = f"{f}()", True
                elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
                    targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
                    for t in targets:
                        base = t.value if isinstance(t, ast.Subscript) else t
                        f = t.id if isinstance(t, ast.Name) else full(base)      # p = sys.path binds p only
                        if _package_path(f):
                            what, package = f"a write of {f}", True
                        elif not isinstance(t, ast.Name) and f in _IMPORT_STATE:
                            what = f"a write of {f}"
                if what and package and line in seen:
                    continue
                if what and package:
                    seen.add(line)
                    self.problems.append((mod.root, mod.file, line, "unresolved/reflection",
                                          f"{what} changes a package's __path__, so its submodules may come from "
                                          f"any directory: an import of one may load code from outside the "
                                          f"scanned roots, which the scan does not read; keep the package's "
                                          f"modules in the roots"))
                elif what:
                    self.problems.append((mod.root, mod.file, line, "unresolved/reflection",
                                          f"{what} changes where Python finds modules: an import after it may "
                                          f"load code from outside the scanned roots (a zip archive, another "
                                          f"directory), which the scan does not read; import from the roots"))

    def _defs(self, mod: _Module, body: list[ast.stmt], prefix: str, cls: str | None) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fid = f"{prefix}.{node.name}"
                self.funcs[fid] = _Func(fid, mod.name, mod.file, mod.root, node.lineno, node.col_offset, node, cls)
                if cls is None:
                    mod.aliases.setdefault(node.name, fid)
                    self.module_funcs_by_name[node.name].add(fid)
                else:
                    self.classes[cls]["methods"][node.name] = fid
                    self.methods_by_name[node.name].add(fid)
                    self.owners_by_name[node.name].add(cls)
                    self._class_events[cls].append(("def", node.name, fid))
                    if node.args.args:                 # how the first parameter is used: cls or self
                        decos = {_dotted(d) for d in node.decorator_list}
                        kind = "cm" if decos & {"classmethod", "builtins.classmethod"} else \
                            "static" if decos & {"staticmethod", "builtins.staticmethod"} else "plain"
                        self.first_params[(node.args.args[0].arg, cls)].add(kind)
            elif isinstance(node, ast.ClassDef):
                cid = f"{prefix}.{node.name}"
                self.classes[cid] = {"methods": {}, "bases": node.bases, "module": mod.name, "node": node}
                self._class_events[cid] = []
                if cls is None:
                    mod.aliases.setdefault(node.name, cid)
                else:
                    self._class_events[cls].append(("other", node.name, ""))
                self._defs(mod, node.body, prefix=cid, cls=cid)
            elif cls is not None:
                self._class_binding(cls, node)

    def _class_binding(self, cls: str, node: ast.stmt) -> None:
        """A class-body statement other than a def or class: `name = <a function>` (a name or a dotted name)
        is a method alias, linked once every module is read; any other binding of a name is a class-level
        value."""
        events = self._class_events[cls]
        if isinstance(node, ast.AnnAssign) and node.value is None:
            return                                     # `x: int` declares, binds nothing
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        d = _dotted(value) if value is not None else None
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        named = {t.id for t in targets if isinstance(t, ast.Name)} if d else set()
        for n in _class_scope_nodes(node):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)) and n.id not in named:
                events.append(("other", n.id, ""))
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                events.extend(("other", (a.asname or a.name).split(".")[0], "") for a in n.names)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not node:
                events.append(("other", n.name, ""))
        if d:
            events.extend(("alias", name, d) for name in sorted(named))

    def link(self) -> None:
        """Make each class-body alias of a function (`post = record`, `__iadd__ = record`, `run = helper`) a
        method of its class under the alias, once every module is read: `Ledger().post(x)` calls record. A
        name is looked up in the class body first (the names bound above it), then in the module."""
        if self._linked:
            return
        self._linked = True
        for cls, events in self._class_events.items():
            info = self.classes.get(cls)
            if info is None:
                continue
            local: dict[str, str | None] = {}
            for kind, name, what in events:
                if kind != "alias":
                    local[name] = what or None
                    continue
                target = self.alias_target(info["module"], what, local)
                local[name] = target
                if target is not None:
                    info["methods"][name] = target
                    self.methods_by_name[name].add(target)
                    self.owners_by_name[name].add(cls)
        self._methods.clear()
        self._fans.clear()
        self._short = None
        self._ctors.clear()
        self._members.clear()

    def alias_target(self, module: str, dotted: str, local: dict[str, str | None]) -> str | None:
        """The scanned function a dotted name in a class body names: through the names bound above it in the
        body (local), else through the module; None for anything else."""
        head = dotted.partition(".")[0]
        if head in local:
            return local[head] if head == dotted else None
        target = self._name_in(module, dotted)
        return target if target in self.funcs and self.funcs[target].node is not None else None

    def constructors(self, cls: str) -> tuple[str, ...]:
        """The scanned functions that run when cls is called: its `__init__`, `__new__` and `__post_init__`
        (inherited ones too), and its metaclass's `__call__`."""
        got = self._ctors.get(cls)
        if got is None:
            self.link()
            out = [m for m in (self.method(cls, n) for n in ("__init__", "__new__", "__post_init__")) if m]
            for c in self.mro(cls) or [cls]:
                node = self.classes[c].get("node") if c in self.classes else None
                for kw in getattr(node, "keywords", []):
                    d = _dotted(kw.value) if kw.arg == "metaclass" else None
                    meta = self._follow(self._name_in(self.classes[c]["module"], d)) if d else None
                    if meta in self.classes and (call := self.method(meta, "__call__")):
                        out.append(call)
            got = self._ctors[cls] = tuple(sorted(set(out)))
        return got

    def all_constructors(self) -> tuple[str, ...]:
        """The constructors of every scanned class: what building an object of a class the scan cannot type
        (`type(x)(...)`) may run."""
        got = self._members.get("<every class>")
        if got is None:
            got = self._members["<every class>"] = tuple(sorted({c for k in self.classes for c in self.constructors(k)}))
        return got

    def module_members(self, module: str) -> tuple[str, ...]:
        """What a scanned module used as a value can call: its functions (and its submodules'), the
        constructors of its classes, and what it re-exports (`from app.gateway import send_to_gateway`)."""
        got = self._members.get(module)
        if got is None:
            self.link()
            out: set[str] = set()
            seen: set[str] = set()
            todo = [module]
            while todo:
                m = todo.pop()
                if m in seen or m not in self.modules:
                    continue
                seen.add(m)
                todo += [k for k in self.modules if k.startswith(m + ".")]
                out.update(fid for fid, f in self.funcs.items()
                           if f.module == m and f.cls is None and f.node is not None)
                for cid, info in self.classes.items():
                    if info["module"] == m:
                        out.update(self.constructors(cid))
                for target in self.modules[m].aliases.values():
                    hit = self.canon(target)
                    if hit in self.funcs and self.funcs[hit].node is not None:
                        out.add(hit)
                    elif hit in self.classes:
                        out.update(self.constructors(hit))
            got = self._members[module] = tuple(sorted(out))
        return got

    # canonical names ---------------------------------------------------------------------------
    def canon(self, fq: str, depth: int = 0) -> str:
        """Follow re-exports and inheritance until fq names a known function, class or module."""
        if fq in self.funcs or fq in self.classes or fq in self.modules or depth > 12:
            return fq
        if self.module_alias and (r := self.real(fq)) != fq:
            return self.canon(r, depth + 1)
        parts = fq.split(".")
        for i in range(len(parts) - 1, 0, -1):
            head = ".".join(parts[:i])
            if head in self.classes:
                m = self.method(head, parts[i])
                if m:
                    return self.canon(".".join([m, *parts[i + 1:]]), depth + 1)
                return fq
            if head in self.modules:
                mod = self.modules[head]
                name = parts[i]
                target = mod.aliases.get(name) or self._star_lookup(mod, name, depth)
                if target and target != fq and target != ".".join(parts[: i + 1]):
                    return self.canon(".".join([target, *parts[i + 1:]]), depth + 1)
                return fq
        return fq

    def _star_lookup(self, mod: _Module, name: str, depth: int) -> str | None:
        for src in mod.star:
            if src in self.modules and depth < 12:
                hit = self.canon(f"{src}.{name}", depth + 1)
                if hit in self.funcs or hit in self.classes or hit in self.modules:
                    return hit
        return None

    def method(self, cls: str, name: str) -> str | None:
        """A method by name on cls or its project-local bases: the first along cls's method resolution order
        (C3, as Python looks it up; fix round 27: not depth first, so in `class D(B, C)` with B and C both
        subclasses of A, C's override comes before A's method that B inherits)."""
        self.link()
        key = (cls, name)
        if key not in self._methods:
            self._methods[key] = next((self.classes[c]["methods"][name] for c in (self.mro(cls) or [cls])
                                       if c in self.classes and name in self.classes[c]["methods"]), None)
        return self._methods[key]

    def bases(self, cls: str) -> list[str]:
        """The canonical names of a class's bases, as written (`Base[T]`, a generic base, is Base; a
        builtin such as `dict` is builtins.dict)."""
        return [n for n in self._base_names(cls) if n is not None]

    def _base_names(self, cls: str) -> list[str | None]:
        """bases(), with None for a base that is not a plain name (a call, `*bases`, a getattr())."""
        got = self._bases.get(cls)
        if got is None:
            got = self._bases[cls] = self._base_names_of(cls)
        return got

    def _base_names_of(self, cls: str) -> list[str | None]:
        info = self.classes[cls]
        out: list[str | None] = []
        for b in info["bases"]:
            d = _dotted(b.value if isinstance(b, ast.Subscript) else b)
            out.append(self._follow(self._name_in(info["module"], d)) if d else None)
        return out

    def _name_in(self, module: str, dotted: str) -> str:
        """The canonical name a dotted name written in a scanned module refers to."""
        mod = self.modules[module]
        head, _, rest = dotted.partition(".")
        here = f"{mod.name}.{head}"
        first = mod.aliases.get(head) or mod.nested.get(head) or (
            f"builtins.{head}" if head in _BUILTINS and here not in self.classes else here)
        return self.canon(".".join(filter(None, [first, rest])))

    def _follow(self, fq: str, depth: int = 0) -> str:
        """fq, or the class a module-level alias of it names (`B = SinkRepo`, bound once in its module)."""
        module, _, name = fq.rpartition(".")
        if fq in self.classes or module not in self.modules or depth > 8:
            return fq
        value = self.module_values(module).get(name)
        d = _dotted(value) if value is not None else None
        return self._follow(self._name_in(module, d), depth + 1) if d else fq

    def unseen_bases(self, cls: str) -> bool:
        """cls has a base that is neither a scanned class nor a builtin or standard-library class: a call,
        a getattr(), `*bases`, an unresolved name, a third-party or excluded class. The scan cannot place
        it in the method resolution order."""
        return any(b is None or (b not in self.classes and not b.startswith("builtins.")
                                 and b.split(".")[0] not in _KNOWN_BASE_MODULES)
                   for b in self._base_names(cls))

    def _excluded_module(self, name: str) -> tuple[ast.Module, dict[str, str]] | None:
        """An excluded module's tree and its import aliases, parsed once (never walked like scanned code)."""
        if name not in self._excluded_trees:
            file = self.excluded_files[name]
            try:
                tree = ast.parse(file.read_bytes(), filename=str(file))
            except (SyntaxError, ValueError, OSError):
                self._excluded_trees[name] = None
                return None
            package = name if file.name == "__init__.py" else name.rpartition(".")[0]
            mod = _Module(name, file, file.parent, tree, package)
            aliases: dict[str, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    aliases.update({a.asname or a.name.split(".")[0]: a.name if a.asname else a.name.split(".")[0]
                                    for a in node.names})
                elif isinstance(node, ast.ImportFrom):
                    src = self._absolute(mod, node)
                    aliases.update({a.asname or a.name: f"{src}.{a.name}" for a in node.names if a.name != "*"})
            self._excluded_trees[name] = (tree, aliases)
        return self._excluded_trees[name]

    def excluded_subclass(self, classes: set[str]) -> str | None:
        """A class in an excluded module (read for its class statements only) with a base in classes."""
        if self._excluded_bases is None:
            self._excluded_bases = []
            for name in self.excluded_files:
                got_mod = self._excluded_module(name)
                if got_mod is None:
                    continue
                tree, aliases = got_mod
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef):
                        got = set()
                        for b in node.bases:
                            d = _dotted(b.value if isinstance(b, ast.Subscript) else b)
                            if d:
                                head, _, rest = d.partition(".")
                                got.add(self._follow(self.canon(".".join(filter(None, [aliases.get(head, head), rest])))))
                        self._excluded_bases.append((f"{name}.{node.name}", got))
        return next((c for c, bs in self._excluded_bases if bs & classes), None)

    def import_effects(self, name: str) -> bool:
        """The excluded module runs code at import time beyond definitions: a decorated function or class
        (`@router.post`, `@app.get`) or a top-level call, other than constants, dataclass and typing
        definitions and logging setup. A module the scan cannot parse counts as running code."""
        if name not in self._effects:
            if name in self.no_source:                   # compiled code runs whatever it likes on import
                self._effects[name] = True
                return True
            got = self._excluded_module(name) if name in self.excluded_files else None
            self._effects[name] = (_runs_code(got[0].body, got[1]) if got is not None
                                   else name in self.excluded_files)
        return self._effects[name]

    def mro(self, cls: str, seen: frozenset[str] = frozenset()) -> list[str]:
        """The method resolution order of a class through the scanned classes (C3; bases the scan does
        not see are left out; depth first where C3 finds no consistent order)."""
        if seen:
            return self._c3(cls, seen, strict=False) or []
        if (cls, False) not in self._mros:
            self._mros[(cls, False)] = self._c3(cls, seen, strict=False)
        return self._mros[(cls, False)] or []

    def strict_mro(self, cls: str) -> list[str] | None:
        """mro(), or None when the scanned classes give no consistent C3 order (or inherit in a cycle)."""
        if (cls, True) not in self._mros:
            self._mros[(cls, True)] = self._c3(cls, frozenset(), strict=True)
        return self._mros[(cls, True)]

    def _c3(self, cls: str, seen: frozenset[str], strict: bool) -> list[str] | None:
        if cls not in self.classes:
            return []
        if cls in seen:
            return None if strict else []
        direct = [b for b in self.bases(cls) if b in self.classes]
        seqs: list[list[str]] = []
        for b in direct:
            got = self._c3(b, seen | {cls}, strict)
            if got is None:
                return None
            seqs.append(list(got))
        seqs.append(list(direct))
        out = [cls]
        while True:
            seqs = [q for q in seqs if q]
            if not seqs:
                return out
            head = next((q[0] for q in seqs if not any(q[0] in t[1:] for t in seqs)), None)
            if head is None:                                   # no consistent order
                return None if strict else out + [c for q in seqs for c in q if c not in out]
            out.append(head)
            for q in seqs:
                if q[0] == head:
                    del q[0]

    def subclasses(self, cls: str) -> list[str]:
        """Every scanned class that inherits cls, directly or not."""
        if self._subs is None:
            direct: collections.defaultdict[str, set[str]] = collections.defaultdict(set)
            for c in self.classes:
                for b in self.bases(c):
                    direct[b].add(c)
            self._subs = {}
            for c in self.classes:
                seen: set[str] = set()
                stack = list(direct[c])
                while stack:
                    n = stack.pop()
                    if n not in seen and n != c:
                        seen.add(n)
                        stack.extend(direct[n])
                self._subs[c] = sorted(seen)
        return self._subs.get(cls, [])

    def module_values(self, module: str) -> dict[str, ast.expr]:
        """Module-level `NAME = value` (or `type NAME = value`) bound once in the module: type aliases
        such as `RepoDep = Annotated[Repo, Depends(get_repo)]`."""
        if module in self._values:
            return self._values[module]
        tree = self.modules[module].tree
        found: dict[str, ast.expr] = {}
        for st in tree.body:
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                found[st.targets[0].id] = st.value
            elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
                found[st.target.id] = st.value
            elif _TYPE_ALIAS is not None and isinstance(st, _TYPE_ALIAS) and isinstance(st.name, ast.Name):
                found[st.name.id] = st.value
        binds: collections.Counter[str] = collections.Counter()
        for n in _walk_of(tree, ast.Name, *DEFS, ast.Global, ast.Nonlocal, *IMPORTS):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                binds[n.id] += 1
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                binds[n.name] += 1
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                binds.update({x: 2 for x in n.names})
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                binds.update((a.asname or a.name).split(".")[0] for a in n.names)
        self._values[module] = out = {k: v for k, v in found.items() if binds[k] == 1}
        return out


_NESTED_SCOPES = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.FunctionDef,
                  ast.AsyncFunctionDef, ast.ClassDef)


def _class_scope_nodes(node: ast.AST) -> list[ast.AST]:
    """node and the nodes under it that run in the class body's own scope: not inside a lambda, a
    comprehension or a nested def or class (whose names are not the class's)."""
    out: list[ast.AST] = []
    stack = [node]
    while stack:
        n = stack.pop()
        out.append(n)
        if n is node or not isinstance(n, _NESTED_SCOPES):
            stack.extend(ast.iter_child_nodes(n))
    return out


def _package_path(f: str) -> bool:
    """f is a package's __path__ (`__path__` in its __init__, or `pkg.__path__`)."""
    return f == "__path__" or f.endswith(".__path__")


def _state_aliases(mod: _Module) -> dict[str, str]:
    """Names mod binds to sys.path (or another of the import state lists) or a __path__: `p = sys.path`,
    anywhere in the module (an over-approximation: a name bound so once counts everywhere)."""
    out: dict[str, str] = {}
    binds: list[tuple[ast.Name, str]] = []             # (name, the dotted value it is bound to)
    for n in _walk_of(mod.tree, ast.Assign, ast.AnnAssign, ast.NamedExpr):
        value = getattr(n, "value", None)
        if not isinstance(value, (ast.Name, ast.Attribute)) or not (dotted := _full(mod, value)):
            continue
        targets = n.targets if isinstance(n, ast.Assign) else [getattr(n, "target", None)]
        binds += [(t, dotted) for t in targets if isinstance(t, ast.Name)]
    for _ in range(3):                                 # p = sys.path; q = p
        for t, f in binds:
            head, _, rest = f.partition(".")
            f = ".".join(filter(None, [out[head], rest])) if head in out else f
            if (f in _IMPORT_STATE or _package_path(f)) and t.id != f:
                out.setdefault(t.id, f)
    return out


def _full(mod: _Module, node: ast.AST) -> str | None:
    """A dotted name as written in mod, its first part replaced by what mod imports under it."""
    d = _dotted(node)
    if d is None:
        return None
    head, _, rest = d.partition(".")
    return ".".join(filter(None, [mod.aliases.get(head) or mod.nested.get(head, head), rest]))


# calls and decorators that only define things at import time (constants, dataclasses, typing, logging)
_DEFINING_MODULES = frozenset({"typing", "typing_extensions", "dataclasses", "logging", "enum", "re",
                               "collections", "functools", "abc", "contextlib"})
_DEFINING_NAMES = frozenset({"frozenset", "set", "dict", "list", "tuple", "str", "int", "float", "bool",
                             "bytes", "object", "property", "staticmethod", "classmethod", "len", "range"})


def _defines_only(node: ast.AST | None, aliases: dict[str, str]) -> bool:
    """Every call in node (a decorator, an assigned value) only defines something."""
    if node is None:
        return True
    for n in ast.walk(node):
        f = n.func if isinstance(n, ast.Call) else n if n is node and _dotted(n) else None
        if f is None:
            continue
        d = _dotted(f)
        if d is None:
            return False
        head, _, rest = d.partition(".")
        full = ".".join(filter(None, [aliases.get(head, head), rest]))
        if not (full.split(".")[0] in _DEFINING_MODULES or full in _DEFINING_NAMES
                or full.endswith(".getLogger")):
            return False
    return True


def _runs_code(body: list[ast.stmt], aliases: dict[str, str]) -> bool:
    """A module body (or a top-level if/try branch of one) runs code beyond definitions when imported."""
    for st in body:
        if isinstance(st, (ast.Import, ast.ImportFrom, ast.Pass, ast.Global)):
            continue
        if isinstance(st, ast.If):
            t = st.test                                      # `if __name__ == "__main__":` runs only as a script
            if isinstance(t, ast.Compare) and _dotted(t.left) == "__name__":
                continue
            if _runs_code(st.body, aliases) or _runs_code(st.orelse, aliases) or not _defines_only(t, aliases):
                return True
        elif isinstance(st, ast.Try):
            if any(_runs_code(b, aliases) for b in (st.body, st.orelse, st.finalbody,
                                                    *[h.body for h in st.handlers])):
                return True
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # a decorator runs at import (`@router.post` registers a route); class bodies are not read
            if not all(_defines_only(d, aliases) for d in st.decorator_list):
                return True
        elif isinstance(st, (ast.Assign, ast.AnnAssign)):
            if not _defines_only(st.value, aliases):
                return True
        elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
            continue                                         # a docstring
        elif _TYPE_ALIAS is not None and isinstance(st, _TYPE_ALIAS):
            continue
        else:                                                # a call, a loop, a with block, ...
            return True
    return False
