"""`inv init`: draft a first rule file from the laws a codebase already has, on its API boundaries.

It reads the source (it does not import it) and finds:

- pydantic models with constraints (Field(gt=..., le=..., max_length=...), Literal, constr/conint...):
  value laws on the APIs that take or return them. Each becomes a `pydantic` contract. Nothing else is
  needed: `inv lock` pins the model's JSON Schema, and a later change that lets it accept more input
  is refused as a weakening.
- doors (doors.py): path laws between the service layer and the repositories it calls.
- preconditions at the top of a function: `if <cond>: raise ...` and `assert <cond>`, where the condition
  only reads the parameters. Each function's preconditions become one short-form rule (guard + bad),
  translated from Python to CEL. These are written commented out: a rule needs `@inv.guard("ID")` on the
  function before `inv check` passes, so `inv init` leaves that decision, and the edit, to a person.

The file it writes passes `inv check` as written. It never overwrites a rule file.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import pathlib
import re
from typing import Any, Iterable

from .scan import anywhere, iter_sources

EXCLUDE = anywhere("tests", "test", "testing", "test_*.py", "*_test.py", "conftest.py", "examples", "docs",
                   "scripts", "migrations", "alembic", "benchmarks", "bench", "noxfile.py", "setup.py")
FIELD_CONSTRAINTS = frozenset({"gt", "ge", "lt", "le", "min_length", "max_length", "pattern", "multiple_of",
                               "max_digits", "decimal_places", "min_items", "max_items", "strict",
                               "allow_inf_nan", "union_mode"})
CON_TYPES = frozenset({"conint", "confloat", "constr", "conlist", "conset", "condecimal", "conbytes", "condate"})


class Unsupported(Exception):
    """A Python expression with no faithful CEL translation."""


# --- Python -> CEL -------------------------------------------------------------------------------
_CMP = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}
_NEG = {ast.Lt: ast.GtE, ast.LtE: ast.Gt, ast.Gt: ast.LtE, ast.GtE: ast.Lt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
        ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}
_ARITH = {ast.Add: ("+", 1), ast.Sub: ("-", 1), ast.Mult: ("*", 2), ast.Mod: ("%", 2)}
_METHODS = {"startswith": "startsWith", "endswith": "endsWith"}


class _Cel:
    """Translate one condition. Parameters read as args.<name>; `self` stays `self`; a module constant
    (NAME = <literal>, assigned once) is written as its value, so the rule keeps today's limit even if
    the constant is later raised (that is a change to the law, made in the rule file)."""

    def __init__(self, params: set[str], consts: dict[str, Any] | None = None) -> None:
        self.params = params
        self.consts = consts or {}

    def expr(self, n: ast.expr) -> str:
        if isinstance(n, ast.BoolOp):
            op = " && " if isinstance(n.op, ast.And) else " || "
            return op.join(self._bool_operand(v, n.op) for v in n.values)
        if isinstance(n, ast.UnaryOp):
            if isinstance(n.op, ast.Not):
                return "!" + self._atom(n.operand)
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

    def _value(self, n: ast.expr) -> str:
        if isinstance(n, ast.Constant):
            v = n.value
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


def to_cel(cond: ast.expr, params: set[str], consts: dict[str, Any] | None = None) -> str:
    """The CEL form of a Python condition over the parameters (and module constants); raises Unsupported."""
    return _Cel(params, consts).expr(cond)


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


# --- finding existing checks ---------------------------------------------------------------------
@dataclasses.dataclass
class Guard:
    module: str
    qualname: str
    clauses: list[tuple[str, str]]          # (CEL, message)
    skipped: list[str]                      # preconditions that could not be translated, with why
    line: int


@dataclasses.dataclass
class Model:
    module: str
    name: str
    constraints: list[str]
    validators: int


def _message(exc: ast.expr | None, fallback: str) -> str:
    call = exc if isinstance(exc, ast.Call) else None
    if call and call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return fallback


def _preconditions(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterable[tuple[ast.expr, str, ast.stmt]]:
    """(bad condition, message, statement) for the leading `if c: raise` / `assert c` statements."""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    for st in body:
        if isinstance(st, ast.If) and not st.orelse and len(st.body) == 1 and isinstance(st.body[0], ast.Raise):
            yield st.test, _message(st.body[0].exc, ast.unparse(st.test)), st
        elif isinstance(st, ast.Assert):
            msg = st.msg.value if isinstance(st.msg, ast.Constant) and isinstance(st.msg.value, str) \
                else f"assert {ast.unparse(st.test)}"
            yield negate(st.test), msg, st
        else:
            return


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    a = fn.args
    return {x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs]} - {"self", "cls"}


def _guards(module: str, tree: ast.Module) -> list[Guard]:
    out: list[Guard] = []
    consts = module_constants(tree)

    def visit(body: list[ast.stmt], prefix: str, in_class: bool) -> None:
        for n in body:
            if isinstance(n, ast.ClassDef) and not prefix:
                visit(n.body, f"{n.name}.", True)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("__"):
                if any(isinstance(d, ast.Name) and d.id in ("staticmethod", "classmethod", "property")
                       for d in n.decorator_list) and in_class:
                    continue
                params = _params(n)
                clauses, skipped = [], []
                for cond, msg, st in _preconditions(n):
                    try:
                        clauses.append((to_cel(cond, params, consts), msg))
                    except Unsupported as e:
                        skipped.append(f"line {st.lineno}: {ast.unparse(st).splitlines()[0]} ({e})")
                if clauses or skipped:
                    out.append(Guard(module, prefix + n.name, clauses, skipped, n.lineno))

    visit(tree.body, "", False)
    return out


MODEL_ROOTS = ("BaseModel", "RootModel", "SQLModel")   # pydantic models; SQLModel subclasses BaseModel


def _base_name(b: ast.expr) -> str:
    if isinstance(b, ast.Subscript):              # RootModel[list[int]], BaseModel subclass Generic[T]
        b = b.value
    return b.attr if isinstance(b, ast.Attribute) else b.id if isinstance(b, ast.Name) else ""


def _constraint_terms(node: ast.AST) -> list[str]:
    """Constraint keywords and constrained types inside an annotation or a field default."""
    found: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            name = _base_name(n.func)
            if name == "Field":
                found += [f"{k.arg}={ast.unparse(k.value)}" for k in n.keywords if k.arg in FIELD_CONSTRAINTS]
            elif name in CON_TYPES:
                found.append(ast.unparse(n))
        elif isinstance(n, ast.Subscript) and _base_name(n.value) == "Literal":
            found.append(ast.unparse(n))
    return found


def _models(module: str, tree: ast.Module, known: set[str]) -> list[Model]:
    out = []
    for n in tree.body:
        if not isinstance(n, ast.ClassDef) or not any(_base_name(b) in known for b in n.bases):
            continue
        terms, validators = [], 0
        for st in n.body:
            if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                for t in _constraint_terms(st.annotation) + (_constraint_terms(st.value) if st.value else []):
                    terms.append(f"{st.target.id}: {t}")
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    _base_name(d.func if isinstance(d, ast.Call) else d) in
                    ("field_validator", "model_validator", "validator", "root_validator") for d in st.decorator_list):
                validators += 1
        out.append(Model(module, n.name, terms, validators))
    return out


@dataclasses.dataclass
class Found:
    guards: list[Guard]
    models: list[Model]
    files: int
    doors: Any = None                       # doors.DoorsFound, when door discovery ran


def discover(project: pathlib.Path, roots: list[pathlib.Path], exclude: Iterable[str] = ()) -> Found:
    """Read every source file under the roots (not importing anything)."""
    exclude = [*EXCLUDE, *exclude]
    trees: list[tuple[str, ast.Module, pathlib.Path]] = []
    for _, f, mod in iter_sources(roots, exclude):
        try:
            trees.append((mod, ast.parse(f.read_text(), str(f)), f))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
    known = set(MODEL_ROOTS)
    while True:                                   # subclasses of models are models (by class name)
        more = {n.name for _, t, _ in trees for n in t.body if isinstance(n, ast.ClassDef)
                and any(_base_name(b) in known for b in n.bases)} - known
        if not more:
            break
        known |= more
    guards: list[Guard] = []
    models: list[Model] = []
    for mod, tree, _ in trees:
        guards += _guards(mod, tree)
        models += _models(mod, tree, known)
    return Found(guards, models, len(trees))


# --- writing the rule file -----------------------------------------------------------------------
def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _rule_id(g: Guard, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "-", g.qualname).strip("-").upper()
    rid = base if base not in taken else re.sub(r"[^A-Za-z0-9]+", "-", f"{g.module}.{g.qualname}").upper()
    taken.add(rid)
    return rid


def render(found: Found) -> str:
    """The rule file text: contracts active, suggested rules commented out."""
    lines = ["# invariants.yaml, drafted by `inv init`. Review it, then run `inv lock` and commit both files.",
             "#",
             "# The contracts below pin rules your code already has. From now on `inv check` fails when a",
             "# change lets a model accept more than it does today (a raised limit, a removed",
             "# constraint, a new enum value), unless someone accepts it with `inv lock --accept-weakening`.",
             "spec: invariant/v0.1", ""]
    contracts: list[str] = []
    names: set[str] = set()
    for m in found.models:
        if not m.constraints:
            continue
        name = _snake(m.name)
        if name in names:
            name = _snake(f"{m.module.replace('.', '_')}_{m.name}")
        names.add(name)
        shown = ", ".join(m.constraints[:4]) + (f", +{len(m.constraints) - 4} more" if len(m.constraints) > 4 else "")
        contracts.append(f"  # {shown}")
        if m.validators:
            contracts.append(f"  # (its {m.validators} validator function(s) are code, not schema: not pinned)")
        contracts.append(f"  {name}: {{type: pydantic, ref: {_q(f'{m.module}:{m.name}')}}}")
    lines += ["contracts:", *contracts, ""] if contracts else ["contracts: {}", ""]
    rules = [g for g in found.guards if g.clauses]
    lines.append("rules:")
    if found.doors is not None:
        from . import doors
        lines += doors.render(found.doors)
    if rules:
        lines += ["",
                  "# Suggested rules, from the preconditions at the top of these functions. Each one keeps the",
                  "# check even if a later edit deletes the `if` or `assert`. To turn one on:",
                  "#   1. uncomment it,",
                  "#   2. put @inv.guard(\"<id>\") on the function (import invariants as inv),",
                  "#   3. write a test that calls it with bad input and expects inv.Rejected, and list it in `tests:`",
                  "#      (until a rule names a test it only logs; with one it blocks, and `pytest --inv-ablate`",
                  "#      proves the test fails when the guard is switched off),",
                  "#   4. run `inv lock`.",
                  "#"]
        taken: set[str] = set()
        for g in rules:
            rid = _rule_id(g, taken)
            lines.append(f"#  - id: {rid}")
            lines.append(f"#    guard: {_q(f'py:{g.module}.{g.qualname}')}")
            if len(g.clauses) == 1:
                cel, msg = g.clauses[0]
                lines.append(f"#    statement: {_q(msg)}")
                lines.append(f"#    bad: {_q(cel)}")
            else:
                lines.append("#    bad:")
                for cel, msg in g.clauses:
                    lines.append(f"#      - {{when: {_q(cel)}, message: {_q(msg)}}}")
            lines.append("#    tests: []")
            for s in g.skipped:
                lines.append(f"#    # not translated, {s}")
            lines.append("#")
    untranslated = [g for g in found.guards if not g.clauses]
    if untranslated:
        lines.append("# Preconditions found but not translated to CEL (write these by hand if they matter):")
        for g in untranslated:
            for s in g.skipped:
                lines.append(f"#   {g.module}.{g.qualname} {s}")
    return "\n".join(lines).rstrip() + "\n"


def summary(found: Found) -> list[str]:
    models = [m for m in found.models if m.constraints]
    n_cons = sum(len(m.constraints) for m in models)
    rules = [g for g in found.guards if g.clauses]
    return [f"read {found.files} source file(s)",
            f"pinned {len(models)} pydantic model(s) with {n_cons} constraint(s)",
            f"suggested {len(rules)} rule(s) from function preconditions (commented out; see the file)",
            *(_doors_summary(found))]


def _doors_summary(found: Found) -> list[str]:
    if found.doors is None:
        return []
    from . import doors
    return doors.summary(found.doors)
