"""Python -> CEL: one precondition over the parameters (and module constants) as a CEL clause."""
from __future__ import annotations

import ast
import json
from typing import Any


class Unsupported(Exception):
    """A Python expression with no faithful CEL translation."""


_CMP = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}
_NEG = {ast.Lt: ast.GtE, ast.LtE: ast.Gt, ast.Gt: ast.LtE, ast.GtE: ast.Lt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
        ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}
_ARITH = {ast.Add: ("+", 1), ast.Sub: ("-", 1), ast.Mult: ("*", 2), ast.Mod: ("%", 2)}
_METHODS = {"startswith": "startsWith", "endswith": "endsWith"}


_SIZED = frozenset({"str", "bytes", "list", "dict", "set", "frozenset", "tuple",
                    "List", "Dict", "Set", "FrozenSet", "Tuple"})


def _base_name(b: ast.expr) -> str:
    if isinstance(b, ast.Subscript):              # RootModel[list[int]], BaseModel subclass Generic[T]
        b = b.value
    return b.attr if isinstance(b, ast.Attribute) else b.id if isinstance(b, ast.Name) else ""


def truth_kind(ann: ast.expr | None, default: ast.expr | None = None) -> str | None:
    """How a parameter's truthiness reads in CEL, from its annotation: "sized" (str, bytes, list, dict,
    set: empty is false), "bool", "sized?" / "bool?" when it may also be None (Optional[...], `| None`,
    or a None default); None when there is no exact form (no annotation, int, float, a class...)."""
    optional = isinstance(default, ast.Constant) and default.value is None
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            ann = ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return None
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        sides = [x for x in (ann.left, ann.right) if not (isinstance(x, ast.Constant) and x.value is None)]
        if len(sides) != 1:
            return None
        ann, optional = sides[0], True
    if isinstance(ann, ast.Subscript):
        head = _base_name(ann.value)
        if head == "Optional":
            ann, optional = ann.slice, True
        elif head == "Union" and isinstance(ann.slice, ast.Tuple):
            sides = [x for x in ann.slice.elts if not (isinstance(x, ast.Constant) and x.value is None)]
            if len(sides) != 1 or len(sides) == len(ann.slice.elts):
                return None
            ann, optional = sides[0], True
    if isinstance(ann, ast.Subscript):
        ann = ann.value                                  # list[int], dict[str, int]: the container
    name = _base_name(ann) if isinstance(ann, (ast.Name, ast.Attribute)) else ""
    kind = "sized" if name in _SIZED else "bool" if name == "bool" else None
    return kind and (kind + "?" if optional else kind)


