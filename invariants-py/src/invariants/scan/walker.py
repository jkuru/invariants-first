"""The second pass: calls and sites in one module, attributing nested code to its owner."""
from __future__ import annotations

import ast
from typing import Any, Callable

from .astutil import (
    _HARMLESS_DUNDERS,
    _computed_dict_writes,
    _dotted,
    _dunder,
    _lit,
    _locals,
    _params,
)
from .contracts import DYNAMIC_IMPORT, REFLECTION, _Edge, _Fan, _Module, _Site
from .indexer import _class_scope_nodes, _Index
from .inference import _Inference, _rebinds

# fix round 26: the methods that run where their class is called (an edge from each call of the class)
_CONSTRUCTORS = frozenset({"__init__", "__new__", "__post_init__"})
# decorators that hand back what they decorate (or a wrapper that calls it) and keep it nowhere else
_KEEPS = frozenset({"builtins.property", "builtins.staticmethod", "builtins.classmethod", "functools.cached_property",
                    "functools.lru_cache", "functools.cache", "functools.singledispatch",
                    "functools.singledispatchmethod", "functools.total_ordering", "abc.abstractmethod",
                    "abc.abstractproperty", "abc.abstractclassmethod", "abc.abstractstaticmethod",
                    "contextlib.contextmanager", "contextlib.asynccontextmanager", "dataclasses.dataclass",
                    "enum.unique", *(f"{m}.{n}" for m in ("typing", "typing_extensions")
                                     for n in ("overload", "final", "override", "no_type_check",
                                               "runtime_checkable", "type_check_only"))})
_KEEPS_CALLED = frozenset({"functools.wraps", "functools.lru_cache", "dataclasses.dataclass", "enum.verify"})
_INV_GUARDS = frozenset({"guard", "model_call", "edge"})       # an inv guard wraps it (scan._guarded)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

# calls that run code the scan cannot read or look a callable up from data (a pickle names its
# functions by module and name): unresolved/reflection
_RUNS_DATA = frozenset({"pickle.loads", "pickle.load", "pickle.Unpickler", "_pickle.loads", "_pickle.load",
                        "_pickle.Unpickler", "shelve.open", "logging.config.fileConfig", "logging.config.listen",
                        "runpy.run_path", "runpy.run_module", "importlib.util.spec_from_file_location",
                        "importlib.machinery.SourceFileLoader",
                        # they run code given as a string, or hand back objects found by name or by memory
                        "string.Formatter", "code.InteractiveInterpreter", "code.InteractiveConsole",
                        "code.interact", "timeit.main", "doctest.run_docstring_examples", "doctest.testmod",
                        "doctest.testfile", "doctest.DocTestRunner", "doctest.DebugRunner", "doctest.DocTestSuite",
                        "doctest.DocFileSuite", "doctest.debug", "doctest.debug_src", "doctest.debug_script",
                        "trace.Trace", "gc.get_objects", "gc.get_referrers", "gc.get_referents",
                        "inspect.getmodule", "sys._getframe", "inspect.currentframe", "inspect.stack",
                        "inspect.trace", "inspect.getouterframes", "inspect.getinnerframes",
                        "sys._current_frames"})
