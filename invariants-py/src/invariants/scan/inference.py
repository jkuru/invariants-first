"""Receiver and type inference: conservative, so a method call becomes an extracted edge only when certain."""
from __future__ import annotations

import ast
import collections

from .astutil import _dotted, _own_nodes, _pairs, _single_types
from .contracts import _Module
from .indexer import _BUILTINS, _Index

# builtin containers, as receivers: their own methods (d.update(), s.add()) are not a scanned sink
_CONTAINERS = {"builtins.dict": "builtins.dict", "typing.Dict": "builtins.dict", "builtins.set": "builtins.set",
               "typing.Set": "builtins.set", "builtins.list": "builtins.list", "typing.List": "builtins.list",
               "builtins.frozenset": "builtins.frozenset", "typing.FrozenSet": "builtins.frozenset"}
_LITERALS = ((ast.Dict, "builtins.dict"), (ast.DictComp, "builtins.dict"), (ast.Set, "builtins.set"),
             (ast.SetComp, "builtins.set"), (ast.List, "builtins.list"), (ast.ListComp, "builtins.list"))
_OPTIONAL = frozenset({"typing.Optional", "typing_extensions.Optional"})
_UNION = frozenset({"typing.Union", "typing_extensions.Union"})
_SELF = frozenset({"typing.Self", "typing_extensions.Self"})
_ANNOTATED = frozenset({"typing.Annotated", "typing_extensions.Annotated"})
_PROPERTY = frozenset({"builtins.property", "functools.cached_property"})
_CM = {"contextlib.contextmanager": False, "contextlib.asynccontextmanager": True}
_YIELDS = frozenset({f"{m}.{n}" for m in ("typing", "collections.abc")
                     for n in ("Iterator", "Generator", "Iterable", "AsyncIterator", "AsyncGenerator", "AsyncIterable")})