class _Cel:
    """Translate one condition. Parameters read as args.<name>; `self` stays `self`; a module constant
    (NAME = <literal>, assigned once) is written as its value, so the rule keeps today's limit even if
    the constant is later raised (that is a change to the law, made in the rule file).

    With `kinds` (each parameter's truth_kind), a value tested for truth (`if not name`, `assert items`,
    `a and b`) is translated by its type: `size(args.name) == 0` for a string or collection, `!args.ok`
    for a bool, with a null test first when it may be None. Anything else tested for truth has no exact
    CEL form (Python's truthiness covers 0, "", [] and None alike) and is refused."""

    def __init__(self, params: set[str], consts: dict[str, Any] | None = None,
                 kinds: dict[str, str | None] | None = None) -> None:
        self.params = params
        self.consts = consts or {}
        self.kinds = kinds

    def cond(self, n: ast.expr, negated: bool = False) -> str:
        """n (or not n) used as a condition."""
        if self.kinds is None:                                  # no annotations given: as written
            return "!" + self._atom(n) if negated else self.expr(n)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            return self.cond(n.operand, not negated)
        if isinstance(n, ast.Name) and n.id in self.params:
            kind = self.kinds.get(n.id)
            x = f"args.{n.id}"
            if kind is None:
                raise Unsupported(f"the truth of {n.id} depends on its type, and its annotation gives no exact "
                                  f"CEL form")
            test = (f"size({x}) == 0" if negated else f"size({x}) != 0") if kind.startswith("sized") else \
                (f"!{x}" if negated else x)
            if kind.endswith("?"):
                return f"{x} == null || {test}" if negated else f"{x} != null && {test}"
            return test
        if isinstance(n, ast.BoolOp) or isinstance(n, ast.Compare) or (
                isinstance(n, ast.Constant) and isinstance(n.value, bool)) or (
                isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _METHODS):
            s = self.expr(n)
            if not negated:
                return s
            return "!" + (f"({s})" if isinstance(n, (ast.BoolOp, ast.Compare)) else s)
        raise Unsupported(f"the truth of {ast.unparse(n)} depends on its type")

    def expr(self, n: ast.expr) -> str:
        if isinstance(n, ast.BoolOp):
            op = " && " if isinstance(n.op, ast.And) else " || "
            return op.join(self._bool_operand(v, n.op) for v in n.values)
        if isinstance(n, ast.UnaryOp):
            if isinstance(n.op, ast.Not):
                return self.cond(n.operand, negated=True) if self.kinds is not None else "!" + self._atom(n.operand)
            if isinstance(n.op, ast.USub):
                return "-" + self._atom(n.operand)
            raise Unsupported(type(n.op).__name__)
        if isinstance(n, ast.Compare):
            parts: list[str] = []
            left: ast.expr = n.left
            for cop, right in zip(n.ops, n.comparators):
                parts.append(self._compare(left, cop, right))
                left = right
            return " && ".join(parts)
        if isinstance(n, ast.BinOp):
            if type(n.op) not in _ARITH:
                raise Unsupported(f"operator {type(n.op).__name__} (Python and CEL divide differently)")
            sym, prec = _ARITH[type(n.op)]
            return f"{self._arith(n.left, prec, False)} {sym} {self._arith(n.right, prec, True)}"
        return self._value(n)

    def _bool_operand(self, v: ast.expr, op: ast.boolop) -> str:
        if self.kinds is not None and not isinstance(v, ast.BoolOp):
            s = self.cond(v)
            compound = (" || " in s or " && " in s) and not isinstance(v, ast.Compare)
            return f"({s})" if compound else s
        s = self.expr(v)
        return f"({s})" if isinstance(v, ast.BoolOp) and type(v.op) is not type(op) else s

    def _atom(self, n: ast.expr) -> str:
        s = self.expr(n)
        return s if isinstance(n, (ast.Name, ast.Attribute, ast.Constant, ast.Call, ast.Subscript)) else f"({s})"

    def _arith(self, n: ast.expr, prec: int, right: bool) -> str:
        s = self.expr(n)
        if isinstance(n, ast.BinOp) and type(n.op) in _ARITH:
            p = _ARITH[type(n.op)][1]
            if p < prec or (right and p == prec):
                return f"({s})"
        elif isinstance(n, (ast.BoolOp, ast.Compare)):
            return f"({s})"
        return s

    def _compare(self, left: ast.expr, op: ast.cmpop, right: ast.expr) -> str:
        if isinstance(op, (ast.Is, ast.IsNot)):
            if not (isinstance(right, ast.Constant) and right.value is None):
                raise Unsupported("`is` with something other than None")
            return f"{self._arith(left, 0, False)} {'==' if isinstance(op, ast.Is) else '!='} null"
        if isinstance(op, (ast.In, ast.NotIn)):
            if isinstance(right, ast.Name) and right.id not in self.params and isinstance(self.consts.get(right.id), tuple):
                right = _const_node(self.consts[right.id])          # CURRENCIES = ("usd", "eur")
            if not isinstance(right, (ast.List, ast.Tuple, ast.Set)):
                raise Unsupported("`in` on something other than a literal list (Python also searches strings)")
            s = f"{self._arith(left, 0, False)} in {self._list(right)}"
            return s if isinstance(op, ast.In) else f"!({s})"
        return f"{self._arith(left, 0, False)} {_CMP[type(op)]} {self._arith(right, 0, True)}"

    def _list(self, n: ast.List | ast.Tuple | ast.Set) -> str:
        return "[" + ", ".join(self.expr(e) for e in n.elts) + "]"

    def _constant(self, v: Any) -> str:
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, float):
            return repr(v) if "e" not in repr(v) and "inf" not in repr(v) else _raise(f"float {v!r}")
        if isinstance(v, str):
            return json.dumps(v)
        raise Unsupported(f"constant {type(v).__name__}")

    def _value(self, n: ast.expr) -> str:
        if isinstance(n, ast.Constant):
            return self._constant(n.value)
        if isinstance(n, ast.Name):
            if n.id == "self":
                return "self"
            if n.id in self.params:
                return f"args.{n.id}"
            if n.id in self.consts:
                return self.expr(_const_node(self.consts[n.id]))
            raise Unsupported(f"name {n.id} is not a parameter or a module constant")
        if isinstance(n, ast.Attribute):
            if n.attr.startswith("_"):
                raise Unsupported(f"private attribute {n.attr}")
            return f"{self._value(n.value)}.{n.attr}"
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and \
                isinstance(n.slice.value, (str, int)) and not isinstance(n.slice.value, bool):
            return f"{self._value(n.value)}[{json.dumps(n.slice.value)}]"
        if isinstance(n, ast.Call) and not n.keywords:
            if isinstance(n.func, ast.Name) and n.func.id == "len" and len(n.args) == 1:
                return f"size({self.expr(n.args[0])})"
            if isinstance(n.func, ast.Attribute) and n.func.attr in _METHODS and len(n.args) == 1:
                return f"{self._value(n.func.value)}.{_METHODS[n.func.attr]}({self.expr(n.args[0])})"
        if isinstance(n, (ast.List, ast.Tuple)):
            return self._list(n)
        raise Unsupported(type(n).__name__)


