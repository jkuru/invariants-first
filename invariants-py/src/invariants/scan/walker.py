"""The second pass: calls and sites in one module, attributing nested code to its owner."""
from __future__ import annotations

import ast

from typing import Any, Callable

from .astutil import _HARMLESS_DUNDERS, _dotted, _dunder, _lit, _locals, _params
from .contracts import DYNAMIC_IMPORT, REFLECTION, _Edge, _Module, _Site
from .indexer import _Index
from .inference import _Inference

# calls that run code the scan cannot read or look a callable up from data (a pickle names its
# functions by module and name): unresolved/reflection
_RUNS_DATA = frozenset({"pickle.loads", "pickle.load", "pickle.Unpickler", "_pickle.loads", "_pickle.load",
                        "_pickle.Unpickler", "shelve.open", "logging.config.fileConfig", "logging.config.listen",
                        "runpy.run_path", "runpy.run_module", "importlib.util.spec_from_file_location",
                        "importlib.machinery.SourceFileLoader"})
# a dotted name in a string, looked up (`pydoc.locate("pkg.mod.f")`): a getattr of it
_NAME_LOOKUP = frozenset({"pydoc.locate", "pkgutil.resolve_name"})
# reflective builtins and helpers taken as a value (`g = getattr`): calls through the name hide a lookup
_REFLECTIVE = frozenset({*(f"builtins.{n}" for n in ("getattr", "setattr", "delattr", *REFLECTION)),
                         *DYNAMIC_IMPORT, "operator.attrgetter", "operator.methodcaller", *_RUNS_DATA,
                         *_NAME_LOOKUP, "logging.config.dictConfig", "inspect.unwrap", "inspect.getattr_static"})