# reads of a namespace that give its names only (strings, a count, a bool), never one of its values
_NAMES_ONLY = frozenset({"keys", "__len__", "__contains__"})
# a frame's namespaces: `frame.f_globals["name"]` looks a name up (any frame, from anywhere)
_FRAME_NAMESPACES = frozenset({"f_globals", "f_locals", "f_builtins"})
# timeit runs its stmt and setup as code when they are strings
_TIMEIT = frozenset({"timeit.timeit", "timeit.repeat", "timeit.Timer"})
# subprocess runners: one whose command runs Python (sys.executable, "python ...", a .py file) runs code
# the scan does not follow from this call
_RUNNERS = frozenset({"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call",
                      "subprocess.check_output", "subprocess.getoutput", "subprocess.getstatusoutput",
                      "os.system", "os.popen", "os.posix_spawn", "os.posix_spawnp",
                      "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
                      *(f"os.exec{x}" for x in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")),
                      *(f"os.spawn{x}" for x in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe"))})
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
        self.edges: list[_Edge | _Fan] = []
        self.sites: list[_Site] = []
        self._called: set[int] = set()                        # ids of call targets: a call, not a value
        self._type_uses: set[int] = set()                     # ids of names only tested or inherited (_type_use)

    def run(self) -> None:
        self._body(self.mod.tree.body, f"{self.mod.name}.<module>", set(), None, self.mod.name)
        self._dict_writes(self.mod.tree, f"{self.mod.name}.<module>", None, nested=False)

    def _dict_writes(self, fn: ast.AST, owner: str, me: tuple[str, str] | None, nested: bool) -> None:
        """Writes through `o.__dict__` (or a written alias of it) whose names the scan cannot read, on an
        object other than a method's own self (whose attributes then go untyped): like a setattr with a
        computed name, they may set any attribute, a sink's holder too."""
        for node, obj in _computed_dict_writes(fn, nested, self.mod.tree):
            if not (me and isinstance(obj, ast.Name) and obj.id == me[0]):
                self._site("setattr", owner, node, f"{_dotted(obj) or 'an object'}.__dict__|")

    def _body(self, body: list[ast.stmt], owner: str, local: set[str], self_cls: tuple[str, str] | None,
              prefix: str) -> None:
        scope = self._class_scope                      # the class body being walked (None: the module body)
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and f"{prefix}.{stmt.name}" in self.idx.funcs \
                    and owner.endswith(".<module>"):
                fid = f"{prefix}.{stmt.name}"
                for d in [*stmt.decorator_list, *stmt.args.defaults, *[k for k in stmt.args.kw_defaults if k]]:
                    self._expr(d, owner, local, self_cls)
                func = self.idx.funcs[fid]
                self._defined(stmt, fid, func.cls, owner, local, self_cls)
                me = None
                if func.cls and stmt.args.args and not any(_dotted(d) == "staticmethod" for d in stmt.decorator_list):
                    me = (stmt.args.args[0].arg, func.cls)
                names = _locals(stmt)                  # once per function, not per statement
                self._class_scope = None               # a method body does not see the class body's names
                try:
                    for s in stmt.body:
                        self._node(s, fid, names, me)
                finally:
                    self._class_scope = scope
                self._dict_writes(stmt, fid, me, nested=True)
                if scope is not None:
                    scope[stmt.name] = fid
            elif isinstance(stmt, ast.ClassDef) and owner.endswith(".<module>") \
                    and f"{prefix}.{stmt.name}" in self.idx.classes:
                cid = f"{prefix}.{stmt.name}"
                for b in stmt.bases:                   # a base is inherited, not called
                    self._type_use(b)
                for d in [*stmt.decorator_list, *stmt.bases, *[k.value for k in stmt.keywords]]:
                    self._expr(d, owner, local, self_cls)
                if not all(self._keeps_value(d, local, self_cls) for d in stmt.decorator_list):
                    for c in self.idx.constructors(cid):   # the decorator is handed the class: a value
                        self._own_ref(owner, stmt, c)
                self._class_scope = {}
                try:
                    self._body(stmt.body, owner, local, self_cls, cid)
                finally:
                    self._class_scope = scope
                if scope is not None:
                    scope[stmt.name] = cid
            elif scope is not None:
                self._class_stmt(stmt, owner, local, self_cls, prefix, scope)
            else:
                self._node(stmt, owner, local, self_cls)

    def _defined(self, stmt: ast.FunctionDef | ast.AsyncFunctionDef, fid: str, cls: str | None, owner: str,
                 local: set[str], me: tuple[str, str] | None) -> None:
        """A def of a scanned function: handed to a decorator that may keep it (a registry, a route, atexit), it
        is used as a value there; bound to a dunder name, Python calls it from syntax (`a + b`, `x[k]`, `with`,
        `len()`, a format, a module `__getattr__`), so it is an entrypoint: used as a value where it is
        defined. A constructor (`__init__`, `__new__`, `__post_init__`) runs where its class is called."""
        kept = not all(self._keeps_value(d, local, me) for d in stmt.decorator_list)
        if kept or (_dunder(stmt.name) and not (cls and stmt.name in _CONSTRUCTORS)):
            self._own_ref(owner, stmt, fid)

    def _own_ref(self, owner: str, stmt: ast.AST, fid: str) -> None:
        """fid used as a value by its own definition (its decorator, or a dunder name Python calls it by): an
        entrypoint, which an allowed back door already is (recv "own" says so)."""
        self._site("ref", owner, stmt, fid)
        self.sites[-1].recv = "own"

    def _class_stmt(self, stmt: ast.stmt, owner: str, local: set[str], me: tuple[str, str] | None, cls: str,
                    scope: dict[str, str | None]) -> None:
        """A class-body statement other than a scanned def or class: `name = <function>` aliases it as a method
        (a dunder alias makes it an entrypoint, as a dunder def does); an import binds a class attribute to a
        module, function or class, a value; any other binding is a class-level value."""
        target = None
        if isinstance(stmt, ast.Assign) and all(isinstance(t, ast.Name) for t in stmt.targets) \
                and (d := _dotted(stmt.value)) and not self._alternatives(stmt.value, local, me):
            target = self.idx.alias_target(self.mod.name, d, scope)
        if target is not None and target in self.idx.funcs and isinstance(stmt, ast.Assign):
            for name in [t.id for t in stmt.targets if isinstance(t, ast.Name)]:
                scope[name] = target
                if _dunder(name) and name not in _CONSTRUCTORS:
                    self._own_ref(owner, stmt, target)
            return
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            self._node(stmt, owner, local, me)
            for a in stmt.names:
                if a.name == "*":
                    continue
                full = a.name if isinstance(stmt, ast.Import) and a.asname else \
                    a.name.split(".")[0] if isinstance(stmt, ast.Import) else \
                    f"{self.idx._absolute(self.mod, stmt)}.{a.name}"
                hit = self.idx.canon(self.idx.real(full))
                for ref in self._held_by(hit):
                    self._held_ref(owner, stmt, ref, hit)
                scope[(a.asname or a.name).split(".")[0]] = None
            return
        self._node(stmt, owner, local, me)
        for n in _class_scope_nodes(stmt):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)) and not (
                    isinstance(stmt, ast.AnnAssign) and stmt.value is None):
                scope[n.id] = None
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                scope.update({(a.asname or a.name).split(".")[0]: None for a in n.names})
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not stmt:
                scope[n.name] = None

    def _held_ref(self, owner: str, at: ast.AST, ref: str, value: str) -> None:
        """ref used as a value because value (a module or class that holds it) is: recv says which."""
        self._site("ref", owner, at, ref)
        if value != ref:
            self.sites[-1].recv = f"{'module' if value in self.idx.modules else 'class'}:{value}"

    def _held_by(self, fq: str) -> list[str]:
        """What calling (or reading the functions of) a value naming fq can run: a scanned function, a scanned
        class's constructors, or a scanned module's functions and constructors."""
        if fq in self.idx.funcs and self.idx.funcs[fq].node is not None:
            return [fq]
        if fq in self.idx.classes:
            return list(self.idx.constructors(fq))
        if fq in self.idx.modules:
            return list(self.idx.module_members(fq))
        return []

    def _keeps_value(self, d: ast.expr, local: set[str], me: tuple[str, str] | None) -> bool:
        """The decorator d hands back what it decorates, or a wrapper around it, without keeping it anywhere
        else: a known standard-library one (property, staticmethod, functools.lru_cache, contextmanager, ...),
        an inv guard, or a scanned decorator whose parameter is only returned or called by a nested wrapper it
        returns (`@register` that appends it to a list keeps it). Anything else (a route `@app.post(...)`,
        `@atexit.register`, a third-party decorator) may keep it: the function is used as a value there."""
        f = d.func if isinstance(d, ast.Call) else d
        fq = self._resolve(f, local, me) if _dotted(f) else None
        if fq is None:
            return False
        if fq in self.idx.funcs:
            fn = self.idx.funcs[fq].node
            if not isinstance(fn, ast.FunctionDef):
                return False
            return _factory_passes(fn) if isinstance(d, ast.Call) else _passes_through(fn)
        if fq.split(".")[0] == "invariants" and fq.rpartition(".")[2] in _INV_GUARDS:
            return True
        return fq in (_KEEPS_CALLED if isinstance(d, ast.Call) else _KEEPS)

    def _type_use(self, node: ast.AST) -> None:
        """node names a type where it is only tested or inherited, never called (`isinstance(x, C)`, a base,
        `except C`, `case C()`, `x is C`, `dict[str, C]`): a class or module there is not used as a value."""
        if isinstance(node, (ast.Tuple, ast.List)):
            for e in node.elts:
                self._type_use(e)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            self._type_use(node.left)
            self._type_use(node.right)
        elif isinstance(node, ast.Subscript):
            self._type_use(node.value)
            self._type_use(node.slice)
        elif isinstance(node, (ast.Name, ast.Attribute)):
            self._type_uses.add(id(node))

    def _node(self, node: ast.AST, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        if isinstance(node, ast.expr):
            self._expr(node, owner, local, me)
            return
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            self._type_use(node.type)                          # `except C`: matched, not called
        elif isinstance(node, ast.MatchClass):
            self._type_use(node.cls)                           # `case C()`: matched, not called
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
                self._shadowed(node, node.args, self._node, child, owner, local, me)
            elif isinstance(node, ast.ClassDef) and child in node.body:
                self._shadowed(node, None, self._node, child, owner, local, me)
            else:
                self._node(child, owner, local, me)

    def _shadowed(self, scope: ast.AST, args: ast.arguments | None, walk: Callable[..., None], node: ast.AST,
                  *rest: Any) -> None:
        """Walk node, in the body of a nested function, lambda or class (scope), with its parameters
        shadowing the owner's names and its own bindings typing the names it binds."""
        shadow, self._shadow = self._shadow, self._shadow | set(_params(args) if args is not None else ())
        scopes, self._scopes = self._scopes, (*self._scopes, scope)
        try:
            walk(node, *rest)
        finally:
            self._shadow, self._scopes = shadow, scopes

    def _unwrapped(self, node: ast.AST | None, local: set[str], me: tuple[str, str] | None, how: str = "") -> str:
        """The detail of a `x.__wrapped__` site: "x as written|the scanned function x is" (empty when the
        scan cannot tell which function x is), how the guard is lifted when it is not `.__wrapped__`
        (`inspect.unwrap(x)`, `x.__closure__`, `x.__dict__["__wrapped__"]`), and "outside" when x names
        an object outside the scanned code (the standard library, a third-party package)."""
        fq = self._resolve(node, local, me) if node is not None and _dotted(node) else None
        shown = (_dotted(node) or "") if node is not None else ""
        outside = "outside" if fq and self.idx.outside(fq) else ""
        lifted = how.format(shown or "an object") if how else ""
        return f"{shown}|{fq if fq in self.idx.funcs else ''}|{lifted}|{outside}"

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

    def _names_of(self, node: ast.AST, local: set[str], me: tuple[str, str] | None) -> list[ast.AST] | None:
        """node is a namespace whose names alone are read (`globals()`, `locals()`, `vars()`, `vars(x)`,
        `x.__dict__`, `f.__globals__`): the expressions inside it still to walk; else None."""
        if isinstance(node, ast.Call) and _dotted(node.func) and not node.keywords and \
                self._resolve(node.func, local, me) in ("builtins.globals", "builtins.locals", "builtins.vars") \
                and len(node.args) <= (1 if self._resolve(node.func, local, me) == "builtins.vars" else 0):
            return list(node.args)
        d = self._ns_dict(node, local, me)
        return list(d[2]) if d else None

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
                           self._unwrapped(x.value, local, me, "{}.__dict__ (which holds __wrapped__)"))
        elif kind == "foreign":
            if lit is None or f"{ns}.{lit}" in _REFLECTIVE or f"{ns}.{lit}" == "builtins.hasattr" \
                    or lit in ("__subclasses__", "__mro__"):
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
            rest = [c for c in node.comparators if not (_dotted(c) and self._resolve(c, local, me) == "sys.modules")
                    and self._names_of(c, local, me) is None]
            if len(rest) < len(node.comparators):             # `name in sys.modules`, `name in globals()` only ask
                inner = [x for c in node.comparators if (got := self._names_of(c, local, me)) for x in got]
                self._exprs([node.left, *rest, *inner], owner, local, me)
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
        if isinstance(f, ast.Attribute) and f.attr in _NAMES_ONLY \
                and (inner := self._names_of(f.value, local, me)) is not None:
            self._exprs([*inner, *node.args, *kw], owner, local, me)   # globals().keys(): names, never a value
            return True
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
            self._type_use(node.value)                     # `store.write = x` sets an attribute of store
        if isinstance(node, ast.Lambda):
            local = local | _locals(node)
            self._expr(node.args, owner, local, me)            # its defaults, in the enclosing scope
            self._shadowed(node, node.args, self._expr, node.body, owner, local, me)
            return
        if isinstance(node, ast.Compare):
            for x in [node.left, *node.comparators]:          # `x is C`, `type(x) == C`: compared, not called
                if isinstance(x, (ast.Name, ast.Attribute)):
                    self._type_use(x)
        elif isinstance(node, ast.Subscript) and _dotted(node.value) and _generic(self._resolve(node.value, local, me)):
            self._type_use(node.slice)                        # `list[C]`, `Optional[C]`: a type, never called
        if isinstance(node, _COMPREHENSIONS) and self._class_scope is not None and not self._scopes:
            # in a class body, only a comprehension's first iterable sees the class's names
            self._expr(node.generators[0].iter, owner, local, me)
            scope, self._class_scope = self._class_scope, None
            try:
                for child in ast.iter_child_nodes(node):
                    if child is not node.generators[0]:
                        self._expr(child, owner, local, me)
                gen = node.generators[0]
                for child in [gen.target, *gen.ifs]:
                    self._expr(child, owner, local, me)
            finally:
                self._class_scope = scope
            return
        for child in ast.iter_child_nodes(node):
            if not (isinstance(node, ast.JoinedStr) and isinstance(child, ast.Constant)):
                self._expr(child, owner, local, me)

    def _call_expr(self, node: ast.Call, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        """A call: its edge or site, then what it is called on and its arguments."""
        self._called.add(id(node.func))
        fq = self._resolve(node.func, local, me) if _dotted(node.func) else None
        if fq in ("builtins.isinstance", "builtins.issubclass"):
            for a in node.args:                            # tested, not called
                self._type_use(a)
        elif fq in ("builtins.getattr", "builtins.setattr", "builtins.delattr", "builtins.hasattr",
                    "inspect.getattr_static", "builtins.vars", "builtins.dir") and node.args:
            self._type_use(node.args[0])                   # a lookup on it: a site of its own (_attr_builtin)
        elif fq in ("builtins.super", "typing.cast", "typing_extensions.cast"):
            for a in node.args[:2 if fq == "builtins.super" else 1]:
                self._type_use(a)
        self._call(node, owner, local, me)
        if not _dotted(node.func):
            self._expr(node.func, owner, local, me)
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "__wrapped__":
            self._site("wrapped", owner, node.func, self._unwrapped(node.func.value, local, me))
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "__subclasses__":
            self._site("reflection", owner, node.func, "__subclasses__")   # `object.__subclasses__()`: any class
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
                       self._unwrapped(node.value, local, me, "{}.__closure__ (whose cells hold what it wraps)"))
        elif isinstance(node, ast.Attribute) and node.attr == "__mro__" and not (
                _dotted(node.value) and (v := self._resolve(node.value, local, me)) and self._outside(v)):
            self._site("reflection", owner, node, "__mro__")                     # C.__mro__: its classes as values
        elif isinstance(node, ast.Attribute) and node.attr == "__subclasses__":
            self._site("reflection", owner, node, "__subclasses__")   # every subclass, object's: any class
        elif isinstance(node, ast.Name) and node.id == "__builtins__":
            self._site("reflection", owner, node, "__builtins__")
        elif isinstance(node, ast.Attribute) and node.attr in _FRAME_NAMESPACES:
            self._site("reflection", owner, node, f"frame.{node.attr}")      # sys._getframe().f_globals
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
        on_instance = at is node and isinstance(node, ast.Attribute) and not (
            _dotted(node.value) and self._resolve(node.value, local, me) in self.idx.classes)
        for ref in refs:
            if on_instance and self._property(ref):               # obj.prop runs the getter: a call
                self.edges.append(_Edge(owner, ref, getattr(at, "lineno", 0), getattr(at, "col_offset", 0), False))
            else:
                self._site("ref", owner, at, ref)
        if not refs and _dotted(node) and id(node) not in self._type_uses:
            # a class used as a value is called by whoever holds it (its constructors run); a scanned module
            # used as a value hands out every function it holds (fix round 26)
            fq = self._resolve(node, local, me)
            if fq in self.idx.classes or (fq in self.idx.modules and at is node):
                held = self._held_by(fq)
                for ref in held:
                    self._held_ref(owner, at, ref, fq)
                if held:
                    return
        if refs or not isinstance(node, ast.Attribute) or node.attr not in self.idx.short_names():
            return
        fq = self._resolve(node, local, me) if _dotted(node) else None
        if (fq is None or self._via_function(node, local, me)) and self._typed(node, owner, local, me) == ([], None):
            self._site("attrref", owner, at, node.attr)
            if at is node and (props := self._getters(node.attr)):   # obj.total may run a property getter
                self.edges.append(_Fan(owner, props, getattr(at, "lineno", 0), getattr(at, "col_offset", 0)))

    def _getters(self, name: str) -> tuple[str, ...]:
        """Every scanned property getter named name (what `obj.name` on an unknown receiver may run)."""
        got = self.idx.getters.get(name)
        if got is None:
            got = self.idx.getters[name] = tuple(m for m in sorted(self.idx.methods_by_name.get(name, ()))
                                                 if self._property(m))
        return got


    def _property(self, fid: str) -> bool:
        """fid is a property getter (`@property`, `@functools.cached_property`), read where it is defined."""
        f = self.idx.funcs.get(fid)
        fn = f.node if f is not None else None
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or f is None or f.module not in self.idx.modules:
            return False
        home = self._home(f.module)
        return any(_dotted(d) and home._resolve(d, set(), None) in ("builtins.property", "functools.cached_property")
                   for d in fn.decorator_list)

    def _call(self, node: ast.Call, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        func = node.func
        while isinstance(func, ast.Attribute) and func.attr == "__call__":    # f.__call__(x) calls f
            func = func.value
        fq = self._resolve(func, local, me) if _dotted(func) else None
        if func is not node.func and not (fq and self._known_callee(fq)) and not (
                isinstance(func, ast.Attribute) and (fq is None or self._typed(func, owner, local, me)[0])):
            func, fq = node.func, None                 # not a known function: an ordinary attribute call
        for alt in sorted(self._alternatives(func, local, me)):        # refund bound to _send as well
            if alt != fq:
                for target in [alt] if alt in self.idx.funcs else self.idx.constructors(alt) \
                        if alt in self.idx.classes else []:
                    self.edges.append(_Edge(owner, target, node.lineno, node.col_offset, False))
        self._remade(node, fq, owner, local, me)
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
        if fq in _TIMEIT and self._code_string(node, ("stmt", "setup"), 2, local, me):
            self._site("reflection", owner, node, f"{fq} with a string statement")
            return
        if fq in _RUNNERS and self._runs_python(node, local, me):
            self._site("reflection", owner, node, f"{fq} running Python")
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
            for init in self.idx.constructors(fq):     # __init__, __new__, __post_init__, a metaclass __call__
                self.edges.append(_Edge(owner, init, node.lineno, node.col_offset, False))
        elif isinstance(func, ast.Attribute) and (fq is None or self._via_function(func, local, me)):
            hits, container = self._typed(func, owner, local, me)
            for m in hits:                              # the class's method, and a subclass's override
                self.edges.append(_Edge(owner, m, node.lineno, node.col_offset, False))
            if not hits and container is None:          # d.update() on a dict is the dict's own method
                self._site("attrcall", owner, func, func.attr)
                if fan := self.idx.fan(func.attr):         # an inferred edge to each method of that name
                    self.edges.append(_Fan(owner, fan, node.lineno, node.col_offset))

    def _remade(self, node: ast.Call, fq: str | None, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        """A call that builds another instance of an object's class: `type(x)(...)`, `x.__class__(...)`,
        `dataclasses.replace(x, ...)`, `copy.copy(x)` (which runs `__new__`). The constructors of x's class
        (and of its subclasses, unless x was built here), or, when the scan cannot type x, of any class."""
        f = node.func
        if isinstance(f, ast.Call) and len(f.args) == 1 and _dotted(f.func) and \
                self._resolve(f.func, local, me) == "builtins.type":
            x: ast.AST = f.args[0]
        elif isinstance(f, ast.Attribute) and f.attr == "__class__":
            x = f.value
        elif fq in ("dataclasses.replace", "copy.copy", "copy.deepcopy") and node.args:
            x = node.args[0]
        else:
            return
        rcv = (me[1], False) if me and isinstance(x, ast.Name) and x.id == me[0] else \
            self._receiver(x, owner, local, me)
        if rcv is not None and rcv[0] in self.idx.classes and rcv[0] not in self._untrusted:
            for c in [rcv[0], *([] if rcv[1] else self.idx.subclasses(rcv[0]))]:
                for init in self.idx.constructors(c):
                    self.edges.append(_Edge(owner, init, node.lineno, node.col_offset, False))
        elif ctors := self.idx.all_constructors():
            self.edges.append(_Fan(owner, ctors, node.lineno, node.col_offset))

    def _code_string(self, node: ast.Call, names: tuple[str, ...], positions: int, local: set[str],
                     me: tuple[str, str] | None) -> bool:
        """An argument (the first `positions` positional ones, or a keyword in names) that is not a lambda
        or a scanned function: a string of code, or something that may be one."""
        args = [*node.args[:positions], *[k.value for k in node.keywords if k.arg in names]]
        return any(not (isinstance(a, ast.Lambda) or (_dotted(a) and self._resolve(a, local, me) in self.idx.funcs))
                   for a in args)

    def _runs_python(self, node: ast.Call, local: set[str], me: tuple[str, str] | None) -> bool:
        """A subprocess command (its arguments, not env or cwd) that names sys.executable, or a string with
        `python` in it or a `.py` file: it runs Python code the scan does not follow from here."""
        parts = [*node.args, *[k.value for k in node.keywords if k.arg in ("args", "argv", "cmd", "command")]]
        for p in parts:
            for n in ast.walk(p):
                if _dotted(n) and self._resolve(n, local, me) in ("sys.executable", "sys.orig_argv", "sys.argv"):
                    return True
                text = _lit(n)
                if text is not None and ("python" in text.lower() or ".py" in text):
                    return True
        return False

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
        if lit is None:                                 # the class of the receiver, for a computed name
            self.sites[-1].recv = self._receiver_class(node.args[0], obj, owner, local, me)
        if lit is not None and obj and f"{obj}.{lit}" in _REFLECTIVE:       # getattr(builtins, "getattr")
            self._site("reflection", owner, node, f"{lit} looked up by name")
        if lit in _FRAME_NAMESPACES:                                         # getattr(frame, "f_globals")
            self._site("reflection", owner, node, f"frame.{lit}")
        if lit in ("__subclasses__", "__mro__"):                            # getattr(object, "__subclasses__")
            self._site("reflection", owner, node, lit)
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
            if not hits and container is None and (fan := self.idx.fan(lit)):
                self.edges.append(_Fan(owner, fan, node.lineno, node.col_offset, via_getattr=True))

    def _receiver_class(self, arg: ast.AST, obj: str | None, owner: str, local: set[str],
                        me: tuple[str, str] | None) -> str:
        """The class a getattr-style receiver is an instance of ("C"), or is exactly ("C!": the class object
        itself, or built here by C(...)), when the scan knows it; else ""."""
        if obj and obj.startswith("self:"):
            return obj[5:] if obj[5:] in self.idx.classes else ""
        if obj in self.idx.classes:
            return f"{obj}!"
        if obj is not None and obj not in self.idx.funcs and not (
                self._data_attr(obj) and self._module_instance(arg, local, me)):
            return ""                                    # a module, a foreign name, an untyped data attribute
        rcv = self._receiver(arg, owner, local, me)
        if rcv is None or rcv[0] not in self.idx.classes:
            return ""
        return rcv[0] + ("!" if rcv[1] else "")

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
            targets = [hit] if hit in self.idx.funcs else list(self.idx.constructors(hit)) \
                if hit in self.idx.classes else []
            for target in targets:
                self.edges.append(_Edge(owner, target, node.lineno, node.col_offset, False))
            if targets:
                continue
            if ex := self.idx.excluded_module(hit):
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
        for lit in lits:                       # on whatever it is applied to: each function of that name
            for part in lit.split(".") if fq == "operator.attrgetter" else [lit]:
                if fan := self.idx.fan(part):
                    self.edges.append(_Fan(owner, fan, node.lineno, node.col_offset, via_getattr=True))
        for a in [*node.args, *[k.value for k in node.keywords]]:
            self._expr(a, owner, local, me)


def _generic(fq: str | None) -> bool:
    """fq is a generic type whose subscript names types only (`list[C]`, `typing.Optional[C]`); not
    `Annotated[C, Depends()]`, whose metadata a framework may call."""
    return fq is not None and (fq in ("builtins.list", "builtins.dict", "builtins.set", "builtins.frozenset",
                                      "builtins.tuple", "builtins.type") or (
        fq.startswith(("typing.", "typing_extensions.", "collections.abc.")) and not fq.endswith(".Annotated")))


def _passes_through(fn: ast.FunctionDef) -> bool:
    """A scanned decorator that keeps nothing: every return gives back its first parameter or a function
    defined in it, the parameter is never rebound, and every other use of it is a call (or functools.wraps of
    it) inside such a nested wrapper, which is only returned (`HOOKS.append(f); return f` keeps it)."""
    if not fn.args.args or _rebinds(fn, fn.args.args[0].arg):
        return False
    p = fn.args.args[0].arg
    inner = [n for n in fn.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    names = {n.name for n in inner}
    rets = _own(fn, ast.Return)
    if not rets or _own(fn, ast.Yield, ast.YieldFrom) or not all(
            isinstance(r.value, ast.Name) and (r.value.id == p or r.value.id in names) for r in rets):
        return False
    returned = {id(r.value) for r in rets}
    in_wrapper = {id(x) for w in inner for x in ast.walk(w)}
    parent = {id(c): n for n in ast.walk(fn) for c in ast.iter_child_nodes(n)}
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)) or id(n) in returned:
            continue
        if n.id in names:
            return False                                  # the wrapper itself handed somewhere else
        if n.id != p:
            continue
        up = parent.get(id(n))
        called = isinstance(up, ast.Call) and up.func is n
        wrapped = isinstance(up, ast.Call) and n in up.args and _dotted(up.func) in ("functools.wraps", "wraps")
        if not (called or wrapped) or id(n) not in in_wrapper:
            return False
    return True


def _factory_passes(fn: ast.FunctionDef) -> bool:
    """`@deco(...)`: every return of the factory is a decorator defined in it that keeps nothing, and is
    used nowhere else."""
    inner = {n.name: n for n in fn.body if isinstance(n, ast.FunctionDef)}
    rets = _own(fn, ast.Return)
    uses = sum(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in inner for n in ast.walk(fn))
    return bool(rets) and uses == len(rets) and all(
        isinstance(r, ast.Return) and isinstance(r.value, ast.Name) and r.value.id in inner
        and _passes_through(inner[r.value.id]) for r in rets)


def _own(fn: ast.AST, *kinds: type) -> list[Any]:
    """The nodes of those kinds in fn itself, not in the functions, lambdas or classes defined in it."""
    out: list[Any] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        if isinstance(n, kinds):
            out.append(n)
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(n))
    return out