def _raise(why: str) -> str:
    raise Unsupported(why)


def negate(n: ast.expr) -> ast.expr:
    """not n, pushed into a single comparison where that is exact."""
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
        return n.operand
    if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in _NEG:
        return ast.Compare(left=n.left, ops=[_NEG[type(n.ops[0])]()], comparators=n.comparators)
    return ast.UnaryOp(op=ast.Not(), operand=n)


def to_cel(cond: ast.expr, params: set[str], consts: dict[str, Any] | None = None,
           kinds: dict[str, str | None] | None = None) -> str:
    """The CEL form of a Python condition over the parameters (and module constants); raises Unsupported.
    kinds (truth_kind per parameter) makes a truth test exact by type; without it, it is written as is."""
    c = _Cel(params, consts, kinds)
    return c.cond(cond) if kinds is not None else c.expr(cond)


def _literal(n: ast.expr | None, depth: int = 0) -> tuple[bool, Any]:
    """(True, value) for an int, float, str, bool or None literal (a negative number too), or a tuple,
    list or set of them (as a tuple); else (False, None)."""
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub) and isinstance(n.operand, ast.Constant):
        v = n.operand.value
        if type(v) is int or type(v) is float:
            return True, -v
    if isinstance(n, ast.Constant) and (n.value is None or type(n.value) in (int, float, str, bool)):
        return True, n.value
    if isinstance(n, (ast.Tuple, ast.List, ast.Set)) and depth == 0:
        items = [_literal(e, 1) for e in n.elts]
        if all(ok for ok, _ in items):
            return True, tuple(v for _, v in items)
    return False, None


def _const_node(v: Any) -> ast.expr:
    """An AST literal for a module constant's value (a tuple becomes a list literal)."""
    if isinstance(v, tuple):
        return ast.List([_const_node(x) for x in v], ast.Load())
    return ast.Constant(v)


def module_constants(tree: ast.Module) -> dict[str, Any]:
    """Module-level NAME = <literal> bound exactly once and never rebound anywhere in the module (no
    second assignment, augmented assignment, `global`, loop, `with`, import or def of that name)."""
    found: dict[str, Any] = {}
    for st in tree.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            name, value = st.targets[0].id, st.value
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
            name, value = st.target.id, st.value
        else:
            continue
        ok, v = _literal(value)
        if ok:
            found[name] = v
    binds: dict[str, int] = {}
    for n in ast.walk(tree):
        names: list[str] = []
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names = [n.id]
        elif isinstance(n, ast.Global):
            names = list(n.names) * 2                 # rebinding from a function: never a constant
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [n.name]
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [(a.asname or a.name).split(".")[0] for a in n.names]
        for x in names:
            binds[x] = binds.get(x, 0) + 1
    return {k: v for k, v in found.items() if binds.get(k, 0) == 1}
