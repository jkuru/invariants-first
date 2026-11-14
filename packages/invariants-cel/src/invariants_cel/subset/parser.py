"""The recursive-descent parser: rule text to a tuple tree (Node), with the load-time inv.* shape checks.

Trees: ("lit", v), ("id", name), ("sel", e, field), ("idx", e, i), ("call", name, receiver or None,
[args]), ("list", [e]), ("map", [(k, v)]), ("un", op, e), ("bin", op, l, r), ("cond", c, a, b).
"""
from __future__ import annotations

from typing import Any

from ..errors import CelSyntaxError
from .catalog import _MACROS, INV_ARITY
from .lexer import INT_MAX, INT_MIN, tokenize

Node = tuple

# How deep a clause may nest. Each nesting (a parenthesis, bracket, call argument, ?: branch or unary
# operator) costs the parser about ten Python frames, and every node of the tree (a long && or +
# chain is a deep tree too) costs each walker over it a few: past these the clause is refused with a
# CelSyntaxError (a LoadError naming the rule), never a RecursionError. cel-expr's own limit is
# lower (32 levels: expr_python.RECURSION_LIMIT).
MAX_NESTING = 64
MAX_TREE_DEPTH = 256


class _Parser:
    _LEVELS = [("||",), ("&&",), ("==", "!=", "<", "<=", ">", ">=", "in"), ("+", "-"), ("*", "/", "%")]

    def __init__(self, src: str) -> None:
        self.t, self.i, self.depth = tokenize(src), 0, 0

    def peek(self, v: str | None = None) -> Any:
        k, x = self.t[self.i]
        return (k == "op" and x == v) if v else (k, x)

    def eat(self, v: str) -> None:
        if not self.peek(v):
            raise CelSyntaxError(f"expected {v!r}, got {self.t[self.i][1]!r}")
        self.i += 1

    def parse(self) -> Node:
        e = self.expr()
        if self.t[self.i][0] != "end":
            raise CelSyntaxError(f"trailing input at {self.t[self.i][1]!r}")
        if tree_depth(e) > MAX_TREE_DEPTH:
            raise CelSyntaxError(f"the clause is more than {MAX_TREE_DEPTH} levels deep as a tree (a very long "
                                 f"&&, ||, + or field chain, say); split it into several clauses")
        return e

    def nest(self) -> None:
        self.depth += 1
        if self.depth > MAX_NESTING:
            raise CelSyntaxError(f"the clause is nested more than {MAX_NESTING} levels deep (parentheses, "
                                 f"brackets, calls, ?: or unary operators); split it into smaller expressions")

    def expr(self) -> Node:
        self.nest()
        c = self.binary(0)
        if self.peek("?"):
            self.eat("?")
            a = self.binary(0)              # CEL: cond ? conditionalOr : expr (a ?: in the middle needs ())
            self.eat(":")
            c = ("cond", c, a, self.expr())
        self.depth -= 1
        return c

    def binary(self, lvl: int) -> Node:
        if lvl == len(self._LEVELS):
            return self.unary()
        left = self.binary(lvl + 1)
        while True:
            k, x = self.peek()
            op = x if k == "op" else ("in" if (k, x) == ("id", "in") else None)
            if op not in self._LEVELS[lvl]:
                return left
            self.i += 1
            left = ("bin", op, left, self.binary(lvl + 1))

    def unary(self, run: bool = False) -> Node:
        if self.peek("!") or self.peek("-"):
            self.nest()
            e = self._unary(run)
            self.depth -= 1
            return e
        return self.member()

    def _unary(self, run: bool) -> Node:
        if self.peek("!"):
            self.i += 1
            return ("un", "!", self.unary())
        if self.peek("-"):
            self.i += 1
            # CEL's grammar reads a lone minus sign on an int literal as a negative literal, so
            # -9223372036854775808 is INT_MIN (bound before any .f or [i]); after two or more
            # minus signs (--9223372036854775808) every one is an operator and the literal overflows.
            if not run and self.peek() == ("int", -INT_MIN):
                self.i += 1
                return self.member(("lit", INT_MIN))
            return ("un", "-", self.unary(run=True))
        raise AssertionError("unreachable")

    def args(self, close: str) -> list[Node]:
        out = []
        while not self.peek(close):
            out.append(self.expr())
            if not self.peek(close):
                self.eat(",")
        self.eat(close)
        return out

    def member(self, e: Node | None = None) -> Node:
        e = self.primary() if e is None else e
        while True:
            if self.peek("."):
                self.i += 1
                k, name = self.peek()
                if k != "id":
                    raise CelSyntaxError("expected field name after '.'")
                self.i += 1
                if self.peek("("):
                    self.i += 1
                    a = self.args(")")
                    if name == "has" and e != ("id", "inv"):
                        raise CelSyntaxError("has() has only the global form: has(a.b)")
                    e = ("call", "inv." + name, None, a) if e == ("id", "inv") else ("call", name, e, a)
                    if name in _MACROS and (len(a) != 2 or a[0][0] != "id"):
                        raise CelSyntaxError(f"{name}() needs (var, expr)")
                    _check_inv(e)
                else:
                    e = ("sel", e, name)
            elif self.peek("["):
                self.i += 1
                ix = self.expr()
                self.eat("]")
                e = ("idx", e, ix)
            else:
                return e

    def primary(self) -> Node:
        k, x = self.peek()
        self.i += 1
        if k == "lit":
            return ("lit", x)
        if k == "int":
            if x > INT_MAX:
                raise CelSyntaxError(f"invalid int literal {x}: outside int64")
            return ("lit", x)
        if k == "uint":                         # 5u is uint(5): the same tree, the same checks
            return ("call", "uint", None, [("lit", x)])
        if k == "id":
            if self.peek("("):
                self.i += 1
                a = self.args(")")
                if x == "has" and (len(a) != 1 or a[0][0] != "sel"):
                    raise CelSyntaxError("has() takes a field selection, e.g. has(a.b)")
                if x in _MACROS:                 # the macros have only the receiver form
                    raise CelSyntaxError(f"{x}() is a macro: write list.{x}(var, predicate)")
                return ("call", x, None, a)
            return ("id", x)
        if (k, x) == ("op", "("):
            e = self.expr()
            self.eat(")")
            return e
        if (k, x) == ("op", "["):
            return ("list", self.args("]"))
        if (k, x) == ("op", "{"):
            items = []
            while not self.peek("}"):
                key = self.expr()
                self.eat(":")
                items.append((key, self.expr()))
                if not self.peek("}"):
                    self.eat(",")
            self.eat("}")
            return ("map", items)
        raise CelSyntaxError(f"unexpected {x!r}")


