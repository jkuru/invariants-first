"""Finding the laws a codebase already has, by reading its source: preconditions at the top of functions
(guards) and pydantic models with constraints."""
from __future__ import annotations

import ast
import dataclasses
import pathlib
from typing import Any, Iterable

from ..scan import anywhere, iter_sources
from .translate import Unsupported, _base_name, module_constants, negate, to_cel, truth_kind

EXCLUDE = anywhere("tests", "test", "testing", "test_*.py", "*_test.py", "conftest.py", "examples", "docs",
                   "scripts", "migrations", "alembic", "benchmarks", "bench", "noxfile.py", "setup.py")
FIELD_CONSTRAINTS = frozenset({"gt", "ge", "lt", "le", "min_length", "max_length", "pattern", "multiple_of",
                               "max_digits", "decimal_places", "min_items", "max_items", "strict",
                               "allow_inf_nan", "union_mode"})
CON_TYPES = frozenset({"conint", "confloat", "constr", "conlist", "conset", "condecimal", "conbytes", "condate"})
MODEL_ROOTS = ("BaseModel", "RootModel", "SQLModel")   # pydantic models; SQLModel subclasses BaseModel


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
    problem: str | None = None              # why `inv lock` cannot pin it (then written commented out)


@dataclasses.dataclass
class Found:
    guards: list[Guard]
    models: list[Model]
    files: int
    doors: Any = None                       # doors.DoorsFound, when door discovery ran


# --- preconditions -------------------------------------------------------------------------------
def _message(exc: ast.expr | None, fallback: str) -> str:
    call = exc if isinstance(exc, ast.Call) else None
    if call and call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return fallback


def _preconditions(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterable[tuple[ast.expr, str, ast.stmt]]:
    """(bad condition, message, statement) for the leading `if c: raise` / `assert c` statements.

    Leading means before the first statement that may do something: a docstring, `pass`, a logging
    call or an assignment of a plain value (no call other than a few builtins) may come first. A
    condition that reads a name assigned there is no longer about the arguments, and ends the
    search. `assert self.x is not None` (a type-narrowing check on the object's own state, not on
    an argument) is skipped."""
    assigned: set[str] = set()
    for st in fn.body:
        if isinstance(st, ast.If) and not st.orelse and len(st.body) == 1 and isinstance(st.body[0], ast.Raise):
            if _names(st.test) & assigned:
                return
            yield st.test, _message(st.body[0].exc, ast.unparse(st.test)), st
        elif isinstance(st, ast.Assert):
            if _self_check(st.test):
                continue
            if _names(st.test) & assigned:
                return
            msg = st.msg.value if isinstance(st.msg, ast.Constant) and isinstance(st.msg.value, str) \
                else f"assert {ast.unparse(st.test)}"
            yield negate(st.test), msg, st
        elif _inert(st):
            assigned |= _bound(st)
        else:
            return


# calls that compute a value and do nothing else, allowed in a statement before the preconditions
PURE_CALLS = frozenset({"len", "int", "str", "float", "bool", "min", "max", "abs", "tuple", "list", "set",
                        "frozenset", "dict", "sorted", "isinstance", "round"})
LOG_RECEIVERS = frozenset({"log", "logger", "logging", "_log", "_logger", "LOG", "LOGGER"})


def _names(n: ast.AST) -> set[str]:
    return {x.id for x in ast.walk(n) if isinstance(x, ast.Name)}


def _self_check(test: ast.expr) -> bool:
    """`self.x is not None`, `self.x` or `isinstance(self.x, T)`: a check on the object's own state."""
    def on_self(n: ast.expr) -> bool:
        while isinstance(n, (ast.Attribute, ast.Subscript)):
            n = n.value
        return isinstance(n, ast.Name) and n.id in ("self", "cls")
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.IsNot) \
            and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value is None:
        return isinstance(test.left, ast.Attribute) and on_self(test.left)
    if isinstance(test, ast.Attribute):
        return on_self(test)
    if isinstance(test, ast.Call) and isinstance(test.func, ast.Name) and test.func.id == "isinstance" and test.args:
        return isinstance(test.args[0], ast.Attribute) and on_self(test.args[0])
    return False


