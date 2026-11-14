"""Receiver and type inference: conservative, so a method call becomes an extracted edge only when certain."""
from __future__ import annotations

import ast
import collections
import dataclasses
import weakref

from .astutil import (
    BINDERS,
    WRITES,
    _attr_writes,
    _computed_dict_writes,
    _own_under,
    _under,
    _dotted,
    _own_nodes,
    _pairs,
    _scope_binds,
    _scope_nodes,
    _single_types,
    _walk_of,
)
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

    def __init__(self, idx: _Index, mod: _Module, raw: bool = False) -> None:
        self.idx, self.mod = idx, mod
        # raw: names resolve as written, trusting every class (only to find the classes not to trust)
        self._raw = raw
        self._untrusted: frozenset[str] = frozenset() if raw else subverted(idx)
        # (class, method name): a method the code replaces or deletes on the class (Safe.save = len,
        # del Safe.save), so what `Safe().save` runs is not the scanned Safe.save
        self._untrusted_members: frozenset[tuple[str, str]] = frozenset() if raw else subverted_members(idx)
        self._rebound_classes: frozenset[str] = frozenset() if raw else rebound_class_names(idx, mod)
        self._local_types: dict[str, dict[str, str]] = {}
        # the parameters of the nested functions and lambdas around the code being walked: their own
        # values, which a binding of the same name in the owner does not type
        self._shadow: frozenset[str] = frozenset()
        # the nested functions, lambdas and classes around the code being walked (innermost last): a name
        # is typed by the bindings of the scope it is looked up in, never by another scope's
        self._scopes: tuple[ast.AST, ...] = ()
        self._scope_names: dict[int, set[str]] = {}

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
                return None if head in self._untrusted else head
            return _CONTAINERS.get(head or "")
        if isinstance(node, (ast.Name, ast.Attribute)):
            fq = self._resolve(node, set(), None)
            if fq is None and isinstance(node, ast.Name):
                fq = f"{self.mod.name}.{node.id}"
            if fq is None:
                return None
            if fq in self.idx.classes:
                return None if fq in self._untrusted else fq
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
            return fq if fq in self.idx.classes and fq not in self._untrusted else None
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
            return None if me[1] in self._untrusted else me[1]  # cls() in a @classmethod: cls or a subclass
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
                got = None if f.cls in self._untrusted else f.cls   # -> Self: the class, or a subclass
            else:
                got = home._class_named(fn.returns, set(), None)
            if got in self.idx.classes and home._returns_other(fn, got):
                got = None                                     # `-> Safe` over a visible `return Repo()`
        self.idx.returns[fid] = got
        return got

    def _returns_other(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, cls: str) -> bool:
        """fn, annotated to return cls, visibly returns an instance of a scanned class that is neither cls
        nor a subclass of it: the annotation is not what it returns. A return of a call to an unannotated
        scanned function counts what that function visibly returns, up to three calls deep (a type checker
        reads an unannotated function as returning Any, so it would not flag `-> Safe: return mk()`)."""
        return any(got in self.idx.classes and got != cls and cls not in self.idx.mro(got)
                   for got in self._visible_returns(fn, 0))

    def _visible_returns(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, depth: int) -> set[str | None]:
        """The classes fn's returns are visibly built as (Cls(...), an annotated factory), following calls
        to unannotated scanned functions, read where each is defined, to a bounded depth."""
        out: set[str | None] = set()
        for node in _own_nodes(fn):
            if not (isinstance(node, ast.Return) and node.value is not None):
                continue
            v = node.value.value if isinstance(node.value, ast.Await) else node.value
            got = self._made(v, set(), None)
            if got is None and depth < 3 and isinstance(v, ast.Call) and _dotted(v.func):
                fq = self._resolve(v.func, set(), None)
                f = self.idx.funcs.get(fq or "")
                if f is not None and isinstance(f.node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and f.node.returns is None and f.module in self.idx.modules:
                    out |= self._home(f.module)._visible_returns(f.node, depth + 1)
                    continue
            out.add(got)
        return out

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
        scopes, self._scopes = self._scopes, ()
        try:
            b = _Bound()
            self._param_types(fn, local, me, b)
            self._assigned(fn, local, me, owner, b)
            _unknown_bindings(fn, b)
            if b.deferred and owner is not None:
                self._entered_late(owner, local, me, b)
            return _single_types(b.seen)
        finally:
            self._shadow, self._scopes = shadow, scopes

    def _param_types(self, fn: ast.AST, local: set[str], me: tuple[str, str] | None, b: _Bound) -> None:
        """fn's parameters: an annotated one is its class, *args and **kwargs are unknown."""
        args = getattr(fn, "args", None)
        if not isinstance(args, ast.arguments):
            return
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if me and a.arg == me[0]:
                continue
            b.seen[a.arg].add(self._class_named(a.annotation, local, me) if a.annotation else None)
        for va in (args.vararg, args.kwarg):
            if va is not None:
                b.seen[va.arg].add(None)

    def _assigned(self, fn: ast.AST, local: set[str], me: tuple[str, str] | None, owner: str | None,
                  b: _Bound) -> None:
        """The plain bindings in fn: `x = Cls(...)`, `x: Cls = ...`, `with Session() as x`."""
        for node in _scope_nodes(fn):
            if isinstance(node, ast.Assign):
                for t, v in _pairs(node):
                    if isinstance(t, ast.Name):
                        b.seen[t.id].add(self._made(v, local, me))
                        b.handled.add(id(t))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                b.seen[node.target.id].add(self._made(node.value, local, me) if node.value is not None
                                           else self._class_named(node.annotation, local, me))
                b.handled.add(id(node.target))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if isinstance(item.optional_vars, ast.Name):
                        self._with_bound(item.optional_vars, item.context_expr, isinstance(node, ast.AsyncWith),
                                         local, me, owner, b)

    def _with_bound(self, name: ast.Name, ctx: ast.expr, is_async: bool, local: set[str],
                    me: tuple[str, str] | None, owner: str | None, b: _Bound) -> None:
        """`with ctx as name`: the class it enters, or (`with recv.cm()` on a receiver typed by fn's other
        bindings) left for the fixed point."""
        got = self._entered(ctx, is_async, local, me)
        if got is None and owner is not None and isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute):
            b.deferred.append((name.id, ctx, is_async))
        else:
            b.seen[name.id].add(got)
        b.handled.add(id(name))

    def _entered_late(self, owner: str, local: set[str], me: tuple[str, str] | None, b: _Bound) -> None:
        """The deferred `with recv.cm() as x`, typed from the other bindings to a fixed point."""
        saved = self._local_types.pop(owner, None)
        pending = b.deferred
        while pending:
            waiting = {name for name, _, _ in pending}
            self._local_types[owner] = {k: v for k, v in _single_types(b.seen).items() if k not in waiting}
            left = []
            for name, ctx, is_async in pending:
                got = self._entered(ctx, is_async, local, me, owner)
                if got is None:
                    left.append((name, ctx, is_async))
                else:
                    b.seen[name].add(got)
            if len(left) == len(pending):                      # no progress: the rest stay unknown
                for name, _, _ in left:
                    b.seen[name].add(None)
                break
            pending = left
        del self._local_types[owner]
        if saved is not None:
            self._local_types[owner] = saved

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
            for node in _under(fn):
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
            scope = self._binding_scope(node.id, fn)
            if scope is None:
                return None                                   # bound only in a scope that does not enclose it
            if scope is not fn:
                key = f"{owner}@{id(scope)}"
                if key not in self._local_types:
                    self._local_types[key] = self._types_in(scope, local, me)
                got = self._local_types[key].get(node.id)
                return (got, False) if got else None
            if owner not in self._local_types:
                self._local_types[owner] = self._types_in(fn, local, me, owner)
            got = self._local_types[owner].get(node.id)
            return (got, False) if got else None
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and me and node.value.id == me[0]:
                base: tuple[str, bool] | None = None if me[1] in self._untrusted else (me[1], False)
            else:                                             # ctx.billing.refund(): ctx's class, then billing's
                base = self._receiver(node.value, owner, local, me)
            if base is None or base[0] not in self.idx.classes or node.attr in self._foreign_attrs() \
                    or self._dict_written(base[0]):
                return None
            got = self._attr_types(base[0]).get(node.attr)
            return (got, False) if got else None
        return None

    def _binding_scope(self, name: str, fn: ast.AST) -> ast.AST | None:
        """The scope a name used in the code being walked is looked up in: the innermost of the nested
        functions, lambdas and classes around it that binds it (a class body only when the name is used
        directly in it), else the owner fn when fn binds it, else None (a global, or a name bound only in
        a nested scope that does not enclose this code)."""
        for i in range(len(self._scopes) - 1, -1, -1):
            s = self._scopes[i]
            if isinstance(s, ast.ClassDef) and i != len(self._scopes) - 1:
                continue                                       # a class body does not enclose its methods
            if name in self._binds(s):
                return s
        return fn if name in self._binds(fn) else None

    def _binds(self, scope: ast.AST) -> set[str]:
        got = self._scope_names.get(id(scope))
        if got is None:
            got = self._scope_names[id(scope)] = _scope_binds(scope)
        return got

    def _foreign_attrs(self) -> set[str]:
        """Attribute names some code sets from outside the object (`obj.x = ...`, `setattr(obj, "x", ...)`):
        such an attribute can hold anything, so it is never given a type."""
        if self.idx.foreign is None:
            self.idx.foreign = set().union(*(_set_from_outside(self.idx, mod) for mod in self.idx.modules.values()))
        return self.idx.foreign

    def _dict_written(self, cls: str) -> bool:
        """A method of cls, of a base or of a subclass (self may be any of them) writes names the scan cannot
        read into its own `self.__dict__` (`self.__dict__.update(kw)`, `d = self.__dict__; d[k] = v`): no
        attribute of self has a type."""
        marked = _DICT_WRITTEN.get(self.idx)
        if marked is None:
            marked = _DICT_WRITTEN[self.idx] = frozenset(x[:-2] for x in self._foreign_attrs() if x.endswith(":*"))
        return bool(marked) and any(c in marked for c in [*(self.idx.mro(cls) or [cls]), *self.idx.subclasses(cls)])

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
        if self.idx.excluded_subclass(set(family)) or start in self._untrusted or self._untrusted & set(family) \
                or any((c, node.attr) in self._untrusted_members for c in family):
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
        if rcv is None or rcv[0] in self._untrusted or (rcv[0], node.attr) in self._untrusted_members:
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
        """`self.repo.m` where repo is a method or property of a scanned class, or `mod.repo.m` /
        `Cls.repo.m` where repo is a data attribute of a scanned module or class (`repo = Repo()`,
        `Holder.r = Repo()`): its name resolves through a value, so the receiver's type decides what m is."""
        if not _dotted(node.value):
            return False
        fq = self._resolve(node.value, local, me)
        return fq in self.idx.funcs or (fq is not None and self._data_attr(fq))

    def _data_attr(self, fq: str) -> bool:
        """fq is a name inside a scanned module or class that is not a function, class or module: a value."""
        if fq in self.idx.funcs or fq in self.idx.classes or fq in self.idx.modules or fq.startswith("self:"):
            return False
        parts = fq.split(".")
        return any(".".join(parts[:i]) in self.idx.modules or ".".join(parts[:i]) in self.idx.classes
                   for i in range(len(parts) - 1, 0, -1))

    def _value_refs(self, node: ast.AST, owner: str, local: set[str], me: tuple[str, str] | None) -> list[str]:
        """The scanned functions a Name/Attribute expression names when it is used as a value."""
        if not isinstance(node, (ast.Name, ast.Attribute)):
            return []
        fq = self._resolve(node, local, me) if _dotted(node) else None
        alts = [a for a in self._alternatives(node, local, me) if a != fq and a in self.idx.funcs]
        if fq and fq in self.idx.funcs:
            return [fq, *alts]
        if isinstance(node, ast.Attribute) and (fq is None or self._via_function(node, local, me)):
            return self._typed(node, owner, local, me)[0]
        return alts

    def _alternatives(self, node: ast.AST, local: set[str], me: tuple[str, str] | None) -> frozenset[str]:
        """The other objects a module-level name may be bound to (alternatives()), for a plain name that is
        not a local, a nested function's parameter or self."""
        if not isinstance(node, ast.Name) or node.id in local or node.id in self._shadow or (me and node.id == me[0]):
            return frozenset()
        return alternatives(self.idx, self.mod).get(node.id, frozenset())

    def _subclasses_overriding(self, cls: str, name: str) -> list[str]:
        out = []
        # only the classes that define a method of that name (the index by name), not every class
        for c in {self.idx.funcs[m].cls for m in self.idx.methods_by_name.get(name, ())}:
            info = self.idx.classes.get(c) if c is not None else None
            if c is not None and info is not None and c != cls and name in info["methods"] \
                    and self._inherits(c, cls, set()):
                out.append(info["methods"][name])
        return sorted(out)

    def _inherits(self, c: str, base: str, seen: set[str]) -> bool:
        if c in seen or c not in self.idx.classes:
            return False
        seen.add(c)
        return any(fq == base or self._inherits(fq, base, seen) for fq in self.idx.bases(c))

    def _resolve(self, node: ast.AST, local: set[str], me: tuple[str, str] | None, raw: bool = False) -> str | None:
        """The canonical name a Name/Attribute chain refers to, or None when it is a runtime value. A name
        through a class the code subverts (or a class name bound again) is a runtime value too, unless
        raw: the name as written (what an assignment over it replaces)."""
        raw = raw or self._raw
        if isinstance(node, ast.Name):
            if me and node.id == me[0]:
                return f"self:{me[1]}"
            if node.id in local:
                return None
            if node.id in self._rebound_classes and not raw:
                return None                                    # a class name bound again: which one is it?
            hit = self.mod.aliases.get(node.id) or self.idx._star_lookup(self.mod, node.id, 0)
            if hit:
                return self.idx.canon(hit)
            return f"builtins.{node.id}" if node.id in _BUILTINS else None
        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value, local, me, raw)
            if base is None:
                return None
            cls = base[5:] if base.startswith("self:") else base
            if (cls in self._untrusted or (cls, node.attr) in self._untrusted_members) and not raw:
                return None                                    # Safe.save on a class the code subverts
            if base.startswith("self:"):
                return self.idx.method(cls, node.attr)
            return self.idx.canon(f"{base}.{node.attr}")
        return None

    def _config_strings(self, node: ast.AST, local: set[str], me: tuple[str, str] | None,
                        depth: int) -> list[str] | None:
        """Every string in a configuration value the scan can read: literals (dicts, lists, tuples) and
        module-level names bound once to one, in the module that binds them; a name outside the scanned
        code (`logging.INFO`, `sys.stdout`) holds no name to look up. None when any part is unknown (a
        call, a local, `**rest`)."""
        if depth > 8:
            return None
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) else []
        if isinstance(node, ast.Dict):
            if any(k is None for k in node.keys):
                return None
            parts: list[ast.AST] = [*[k for k in node.keys if k is not None], *node.values]
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            parts = list(node.elts)
        elif _dotted(node):
            fq = self._resolve(node, local, me)
            if fq is None and isinstance(node, ast.Name) and node.id not in local:
                fq = f"{self.mod.name}.{node.id}"              # a module-level name of this module
            if fq is None:
                return None
            mod, _, name = fq.rpartition(".")
            if mod in self.idx.modules and (value := self.idx.module_values(mod).get(name)) is not None:
                return self._home(mod)._config_strings(value, set(), None, depth + 1)
            tops = {m.split(".")[0] for m in self.idx.modules}
            return [] if fq.split(".")[0] not in tops and self.idx.excluded_module(fq) is None else None
        else:
            return None
        out: list[str] = []
        for p in parts:
            got = self._config_strings(p, local, me, depth + 1)
            if got is None:
                return None
            out += got
        return out

    def _namespace(self, node: ast.AST, local: set[str], me: tuple[str, str] | None) -> str | None:
        """The module or class a Name/Attribute chain names, else None."""
        if not _dotted(node):
            return None
        fq = self._resolve(node, local, me)
        return fq if fq and (fq in self.idx.modules or fq in self.idx.classes) else None


