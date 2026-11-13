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


def _fold_str(node: ast.AST) -> str | None:
    """'INSERT INTO ' + 'leases ...' (or + name) as one text, a non-literal part as '{}'; None if no literal."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _fold_str(node.left), _fold_str(node.right)
        if a is None and b is None:
            return None
        return (a if a is not None else "{}") + (b if b is not None else "{}")
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(str(v.value) if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def _add_leaves(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [*_add_leaves(node.left), *_add_leaves(node.right)]
    return [node]


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


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
    `o.__dict__.update(x=v)` / `.setdefault("x", v)` (reflective: they write past any class-level rule)."""
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
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
            if args or any(n is None for n in names):
                names.append(None)                                      # update(mapping), update(**kw)
            return [(obj, n, True) for n in names]
        return [(obj, _lit(args[0]) if args else None, True)]
    return []