def _logging_call(n: ast.expr) -> bool:
    """logger.debug(...), log.info(...), logging.warning(...)."""
    if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
        return False
    recv = n.func.value
    name = recv.attr if isinstance(recv, ast.Attribute) else recv.id if isinstance(recv, ast.Name) else ""
    return name in LOG_RECEIVERS


def _inert(st: ast.stmt) -> bool:
    """A statement with no effect beyond binding local names: a docstring or `...`, pass, a logging
    call, or `x = <value>` whose value calls nothing but PURE_CALLS."""
    if isinstance(st, ast.Pass) or (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)):
        return True
    if isinstance(st, ast.Expr):
        v = st.value
        return isinstance(v, ast.Call) and _logging_call(v) and all(_pure(a) for a in v.args)
    if isinstance(st, ast.Assign):
        return all(isinstance(t, ast.Name) for t in st.targets) and _pure(st.value)
    if isinstance(st, ast.AnnAssign):
        return isinstance(st.target, ast.Name) and (st.value is None or _pure(st.value))
    return False


def _pure(n: ast.AST) -> bool:
    for x in ast.walk(n):
        if isinstance(x, (ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr, ast.Lambda)):
            return False
        if isinstance(x, ast.Call) and not (isinstance(x.func, ast.Name) and x.func.id in PURE_CALLS):
            return False
    return True


def _bound(st: ast.stmt) -> set[str]:
    if isinstance(st, ast.Assign):
        return {t.id for t in st.targets if isinstance(t, ast.Name)}
    if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
        return {st.target.id}
    return set()


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    a = fn.args
    return {x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs]} - {"self", "cls"}


def _kinds(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str | None]:
    """truth_kind of each parameter, from its annotation and a None default."""
    a = fn.args
    positional = [*a.posonlyargs, *a.args]
    defaults: dict[str, ast.expr | None] = dict(zip([x.arg for x in positional][len(positional) - len(a.defaults):],
                                                    a.defaults))
    defaults.update(zip([x.arg for x in a.kwonlyargs], a.kw_defaults))
    return {x.arg: truth_kind(x.annotation, defaults.get(x.arg)) for x in [*positional, *a.kwonlyargs]}


def _guard(module: str, n: ast.FunctionDef | ast.AsyncFunctionDef, qualname: str,
           consts: dict[str, Any]) -> Guard | None:
    """One function's preconditions as CEL clauses (and the ones that do not translate), or None."""
    params, kinds = _params(n), _kinds(n)
    clauses, skipped = [], []
    for cond, msg, st in _preconditions(n):
        try:
            clauses.append((to_cel(cond, params, consts, kinds), msg))
        except Unsupported as e:
            skipped.append(f"line {st.lineno}: {ast.unparse(st).splitlines()[0]} ({e})")
    return Guard(module, qualname, clauses, skipped, n.lineno) if clauses or skipped else None


def _guards(module: str, tree: ast.Module) -> list[Guard]:
    out: list[Guard] = []
    consts = module_constants(tree)

    def visit(body: list[ast.stmt], prefix: str, in_class: bool) -> None:
        for n in body:
            if isinstance(n, ast.ClassDef) and not prefix and not n.name.startswith("_"):
                visit(n.body, f"{n.name}.", True)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_"):
                # a private function or method (_helper, __init__) is not an API boundary: no rule
                if any(isinstance(d, ast.Name) and d.id in ("staticmethod", "classmethod", "property")
                       for d in n.decorator_list) and in_class:
                    continue
                g = _guard(module, n, prefix + n.name, consts)
                if g is not None:
                    out.append(g)

    visit(tree.body, "", False)
    return out


# --- pydantic models -----------------------------------------------------------------------------
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


def discover(project: pathlib.Path, roots: list[pathlib.Path], exclude: Iterable[str] = ()) -> Found:
    """Read every source file under the roots (not importing anything)."""
    exclude = [*EXCLUDE, *exclude]
    trees: list[tuple[str, ast.Module, pathlib.Path]] = []
    for _, f, mod in iter_sources(roots, exclude):
        try:
            trees.append((mod, ast.parse(f.read_text(encoding="utf-8"), str(f)), f))
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