_WRITE_TYPES = frozenset(WRITES)
_DICT_WRITTEN: weakref.WeakKeyDictionary[_Index, frozenset[str]] = weakref.WeakKeyDictionary()


@dataclasses.dataclass
class _Bound:
    """What _types_in collects in one function: every class each name is bound to (None: unknown), the
    Name nodes a plain binding typed, and the `with recv.cm() as x` left for the fixed point."""

    seen: dict[str, set[str | None]] = dataclasses.field(default_factory=lambda: collections.defaultdict(set))
    handled: set[int] = dataclasses.field(default_factory=set)
    deferred: list[tuple[str, ast.expr, bool]] = dataclasses.field(default_factory=list)


def _unknown_bindings(fn: ast.AST, b: _Bound) -> None:
    """Any other binding in fn is unknown: for, walrus, unpacking, a nested def or class, an import,
    `except E as x`, a match capture, global and nonlocal."""
    for node in _scope_nodes(fn):
        t = type(node)
        if t in _WRITE_TYPES:                                  # never a binder: only its attribute writes
            for obj, name, _ in _attr_writes(node):
                if isinstance(obj, ast.Name) and name in ("__class__", None):
                    b.seen[obj.id].add(None)                   # x.__class__ = Other: x is no longer typed
            continue
        if t not in BINDERS:
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and id(node) not in b.handled:
            b.seen[node.id].add(None)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not fn:
            b.seen[node.name].add(None)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            b.seen[node.name].add(None)                          # except E as err, case Repo() as repo
        elif isinstance(node, ast.MatchMapping) and node.rest:
            b.seen[node.rest].add(None)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                b.seen[(al.asname or al.name).split(".")[0]].add(None)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for n in node.names:
                b.seen[n].add(None)