def tree_depth(e: Node) -> int:
    """How many levels deep the tree is, found without recursion."""
    deepest, todo = 0, [(e, 1)]
    while todo:
        n, d = todo.pop()
        deepest = max(deepest, d)
        for kid in _kids(n):
            todo.append((kid, d + 1))
    return deepest


def _kids(n: Node) -> list[Node]:
    k = n[0]
    if k in ("lit", "id"):
        return []
    if k == "call":
        return ([n[2]] if n[2] is not None else []) + list(n[3])
    if k == "list":
        return list(n[1])
    if k == "map":
        return [x for kv in n[1] for x in kv]
    return [x for x in n[1:] if isinstance(x, tuple)]


def _check_inv(e: Node) -> None:
    """Load-time shape check for inv.* calls: arity, and unique_by's variable."""
    want = INV_ARITY.get(e[1]) if e[2] is None else None
    if want is None:
        return
    if len(e[3]) != want:
        raise CelSyntaxError(f"{e[1]}() takes {want} argument(s), got {len(e[3])}")
    if e[1] == "inv.unique_by" and e[3][1][0] != "id":
        raise CelSyntaxError("inv.unique_by() needs (list, var, expr)")


def parse(src: str) -> Node:
    """Parse rule text to a tuple tree. Raises CelSyntaxError."""
    return _Parser(src).parse()