class _Inference:
    """Type queries in one module: what a name, receiver or annotation is."""

    def __init__(self, idx: _Index, mod: _Module) -> None:
        self.idx, self.mod = idx, mod
        self._local_types: dict[str, dict[str, str]] = {}
        # the parameters of the nested functions and lambdas around the code being walked: their own
        # values, which a binding of the same name in the owner does not type
        self._shadow: frozenset[str] = frozenset()

    def _class_named(self, node: ast.AST | None, local: set[str], me: tuple[str, str] | None,
                     depth: int = 0) -> str | None:
        """The scanned class an annotation names (Cls, mod.Cls, 'Cls', Cls | None, Optional[Cls],
        Annotated[Cls, ...] and a module-level alias of it), or the builtin container it names
        (dict, dict[str, int], typing.Set[str], ...)."""
        if depth > 8:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                node = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            sides = [x for x in (node.left, node.right) if not (isinstance(x, ast.Constant) and x.value is None)]
            return self._class_named(sides[0], local, me, depth + 1) if len(sides) == 1 else None
        if isinstance(node, ast.Subscript):
            head = self._resolve(node.value, set(), None) if _dotted(node.value) else None
            if head in _OPTIONAL or _dotted(node.value) == "Optional":
                return self._class_named(node.slice, local, me, depth + 1)
            if head in _UNION:                                 # Union[Repo, None]: Repo; Union[A, B]: unknown
                elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
                sides = [x for x in elts if not (isinstance(x, ast.Constant) and x.value is None)]
                return self._class_named(sides[0], local, me, depth + 1) if len(sides) == 1 else None
            if head in _ANNOTATED:                             # Annotated[Repo, Depends(get_repo)]: Repo
                first = node.slice.elts[0] if isinstance(node.slice, ast.Tuple) and node.slice.elts else node.slice
                return self._class_named(first, local, me, depth + 1)
            if head in self.idx.classes:                       # BaseRepo[int]: a generic scanned class
                return head
            return _CONTAINERS.get(head or "")
        if isinstance(node, (ast.Name, ast.Attribute)):
            fq = self._resolve(node, set(), None)
            if fq is None and isinstance(node, ast.Name):
                fq = f"{self.mod.name}.{node.id}"
            if fq is None:
                return None
            if fq in self.idx.classes:
                return fq
            if fq in _CONTAINERS:
                return _CONTAINERS[fq]
            mod, _, name = fq.rpartition(".")                  # RepoDep = Annotated[Repo, ...]
            if mod in self.idx.modules and (value := self.idx.module_values(mod).get(name)) is not None:
                return self._home(mod)._class_named(value, set(), None, depth + 1)
        return None

    def _home(self, module: str) -> _Inference:
        """Type queries for the module that defines a name, so its annotations resolve where they are written."""
        return self if module == self.mod.name else _Inference(self.idx, self.idx.modules[module])

    def _constructed(self, node: ast.AST | None, local: set[str], me: tuple[str, str] | None) -> str | None:
        """Cls(...) on a scanned class: an instance of exactly that class; dict(...), set(...), list(...)
        and a container literal or comprehension: that builtin container."""
        for kind, name in _LITERALS:
            if isinstance(node, kind):
                return name
        if isinstance(node, ast.Call) and _dotted(node.func):
            fq = self._resolve(node.func, local, me)
            if fq in _CONTAINERS and fq.startswith("builtins."):
                return fq
            return fq if fq in self.idx.classes else None
        return None

    def _returned(self, node: ast.AST | None, local: set[str], me: tuple[str, str] | None) -> str | None:
        """f(...) on a scanned function annotated `-> Cls` (or `Cls | None`, `Optional[Cls]`): an instance of
        Cls or a subclass. A factory or a FastAPI dependency (`def get_repo() -> RefundRepo`), awaited
        too (`await get_repo()` on an `async def`)."""
        if isinstance(node, ast.Await):
            node = node.value
        if not (isinstance(node, ast.Call) and _dotted(node.func)):
            return None
        if isinstance(node.func, ast.Name) and me and node.func.id == me[0] and self.idx.class_param(me):
            return me[1]                                       # cls() in a @classmethod: cls or a subclass
        fq = self._resolve(node.func, local, me)
        return self.return_type(fq) if fq else None

    def _chained(self, node: ast.AST | None, owner: str | None, local: set[str],
                 me: tuple[str, str] | None) -> str | None:
        """recv.m(...) (awaited too) where recv's class is known: the class every method it may call is
        annotated to return (a fluent `Repo().with_x().save()`, or `repo.with_x()` on a local)."""
        if isinstance(node, ast.Await):
            node = node.value
        if owner is None or not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            return None
        hits = self._typed(node.func, owner, local, me)[0]
        got = {self.return_type(h) for h in hits}
        return next(iter(got)) if len(got) == 1 else None

    def return_type(self, fid: str) -> str | None:
        """The scanned class fid's return annotation names, read in the module that defines fid."""
        if fid in self.idx.returns:
            return self.idx.returns[fid]
        self.idx.returns[fid] = None                          # guards a cycle
        f = self.idx.funcs.get(fid)
        fn = f.node if f is not None else None
        got = None
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.returns is not None \
                and f is not None and f.module in self.idx.modules:
            home = self._home(f.module)
            ann: ast.AST = fn.returns
            if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
                try:
                    ann = ast.parse(ann.value, mode="eval").body
                except SyntaxError:
                    pass
            if f.cls and _dotted(ann) and home._resolve(ann, set(), None) in _SELF:
                got = f.cls                                    # -> Self: the class, or a subclass
            else:
                got = home._class_named(fn.returns, set(), None)
        self.idx.returns[fid] = got
        return got

    def _made(self, node: ast.AST | None, local: set[str], me: tuple[str, str] | None) -> str | None:
        """The class a value is built as: Cls(...) or a call to a function annotated to return Cls."""
        return self._constructed(node, local, me) or self._returned(node, local, me)

    def _entered(self, node: ast.AST, is_async: bool, local: set[str], me: tuple[str, str] | None,
                 owner: str | None = None) -> str | None:
        """The class `with node as x` binds x to: a scanned class's `__enter__`/`__aenter__` return
        annotation (`-> Self` is the class), or a @contextmanager factory's `-> Iterator[Cls]` (a function,
        or a method on a receiver the scan can type, such as a local `svc` in `async with svc.cm()`)."""
        cls = self._made(node, local, me) or self._chained(node, owner, local, me)
        if cls in self.idx.classes:
            m = self.idx.method(cls, "__aenter__" if is_async else "__enter__")
            fn = self.idx.funcs[m].node if m else None
            if m and isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.returns is not None:
                if self._names_self(m, fn.returns):
                    return cls                                 # -> Self: the receiver's class
                return self.return_type(m)
            return None
        if not isinstance(node, ast.Call):
            return None
        fq = self._resolve(node.func, local, me) if _dotted(node.func) else None
        if fq in self.idx.funcs:
            cands = [fq]
        elif owner is not None and isinstance(node.func, ast.Attribute):
            cands = self._typed(node.func, owner, local, me)[0]
        else:
            return None
        got = {self._yielded(c, is_async) for c in cands}
        return next(iter(got)) if len(got) == 1 else None

    def _names_self(self, fid: str, ann: ast.AST) -> bool:
        """fid's return annotation ann is typing.Self (written as a name or a string), read where fid is."""
        f = self.idx.funcs[fid]
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            try:
                ann = ast.parse(ann.value, mode="eval").body
            except SyntaxError:
                return False
        return bool(_dotted(ann)) and f.module in self.idx.modules and \
            self._home(f.module)._resolve(ann, set(), None) in _SELF

    def _yielded(self, fid: str, is_async: bool) -> str | None:
        """The scanned class a @contextmanager (@asynccontextmanager) function fid yields, by its
        `-> Iterator[Cls]` annotation."""
        f = self.idx.funcs.get(fid)
        fn = f.node if f is not None else None
        if f is None or not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                or f.module not in self.idx.modules:
            return None
        home = self._home(f.module)
        if not any(_CM.get(home._resolve(d, set(), None) or "") is is_async for d in fn.decorator_list
                   if _dotted(d)):
            return None
        ann = fn.returns
        if ann is not None and isinstance(ann, ast.Subscript) and _dotted(ann.value) and \
                home._resolve(ann.value, set(), None) in _YIELDS:
            first = ann.slice.elts[0] if isinstance(ann.slice, ast.Tuple) and ann.slice.elts else ann.slice
            got = home._class_named(first, set(), None)
            return got if got in self.idx.classes else None
        return None

    def _types_in(self, fn: ast.AST, local: set[str], me: tuple[str, str] | None,
                  owner: str | None = None) -> dict[str, str]:
        """Names whose every binding in fn is one class: an annotated parameter or Cls(...) assignments.
        With owner (fn's id), a `with recv.cm() as x` on a local receiver is typed from the other
        bindings (to a fixed point, so nested with blocks chain). What fn binds, wherever it is asked: no
        nested function's parameters shadow its names here (the result is cached)."""
        shadow, self._shadow = self._shadow, frozenset()
        try:
            return self._bindings_in(fn, local, me, owner)
        finally:
            self._shadow = shadow

    def _bindings_in(self, fn: ast.AST, local: set[str], me: tuple[str, str] | None,
                     owner: str | None) -> dict[str, str]:
        deferred: list[tuple[str, ast.expr, bool]] = []
        seen: dict[str, set[str | None]] = collections.defaultdict(set)
        args = getattr(fn, "args", None)
        if isinstance(args, ast.arguments):
            for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                if me and a.arg == me[0]:
                    continue
                seen[a.arg].add(self._class_named(a.annotation, local, me) if a.annotation else None)
            for va in (args.vararg, args.kwarg):
                if va is not None:
                    seen[va.arg].add(None)
        handled: set[int] = set()                              # Name nodes bound by a plain assignment
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                for t, v in _pairs(node):
                    if isinstance(t, ast.Name):
                        seen[t.id].add(self._made(v, local, me))
                        handled.add(id(t))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                seen[node.target.id].add(self._made(node.value, local, me) if node.value is not None
                                         else self._class_named(node.annotation, local, me))
                handled.add(id(node.target))
            elif isinstance(node, (ast.With, ast.AsyncWith)):     # with Session() as repo
                for item in node.items:
                    if isinstance(item.optional_vars, ast.Name):
                        is_async = isinstance(node, ast.AsyncWith)
                        got = self._entered(item.context_expr, is_async, local, me)
                        if got is None and owner is not None and isinstance(item.context_expr, ast.Call) \
                                and isinstance(item.context_expr.func, ast.Attribute):
                            deferred.append((item.optional_vars.id, item.context_expr, is_async))
                        else:
                            seen[item.optional_vars.id].add(got)
                        handled.add(id(item.optional_vars))
        for node in ast.walk(fn):                              # any other binding: for, with, walrus, unpacking
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and id(node) not in handled:
                seen[node.id].add(None)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not fn:
                seen[node.name].add(None)
            elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
                seen[node.name].add(None)                      # except E as err, case Repo() as repo
            elif isinstance(node, ast.MatchMapping) and node.rest:
                seen[node.rest].add(None)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for al in node.names:
                    seen[(al.asname or al.name).split(".")[0]].add(None)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                for n in node.names:
                    seen[n].add(None)
        if deferred and owner is not None:
            saved = self._local_types.pop(owner, None)
            pending = deferred
            while pending:
                waiting = {name for name, _, _ in pending}
                self._local_types[owner] = {k: v for k, v in _single_types(seen).items() if k not in waiting}
                left = []
                for name, ctx, is_async in pending:
                    got = self._entered(ctx, is_async, local, me, owner)
                    if got is None:
                        left.append((name, ctx, is_async))
                    else:
                        seen[name].add(got)
                if len(left) == len(pending):                  # no progress: the rest stay unknown
                    for name, _, _ in left:
                        seen[name].add(None)
                    break
                pending = left
            del self._local_types[owner]
            if saved is not None:
                self._local_types[owner] = saved
        return _single_types(seen)

    def _attr_types(self, cls: str) -> dict[str, str]:
        """self.<attr> types for a class: every `self.attr = ...` in the methods of the class and of its
        scanned bases (an inherited `__init__` or @property counts) has the same known class."""
        if cls in self.idx.attr_types:
            return self.idx.attr_types[cls]
        seen: dict[str, set[str | None]] = collections.defaultdict(set)
        for c in self.idx.mro(cls) or [cls]:
            for name, got in self._attr_seen(c).items():
                seen[name] |= got
        out = _single_types(seen)
        self.idx.attr_types[cls] = out
        return out

    def _attr_seen(self, cls: str) -> dict[str, set[str | None]]:
        """Every class each `self.attr` binding in cls's own body and methods gives (None: unknown)."""
        if cls in self.idx.attr_seen:
            return self.idx.attr_seen[cls]
        home = self.idx.classes[cls]["module"]
        if home != self.mod.name and home in self.idx.modules:   # names resolve where the class is written
            return _Inference(self.idx, self.idx.modules[home])._attr_seen(cls)
        self.idx.attr_seen[cls] = {}                           # guards a cycle
        seen: dict[str, set[str | None]] = collections.defaultdict(set)
        cnode = self.idx.classes[cls].get("node")
        for st in getattr(cnode, "body", []):                  # class-level `billing: BillingService` (dataclasses)
            if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                ann = self._class_named(st.annotation, set(), None)
                seen[st.target.id].add(ann if st.value is None else self._made(st.value, set(), None))
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    _dotted(d) and self._resolve(d, set(), None) in _PROPERTY for d in st.decorator_list):
                # @property def repo(self) -> Repo: self.repo reads it
                seen[st.name].add(self._class_named(st.returns, set(), None) if st.returns is not None else None)
        for fid in self.idx.classes[cls]["methods"].values():
            fn = self.idx.funcs[fid].node
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not fn.args.args:
                continue
            me_name = fn.args.args[0].arg
            params = self._types_in(fn, set(), (me_name, cls), fid)
            for node in ast.walk(fn):
                pairs = _pairs(node) if isinstance(node, ast.Assign) else \
                    [(node.target, node.value)] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
                for t, v in pairs:
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == me_name:
                        got = self._made(v, set(), None) or (
                            params.get(v.id) if isinstance(v, ast.Name) else None)
                        seen[t.attr].add(got if not isinstance(node, ast.AugAssign) else None)
                    elif isinstance(t, (ast.Tuple, ast.List, ast.Starred)):
                        for x in ast.walk(t):                  # unpacking we cannot pair: unknown
                            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == me_name:
                                seen[x.attr].add(None)
        self.idx.attr_seen[cls] = dict(seen)
        return self.idx.attr_seen[cls]

    def _receiver(self, node: ast.AST, owner: str, local: set[str], me: tuple[str, str] | None) -> tuple[str, bool] | None:
        """(class, exact) of a call's receiver when the scan can be sure of it, else None. exact: built by
        Cls(...) here, so no subclass override can be called instead."""
        cls = self._constructed(node, local, me)
        if cls:
            return cls, True
        # get_repo().add(), Repo().with_x().add(): Cls or a subclass
        made = self._returned(node, local, me) or self._chained(node, owner, local, me)
        if made:
            return made, False
        fn = self.idx.funcs[owner].node if owner in self.idx.funcs else None
        if isinstance(node, ast.Name) and fn is not None and node.id in local:
            if node.id in self._shadow:
                return None
            if owner not in self._local_types:
                self._local_types[owner] = self._types_in(fn, local, me, owner)
            got = self._local_types[owner].get(node.id)
            return (got, False) if got else None
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and me and node.value.id == me[0]:
                base: tuple[str, bool] | None = (me[1], False)
            else:                                             # ctx.billing.refund(): ctx's class, then billing's
                base = self._receiver(node.value, owner, local, me)
            if base is None or base[0] not in self.idx.classes or node.attr in self._foreign_attrs():
                return None
            got = self._attr_types(base[0]).get(node.attr)
            return (got, False) if got else None
        return None

    def _foreign_attrs(self) -> set[str]:
        """Attribute names some code sets from outside the object (`obj.x = ...`, `setattr(obj, "x", ...)`):
        such an attribute can hold anything, so it is never given a type."""
        if self.idx.foreign is None:
            out: set[str] = set()
            for mod in self.idx.modules.values():
                methods = {id(m) for c in ast.walk(mod.tree) if isinstance(c, ast.ClassDef) for m in c.body
                           if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
                for fn in ast.walk(mod.tree):
                    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                        continue
                    # only a method's first parameter is the object itself (self); in a plain function,
                    # `ctx.repo = other` sets another object's attribute
                    first = (fn.args.args[0].arg if not isinstance(fn, ast.Module) and id(fn) in methods
                             and fn.args.args else None)
                    for node in _own_nodes(fn):
                        targets = node.targets if isinstance(node, ast.Assign) else \
                            [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
                        for t in targets:
                            for x in ast.walk(t):
                                if isinstance(x, ast.Attribute) and isinstance(x.ctx, ast.Store) and not (
                                        isinstance(x.value, ast.Name) and x.value.id == first):
                                    out.add(x.attr)
                        if isinstance(node, ast.Call) and _dotted(node.func) in ("setattr", "builtins.setattr") \
                                and len(node.args) >= 2:
                            name = node.args[1]
                            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                                out.add(name.value)
            self.idx.foreign = out
        return self.idx.foreign

    def _super_methods(self, node: ast.Attribute, local: set[str], me: tuple[str, str] | None) -> list[str] | None:
        """super().m (or super(Cls, self).m) inside a method of class C: the next m after Cls in the method
        resolution order of C and of every scanned subclass of C (self may be any of them: a mixin's
        super() can land on a sibling base). [] when the scan cannot tell (no scanned m, a subclass's
        MRO has no consistent order, a class in one has a base the scan cannot see, or an excluded module
        subclasses C or a subclass), so the call is name-matched; None when node is not a super() lookup."""
        v = node.value
        if not (isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and me
                and self._resolve(v.func, local, me) == "builtins.super"):
            return None
        start = me[1]
        if v.args:
            named = self._resolve(v.args[0], local, me) if _dotted(v.args[0]) else None
            if named not in self.idx.classes:
                return []
            start = named
        out: set[str] = set()
        family = [me[1], *self.idx.subclasses(me[1])]
        if self.idx.excluded_subclass(set(family)):
            return []                                          # a subclass the scan does not read
        for k in family:
            order = self.idx.strict_mro(k)
            if order is None or any(self.idx.unseen_bases(c) for c in order):
                return []                                      # fail closed: name-matched
            if start not in order:
                continue
            for c in order[order.index(start) + 1:]:
                if node.attr in self.idx.classes[c]["methods"]:
                    out.add(self.idx.classes[c]["methods"][node.attr])
                    break
        return sorted(out)

    def _typed(self, node: ast.Attribute, owner: str, local: set[str],
               me: tuple[str, str] | None) -> tuple[list[str], str | None]:
        """For `recv.m` on a receiver the scan can type: the methods it may name (the class's m, and the
        overrides a subclass instance may bring), else the builtin container it is (d.update on a dict)."""
        sup = self._super_methods(node, local, me)
        if sup is not None:
            return sup, None
        rcv = self._receiver(node.value, owner, local, me)
        if rcv is None:
            return [], None
        if rcv[0] in self.idx.classes:
            hit = self.idx.method(rcv[0], node.attr)
            if not hit:
                return [], None
            return [hit, *([] if rcv[1] else self._subclasses_overriding(rcv[0], node.attr))], None
        if rcv[0] in _CONTAINERS.values() and (rcv[1] or not self._subclasses_overriding(rcv[0], node.attr)):
            return [], rcv[0]                                  # a scanned subclass overriding it stays unknown
        return [], None

    def _via_function(self, node: ast.Attribute, local: set[str], me: tuple[str, str] | None) -> bool:
        """`self.repo.m` where repo is a method or property of a scanned class: its name resolves through a
        function, so the receiver's type decides what m is."""
        return bool(_dotted(node.value)) and self._resolve(node.value, local, me) in self.idx.funcs

    def _value_refs(self, node: ast.AST, owner: str, local: set[str], me: tuple[str, str] | None) -> list[str]:
        """The scanned functions a Name/Attribute expression names when it is used as a value."""
        if not isinstance(node, (ast.Name, ast.Attribute)):
            return []
        fq = self._resolve(node, local, me) if _dotted(node) else None
        if fq and fq in self.idx.funcs:
            return [fq]
        if isinstance(node, ast.Attribute) and (fq is None or self._via_function(node, local, me)):
            return self._typed(node, owner, local, me)[0]
        return []

    def _subclasses_overriding(self, cls: str, name: str) -> list[str]:
        out = []
        for c, info in self.idx.classes.items():
            if c != cls and name in info["methods"] and self._inherits(c, cls, set()):
                out.append(info["methods"][name])
        return sorted(out)

    def _inherits(self, c: str, base: str, seen: set[str]) -> bool:
        if c in seen or c not in self.idx.classes:
            return False
        seen.add(c)
        return any(fq == base or self._inherits(fq, base, seen) for fq in self.idx.bases(c))

    def _resolve(self, node: ast.AST, local: set[str], me: tuple[str, str] | None) -> str | None:
        """The canonical name a Name/Attribute chain refers to, or None when it is a runtime value."""
        if isinstance(node, ast.Name):
            if me and node.id == me[0]:
                return f"self:{me[1]}"
            if node.id in local:
                return None
            hit = self.mod.aliases.get(node.id) or self.idx._star_lookup(self.mod, node.id, 0)
            if hit:
                return self.idx.canon(hit)
            return f"builtins.{node.id}" if node.id in _BUILTINS else None
        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value, local, me)
            if base is None:
                return None
            if base.startswith("self:"):
                return self.idx.method(base[5:], node.attr)
            return self.idx.canon(f"{base}.{node.attr}")
        return None

    def _namespace(self, node: ast.AST, local: set[str], me: tuple[str, str] | None) -> str | None:
        """The module or class a Name/Attribute chain names, else None."""
        if not _dotted(node):
            return None
        fq = self._resolve(node, local, me)
        return fq if fq and (fq in self.idx.modules or fq in self.idx.classes) else None