def _set_from_outside(idx: _Index, mod: _Module) -> set[str]:
    """The attribute names code in one module sets on an object other than a method's own self, and
    "<class>:*" for a class whose method writes names the scan cannot read into self.__dict__."""
    tree = mod.tree
    methods = {id(m) for c in _walk_of(tree, ast.ClassDef) for m in getattr(c, "body", ())
               if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
    owners = {id(idx.funcs[m].node): cid for cid, info in idx.classes.items() if info["module"] == mod.name
              for m in info["methods"].values()}
    out: set[str] = set()
    for fn in _walk_of(tree, ast.FunctionDef, ast.AsyncFunctionDef, ast.Module):
        # only a method's first parameter is the object itself (self); in a plain function,
        # `ctx.repo = other` sets another object's attribute
        first = (fn.args.args[0].arg if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(fn) in methods
                 and fn.args.args else None)
        for node in _own_under(fn):
            if isinstance(node, WRITES):
                out |= _attrs_set(node, first)
        for _, obj in _computed_dict_writes(fn, tree=tree):
            if isinstance(obj, ast.Name) and obj.id == first and id(fn) in owners:
                out.add(f"{owners[id(fn)]}:*")              # self.__dict__.update(kw): any attribute
    return out


def _attrs_set(node: ast.AST, first: str | None) -> set[str]:
    """The attribute names one node sets on an object other than `first` (the method's self): `obj.x = ...`,
    `obj.x += ...`, `del obj.x`, `setattr(obj, "x", ...)`; and on any object, self too, the reflective
    writes (`object.__setattr__(self, "x", v)`, `self.__dict__["x"] = v`), which skip the plain bindings
    the scan types, and `obj.x.__class__ = ...`, which retypes what obj.x holds."""
    out: set[str] = set()
    for obj, name, reflective in _attr_writes(node):
        if name == "__class__" and isinstance(obj, ast.Attribute):
            out.add(obj.attr)
        if name and (reflective or not (isinstance(obj, ast.Name) and obj.id == first)):
            out.add(name)
    return out


# ---- classes whose construction or identity the code can alter: their inferred types are not trusted ----

_SUBVERTED: weakref.WeakKeyDictionary[_Index, tuple[frozenset[str], frozenset[tuple[str, str]]]] = \
    weakref.WeakKeyDictionary()
# dunders a class carries as data: writing one does not change what the class makes or looks up
_DATA_DUNDERS = frozenset({"__doc__", "__module__", "__qualname__", "__name__", "__annotations__",
                           "__signature__", "__text_signature__", "__slots__", "__match_args__"})
# metaclass methods that decide what calling the class, or looking a name up on it, gives
_META_HOOKS = ("__call__", "__new__", "__getattr__", "__getattribute__")
# class decorators known to hand back the class they are given (or one built from the same body, with the
# same methods); any other decorator, or a decorator expression the scan cannot name (a call of anything
# else, a lambda, functools.partial(...), a subscript), may return another class
_KEEPS_CLASS = frozenset({"dataclasses.dataclass", "functools.total_ordering", "typing.final",
                          "typing_extensions.final", "enum.unique", "typing.runtime_checkable",
                          "typing_extensions.runtime_checkable", "typing.type_check_only"})
_PROTOCOL = frozenset({"typing.Protocol", "typing_extensions.Protocol"})
_KEEPS_CLASS_CALLED = frozenset({"dataclasses.dataclass", "enum.verify"})   # @dataclass(frozen=True)


def subverted(idx: _Index) -> frozenset[str]:
    """The scanned classes whose inferred type the scan does not trust (with their subclasses): code can
    make `Cls(...)`, `Cls.m` or an instance's class something else. A custom `__new__` (one that does not
    just return `super().__new__(cls, ...)`), a scanned metaclass with `__call__`, `__new__`,
    `__getattr__` or `__getattribute__` (or one the scan cannot see into), a class decorator that may
    return another class, a module-level name of the class bound more than once (`Safe = Repo`, an
    import over it, `global Safe`), a write of a dunder on the class (`Leaf.__bases__ = ...`) or of a
    computed name (`setattr(Safe, name, v)`), and `self.__class__ = ...` in its methods. A `__class__`
    or `__bases__` write the scan cannot place distrusts every class. Calls on such a receiver are
    name-matched (unresolved), never trusted. (A method replaced or deleted is subverted_members.)"""
    return _subversions(idx)[0]


def subverted_members(idx: _Index) -> frozenset[tuple[str, str]]:
    """(class, method name) for each method code replaces or deletes on a scanned class, for the class
    and its subclasses: `Safe.save = len`, `del Safe.save` (Safe().save is then a base's save),
    `setattr(Safe, "save", v)`, `self.save = v` in a method; a subclass with its own save is not changed.
    A replacement that is a scanned function
    or a lambda is not one: its code is read where it is (an entrypoint, or the sink as a value)."""
    return _subversions(idx)[1]


def _subversions(idx: _Index) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    got = _SUBVERTED.get(idx)
    if got is None:
        direct: set[str] = set()
        members: set[tuple[str, str]] = set()
        everything = False
        for mod in idx.modules.values():
            everything |= _Subversions(idx, mod, direct, members).run()
        classes = frozenset(idx.classes) if everything else \
            frozenset(direct | {s for c in direct for s in idx.subclasses(c)})
        # a subclass that defines (or inherits from elsewhere) its own m is not changed by a write on c
        got = classes, frozenset({(s, m) for c, m in members for s in [c, *idx.subclasses(c)]
                                  if s == c or idx.method(s, m) == idx.method(c, m)})
        _SUBVERTED[idx] = got
    return got


class _Subversions:
    """The classes one module subverts (into `found`); run() is True when it subverts one it cannot place."""

    def __init__(self, idx: _Index, mod: _Module, found: set[str], members: set[tuple[str, str]]) -> None:
        self.idx, self.mod, self.found, self.members = idx, mod, found, members
        self.inf = _Inference(idx, mod, raw=True)
        self.tops = {m.split(".")[0] for m in idx.modules}
        self.everything = False
        self.values: dict[int, ast.expr] = {}                  # assigned attribute target -> its value
        self.methods = {id(self.idx.funcs[m].node): cid for cid, info in idx.classes.items()
                        if info["module"] == mod.name for m in info["methods"].values()}

    def _fq(self, node: ast.AST) -> str | None:
        return self.inf._resolve(node, set(), None) if _dotted(node) else None

    def run(self) -> bool:
        for cid, info in self.idx.classes.items():
            if info["module"] == self.mod.name and self._altered(info["node"]):
                self.found.add(cid)
        rebound = rebound_class_names(self.idx, self.mod)
        for name in rebound:
            if f"{self.mod.name}.{name}" in self.idx.classes:  # class Safe, then Safe = Repo
                self.found.add(f"{self.mod.name}.{name}")
        for cid, info in self.idx.classes.items():             # class Leaf(Safe) on a rebound name
            if info["module"] == self.mod.name and any(
                    (_dotted(b.value if isinstance(b, ast.Subscript) else b) or ".").split(".")[0] in rebound
                    for b in info["bases"]):
                self.found.add(cid)
            # a Protocol is any class with the right methods, and a type checker accepts any of them where
            # it is annotated: its own methods say nothing about what runs
            if info["module"] == self.mod.name and set(self.idx.bases(cid)) & _PROTOCOL:
                self.found.add(cid)
        for node in _walk_of(self.mod.tree, ast.Call):         # Safe.register(Repo): a virtual subclass
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "register" \
                    and node.args and (fq := self._fq(node.func.value)) in self.idx.classes \
                    and self.idx.method(fq, "register") is None:     # ABCMeta's register, not its own
                self.found.add(fq)
        self._writes_all(self.mod.tree)
        return self.everything

    def _altered(self, node: ast.ClassDef) -> bool:
        """The class statement itself lets the class be something else: __new__, metaclass, decorator."""
        new = next((st for st in node.body if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and st.name == "__new__"), None)
        if new is not None and not _returns_made(new, "__new__"):
            return True
        # every attribute of an instance comes from its own __getattribute__ (a __getattr__ only answers for
        # names the class lacks, which the scan never resolves to a method)
        if any(isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and st.name == "__getattribute__"
               for st in node.body):
            return True                                       # its instances' attributes come from that hook
        for k in node.keywords:
            if k.arg == "metaclass" and not self._plain_meta(k.value):
                return True
        return not all(self._keeps_class(d) for d in node.decorator_list)

    def _foreign(self, fq: str | None) -> bool:
        """fq names something outside the scanned code: the standard library, builtins or a third-party
        package (not an excluded module, nor an unknown name in a scanned one)."""
        return fq is not None and fq not in self.idx.funcs and fq not in self.idx.classes \
            and fq not in self.idx.modules and self.idx.excluded_module(fq) is None \
            and fq.split(".")[0] not in self.tops

    def _plain_meta(self, node: ast.AST) -> bool:
        """A metaclass that makes classes as `type` does: a foreign one, or a scanned one whose hooks
        (through its scanned bases) only hand on to `super()` / `type`."""
        fq = self._fq(node)
        if fq not in self.idx.classes:
            return self._foreign(fq)
        for c in self.idx.mro(fq) or [fq]:
            for name in _META_HOOKS:
                m = self.idx.classes[c]["methods"].get(name)
                fn = self.idx.funcs[m].node if m else None
                if fn is not None and not (name in ("__call__", "__new__") and _returns_made(fn, name)):
                    return False
        return all(b is not None and (b in self.idx.classes or self._foreign(b))
                   for c in self.idx.mro(fq) or [fq] for b in self.idx._base_names(c))

    def _keeps_class(self, d: ast.expr) -> bool:
        """A class decorator that returns the class it is given: one of the known standard-library ones
        (`@dataclass`, `@dataclass(...)`, `@functools.total_ordering`, ...), a scanned function every
        return of which gives back its parameter, never rebound (`@register`), or a scanned factory whose
        every return is such a nested function (`@register("name")`). Any other decorator (third-party,
        excluded, a lambda, `functools.partial(...)`) may return another class."""
        f = d.func if isinstance(d, ast.Call) else d
        fq = self._fq(f)
        if fq in self.idx.funcs:
            fn = self.idx.funcs[fq].node
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return False
            return _factory_of_identity(fn) if isinstance(d, ast.Call) else _returns_param(fn)
        return fq in (_KEEPS_CLASS_CALLED if isinstance(d, ast.Call) else _KEEPS_CLASS)

    def _writes_all(self, tree: ast.AST) -> None:
        """_writes(tree, {}), read from the numbered tree: each node in depth-first order, with the first
        parameters of the methods around it (each write is judged alone, so the order does not matter)."""
        span = tree.__dict__.get("_inv_span")
        if span is None:
            self._writes(tree, {})
            return
        order, i, end = span
        me: dict[str, str] = {}
        around: list[tuple[int, dict[str, str]]] = []     # (where a method's span ends, me inside it)
        for k in range(i + 1, end):
            while around and k >= around[-1][0]:
                around.pop()
                me = around[-1][1] if around else {}
            child = order[k]
            inner = me
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cls = self.methods.get(id(child))
                inner = {**me, child.args.args[0].arg: cls} if cls and child.args.args else me
            elif isinstance(child, (ast.Assign, ast.AnnAssign)) and child.value is not None:
                for t in child.targets if isinstance(child, ast.Assign) else [child.target]:
                    self.values[id(t)] = child.value
            if isinstance(child, WRITES):
                value = self.values.get(id(child)) if isinstance(child, ast.Attribute) else \
                    child.args[2] if isinstance(child, ast.Call) and len(child.args) > 2 \
                    and _dotted(child.func) in ("setattr", "builtins.setattr") else None
                for obj, name, _ in _attr_writes(child):
                    self._write(obj, name, me, value)
            if inner is not me:
                around.append((child.__dict__["_inv_span"][2], inner))
                me = inner

    def _writes(self, node: ast.AST, me: dict[str, str]) -> None:
        """Writes that alter a class, walked with the enclosing methods' first parameters (me: name -> class)."""
        for child in ast.iter_child_nodes(node):
            inner = me
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cls = self.methods.get(id(child))
                inner = {**me, child.args.args[0].arg: cls} if cls and child.args.args else me
            elif isinstance(child, (ast.Assign, ast.AnnAssign)) and child.value is not None:
                for t in child.targets if isinstance(child, ast.Assign) else [child.target]:
                    self.values[id(t)] = child.value
            value = self.values.get(id(child)) if isinstance(child, ast.Attribute) else \
                child.args[2] if isinstance(child, ast.Call) and len(child.args) > 2 \
                and _dotted(child.func) in ("setattr", "builtins.setattr") else None
            for obj, name, _ in (_attr_writes(child) if isinstance(child, WRITES) else ()):
                self._write(obj, name, me, value)
            self._writes(child, inner)

    def _write(self, obj: ast.expr, name: str | None, me: dict[str, str], value: ast.expr | None = None) -> None:
        cls = me.get(obj.id) if isinstance(obj, ast.Name) else None
        fq = cls or self._fq(obj)
        if name == "__class__":
            if fq in self.idx.classes:
                self.found.add(fq)                             # self.__class__ = Other, Safe.__class__ = M
            elif not isinstance(obj, (ast.Name, ast.Attribute)):
                self.everything = True                         # f().__class__ = Other: which object?
            return                                             # a local or attribute: untyped where it is
        if fq in self.idx.classes and name and not name.startswith("__") and (
                isinstance(value, ast.Lambda) or (value is not None and self._fq(value) in self.idx.funcs)):
            return          # Repo.save = fake: a function the scan reads (an entrypoint, or the sink as a value)
        if fq in self.idx.classes:
            # a dunder that shapes the class (Leaf.__bases__ = ...) or a computed name, written on the
            # class: the class; a method of it (or an override in a subclass), written on the class or on
            # self (an instance attribute shadows the method): that method; a plain data attribute
            # (self.x = v, Cls.count += 1) is neither
            dunder = name is not None and name.startswith("__") and name.endswith("__")
            if not cls and (name is None or dunder and name not in _DATA_DUNDERS):
                self.found.add(fq)
            elif name is not None and not dunder and (
                    self.idx.method(fq, name) or self.inf._subclasses_overriding(fq, name)):
                self.members.add((fq, name))
        elif fq in self.idx.modules:
            for cid in self.idx.classes:                       # ops.Safe = Repo, setattr(ops, name, v)
                if cid.rpartition(".")[0] == fq and (name is None or cid == f"{fq}.{name}"):
                    self.found.add(cid)
        elif name == "__bases__" and fq is None:
            self.everything = True                             # x.__bases__ = ...: which class?


_REBOUND: weakref.WeakKeyDictionary[_Index, dict[str, frozenset[str]]] = weakref.WeakKeyDictionary()
_ALTERNATIVES: weakref.WeakKeyDictionary[_Index, dict[str, dict[str, frozenset[str]]]] = weakref.WeakKeyDictionary()


def alternatives(idx: _Index, mod: _Module) -> dict[str, frozenset[str]]:
    """Names in mod that its imports, top-level defs and star imports bind to more than one object, at least
    one of them a scanned function or class: `if x: from payments import _send as refund / else: from
    payments import refund`, try/except ImportError, or `from shadow import *` re-exporting another
    function under a name the module imports. The scan resolves such a name to one of them; a call or a
    value use of it may be any, so each scanned one counts (by the object, not by the local name)."""
    per = _ALTERNATIVES.setdefault(idx, {})
    if mod.name in per:
        return per[mod.name]
    cands: collections.defaultdict[str, set[str]] = collections.defaultdict(set)
    for node in _walk_of(mod.tree, ast.Import, ast.ImportFrom):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    cands[a.asname].add(idx.canon(idx.real(a.name)))
        elif isinstance(node, ast.ImportFrom):
            src = idx._absolute(mod, node)
            for a in node.names:
                if a.name != "*":
                    cands[a.asname or a.name].add(idx.canon(f"{src}.{a.name}" if src else a.name))
    for st in mod.tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            cands[st.name].add(f"{mod.name}.{st.name}")
    for src in mod.star:                                       # a star import may rebind a name bound above
        for name in _exported(idx, src) & set(cands):
            cands[name].add(idx.canon(f"{src}.{name}"))
    scanned = {*idx.funcs, *idx.classes}
    per[mod.name] = out = {k: frozenset(v) for k, v in cands.items() if len(v) > 1 and v & scanned}
    return out


def _exported(idx: _Index, src: str) -> set[str]:
    """The names `from src import *` binds: src's `__all__` when it is a literal list or tuple, else its
    public names (imports and definitions)."""
    mod = idx.modules.get(src)
    if mod is None:
        return set()
    for st in mod.tree.body:
        if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in st.targets) \
                and isinstance(st.value, (ast.List, ast.Tuple)):
            return {x for e in st.value.elts if (x := e.value if isinstance(e, ast.Constant) else None)
                    and isinstance(x, str)}
    return {n for n in mod.aliases if not n.startswith("_")}


