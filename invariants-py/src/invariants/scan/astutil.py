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
