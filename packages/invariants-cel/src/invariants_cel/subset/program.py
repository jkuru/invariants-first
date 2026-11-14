"""The backend object the cel package registers as 'subset', and the programs it compiles."""
from __future__ import annotations

from typing import Any

from ..errors import CelError
from .catalog import FUNCTIONS
from .cost import DEFAULT_BUDGET
from .evaluator import evaluate
from .parser import Node, parse
from .tree import reads, references


class SubsetProgram:
    """A compiled subset expression."""

    __slots__ = ("source", "ast", "budget")

    def __init__(self, source: str, ast: Node, budget: int = DEFAULT_BUDGET) -> None:
        self.source, self.ast, self.budget = source, ast, budget

    def evaluate(self, env: dict[str, Any]) -> Any:
        """Evaluate against env; raises CelError."""
        try:
            return evaluate(self.ast, env, [self.budget])
        except RecursionError:          # a value or tree past Python's stack: an error, never an escape
            raise CelError("value or expression nested too deeply") from None

    def references(self) -> frozenset[tuple[str, ...]]:
        """Free identifiers with their static field paths."""
        return frozenset(references(self.ast))

    def reads(self) -> frozenset[tuple[str, ...]]:
        """What the clause reads, inside lists too (see reads())."""
        return frozenset(reads(self.ast))

    def negated_membership(self) -> tuple[tuple[str, ...], list[Any]] | None:
        """If the expression is `!(path in [literals])`, return (path, literals)."""
        a = self.ast
        if not (a[0] == "un" and a[1] == "!" and a[2][0] == "bin" and a[2][1] == "in"):
            return None
        lhs, rhs = a[2][2], a[2][3]
        if rhs[0] != "list" or not all(x[0] == "lit" for x in rhs[1]):
            return None
        refs = references(lhs)
        if lhs[0] not in ("sel", "id") or len(refs) != 1:
            return None
        return next(iter(refs)), [x[1] for x in rhs[1]]


class SubsetBackend:
    """Backend for the stdlib subset evaluator."""

    name = "subset"
    functions = FUNCTIONS               # the loader refuses a call to any other function name

    def __init__(self, budget: int = DEFAULT_BUDGET) -> None:
        self.budget = budget

    def compile(self, expr: str) -> SubsetProgram:
        """Parse expr; raises CelSyntaxError."""
        return SubsetProgram(expr, parse(expr), self.budget)