def rebound_class_names(idx: _Index, mod: _Module) -> frozenset[str]:
    """The names in mod that name a scanned class but are bound more than once at module scope (`class
    Safe` then `Safe = Repo`, `from a import Safe` then `Safe = Repo`, `global Safe`): the scan cannot
    tell which binding a use sees, so it resolves them to nothing (a runtime value)."""
    per = _REBOUND.setdefault(idx, {})
    if mod.name not in per:
        raw = _Inference(idx, mod, raw=True)
        per[mod.name] = frozenset(
            name for name, n in _module_binds(mod.tree).items() if n > 1 and (
                raw._resolve(ast.Name(name, ast.Load()), set(), None) in idx.classes
                or f"{mod.name}.{name}" in idx.classes))
    return per[mod.name]


def _module_binds(tree: ast.Module) -> collections.Counter[str]:
    """How many times each name is bound at module scope (`global name` in a function counts too)."""
    binds: collections.Counter[str] = collections.Counter()
    for n in _own_nodes(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            binds[n.id] += 1
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            binds[n.name] += 1
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            binds.update((a.asname or a.name).split(".")[0] for a in n.names if a.name != "*")
        elif isinstance(n, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and n.name:
            binds[n.name] += 1
    for n in _walk_of(tree, ast.Global):
        if isinstance(n, ast.Global):
            binds.update({x: 2 for x in n.names})
    return binds


def _returns_made(fn: ast.AST, hook: str) -> bool:
    """Every return of a `__new__` (or metaclass `__call__`) is `super().<hook>(...)` or `X.<hook>(cls, ...)`
    on its own first parameter, which fn never rebinds: it gives what the class would have made anyway. A
    `__new__` must pass that first parameter on (`super().__new__(cls)`, not a keyword default `cls=Repo`),
    and a `super(...)` with arguments must name it."""
    args = getattr(fn, "args", None)
    first = args.args[0].arg if isinstance(args, ast.arguments) and args.args else None
    if first is None or _rebinds(fn, first):
        return False
    for node in _own_nodes(fn):
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return False
        if isinstance(node, ast.Return) and node.value is not None:
            v = node.value
            if not (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == hook):
                return False
            passes = bool(v.args) and isinstance(v.args[0], ast.Name) and v.args[0].id == first
            sup = v.func.value
            if isinstance(sup, ast.Call) and _dotted(sup.func) == "super":
                if sup.args and not (len(sup.args) == 2 and isinstance(sup.args[1], ast.Name)
                                     and sup.args[1].id == first):
                    return False                              # super(Safe, Repo).__new__(...)
                if hook == "__new__" and not passes:
                    return False                              # super().__new__(other)
            elif not passes:
                return False
    return True


def _rebinds(fn: ast.AST, name: str) -> bool:
    """fn (or a function nested in it) binds name again: an assignment, a for/with/except/match target,
    `del`, an import, or a nonlocal/global declaration of it."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del)):
            return True
        if isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            return True
        if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name == name:
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                (a.asname or a.name).split(".")[0] == name for a in node.names):
            return True
    return False


def _returns_param(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Every return of fn gives back its first parameter (a decorator that registers the class)."""
    if not fn.args.args or isinstance(fn, ast.AsyncFunctionDef):
        return False
    first = fn.args.args[0].arg
    if _rebinds(fn, first):
        return False                                          # c = Repo; return c
    rets = [n for n in _own_nodes(fn) if isinstance(n, ast.Return)]
    return bool(rets) and all(isinstance(r.value, ast.Name) and r.value.id == first for r in rets) and \
        not any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _own_nodes(fn))


def _factory_of_identity(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Every return of fn gives a function defined in it that returns its parameter (`@register("x")`)."""
    inner = {n.name: n for n in fn.body if isinstance(n, ast.FunctionDef)}
    rets = [n for n in _own_nodes(fn) if isinstance(n, ast.Return)]
    return bool(rets) and all(isinstance(r.value, ast.Name) and r.value.id in inner
                              and _returns_param(inner[r.value.id]) for r in rets)
