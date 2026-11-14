"""Small pure helpers over `ast` nodes, shared by the index and the walker."""
from __future__ import annotations

import ast
from typing import Iterator


def _dotted(node: ast.AST) -> str | None:
    """'a.b.c' for a plain Name/Attribute chain, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return None


_HARMLESS_DUNDERS = frozenset({"__name__", "__qualname__", "__module__", "__doc__", "__annotations__",
                               "__dict__", "__defaults__", "__kwdefaults__", "__signature__", "__text_signature__"})


def _dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _locals(fn: ast.AST) -> set[str]:
    """Parameters and assigned names in a function, including nested scopes (an over-approximation)."""
    names: set[str] = set()
    glob: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.arguments):
            for a in [*node.posonlyargs, *node.args, *node.kwonlyargs, node.vararg, node.kwarg]:
                if a is not None:
                    names.add(a.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            glob.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not fn:
            names.add(node.name)
    return names - glob


def _params(args: ast.arguments) -> list[str]:
    """The names a function or lambda takes as parameters."""
    return [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg] if a is not None]


def _own_nodes(fn: ast.AST) -> Iterator[ast.AST]:
    """The nodes of a function (or module) body, not descending into nested functions or classes."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _scope_nodes(fn: ast.AST, nested_effects: bool = True) -> list[ast.AST]:
    """The nodes of one scope (a function, lambda, class or module body), not the bodies of the scopes
    nested in it. nested_effects: also what nested code binds in this scope, a walrus inside a nested
    lambda (with its target) and a nested function's `nonlocal` declarations (they rebind names here)."""
    out: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        out.append(node)
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))
        elif nested_effects:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Nonlocal):
                    out.append(inner)
                elif isinstance(inner, ast.NamedExpr) and isinstance(node, ast.Lambda):
                    out.extend([inner, *ast.walk(inner.target)])
    return out


def _scope_binds(fn: ast.AST) -> set[str]:
    """The names one scope binds (parameters, assignments and other targets, defs, imports), less those it
    declares global or nonlocal: where a name used in it, or in a scope nested in it, is looked up."""
    args = getattr(fn, "args", None)
    names: set[str] = set(_params(args)) if isinstance(args, ast.arguments) else set()
    declared: set[str] = set()
    for node in _scope_nodes(fn, nested_effects=False):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
    return names - declared


def _pairs(node: ast.Assign) -> list[tuple[ast.expr, ast.expr | None]]:
    """(target, value) pairs of an assignment; `a, b = x, y` pairs element-wise, other unpacking has no
    value (None), so what it binds is unknown."""
    out: list[tuple[ast.expr, ast.expr | None]] = []
    for t in node.targets:
        if isinstance(t, (ast.Tuple, ast.List)):
            v = node.value
            if isinstance(v, (ast.Tuple, ast.List)) and len(v.elts) == len(t.elts) and \
                    not any(isinstance(e, ast.Starred) for e in [*t.elts, *v.elts]):
                out += list(zip(t.elts, v.elts))
            else:
                out.append((t, None))
        else:
            out.append((t, node.value))
    return out


def _single_types(seen: dict[str, set[str | None]]) -> dict[str, str]:
    """The names bound to exactly one known class (no unknown binding among them)."""
    out: dict[str, str] = {}
    for name, classes in seen.items():
        only = next(iter(classes)) if len(classes) == 1 else None
        if only is not None:
            out[name] = only
    return out


