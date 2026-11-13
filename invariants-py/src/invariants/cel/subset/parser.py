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


class _Parser:
    _LEVELS = [("||",), ("&&",), ("==", "!=", "<", "<=", ">", ">=", "in"), ("+", "-"), ("*", "/", "%")]

    def __init__(self, src: str) -> None:
        self.t, self.i = tokenize(src), 0

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
        return e

    def expr(self) -> Node:
        c = self.binary(0)
        if self.peek("?"):
            self.eat("?")
            a = self.expr()
            self.eat(":")
            return ("cond", c, a, self.expr())
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
        return self.member()

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
