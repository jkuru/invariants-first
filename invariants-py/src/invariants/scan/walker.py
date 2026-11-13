"""The second pass: calls and sites in one module, attributing nested code to its owner."""
from __future__ import annotations

import ast

from typing import Any, Callable

from .astutil import _HARMLESS_DUNDERS, _add_leaves, _docstrings, _dotted, _dunder, _fold_str, _locals, _params
from .contracts import DYNAMIC_IMPORT, REFLECTION, _Edge, _Module, _Site
from .indexer import _Index
from .inference import _Inference

# reflective builtins and helpers taken as a value (`g = getattr`): calls through the name hide a lookup
_REFLECTIVE = frozenset({*(f"builtins.{n}" for n in ("getattr", "setattr", "delattr", *REFLECTION)),
                         *DYNAMIC_IMPORT, "operator.attrgetter", "operator.methodcaller"})


class _Walker(_Inference):
    """Resolves calls and records sites in one module, attributing nested code to its owner."""

    def __init__(self, idx: _Index, mod: _Module) -> None:
        super().__init__(idx, mod)
        self.edges: list[_Edge] = []
        self.sites: list[_Site] = []
        self.doc_ids = _docstrings(mod.tree)
        self._called: set[int] = set()                        # ids of call targets: a call, not a value

    def run(self) -> None:
        self._body(self.mod.tree.body, f"{self.mod.name}.<module>", set(), None, self.mod.name)

    def _body(self, body: list[ast.stmt], owner: str, local: set[str], self_cls: tuple[str, str] | None,
              prefix: str) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and f"{prefix}.{stmt.name}" in self.idx.funcs \
                    and owner.endswith(".<module>"):
                fid = f"{prefix}.{stmt.name}"
                for d in [*stmt.decorator_list, *stmt.args.defaults, *[k for k in stmt.args.kw_defaults if k]]:
                    self._expr(d, owner, local, self_cls)
                func = self.idx.funcs[fid]
                me = None
                if func.cls and stmt.args.args and not any(_dotted(d) == "staticmethod" for d in stmt.decorator_list):
                    me = (stmt.args.args[0].arg, func.cls)
                for s in stmt.body:
                    self._node(s, fid, _locals(stmt), me)
            elif isinstance(stmt, ast.ClassDef) and owner.endswith(".<module>") \
                    and f"{prefix}.{stmt.name}" in self.idx.classes:
                for d in [*stmt.decorator_list, *stmt.bases, *[k.value for k in stmt.keywords]]:
                    self._expr(d, owner, local, self_cls)
                self._body(stmt.body, owner, local, self_cls, f"{prefix}.{stmt.name}")
            else:
                self._node(stmt, owner, local, self_cls)

    def _node(self, node: ast.AST, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        if isinstance(node, ast.expr):
            self._expr(node, owner, local, me)
            return
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            src = self.idx._absolute(self.mod, node)
            if ex := self.idx.excluded_module(src):           # its names are unknown to the scan
                self._site("excluded", owner, node, f"{src}|{ex}|star")
        elif isinstance(node, (ast.Import, ast.ImportFrom)) and owner.endswith(".<module>"):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                src = self.idx._absolute(self.mod, node)
                names = [src, *(f"{src}.{a.name}" for a in node.names)]
            # importing a.b.c runs a, a.b and a.b.c: one that is excluded and runs code is reported
            run = [p for n in names for p in (".".join(n.split(".")[:i]) for i in range(1, n.count(".") + 2))
                   if self.idx.excluded_module(p) == p and self.idx.import_effects(p)]
            if run:
                self._site("excluded", owner, node, f"{run[0]}|{run[0]}|import")
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute):
                    fq = self._resolve(t, local, me)
                    if fq:
                        self._site("patch", owner, t, fq)
        for child in ast.iter_child_nodes(node):
            if isinstance(node, ast.AnnAssign) and child is node.annotation:
                continue                                       # `h: Helper = x` names a type, runs nothing
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and child in node.body:
                self._shadowed(node.args, self._node, child, owner, local, me)
            else:
                self._node(child, owner, local, me)

    def _shadowed(self, args: ast.arguments, walk: Callable[..., None], node: ast.AST, *rest: Any) -> None:
        """Walk node, the body of a nested function or lambda, with its parameters shadowing the owner's names."""
        shadow, self._shadow = self._shadow, self._shadow | set(_params(args))
        try:
            walk(node, *rest)
        finally:
            self._shadow = shadow

    def _unwrapped(self, node: ast.AST, local: set[str], me: tuple[str, str] | None) -> str:
        """The detail of a `x.__wrapped__` site: "x as written|the scanned function x is" (empty when the
        scan cannot tell which function x is)."""
        fq = self._resolve(node, local, me) if _dotted(node) else None
        return f"{_dotted(node) or ''}|{fq if fq in self.idx.funcs else ''}"

    def _site(self, what: str, owner: str, node: ast.AST, detail: str = "") -> None:
        self.sites.append(_Site(what, owner, getattr(node, "lineno", 0), getattr(node, "col_offset", 0), detail))

    def _lookup(self, owner: str, node: ast.AST, ns: str, key: ast.AST | None, how: str) -> None:
        """A name looked up in the namespace of module or class `ns` (ns.__dict__[k], .get(k),
        __getattribute__(k)): a literal key is a getattr with that name, anything else reflection."""
        lit = key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None
        if lit is None:
            self._site("reflection", owner, node, f"{ns}.{how}")
            return
        self._site("getattr", owner, node, f"{ns}|{lit}")
        hit = self.idx.canon(f"{ns}.{lit}")
        if hit and hit in self.idx.funcs:
            self.edges.append(_Edge(owner, hit, getattr(node, "lineno", 0), getattr(node, "col_offset", 0),
                                    False, via_getattr=True))

    def _exprs(self, xs: list[ast.AST], owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        for x in xs:
            self._expr(x, owner, local, me)

    def _ns_dict(self, x: ast.AST, local: set[str], me: tuple[str, str] | None) -> tuple[str, list[ast.AST]] | None:
        """For `m.__dict__` on a scanned module or class m, or `type(obj).__dict__` /
        `obj.__class__.__dict__`: the namespace it is, and the expressions inside still to walk (obj)."""
        if not (isinstance(x, ast.Attribute) and x.attr == "__dict__"):
            return None
        v = x.value
        if isinstance(v, ast.Attribute) and v.attr == "__class__":
            return "type(...)", [v.value]
        if isinstance(v, ast.Call) and len(v.args) == 1 and _dotted(v.func) \
                and self._resolve(v.func, local, me) == "builtins.type":
            return "type(...)", [v.args[0]]
        ns = self._namespace(v, local, me)
        return (ns, []) if ns else None

    def _reflect(self, node: ast.AST, owner: str, local: set[str], me: tuple[str, str] | None) -> bool:
        """Namespace reflection on a scanned module or class: `m.__dict__` in any use,
        `m.__getattribute__(k)`, `object.__getattribute__(m, k)` (or any `X.__getattribute__(m, k)`)
        and `inspect.getmembers(m)`. Records the site and visits the rest; True when handled.
        A function's `__dict__` is its metadata and stays harmless."""
        if isinstance(node, ast.Compare) and all(isinstance(o, (ast.In, ast.NotIn)) for o in node.ops):
            rest = [c for c in node.comparators if not (_dotted(c) and self._resolve(c, local, me) == "sys.modules")]
            if len(rest) < len(node.comparators):             # `name in sys.modules` only asks
                self._exprs([node.left, *rest], owner, local, me)
                return True
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and \
                (d := self._ns_dict(node.value, local, me)):
            self._lookup(owner, node, d[0], node.slice, "__dict__[...]")
            self._exprs([*d[1], node.slice], owner, local, me)
            return True
        if isinstance(node, ast.Call):
            return self._reflect_call(node, owner, local, me)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and (d := self._ns_dict(node, local, me)):
            self._site("reflection", owner, node, f"{d[0]}.__dict__")      # vars(m) by another name
            self._exprs(d[1], owner, local, me)
            return True
        return False

    def _reflect_call(self, node: ast.Call, owner: str, local: set[str], me: tuple[str, str] | None) -> bool:
        """`m.__dict__.get(k)` (a lookup) or `.items()`, `.values()`, ... (reflection), the
        `__getattribute__` forms and `inspect.getmembers(m)`."""
        f, kw = node.func, [k.value for k in node.keywords]
        if isinstance(f, ast.Attribute) and (d := self._ns_dict(f.value, local, me)):
            if f.attr == "get":
                self._lookup(owner, node, d[0], node.args[0] if node.args else None, "__dict__.get(...)")
            else:                                                       # .items(), .values(), ...
                self._site("reflection", owner, node, f"{d[0]}.__dict__.{f.attr}()")
            self._exprs([*d[1], *node.args, *kw], owner, local, me)
            return True
        if isinstance(f, ast.Attribute) and f.attr == "__getattribute__" and self._getattribute(node, f, owner, local, me):
            return True
        fq = self._resolve(f, local, me) if _dotted(f) and node.args else None
        if fq in ("inspect.getmembers", "inspect.getmembers_static") and self._namespace(node.args[0], local, me):
            self._site("reflection", owner, node, fq)
            self._exprs([*node.args[1:], *kw], owner, local, me)
            return True
        return False

    def _getattribute(self, node: ast.Call, f: ast.Attribute, owner: str, local: set[str],
                      me: tuple[str, str] | None) -> bool:
        """`m.__getattribute__(k)` and `X.__getattribute__(m, k)` on a scanned module or class m."""
        kw = [k.value for k in node.keywords]
        ns = self._namespace(f.value, local, me)
        if ns:                                                          # m.__getattribute__(k)
            self._lookup(owner, node, ns, node.args[0] if node.args else None, "__getattribute__(...)")
            self._exprs([*node.args, *kw], owner, local, me)
            return True
        ns = self._namespace(node.args[0], local, me) if node.args else None
        if ns:                                                          # object.__getattribute__(m, k)
            self._lookup(owner, node, ns, node.args[1] if len(node.args) > 1 else None, "__getattribute__(...)")
            self._exprs([f.value, *node.args[1:], *kw], owner, local, me)
            return True
        return False

    def _expr(self, node: ast.AST, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        if self._reflect(node, owner, local, me):
            return
        if isinstance(node, ast.Call):
            self._call_expr(node, owner, local, me)
            return
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            self._load(node, owner, local, me)
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in self.doc_ids:
            self._site("sql", owner, node, node.value)
            return
        if isinstance(node, ast.JoinedStr):
            text = "".join(str(v.value) if isinstance(v, ast.Constant) else "{}" for v in node.values)
            self._site("sql", owner, node, text)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and self._folded(node, owner, local, me):
            return
        if isinstance(node, ast.Lambda):
            local = local | _locals(node)
            self._expr(node.args, owner, local, me)            # its defaults, in the enclosing scope
            self._shadowed(node.args, self._expr, node.body, owner, local, me)
            return
        for child in ast.iter_child_nodes(node):
            if not (isinstance(node, ast.JoinedStr) and isinstance(child, ast.Constant)):
                self._expr(child, owner, local, me)

    def _call_expr(self, node: ast.Call, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        """A call: its edge or site, then what it is called on and its arguments."""
        self._called.add(id(node.func))
        self._call(node, owner, local, me)
        if not _dotted(node.func):
            self._expr(node.func, owner, local, me)
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "__wrapped__":
            self._site("wrapped", owner, node.func, self._unwrapped(node.func.value, local, me))
        elif isinstance(node.func, ast.Attribute) and _dunder(node.func.attr) \
                and node.func.attr not in ("__call__", *_HARMLESS_DUNDERS):
            self._escapes(node.func.value, owner, local, me, node.func)           # f.__get__(o)(...): f
        self._exprs([*node.args, *[k.value for k in node.keywords]], owner, local, me)

    def _load(self, node: ast.Name | ast.Attribute, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        """A name or attribute read: an unwrap, a function used as a value, a reflective builtin taken as a
        value, sys.modules, or a reference into an excluded module."""
        if isinstance(node, ast.Attribute) and node.attr == "__wrapped__":
            self._site("wrapped", owner, node, self._unwrapped(node.value, local, me))
        elif isinstance(node, ast.Attribute) and _dunder(node.attr) and node.attr not in _HARMLESS_DUNDERS:
            self._escapes(node.value, owner, local, me, node)                 # f.__call__, f.__get__,
        fq = self._resolve(node, local, me) if _dotted(node) else None
        called = id(node) in self._called
        if fq in _REFLECTIVE and not called:                                  # g = getattr; g(obj, name)
            self._site("reflection", owner, node, f"{fq.rpartition('.')[2]} used as a value")
        elif fq == "sys.modules":                                             # sys.modules[name].Repo
            self._site("reflection", owner, node, "sys.modules")
        elif not called and fq and (ex := self.idx.excluded_module(fq)):
            self._site("excluded", owner, node, f"{fq}|{ex}|ref")             # cb = helpers.write
        elif not called:                                                      # cb = repo.save: escapes
            self._escapes(node, owner, local, me, node)
        if isinstance(node, ast.Attribute) and not _dotted(node):
            self._expr(node.value, owner, local, me)

    def _escapes(self, node: ast.AST, owner: str, local: set[str], me: tuple[str, str] | None,
                 at: ast.AST) -> None:
        """node is used as a value (at the site at): the scanned functions it names escape ("ref"); `recv.m`
        on a receiver the scan cannot type, with m the name of a scanned function, may be one ("attrref")."""
        refs = self._value_refs(node, owner, local, me)
        for ref in refs:
            self._site("ref", owner, at, ref)
        if refs or not isinstance(node, ast.Attribute) or node.attr not in self.idx.short_names():
            return
        fq = self._resolve(node, local, me) if _dotted(node) else None
        if (fq is None or self._via_function(node, local, me)) and self._typed(node, owner, local, me) == ([], None):
            self._site("attrref", owner, at, node.attr)

    def _folded(self, node: ast.BinOp, owner: str, local: set[str], me: tuple[str, str] | None) -> bool:
        """'INSERT INTO ' + 'leases ...' (or + name): one text, one site; the non-literal parts are walked."""
        folded = _fold_str(node)
        if folded is None:
            return False
        self._site("sql", owner, node, folded)
        for part in _add_leaves(node):
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                continue
            self._exprs([v for v in part.values if not isinstance(v, ast.Constant)]
                        if isinstance(part, ast.JoinedStr) else [part], owner, local, me)
        return True

    def _call(self, node: ast.Call, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        func = node.func
        while isinstance(func, ast.Attribute) and func.attr == "__call__":    # f.__call__(x) calls f
            func = func.value
        fq = self._resolve(func, local, me) if _dotted(func) else None
        if func is not node.func and not (fq and self._known_callee(fq)) and not (
                isinstance(func, ast.Attribute) and (fq is None or self._typed(func, owner, local, me)[0])):
            func, fq = node.func, None                 # not a known function: an ordinary attribute call
        if fq in ("builtins.getattr", "builtins.setattr", "builtins.delattr", "builtins.hasattr") and node.args:
            self._attr_builtin(node, fq, owner, local, me)
            return
        if fq and fq.startswith("builtins.") and fq.split(".", 1)[1] in REFLECTION:
            self._site("reflection", owner, node, fq.split(".", 1)[1])
            return
        if fq in DYNAMIC_IMPORT:
            self._site("dynimport", owner, node, fq)
            return
        if fq and fq.startswith("sys.modules."):                   # sys.modules.get(name).Repo
            self._site("reflection", owner, node, fq)
            return
        if fq in ("operator.attrgetter", "operator.methodcaller"):
            self._attr_lookup(node, fq, owner, local, me)
            return
        if fq and fq in self.idx.funcs:
            self.edges.append(_Edge(owner, fq, node.lineno, node.col_offset, False))
        elif fq and (ex := self.idx.excluded_module(fq)):
            self._site("excluded", owner, node, f"{fq}|{ex}|call")
        elif fq and fq in self.idx.classes:
            init = self.idx.method(fq, "__init__")
            if init:
                self.edges.append(_Edge(owner, init, node.lineno, node.col_offset, False))
        elif isinstance(func, ast.Attribute) and (fq is None or self._via_function(func, local, me)):
            hits, container = self._typed(func, owner, local, me)
            for m in hits:                              # the class's method, and a subclass's override
                self.edges.append(_Edge(owner, m, node.lineno, node.col_offset, False))
            if not hits and container is None:          # d.update() on a dict is the dict's own method
                self._site("attrcall", owner, func, func.attr)
                for m in sorted(self.idx.methods_by_name.get(func.attr, ())):
                    self.edges.append(_Edge(owner, m, node.lineno, node.col_offset, True))

    def _known_callee(self, fq: str) -> bool:
        """fq, called through `.__call__`, is still what a plain call to it is: a scanned function or class,
        a name in an excluded module, or a reflective builtin or helper (`getattr.__call__(o, "save")`)."""
        return fq in self.idx.funcs or fq in self.idx.classes or fq in _REFLECTIVE or fq == "builtins.hasattr" \
            or fq.startswith("sys.modules.") or self.idx.excluded_module(fq) is not None

    def _attr_builtin(self, node: ast.Call, fq: str, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        """getattr/setattr/delattr/hasattr(obj, name, ...): a site; a getattr with a literal name is an edge too
        (to the function it names, or to the methods a runtime value's attribute may be)."""
        name = node.args[1] if len(node.args) > 1 else None
        lit = name.value if isinstance(name, ast.Constant) and isinstance(name.value, str) else None
        obj = self._resolve(node.args[0], local, me)
        self._site(fq.split(".")[1], owner, node, f"{obj or '?'}|{lit if lit is not None else ''}")
        if lit is not None and obj and fq == "builtins.getattr":     # a literal name is an edge
            if obj in self.idx.funcs and lit == "__call__":          # getattr(f, "__call__") is f
                self.edges.append(_Edge(owner, obj, node.lineno, node.col_offset, False))
                return
            if obj in self.idx.funcs and _dunder(lit):              # __get__, __func__, ...: f as a value
                self._site("ref", owner, node, obj)
                return
            hit = self.idx.method(obj[5:], lit) if obj.startswith("self:") else self.idx.canon(f"{obj}.{lit}")
            if hit and hit in self.idx.funcs:
                self.edges.append(_Edge(owner, hit, node.lineno, node.col_offset, False, via_getattr=True))
        elif lit is not None and fq == "builtins.getattr" and not _dunder(lit):
            # getattr(repo, "save") on a runtime value: the methods it may name, as for repo.save
            # (typed: the class's method and its overrides; otherwise name-matched)
            hits, container = self._typed(ast.Attribute(node.args[0], lit, ast.Load()), owner, local, me)
            for m in hits:
                self.edges.append(_Edge(owner, m, node.lineno, node.col_offset, False, via_getattr=True))
            if not hits and container is None:
                for m in sorted(self.idx.methods_by_name.get(lit, ())):
                    self.edges.append(_Edge(owner, m, node.lineno, node.col_offset, True, via_getattr=True))

    def _attr_lookup(self, node: ast.Call, fq: str, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        """operator.attrgetter/methodcaller(...): a getattr site with every literal name (none: computed)."""
        # attrgetter takes any number of names, each maybe dotted ("repo.save"); methodcaller one. A
        # name that is not a literal is a computed lookup, never a silent pass
        names = node.args if fq == "operator.attrgetter" else node.args[:1]
        lits = [a.value for a in names if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        how = f"?{fq.rpartition('.')[2]}"
        if len(lits) < len(names) or not names:
            self._site("getattr", owner, node, f"{how}|")
        else:
            self._site("getattr", owner, node, f"{how}|{'|'.join(lits)}")
        for a in [*node.args, *[k.value for k in node.keywords]]:
            self._expr(a, owner, local, me)