class _Walker(_Inference):
    """Resolves calls and records sites in one module, attributing nested code to its owner."""

    def __init__(self, idx: _Index, mod: _Module) -> None:
        super().__init__(idx, mod)
        self.edges: list[_Edge] = []
        self.sites: list[_Site] = []
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

    def _unwrapped(self, node: ast.AST | None, local: set[str], me: tuple[str, str] | None, how: str = "") -> str:
        """The detail of a `x.__wrapped__` site: "x as written|the scanned function x is" (empty when the
        scan cannot tell which function x is), and how the guard is lifted when it is not `.__wrapped__`
        (`inspect.unwrap(x)`, `x.__closure__`, `x.__dict__["__wrapped__"]`)."""
        fq = self._resolve(node, local, me) if node is not None and _dotted(node) else None
        shown = (_dotted(node) or "") if node is not None else ""
        return f"{shown}|{fq if fq in self.idx.funcs else ''}" + (f"|{how.format(shown or 'an object')}" if how else "")

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

    def _ns_dict(self, x: ast.AST, local: set[str],
                 me: tuple[str, str] | None) -> tuple[str, str, list[ast.AST]] | None:
        """For `x.__dict__` (or `f.__globals__`): what kind of namespace it is, its name, and the expressions
        inside still to walk. "ns": a scanned module or class `m.__dict__`, a class reached through an
        object (`type(obj).__dict__`, `obj.__class__.__dict__`, `C.__mro__[i].__dict__`, `C.__base__`) or a
        scanned function's module (`f.__globals__`); "func": a scanned function's own `__dict__` (where
        `__wrapped__` is); "foreign": a module outside the scan (`builtins.__dict__`); "globals": the
        `__globals__` of a function the scan cannot name; "value": an object's instance `__dict__`."""
        if not (isinstance(x, ast.Attribute) and x.attr in ("__dict__", "__globals__")):
            return None
        v = x.value
        fq = self._resolve(v, local, me) if _dotted(v) else None
        if x.attr == "__globals__":
            return ("ns", self.idx.funcs[fq].module, []) if fq in self.idx.funcs else ("globals", "?", [v])
        if isinstance(v, ast.Attribute) and v.attr in ("__class__", "__base__"):
            return "ns", "type(...)", [v.value]
        if isinstance(v, ast.Subscript) and isinstance(v.value, ast.Attribute) \
                and v.value.attr in ("__mro__", "__bases__"):
            return "ns", "type(...)", [v.value.value, v.slice]
        if isinstance(v, ast.Call) and len(v.args) == 1 and _dotted(v.func) \
                and self._resolve(v.func, local, me) == "builtins.type":
            return "ns", "type(...)", [v.args[0]]
        ns = self._namespace(v, local, me)
        if ns:
            return "ns", ns, []
        if fq in self.idx.funcs:
            return "func", fq, []
        if fq is not None and self._outside(fq):
            return "foreign", fq, []
        return "value", "?", [] if _dotted(v) else [v]

    def _outside(self, fq: str) -> bool:
        """fq names a module or object outside the scanned code (builtins, the standard library, a package)."""
        return not fq.startswith("self:") and fq.split(".")[0] not in {m.split(".")[0] for m in self.idx.modules} \
            and self.idx.excluded_module(fq) is None

    def _dict_use(self, kind: str, ns: str, x: ast.Attribute, owner: str, node: ast.AST, key: ast.AST | None,
                  how: str, whole: bool, local: set[str], me: tuple[str, str] | None) -> None:
        """A use of the namespace _ns_dict found: a lookup of key (whole: the dict itself, `.items()`, ...)."""
        lit = None if whole else _lit(key)
        if kind == "ns":
            if whole:
                self._site("reflection", owner, node, f"{ns}.{how}")
            else:
                self._lookup(owner, node, ns, key, how)
        elif kind == "func":
            if lit is None or lit == "__wrapped__":       # the dict holds the function a guard wraps
                self._site("wrapped", owner, node,
                           self._unwrapped(x.value, local, me, "{}.__dict__ reaches __wrapped__, which"))
        elif kind == "foreign":
            if lit is None or f"{ns}.{lit}" in _REFLECTIVE or f"{ns}.{lit}" == "builtins.hasattr":
                self._site("reflection", owner, node, f"{ns}.{x.attr}")
        elif kind == "globals":
            self._site("reflection", owner, node, "__globals__")
        elif lit is not None:                             # obj.__dict__["save"]: a getattr of that name
            self._site("getattr", owner, node, f"?|{lit}")

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
                isinstance(node.value, ast.Attribute) and (d := self._ns_dict(node.value, local, me)):
            self._dict_use(d[0], d[1], node.value, owner, node, node.slice, f"{node.value.attr}[...]", False,
                           local, me)
            self._exprs([*d[2], node.slice], owner, local, me)
            return True
        if isinstance(node, ast.Call):
            return self._reflect_call(node, owner, local, me)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and (d := self._ns_dict(node, local, me)):
            self._dict_use(d[0], d[1], node, owner, node, None, node.attr, True, local, me)  # vars(m) by another name
            self._exprs(d[2], owner, local, me)
            return True
        return False

    def _reflect_call(self, node: ast.Call, owner: str, local: set[str], me: tuple[str, str] | None) -> bool:
        """`m.__dict__.get(k)` (a lookup) or `.items()`, `.values()`, ... (reflection), the
        `__getattribute__` forms and `inspect.getmembers(m)`."""
        f, kw = node.func, [k.value for k in node.keywords]
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute) \
                and (d := self._ns_dict(f.value, local, me)):
            if f.attr == "get":
                self._dict_use(d[0], d[1], f.value, owner, node, node.args[0] if node.args else None,
                               f"{f.value.attr}.get(...)", False, local, me)
            else:                                                       # .items(), .values(), ...
                self._dict_use(d[0], d[1], f.value, owner, node, None, f"{f.value.attr}.{f.attr}()", True, local, me)
            self._exprs([*d[2], *node.args, *kw], owner, local, me)
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
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            self._rebound(node, owner, local, me)
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
        elif isinstance(node, ast.Attribute) and node.attr == "__closure__":     # its cells hold what it wraps
            self._site("wrapped", owner, node,
                       self._unwrapped(node.value, local, me, "{}.__closure__ reaches the function it wraps, which"))
        elif isinstance(node, ast.Attribute) and node.attr == "__mro__" and not (
                _dotted(node.value) and (v := self._resolve(node.value, local, me)) and self._outside(v)):
            self._site("reflection", owner, node, "__mro__")                     # C.__mro__: its classes as values
        elif isinstance(node, ast.Name) and node.id == "__builtins__":
            self._site("reflection", owner, node, "__builtins__")
        if isinstance(node, ast.Attribute) and _dunder(node.attr) and node.attr not in _HARMLESS_DUNDERS \
                and node.attr != "__wrapped__" and not (node.attr == "__call__" and id(node) in self._called):
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

    def _rebound(self, node: ast.Attribute, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        """`x.m = ...`, `x.m += ...`, `del x.m`, `for x.m in ...`: a scanned name x.m is patched ("patch"); on
        an instance whose class the scan knows, the methods x.m may be are; on a receiver the scan cannot
        type, with m the name of a scanned function, it may be one ("attrpatch")."""
        fq = self._resolve(node, local, me, raw=True)                # what the assignment replaces
        if fq:
            self._site("patch", owner, node, fq)
            return
        if node.attr not in self.idx.short_names():
            return
        if isinstance(node.value, ast.Name) and me and node.value.id == me[0]:
            rcv: tuple[str, bool] | None = (me[1], False)     # self.m with no m on self's class
        else:
            rcv = self._receiver(node.value, owner, local, me)
        if rcv is None:
            self._site("attrpatch", owner, node, node.attr)
            return
        if rcv[0] not in self.idx.classes:                     # a builtin container's attribute
            return
        hit = self.idx.method(rcv[0], node.attr)
        for m in [*([hit] if hit else []), *([] if rcv[1] else self._subclasses_overriding(rcv[0], node.attr))]:
            self._site("patch", owner, node, m)                # repo.save = fake on a Repo instance

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
        if fq in _RUNS_DATA:
            self._site("reflection", owner, node, fq)
            return
        if fq in _NAME_LOOKUP:
            self._name_lookup(node, fq, owner)
            return
        if fq == "logging.config.dictConfig":
            self._dict_config(node, owner, local, me)
            return
        if fq == "inspect.unwrap":
            self._site("wrapped", owner, node, self._unwrapped(node.args[0] if node.args else None, local, me,
                                                               "inspect.unwrap({})"))
            return
        if fq == "inspect.getattr_static" and node.args:
            self._attr_builtin(node, "builtins.getattr", owner, local, me)
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
        if lit is not None and obj and f"{obj}.{lit}" in _REFLECTIVE:       # getattr(builtins, "getattr")
            self._site("reflection", owner, node, f"{lit} looked up by name")
        if lit is not None and obj and fq == "builtins.getattr":     # a literal name is an edge
            if obj in self.idx.funcs and lit == "__call__":          # getattr(f, "__call__") is f
                self.edges.append(_Edge(owner, obj, node.lineno, node.col_offset, False))
                if id(node) in self._called:                         # and called here: f is not a value
                    self._called.add(id(node.args[0]))
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

    def _name_lookup(self, node: ast.Call, fq: str, owner: str) -> None:
        """pydoc.locate("pkg.mod.f"), pkgutil.resolve_name("pkg.mod:f"): a getattr of the name the string
        ends in, on what the rest names (an edge when it is a scanned function); a computed string is
        reflection."""
        lit = _lit(node.args[0]) if node.args else None
        if lit is None:
            self._site("reflection", owner, node, fq)
            return
        hit = self.idx.canon(lit.replace(":", "."))
        obj, _, name = hit.rpartition(".")
        self._site("getattr", owner, node, f"{obj or '?'}|{name}")
        if hit in self.idx.funcs:
            self.edges.append(_Edge(owner, hit, node.lineno, node.col_offset, False, via_getattr=True))

    def _dict_config(self, node: ast.Call, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        """logging.config.dictConfig(config): it imports and calls the dotted names its strings give ("()"
        factories, "class" handlers, "ext://" objects). A config the scan can read (a literal, or a
        module-level name bound once to one) calls each scanned function or class it names: an edge; any
        other config is reflection."""
        texts = self._config_strings(node.args[0], local, me, 0) if node.args else None
        if texts is None:
            self._site("reflection", owner, node, "logging.config.dictConfig")
            return
        for t in texts:
            t = (t[6:] if t.startswith("ext://") else t).replace(":", ".")
            if not t or not all(p.isidentifier() for p in t.split(".")):
                continue
            hit = self.idx.canon(t)
            target = hit if hit in self.idx.funcs else \
                self.idx.method(hit, "__init__") if hit in self.idx.classes else None
            if target:
                self.edges.append(_Edge(owner, target, node.lineno, node.col_offset, False))
            elif ex := self.idx.excluded_module(hit):
                self._site("excluded", owner, node, f"{hit}|{ex}|call")

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
