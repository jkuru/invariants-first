"""The first pass: modules, functions, classes and aliases across every source root (never imported)."""
from __future__ import annotations

import ast
import builtins
import collections
import pathlib
import sys
from typing import Any

from .astutil import _dotted
from .contracts import _Func, _Module

_BUILTINS = frozenset(dir(builtins))
# top-level modules whose classes are known bases: a base from one of them is not project code
_KNOWN_BASE_MODULES = frozenset({*sys.stdlib_module_names, "typing_extensions"})
_TYPE_ALIAS = getattr(ast, "TypeAlias", None)            # `type X = ...` (Python 3.12+)


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
        self.excluded: dict[str, tuple[str, str]] = {}   # excluded module -> (root-relative file, its glob)
        self.excluded_files: dict[str, pathlib.Path] = {}  # excluded module -> its file (read only for classes)
        self._excluded_bases: list[tuple[str, set[str]]] | None = None   # (class, its base names), lazily
        self._excluded_trees: dict[str, tuple[ast.Module, dict[str, str]] | None] = {}
        self._effects: dict[str, bool] = {}               # excluded module -> runs code at import time
        # (first parameter name, class) -> how the methods naming it so use it: cm, plain, static
        self.first_params: collections.defaultdict[tuple[str, str], set[str]] = collections.defaultdict(set)

    def short_names(self) -> set[str]:
        """The last name of every scanned function (`save` for app.repo.Repo.save): what a sink may be named."""
        if self._short is None:
            self._short = {f.rpartition(".")[2] for f in self.funcs}
        return self._short

    def class_param(self, me: tuple[str, str] | None) -> bool:
        """me's name is the class itself (a @classmethod's `cls`), never an instance, in every method of
        its class that names its first parameter so: `cls()` builds an instance of it or of a subclass."""
        return me is not None and self.first_params.get(me) == {"cm"}

    def excluded_module(self, fq: str) -> str | None:
        """The excluded module fq names or lies in (a scanned module of the same name wins), else None."""
        parts = fq.split(".")
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
        mod = _Module(name, file, root, tree, package)
        self.modules[name] = mod
        self.funcs[f"{name}.<module>"] = _Func(f"{name}.<module>", name, file, root, 1, 0, None)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        mod.aliases[a.asname] = a.name
                    else:
                        top = a.name.split(".")[0]
                        mod.aliases[top] = top
            elif isinstance(node, ast.ImportFrom):
                src = self._absolute(mod, node)
                for a in node.names:
                    if a.name == "*":
                        mod.star.append(src)
                    else:
                        mod.aliases[a.asname or a.name] = f"{src}.{a.name}" if src else a.name
        self._defs(mod, tree.body, prefix=name, cls=None)

    @staticmethod
    def _absolute(mod: _Module, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        base = mod.package.split(".") if mod.package else []
        base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
        return ".".join([*base, *([node.module] if node.module else [])])

    def _defs(self, mod: _Module, body: list[ast.stmt], prefix: str, cls: str | None) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fid = f"{prefix}.{node.name}"
                self.funcs[fid] = _Func(fid, mod.name, mod.file, mod.root, node.lineno, node.col_offset, node, cls)
                if cls is None:
                    mod.aliases.setdefault(node.name, fid)
                else:
                    self.classes[cls]["methods"][node.name] = fid
                    self.methods_by_name[node.name].add(fid)
                    if node.args.args:                 # how the first parameter is used: cls or self
                        decos = {_dotted(d) for d in node.decorator_list}
                        kind = "cm" if decos & {"classmethod", "builtins.classmethod"} else \
                            "static" if decos & {"staticmethod", "builtins.staticmethod"} else "plain"
                        self.first_params[(node.args.args[0].arg, cls)].add(kind)
            elif isinstance(node, ast.ClassDef):
                cid = f"{prefix}.{node.name}"
                self.classes[cid] = {"methods": {}, "bases": node.bases, "module": mod.name, "node": node}
                if cls is None:
                    mod.aliases.setdefault(node.name, cid)
                self._defs(mod, node.body, prefix=cid, cls=cid)

    # canonical names ---------------------------------------------------------------------------
    def canon(self, fq: str, depth: int = 0) -> str:
        """Follow re-exports and inheritance until fq names a known function, class or module."""
        if fq in self.funcs or fq in self.classes or fq in self.modules or depth > 12:
            return fq
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

    def method(self, cls: str, name: str, seen: set[str] | None = None) -> str | None:
        """A method by name on cls or its project-local bases."""
        seen = seen or set()
        if cls in seen or cls not in self.classes:
            return None
        seen.add(cls)
        info = self.classes[cls]
        if name in info["methods"]:
            return info["methods"][name]
        for fq in self.bases(cls):
            hit = self.method(fq, name, seen)
            if hit:
                return hit
        return None

    def bases(self, cls: str) -> list[str]:
        """The canonical names of a class's bases, as written (`Base[T]`, a generic base, is Base; a
        builtin such as `dict` is builtins.dict)."""
        return [n for n in self._base_names(cls) if n is not None]

    def _base_names(self, cls: str) -> list[str | None]:
        """bases(), with None for a base that is not a plain name (a call, `*bases`, a getattr())."""
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
        first = mod.aliases.get(head) or (
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
            got = self._excluded_module(name) if name in self.excluded_files else None
            self._effects[name] = (_runs_code(got[0].body, got[1]) if got is not None
                                   else name in self.excluded_files)
        return self._effects[name]

    def mro(self, cls: str, seen: frozenset[str] = frozenset()) -> list[str]:
        """The method resolution order of a class through the scanned classes (C3; bases the scan does
        not see are left out; depth first where C3 finds no consistent order)."""
        return self._c3(cls, seen, strict=False) or []

    def strict_mro(self, cls: str) -> list[str] | None:
        """mro(), or None when the scanned classes give no consistent C3 order (or inherit in a cycle)."""
        return self._c3(cls, frozenset(), strict=True)

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
        for n in ast.walk(tree):
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