def _lit(node: ast.AST | None) -> str | None:
    """The text of a string literal, else None."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _attr_writes(node: ast.AST) -> list[tuple[ast.expr, str | None, bool]]:
    """The attribute writes one node makes, as (object, attribute name or None when computed, reflective):
    `o.x = v` / `del o.x` / `for o.x in ...` (not reflective), and `setattr(o, "x", v)`, `delattr`,
    `object.__setattr__(o, "x", v)`, `o.__setattr__("x", v)`, `o.__dict__["x"] = v` and
    `o.__dict__.update(x=v)` / `.update({"x": v})` / `.setdefault("x", v)` and `o.__dict__ |= {...}`
    (reflective: they write past any class-level rule). A name the scan cannot read (`update(mapping)`,
    `o.__dict__[k] = v`, `o.__dict__ |= m`) is None."""
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
        if node.attr == "__dict__":                                     # o.__dict__ = m, o.__dict__ |= m
            return [(node.value, None, True)]
        return [(node.value, node.attr, False)]
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)) \
            and isinstance(node.value, ast.Attribute) and node.value.attr == "__dict__":
        return [(node.value.value, _lit(node.slice), True)]
    if not isinstance(node, ast.Call):
        return []
    f, args = node.func, node.args
    if _dotted(f) in ("setattr", "delattr", "builtins.setattr", "builtins.delattr") and args:
        return [(args[0], _lit(args[1]) if len(args) > 1 else None, True)]
    if isinstance(f, ast.Attribute) and f.attr in ("__setattr__", "__delattr__"):
        if len(args) >= (3 if f.attr == "__setattr__" else 2):          # object.__setattr__(o, "x", v)
            return [(args[0], _lit(args[1]), True)]
        return [(f.value, _lit(args[0]) if args else None, True)]       # o.__setattr__("x", v)
    if isinstance(f, ast.Attribute) and f.attr in ("update", "setdefault", "__setitem__") \
            and isinstance(f.value, ast.Attribute) and f.value.attr == "__dict__":
        obj = f.value.value
        if f.attr == "update":
            names: list[str | None] = [k.arg for k in node.keywords]
            for a in args:
                names += _dict_keys(a)                                  # update({"x": v}), update(mapping)
            return [(obj, n, True) for n in names]
        return [(obj, _lit(args[0]) if args else None, True)]
    return []


def _dict_keys(node: ast.AST) -> list[str | None]:
    """The keys a mapping argument writes: each literal key of a dict literal (`{"x": v}`), with None for
    any key the scan cannot read (`**m`, a computed key) or for any other mapping."""
    if not isinstance(node, ast.Dict):
        return [None]
    return [_lit(k) for k in node.keys] if node.keys else []


_DICT_WRITERS = frozenset({"update", "setdefault", "__setitem__", "__delitem__", "__ior__", "pop", "popitem",
                           "clear"})


def _dict_aliases(fn: ast.AST, nested: bool = False) -> list[tuple[ast.AST, ast.expr]]:
    """`d = o.__dict__` (or `vars(o)`) in a function or module body, with d then written there (`d[k] = v`,
    `del d[k]`, `d.update(...)`, `d.setdefault(...)`, `d |= m`): (the binding, o) for each, a write on o
    of names the scan cannot read. nested: the nested functions' bodies too."""
    nodes = list(ast.walk(fn) if nested else _own_nodes(fn))
    bound: dict[str, list[tuple[ast.AST, ast.expr]]] = {}
    for node in nodes:
        targets: list[ast.expr] = list(node.targets) if isinstance(node, ast.Assign) else \
            [node.target] if isinstance(node, (ast.AnnAssign, ast.NamedExpr)) else []
        v = getattr(node, "value", None) if targets else None
        o = v.value if isinstance(v, ast.Attribute) and v.attr == "__dict__" else \
            v.args[0] if isinstance(v, ast.Call) and _dotted(v.func) in ("vars", "builtins.vars") and v.args else None
        for t in targets:
            if o is not None and isinstance(t, ast.Name):
                bound.setdefault(t.id, []).append((node, o))
    written: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)) \
                and isinstance(node.value, ast.Name):
            written.add(node.value.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in _DICT_WRITERS and isinstance(node.func.value, ast.Name):
            written.add(node.func.value.id)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            written.add(node.target.id)
    return [pair for name, pairs in bound.items() if name in written for pair in pairs]


def _computed_dict_writes(fn: ast.AST, nested: bool = False) -> list[tuple[ast.AST, ast.expr]]:
    """(node, o) for each write through `o.__dict__` whose names the scan cannot read: `o.__dict__[k] = v`,
    `o.__dict__.update(mapping)`, `o.__dict__ |= m`, and a written alias of `o.__dict__` or `vars(o)`."""
    out: list[tuple[ast.AST, ast.expr]] = list(_dict_aliases(fn, nested))
    for node in (ast.walk(fn) if nested else _own_nodes(fn)):
        dict_form = (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                     and node.value.attr == "__dict__") or (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "__dict__") or (
            isinstance(node, ast.Attribute) and node.attr == "__dict__")
        if dict_form:
            out += [(node, obj) for obj, name, _ in _attr_writes(node) if name is None]
    return out
