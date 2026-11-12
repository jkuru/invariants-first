"""`inv scan`: a static, read-only call-graph check that every path to a protected sink passes its door.

The rule file declares, per rule, under `mediation`:

    sink:   "py:pkg.store.set_size"          # protected effect sink(s): a string or a list
    door:   "py:pkg.service.resize"          # the chokepoint(s); defaults to subject.target
    tables: [pools, {leases: [insert, update]}]  # optional: raw SQL writes to these are forbidden
                                             # (a mapping limits it to statement kinds: insert, update,
                                             # delete, ddl)

The source is parsed with `ast` and never imported or run. Findings have four kinds:
bypass (a path of extracted call edges from an entrypoint to the sink that avoids every door),
forbidden (a bypass idiom), unresolved (something the scan cannot follow; never counted as passed)
and missing (a declared sink or door that does not exist in the scanned code).
"""
from __future__ import annotations

import ast
import builtins
import collections
import dataclasses
import fnmatch
import pathlib
import re
from typing import Any, Iterable, Iterator

KINDS = ("bypass", "forbidden", "unresolved", "missing")
SKIP_DIRS = frozenset({"__pycache__", "node_modules", "site-packages", "build", "dist", "venv"})
REFLECTION = frozenset({"eval", "exec", "compile", "__import__", "globals", "vars", "locals"})
DYNAMIC_IMPORT = frozenset({"importlib.import_module", "importlib.__import__"})
_BUILTINS = frozenset(dir(builtins))
SQL_OPS = ("insert", "update", "delete", "ddl")
# a table reference: optional schema qualifier, optional quotes, and a name that may hold placeholders
_TABLE = r"""(?:["`\[]?\w+["`\]]?\.)?["`\[]?((?:\w|\{\w*\}|%(?:\(\w+\))?s)+|\?|:\w+)["`\]]?"""
_SQL_FORMS: tuple[tuple[str, frozenset[str], re.Pattern[str]], ...] = tuple(
    (verb, frozenset(ops), re.compile(pat + _TABLE + tail, re.IGNORECASE)) for verb, ops, pat, tail in (
        ("INSERT", {"insert"},
         r"\bINSERT(?:\s+(?:OR\s+\w+|IGNORE|LOW_PRIORITY|DELAYED|HIGH_PRIORITY))*(?:\s+INTO)?\s+", ""),
        ("REPLACE", {"insert"}, r"\bREPLACE(?:\s+(?:LOW_PRIORITY|DELAYED))?\s+INTO\s+", ""),
        ("MERGE", {"insert", "update", "delete"}, r"\bMERGE\s+(?:INTO\s+)?", ""),
        ("UPDATE", {"update"}, r"\bUPDATE(?:\s+(?:OR\s+\w+|ONLY|LOW_PRIORITY|IGNORE))*\s+",
         r"(?:\s+(?:AS\s+)?(?!SET\b)\w+)?\s+SET\b"),
        ("DELETE", {"delete"}, r"\bDELETE(?:\s+(?:LOW_PRIORITY|QUICK|IGNORE))*\s+FROM(?:\s+ONLY)?\s+", ""),
        ("TRUNCATE", {"delete"}, r"\bTRUNCATE(?:\s+TABLE)?(?:\s+ONLY)?\s+", ""),
        ("DROP", {"ddl"}, r"\bDROP\s+TABLE(?:\s+IF\s+EXISTS)?\s+", ""),
        ("ALTER", {"ddl"}, r"\bALTER\s+TABLE(?:\s+IF\s+EXISTS)?(?:\s+ONLY)?\s+", ""),
    ))
_SQL_COMMENT = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)
_KEYWORDS = frozenset({"into", "from", "table", "only", "set", "if", "select", "values", "or", "using"})


@dataclasses.dataclass(frozen=True)
class SqlWrite:
    """One write statement found in a string: its verb, statement kinds, and table (None: computed)."""

    verb: str
    ops: frozenset[str]
    table: str | None


def sql_writes(text: str) -> list[SqlWrite]:
    """The write statements in a string, after SQL comments are removed. A table name holding a
    placeholder ({}, {name}, {0}, %s, %(name)s, ?, :name) is computed (table None)."""
    if not text or len(text) < 6:
        return []
    clean = _SQL_COMMENT.sub(" ", text)
    out: list[SqlWrite] = []
    seen: set[tuple[str, str | None, int]] = set()
    for verb, ops, rx in _SQL_FORMS:
        for m in rx.finditer(clean):
            name = m.group(1)
            computed = name == "?" or name.startswith(":") or "{" in name or "%" in name
            if not computed and name.lower() in _KEYWORDS:
                continue
            table = None if computed else name.lower()
            key = (",".join(sorted(ops)), table, m.start(1))
            if key in seen:
                continue
            seen.add(key)
            out.append(SqlWrite(verb, ops, table))
    return out


def table_specs(tables: Any, where: str = "mediation.tables") -> dict[str, frozenset[str]]:
    """mediation.tables -> {table (lower case): statement kinds forbidden outside the doors}.

    An item is a table name (every kind: insert, update, delete, ddl) or a one-key mapping
    {table: [kinds]} that forbids only those kinds, e.g. {leases: [insert, update]} lets a
    release DELETE through while an INSERT outside the door is still a finding.
    """
    items = [tables] if isinstance(tables, (str, dict)) else tables
    if not isinstance(items, list) or not items:
        raise ScanConfigError(f"{where}: expected a non-empty list of table names or {{table: [kinds]}}")
    out: dict[str, frozenset[str]] = {}
    for it in items:
        if isinstance(it, str) and it:
            out[it.lower()] = frozenset(SQL_OPS)
        elif isinstance(it, dict) and len(it) == 1:
            (name, ops), = it.items()
            ops = [ops] if isinstance(ops, str) else ops
            if not isinstance(name, str) or not name or not isinstance(ops, list) or not ops \
                    or not all(o in SQL_OPS for o in ops):
                raise ScanConfigError(f"{where}: {it!r}: expected {{table: [kinds]}} with kinds from "
                                      f"{', '.join(SQL_OPS)}")
            out[name.lower()] = out.get(name.lower(), frozenset()) | frozenset(ops)
        else:
            raise ScanConfigError(f"{where}: {it!r} is not a table name or a one-key {{table: [kinds]}} mapping")
    return out
_DROP_TRIGGER = re.compile(r"\bDROP\s+TRIGGER\b", re.IGNORECASE)


def _drops_trigger(text: str) -> bool:
    return bool(_DROP_TRIGGER.search(_SQL_COMMENT.sub(" ", text)))


class ScanConfigError(ValueError):
    """The rule file's mediation fields for the scan are malformed."""


@dataclasses.dataclass
class Finding:
    """One scan result."""

    kind: str                      # bypass | forbidden | unresolved | missing
    rule: str                      # rule id
    code: str                      # e.g. bypass, forbidden/getattr-sink, unresolved/reflection
    message: str
    file: str | None = None        # path relative to its source root (posix)
    line: int = 0
    col: int = 0
    root: str | None = None
    path: list[str] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Mediation:
    """The scan fields of one rule."""

    rule: str
    sinks: list[str]
    doors: list[str]
    tables: list[Any]

    @property
    def table_ops(self) -> dict[str, frozenset[str]]:
        return table_specs(self.tables) if self.tables else {}


@dataclasses.dataclass
class Report:
    """Everything one scan found."""

    findings: list[Finding]
    scanned: dict[str, Mediation]
    not_scanned: dict[str, str]
    files: int = 0
    functions: int = 0
    warnings: dict[str, str] = dataclasses.field(default_factory=dict)
    sink_writes: dict[str, dict[str, list[str]]] = dataclasses.field(default_factory=dict)
    uncovered_tables: dict[str, dict[str, list[str]]] = dataclasses.field(default_factory=dict)

    def for_rule(self, rule_id: str) -> list[Finding]:
        return [f for f in self.findings if f.rule == rule_id]

    def counts(self, rule_id: str | None = None) -> dict[str, int]:
        c = collections.Counter(f.kind for f in self.findings if rule_id is None or f.rule == rule_id)
        return {k: c[k] for k in KINDS if c[k]}

    @property
    def ablated_enforce(self) -> list[str]:
        """Enforce rules the scan skipped because they are ablated: the scan does not pass."""
        return [rid for rid, why in self.not_scanned.items() if why.startswith("ablated") and "enforce" in why]

    def rule_result(self, rule_id: str) -> str:
        """pass | fail | unresolved | not_scanned."""
        if rule_id not in self.scanned:
            return "not_scanned"
        c = self.counts(rule_id)
        if c.get("bypass") or c.get("forbidden") or c.get("missing"):
            return "fail"
        return "unresolved" if c.get("unresolved") else "pass"

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": self.files, "functions": self.functions,
            "rules": {rid: {"result": self.rule_result(rid), "sinks": m.sinks, "doors": m.doors,
                            "tables": m.tables, "counts": self.counts(rid),
                            "sink_writes": self.sink_writes.get(rid, {}),
                            "uncovered_tables": self.uncovered_tables.get(rid, {})}
                      for rid, m in self.scanned.items()},
            "not_scanned": self.not_scanned,
            "warnings": self.warnings,
            "findings": [f.as_dict() for f in self.findings],
        }


# --- reading the rule file ----------------------------------------------------------------------
def _str_list(v: Any, where: str) -> list[str]:
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list) or not items or not all(isinstance(x, str) and x for x in items):
        raise ScanConfigError(f"{where}: expected a string or a non-empty list of strings")
    return list(items)


def mediations(doc: dict[str, Any]) -> tuple[dict[str, Mediation], dict[str, str]]:
    """Scan fields per rule, and the reason each other rule is not scanned."""
    scanned: dict[str, Mediation] = {}
    skipped: dict[str, str] = {}
    for raw in doc.get("rules") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            continue
        rid, med = raw["id"], raw.get("mediation") or {}
        if not isinstance(med, dict):
            raise ScanConfigError(f"{rid}: mediation must be a mapping")
        if raw.get("status") == "retired":
            skipped[rid] = "retired"
            continue
        if rid in _ablated():
            from .registry import note_ablation_reached
            note_ablation_reached(rid)
            skipped[rid] = ("ablated (INV_ABLATE); an enforce rule, so the scan cannot pass"
                            if raw.get("status") == "enforce" else "ablated (INV_ABLATE)")
            continue
        if "sink" not in med:
            if "door" in med or "tables" in med:
                raise ScanConfigError(f"{rid}: mediation.door and mediation.tables need mediation.sink")
            skipped[rid] = "mediation declares no sink"
            continue
        sinks = _str_list(med["sink"], f"{rid}: mediation.sink")
        default_door = (raw.get("subject") or {}).get("target")
        if "door" in med:
            doors = _str_list(med["door"], f"{rid}: mediation.door")
        elif isinstance(default_door, str):
            doors = [default_door]
        else:
            raise ScanConfigError(f"{rid}: mediation.sink needs mediation.door or a subject.target")
        for p in sinks + doors:
            if not p.startswith("py:"):
                raise ScanConfigError(f"{rid}: {p!r} must be a 'py:module.name' path")
        tables = med["tables"] if "tables" in med else []
        tables = [tables] if isinstance(tables, (str, dict)) else tables
        if tables:
            table_specs(tables, f"{rid}: mediation.tables")
        scanned[rid] = Mediation(rid, sinks, doors, list(tables))
    return scanned, skipped


_IN_PROCESS = frozenset({"decorator", "middleware", "model_call", "edge"})


def table_warnings(doc: dict[str, Any], scanned: dict[str, Mediation], skipped: dict[str, str]) -> dict[str, str]:
    """Rules whose door guards a transactional or storage effect in process but that declare no
    mediation.tables. Raw SQL that writes the protected table outside the door goes around the guard
    and the scan does not see it, so the scan alone cannot report such a rule held."""
    targets = doc.get("targets") or {}
    out: dict[str, str] = {}
    for raw in doc.get("rules") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            continue
        rid = raw["id"]
        if rid in skipped and rid not in scanned and skipped[rid] != "mediation declares no sink":
            continue                                     # retired or ablated: said already
        enfs = [e for e in raw.get("enforcers") or [] if isinstance(e, dict)]
        target = (raw.get("subject") or {}).get("target")
        if not isinstance(target, str) or not any(e.get("point") in _IN_PROCESS for e in enfs):
            continue                                     # storage-only rules are not bypassed by raw SQL
        med: dict[str, Any] = raw["mediation"] if isinstance(raw.get("mediation"), dict) else {}
        refs = [target]
        for k in ("door", "sink"):
            v = med.get(k)
            refs += [v] if isinstance(v, str) else [x for x in v or [] if isinstance(x, str)]
        txn = any((targets.get(r) or {}).get("effects") == "transactional" for r in refs)
        storage = any(e.get("point") == "storage" for e in enfs)
        if not (txn or storage) or med.get("tables"):
            continue
        kind = ", ".join(x for x, on in (("a transactional door", txn), ("a storage enforcer", storage)) if on)
        if rid in scanned:
            out[rid] = (f"protects stored data ({kind}) but declares no mediation.tables: raw SQL that writes its tables "
                        f"outside the door is not checked, so it cannot be reported as held on the scan alone "
                        f"(declare mediation.tables; {{table: [insert, update]}} limits it to statement kinds)")
        else:
            out[rid] = (f"protects stored data ({kind}) but declares no mediation.sink or mediation.tables: inv scan cannot "
                        f"check that every write passes the door, so it cannot be reported as held on the scan "
                        f"alone")
    return out


def _ablated() -> set[str]:
    """Rules turned off for an ablation run: INV_ABLATE (when armed) and in-process ablated_everywhere."""
    from .registry import GLOBAL_ABLATED, env_ablated
    return env_ablated() | set(GLOBAL_ABLATED)


# --- indexing the source ------------------------------------------------------------------------
@dataclasses.dataclass
class _Func:
    id: str
    module: str
    file: pathlib.Path
    root: pathlib.Path
    line: int
    col: int
    node: ast.AST | None         # None for a module's top-level pseudo-function
    cls: str | None = None


@dataclasses.dataclass
class _Module:
    name: str
    file: pathlib.Path
    root: pathlib.Path
    tree: ast.Module
    package: str
    aliases: dict[str, str] = dataclasses.field(default_factory=dict)
    star: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class _Edge:
    caller: str
    callee: str
    line: int
    col: int
    inferred: bool               # a name-match edge (unknown receiver), not an extracted one
    via_getattr: bool = False    # getattr(obj, "literal"): reported as forbidden when it names the sink


@dataclasses.dataclass
class _Site:
    """Something the second pass saw at one place, for the per-rule checks."""

    what: str                    # getattr | setattr | ref | attrcall | wrapped | reflection | dynimport | sql | patch
    owner: str
    line: int
    col: int
    detail: str = ""
    const: str = ""              # a module-level string constant this sql site is the value of


def _excluded(rel: pathlib.PurePath, patterns: Iterable[str]) -> bool:
    """True if a root-relative posix path (or any parent directory of it) matches a glob."""
    text = rel.as_posix()
    heads = [pathlib.PurePosixPath(*rel.parts[:i]).as_posix() for i in range(1, len(rel.parts))]
    return any(fnmatch.fnmatch(text, p) or any(fnmatch.fnmatch(h, p.rstrip("/")) for h in heads)
               for p in patterns)


def iter_sources(roots: Iterable[pathlib.Path],
                 exclude: Iterable[str] = ()) -> Iterator[tuple[pathlib.Path, pathlib.Path, str]]:
    """(root, file, module name) for every .py file under the roots, minus exclude globs (root-relative)."""
    exclude = list(exclude)
    roots = list(dict.fromkeys(r.resolve() for r in roots))
    dirs = [r for r in roots if r.is_dir()]
    for root in roots:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        base = root.parent if root.is_file() else root
        inner = [d for d in dirs if d != root and d.is_relative_to(root)]
        for f in files:
            if any(f.is_relative_to(d) for d in inner):
                continue                               # named from the deeper root that holds it
            rel = f.relative_to(base)
            if any(p in SKIP_DIRS or p.startswith(".") for p in rel.parts[:-1]):
                continue
            if exclude and _excluded(rel, exclude):
                continue
            parts = list(rel.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            if parts:
                yield base, f, ".".join(parts)


class _Index:
    """Modules, functions, classes and aliases across every source root."""

    def __init__(self) -> None:
        self.modules: dict[str, _Module] = {}
        self.funcs: dict[str, _Func] = {}
        self.classes: dict[str, dict[str, Any]] = {}     # id -> {methods: {name: id}, bases: [expr], module}
        self.methods_by_name: collections.defaultdict[str, set[str]] = collections.defaultdict(set)
        self.parse_errors: list[tuple[pathlib.Path, pathlib.Path, int, str]] = []
        self.consts: set[str] = set()                    # module-level string constants, 'mod.NAME'

    def add(self, root: pathlib.Path, file: pathlib.Path, name: str) -> None:
        try:
            tree = ast.parse(file.read_bytes(), filename=str(file))
        except (SyntaxError, ValueError) as e:
            self.parse_errors.append((root, file, getattr(e, "lineno", 0) or 0, str(e)))
            return
        package = name if file.name == "__init__.py" else name.rpartition(".")[0]
        mod = _Module(name, file, root, tree, package)
        self.modules[name] = mod
        self.funcs[f"{name}.<module>"] = _Func(f"{name}.<module>", name, file, root, 1, 0, None)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        mod.aliases[a.asname] = a.name
                    else:
                        top = a.name.split(".")[0]
                        mod.aliases[top] = top
            elif isinstance(node, ast.ImportFrom):
                src = self._absolute(mod, node)
                for a in node.names:
                    if a.name == "*":
                        mod.star.append(src)
                    else:
                        mod.aliases[a.asname or a.name] = f"{src}.{a.name}" if src else a.name
        self._defs(mod, tree.body, prefix=name, cls=None)
        for node in tree.body:                             # NAME = "INSERT ...": resolvable where it is used
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None \
                    and _fold_str(node.value) is not None:
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    if isinstance(t, ast.Name):
                        self.consts.add(f"{name}.{t.id}")
                        mod.aliases.setdefault(t.id, f"{name}.{t.id}")

    @staticmethod
    def _absolute(mod: _Module, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        base = mod.package.split(".") if mod.package else []
        base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
        return ".".join([*base, *([node.module] if node.module else [])])

    def _defs(self, mod: _Module, body: list[ast.stmt], prefix: str, cls: str | None) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fid = f"{prefix}.{node.name}"
                self.funcs[fid] = _Func(fid, mod.name, mod.file, mod.root, node.lineno, node.col_offset, node, cls)
                if cls is None:
                    mod.aliases.setdefault(node.name, fid)
                else:
                    self.classes[cls]["methods"][node.name] = fid
                    self.methods_by_name[node.name].add(fid)
            elif isinstance(node, ast.ClassDef):
                cid = f"{prefix}.{node.name}"
                self.classes[cid] = {"methods": {}, "bases": node.bases, "module": mod.name}
                if cls is None:
                    mod.aliases.setdefault(node.name, cid)
                self._defs(mod, node.body, prefix=cid, cls=cid)

    # canonical names ---------------------------------------------------------------------------
    def canon(self, fq: str, depth: int = 0) -> str:
        """Follow re-exports and inheritance until fq names a known function, class or module."""
        if fq in self.funcs or fq in self.classes or fq in self.modules or depth > 12:
            return fq
        parts = fq.split(".")
        for i in range(len(parts) - 1, 0, -1):
            head = ".".join(parts[:i])
            if head in self.classes:
                m = self.method(head, parts[i])
                if m:
                    return self.canon(".".join([m, *parts[i + 1:]]), depth + 1)
                return fq
            if head in self.modules:
                mod = self.modules[head]
                name = parts[i]
                target = mod.aliases.get(name) or self._star_lookup(mod, name, depth)
                if target and target != fq and target != ".".join(parts[: i + 1]):
                    return self.canon(".".join([target, *parts[i + 1:]]), depth + 1)
                return fq
        return fq

    def _star_lookup(self, mod: _Module, name: str, depth: int) -> str | None:
        for src in mod.star:
            if src in self.modules and depth < 12:
                hit = self.canon(f"{src}.{name}", depth + 1)
                if hit in self.funcs or hit in self.classes or hit in self.modules:
                    return hit
        return None

    def method(self, cls: str, name: str, seen: set[str] | None = None) -> str | None:
        """A method by name on cls or its project-local bases."""
        seen = seen or set()
        if cls in seen or cls not in self.classes:
            return None
        seen.add(cls)
        info = self.classes[cls]
        if name in info["methods"]:
            return info["methods"][name]
        mod = self.modules[info["module"]]
        for b in info["bases"]:
            base = _dotted(b)
            if base:
                head, _, rest = base.partition(".")
                fq = self.canon(".".join(filter(None, [mod.aliases.get(head, f"{mod.name}.{head}"), rest])))
                hit = self.method(fq, name, seen)
                if hit:
                    return hit
        return None


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


# --- the second pass: calls and sites -----------------------------------------------------------
class _Walker:
    """Resolves calls and records sites in one module, attributing nested code to its owner."""

    def __init__(self, idx: _Index, mod: _Module) -> None:
        self.idx, self.mod = idx, mod
        self.edges: list[_Edge] = []
        self.sites: list[_Site] = []
        self.doc_ids = _docstrings(mod.tree)

    def run(self) -> None:
        self._body(self.mod.tree.body, f"{self.mod.name}.<module>", set(), None, self.mod.name)

    def _body(self, body: list[ast.stmt], owner: str, local: set[str], self_cls: tuple[str, str] | None,
              prefix: str) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and f"{prefix}.{stmt.name}" in self.idx.funcs \
                    and owner.endswith(".<module>"):
                fid = f"{prefix}.{stmt.name}"
                for d in [*stmt.decorator_list, *stmt.args.defaults, *[k for k in stmt.args.kw_defaults if k]]:
                    self._expr(d, owner, local, self_cls)
                func = self.idx.funcs[fid]
                me = None
                if func.cls and stmt.args.args and not any(_dotted(d) == "staticmethod" for d in stmt.decorator_list):
                    me = (stmt.args.args[0].arg, func.cls)
                for s in stmt.body:
                    self._node(s, fid, _locals(stmt), me)
            elif isinstance(stmt, ast.ClassDef) and owner.endswith(".<module>") \
                    and f"{prefix}.{stmt.name}" in self.idx.classes:
                for d in [*stmt.decorator_list, *stmt.bases, *[k.value for k in stmt.keywords]]:
                    self._expr(d, owner, local, self_cls)
                self._body(stmt.body, owner, local, self_cls, f"{prefix}.{stmt.name}")
            else:
                before = len(self.sites)
                self._node(stmt, owner, local, self_cls)
                if owner.endswith(".<module>") and prefix == self.mod.name \
                        and isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                    names = [t.id for t in (stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target])
                             if isinstance(t, ast.Name)]
                    if len(names) == 1 and f"{prefix}.{names[0]}" in self.idx.consts:
                        for site in self.sites[before:]:
                            if site.what == "sql":
                                site.const = f"{prefix}.{names[0]}"

    def _node(self, node: ast.AST, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        if isinstance(node, ast.expr):
            self._expr(node, owner, local, me)
            return
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute):
                    fq = self._resolve(t, local, me)
                    if fq:
                        self._site("patch", owner, t, fq)
        for child in ast.iter_child_nodes(node):
            self._node(child, owner, local, me)

    def _site(self, what: str, owner: str, node: ast.AST, detail: str = "") -> None:
        self.sites.append(_Site(what, owner, getattr(node, "lineno", 0), getattr(node, "col_offset", 0), detail))

    def _resolve(self, node: ast.AST, local: set[str], me: tuple[str, str] | None) -> str | None:
        """The canonical name a Name/Attribute chain refers to, or None when it is a runtime value."""
        if isinstance(node, ast.Name):
            if me and node.id == me[0]:
                return f"self:{me[1]}"
            if node.id in local:
                return None
            hit = self.mod.aliases.get(node.id) or self.idx._star_lookup(self.mod, node.id, 0)
            if hit:
                return self.idx.canon(hit)
            return f"builtins.{node.id}" if node.id in _BUILTINS else None
        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value, local, me)
            if base is None:
                return None
            if base.startswith("self:"):
                return self.idx.method(base[5:], node.attr)
            return self.idx.canon(f"{base}.{node.attr}")
        return None

    def _namespace(self, node: ast.AST, local: set[str], me: tuple[str, str] | None) -> str | None:
        """The module or class a Name/Attribute chain names, else None."""
        if not _dotted(node):
            return None
        fq = self._resolve(node, local, me)
        return fq if fq and (fq in self.idx.modules or fq in self.idx.classes) else None

    def _lookup(self, owner: str, node: ast.AST, ns: str, key: ast.AST | None, how: str) -> None:
        """A name looked up in the namespace of module or class `ns` (ns.__dict__[k], .get(k),
        __getattribute__(k)): a literal key is a getattr with that name, anything else reflection."""
        lit = key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None
        if lit is None:
            self._site("reflection", owner, node, f"{ns}.{how}")
            return
        self._site("getattr", owner, node, f"{ns}|{lit}")
        hit = self.idx.canon(f"{ns}.{lit}")
        if hit and hit in self.idx.funcs:
            self.edges.append(_Edge(owner, hit, getattr(node, "lineno", 0), getattr(node, "col_offset", 0),
                                    False, via_getattr=True))

    def _reflect(self, node: ast.AST, owner: str, local: set[str], me: tuple[str, str] | None) -> bool:
        """Namespace reflection on a scanned module or class: `m.__dict__` in any use,
        `m.__getattribute__(k)`, `object.__getattribute__(m, k)` (or any `X.__getattribute__(m, k)`)
        and `inspect.getmembers(m)`. Records the site and visits the rest; True when handled.
        A function's `__dict__` is its metadata and stays harmless."""
        def visit(*xs: ast.AST | None) -> None:
            for x in xs:
                if x is not None:
                    self._expr(x, owner, local, me)

        def ns_dict(x: ast.AST) -> str | None:
            if isinstance(x, ast.Attribute) and x.attr == "__dict__":
                return self._namespace(x.value, local, me)
            return None

        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and (ns := ns_dict(node.value)):
            self._lookup(owner, node, ns, node.slice, "__dict__[...]")
            visit(node.slice)
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            f = node.func
            key = node.args[0] if node.args else None
            if (ns := ns_dict(f.value)) is not None:
                if f.attr == "get":
                    self._lookup(owner, node, ns, key, "__dict__.get(...)")
                else:                                                   # .items(), .values(), ...
                    self._site("reflection", owner, node, f"{ns}.__dict__.{f.attr}()")
                visit(*node.args, *[k.value for k in node.keywords])
                return True
            if f.attr == "__getattribute__":
                ns = self._namespace(f.value, local, me)
                if ns:                                                  # m.__getattribute__(k)
                    self._lookup(owner, node, ns, key, "__getattribute__(...)")
                    visit(*node.args, *[k.value for k in node.keywords])
                    return True
                ns = self._namespace(node.args[0], local, me) if node.args else None
                if ns:                                                  # object.__getattribute__(m, k)
                    self._lookup(owner, node, ns, node.args[1] if len(node.args) > 1 else None,
                                 "__getattribute__(...)")
                    visit(f.value, *node.args[1:], *[k.value for k in node.keywords])
                    return True
        if isinstance(node, ast.Call) and _dotted(node.func) and node.args:
            fq = self._resolve(node.func, local, me)
            if fq in ("inspect.getmembers", "inspect.getmembers_static") \
                    and self._namespace(node.args[0], local, me):
                self._site("reflection", owner, node, fq)
                visit(*node.args[1:], *[k.value for k in node.keywords])
                return True
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and (ns := ns_dict(node)):
            self._site("reflection", owner, node, f"{ns}.__dict__")        # vars(m) by another name
            return True
        return False

    def _expr(self, node: ast.AST, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        if self._reflect(node, owner, local, me):
            return
        if isinstance(node, ast.Call):
            self._call(node, owner, local, me)
            if not _dotted(node.func):
                self._expr(node.func, owner, local, me)
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "__wrapped__":
                self._site("wrapped", owner, node.func, _dotted(node.func.value) or "")
            elif isinstance(node.func, ast.Attribute) and _dunder(node.func.attr) \
                    and node.func.attr not in ("__call__", *_HARMLESS_DUNDERS):
                base = self._resolve(node.func.value, local, me)  # f.__get__(o)(...): f used as a value
                if base and base in self.idx.funcs:
                    self._site("ref", owner, node.func, base)
            for a in [*node.args, *[k.value for k in node.keywords]]:
                self._expr(a, owner, local, me)
            return
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            if isinstance(node, ast.Attribute) and node.attr == "__wrapped__":
                self._site("wrapped", owner, node, _dotted(node.value) or "")
            elif isinstance(node, ast.Attribute) and _dunder(node.attr) and node.attr not in _HARMLESS_DUNDERS:
                base = self._resolve(node.value, local, me)       # f.__call__, f.__get__, f.__func__ read
                if base and base in self.idx.funcs:               # as a value: f escapes
                    self._site("ref", owner, node, base)
            fq = self._resolve(node, local, me)
            if fq and fq in self.idx.funcs:
                self._site("ref", owner, node, fq)
            elif fq and fq in self.idx.consts:
                self._site("const", owner, node, fq)
            if isinstance(node, ast.Attribute) and not _dotted(node):
                self._expr(node.value, owner, local, me)
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in self.doc_ids:
            self._site("sql", owner, node, node.value)
            return
        if isinstance(node, ast.JoinedStr):
            text = "".join(str(v.value) if isinstance(v, ast.Constant) else "{}" for v in node.values)
            self._site("sql", owner, node, text)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            folded = _fold_str(node)
            if folded is not None:                  # 'INSERT INTO ' + 'leases ...': one text, one site
                self._site("sql", owner, node, folded)
                for part in _add_leaves(node):
                    if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                        if isinstance(part, ast.JoinedStr):
                            for v in part.values:
                                if not isinstance(v, ast.Constant):
                                    self._expr(v, owner, local, me)
                        else:
                            self._expr(part, owner, local, me)
                return
        if isinstance(node, ast.Lambda):
            local = local | _locals(node)
        for child in ast.iter_child_nodes(node):
            if not (isinstance(node, ast.JoinedStr) and isinstance(child, ast.Constant)):
                self._expr(child, owner, local, me)

    def _call(self, node: ast.Call, owner: str, local: set[str], me: tuple[str, str] | None) -> None:
        func = node.func
        while isinstance(func, ast.Attribute) and func.attr == "__call__":    # f.__call__(x) calls f
            func = func.value
        fq = self._resolve(func, local, me) if _dotted(func) else None
        if func is not node.func and not (fq and (fq in self.idx.funcs or fq in self.idx.classes)):
            func, fq = node.func, None                 # not a known function: an ordinary attribute call
        if fq in ("builtins.getattr", "builtins.setattr", "builtins.delattr", "builtins.hasattr") and node.args:
            name = node.args[1] if len(node.args) > 1 else None
            lit = name.value if isinstance(name, ast.Constant) and isinstance(name.value, str) else None
            obj = self._resolve(node.args[0], local, me)
            self._site(fq.split(".")[1], owner, node, f"{obj or '?'}|{lit if lit is not None else ''}")
            if lit is not None and obj and fq == "builtins.getattr":     # a literal name is an edge
                if obj in self.idx.funcs and lit == "__call__":          # getattr(f, "__call__") is f
                    self.edges.append(_Edge(owner, obj, node.lineno, node.col_offset, False))
                    return
                if obj in self.idx.funcs and _dunder(lit):              # __get__, __func__, ...: f as a value
                    self._site("ref", owner, node, obj)
                    return
                hit = self.idx.method(obj[5:], lit) if obj.startswith("self:") else self.idx.canon(f"{obj}.{lit}")
                if hit and hit in self.idx.funcs:
                    self.edges.append(_Edge(owner, hit, node.lineno, node.col_offset, False, via_getattr=True))
            return
        if fq and fq.startswith("builtins.") and fq.split(".", 1)[1] in REFLECTION:
            self._site("reflection", owner, node, fq.split(".", 1)[1])
            return
        if fq in DYNAMIC_IMPORT:
            self._site("dynimport", owner, node, fq)
            return
        if fq in ("operator.attrgetter", "operator.methodcaller") and node.args and \
                isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            self._site("getattr", owner, node, f"?|{node.args[0].value}")
            return
        if fq and fq in self.idx.funcs:
            self.edges.append(_Edge(owner, fq, node.lineno, node.col_offset, False))
        elif fq and fq in self.idx.classes:
            init = self.idx.method(fq, "__init__")
            if init:
                self.edges.append(_Edge(owner, init, node.lineno, node.col_offset, False))
        elif fq is None and isinstance(func, ast.Attribute):
            self._site("attrcall", owner, func, func.attr)
            for m in sorted(self.idx.methods_by_name.get(func.attr, ())):
                self.edges.append(_Edge(owner, m, node.lineno, node.col_offset, True))


# --- the scan -----------------------------------------------------------------------------------
def _canon_py(idx: _Index, p: str) -> str:
    return idx.canon(p[3:])


def scan(doc: dict[str, Any], roots: Iterable[str | pathlib.Path], exclude: Iterable[str] = ()) -> Report:
    """Scan the source roots (minus exclude globs) against every rule that declares mediation.sink."""
    scanned, skipped = mediations(doc)
    idx = _Index()
    for root, f, name in iter_sources((pathlib.Path(r) for r in roots), exclude):
        idx.add(root, f, name)
    edges: list[_Edge] = []
    sites: list[_Site] = []
    for mod in idx.modules.values():
        w = _Walker(idx, mod)
        w.run()
        edges += w.edges
        sites += w.sites
    report = Report([], scanned, skipped, files=len(idx.modules) + len(idx.parse_errors),
                    functions=sum(1 for f in idx.funcs.values() if f.node is not None),
                    warnings=table_warnings(doc, scanned, skipped))
    entries = _entrypoints(idx, edges)
    for med in scanned.values():
        report.findings += _check_rule(idx, med, edges, sites, entries)
        writes = sink_writes(idx, med, edges, sites)
        report.sink_writes[med.rule] = {("(computed)" if t is None else t): sorted(o)
                                        for t, o in sorted(writes.items(), key=lambda kv: str(kv[0]))}
        gap = _uncovered(writes, med.table_ops)
        if gap:
            report.uncovered_tables[med.rule] = gap
            what = ", ".join(f"{t} ({', '.join(o) or 'computed'})" for t, o in gap.items())
            msg = (f"its sink writes {what} with raw SQL, which mediation.tables does not cover: raw SQL that "
                   f"writes those tables outside the door is not checked, so it cannot be reported as held on "
                   f"the scan alone (add them to mediation.tables; {{table: [insert, update]}} limits it to "
                   f"statement kinds)")
            prior = report.warnings.get(med.rule)
            report.warnings[med.rule] = msg if not prior else f"{prior}; {msg}"
    order = {k: i for i, k in enumerate(KINDS)}
    report.findings.sort(key=lambda f: (f.rule, order[f.kind], f.file or "", f.line, f.col, f.code))
    return report


def _entrypoints(idx: _Index, edges: list[_Edge]) -> list[str]:
    """Functions with no extracted caller, module bodies, then anything those cannot reach.

    A name-matched caller is not proof, so it does not stop a function being an entrypoint.
    """
    proven = [e for e in edges if not e.inferred]
    called = {e.callee for e in proven if e.callee != e.caller}
    out = sorted(f for f in idx.funcs if f not in called)
    succ = collections.defaultdict(set)
    for e in proven:
        succ[e.caller].add(e.callee)
    seen = set(out)
    stack = list(out)
    for fid in sorted(idx.funcs):
        if not stack and fid not in seen:
            out.append(fid)
            seen.add(fid)
            stack.append(fid)
        while stack:
            for n in succ[stack.pop()]:
                if n not in seen:
                    seen.add(n)
                    stack.append(n)
    return out


def _loc(idx: _Index, owner: str) -> tuple[pathlib.Path, pathlib.Path]:
    f = idx.funcs[owner]
    return f.root, f.file


def _finding(idx: _Index, rule: str, kind: str, code: str, msg: str, owner: str | None = None,
             line: int = 0, col: int = 0, path: list[str] | None = None) -> Finding:
    file = root = None
    if owner and owner in idx.funcs:
        r, f = _loc(idx, owner)
        file, root = f.relative_to(r).as_posix(), str(r)
        if not line:
            line, col = idx.funcs[owner].line, idx.funcs[owner].col
    return Finding(kind, rule, code, msg, file, line, col + 1 if file else 0, root, path or [])


def _check_rule(idx: _Index, med: Mediation, edges: list[_Edge], sites: list[_Site],
                entries: list[str]) -> list[Finding]:
    rid, out = med.rule, []
    for root, file, line, err in idx.parse_errors:
        out.append(Finding("unresolved", rid, "unresolved/parse-error", f"cannot parse: {err}",
                           file.relative_to(root).as_posix(), line, 1, str(root)))
    sinks, doors = set(), set()
    for p in med.sinks:
        c = _canon_py(idx, p)
        if c in idx.funcs:
            sinks.add(c)
        else:
            out.append(Finding("missing", rid, "missing/sink", f"sink {p} is not defined in the scanned code"))
    for p in med.doors:
        c = _canon_py(idx, p)
        if c in idx.funcs:
            doors.add(c)
        else:
            out.append(Finding("missing", rid, "missing/door", f"door {p} is not defined in the scanned code"))
    trusted = sinks | doors
    dominated = door_only(idx, edges, sites, entries, trusted)
    safe_consts = _consts_used_only_by(sites, trusted | dominated)
    sink_names = {s.rsplit(".", 1)[1] for s in sinks}
    guarded_names = sink_names | {d.rsplit(".", 1)[1] for d in doors}
    tables = med.table_ops
    door_txt = ", ".join(sorted(doors)) or "(no door found)"

    # 1. paths from entrypoints to the sink with the doors removed
    live = sinks - doors
    if live:
        succ: dict[str, list[_Edge]] = collections.defaultdict(list)
        for e in edges:
            if e.caller not in doors and e.callee not in doors and not (e.via_getattr and e.callee in sinks):
                succ[e.caller].append(e)
        for start in entries:
            if start in trusted:
                continue
            for inferred_ok in (False, True):
                hit = _shortest(start, live, succ, inferred_ok)
                if hit:
                    names, last = hit
                    kind = "unresolved" if inferred_ok else "bypass"
                    via = " (through a name-matched call on an unknown receiver)" if inferred_ok else ""
                    out.append(_finding(idx, rid, kind, "unresolved/inferred-path" if inferred_ok else "bypass",
                                        f"{' -> '.join(names)} reaches the sink without passing {door_txt}{via}",
                                        last.caller, last.line, last.col, names))
                    break

    # 2. idioms, per site
    for s in sites:
        if s.owner in trusted and s.what in ("ref", "sql", "attrcall"):
            continue
        if s.owner in dominated and s.what in ("sql", "attrcall"):
            continue                                   # reached only through a door or sink
        if s.what == "sql" and s.const and s.const in safe_consts:
            continue                                   # a constant only door or sink code executes
        obj, _, lit = s.detail.partition("|")
        if s.what == "getattr" and lit in guarded_names:
            what = "sink" if lit in sink_names else "door"
            out.append(_finding(idx, rid, "forbidden", f"forbidden/getattr-{what}",
                                f"getattr-style lookup of the {what} name {lit!r} hides the call from the scan",
                                s.owner, s.line, s.col))
        elif s.what in ("getattr", "setattr", "delattr", "hasattr") and lit == "__wrapped__":
            out.append(_finding(idx, rid, "forbidden", "forbidden/unwrap",
                                "reaches __wrapped__, which calls a guarded function without its guard",
                                s.owner, s.line, s.col))
        elif s.what in ("setattr", "delattr") and lit in guarded_names:
            out.append(_finding(idx, rid, "forbidden", "forbidden/monkeypatch",
                                f"{s.what} on {lit!r} replaces the sink or door at run time", s.owner, s.line, s.col))
        elif s.what in ("getattr", "setattr", "delattr") and not lit and s.what != "hasattr":
            out.append(_finding(idx, rid, "unresolved", "unresolved/computed-attribute",
                                f"{s.what} with a computed name on {obj}: the scan cannot tell whether it reaches "
                                f"the sink", s.owner, s.line, s.col))
        elif s.what == "patch" and s.detail in trusted:
            out.append(_finding(idx, rid, "forbidden", "forbidden/monkeypatch",
                                f"assigns over {s.detail}, replacing the sink or door at run time",
                                s.owner, s.line, s.col))
        elif s.what == "wrapped":
            out.append(_finding(idx, rid, "forbidden", "forbidden/unwrap",
                                f"{s.detail or 'an object'}.__wrapped__ calls a guarded function without its guard",
                                s.owner, s.line, s.col))
        elif s.what == "ref" and s.detail in sinks:
            out.append(_finding(idx, rid, "unresolved", "unresolved/sink-escapes",
                                f"the sink {s.detail} is used as a value, so calls through it cannot be traced",
                                s.owner, s.line, s.col))
        elif s.what == "attrcall" and s.detail in sink_names:
            out.append(_finding(idx, rid, "unresolved", "unresolved/unknown-receiver",
                                f".{s.detail}() on a receiver the scan cannot resolve may be the sink",
                                s.owner, s.line, s.col))
        elif s.what in ("reflection", "dynimport"):
            out.append(_finding(idx, rid, "unresolved", "unresolved/reflection",
                                f"{s.detail} runs or looks up code the scan cannot follow", s.owner, s.line, s.col))
        elif s.what == "sql" and tables:
            for w in sql_writes(s.detail):
                if w.table is None:
                    if any(w.ops & ops for ops in tables.values()):
                        out.append(_finding(idx, rid, "unresolved", "unresolved/computed-sql",
                                            f"raw SQL {w.verb} on a computed table name: the scan cannot tell "
                                            f"whether it writes a protected table", s.owner, s.line, s.col))
                elif w.table in tables and w.ops & tables[w.table]:
                    out.append(_finding(idx, rid, "forbidden", "forbidden/raw-sql",
                                        f"raw SQL {w.verb} on the protected table {w.table!r} "
                                        f"outside the sink and door", s.owner, s.line, s.col))
            if _drops_trigger(s.detail):
                out.append(_finding(idx, rid, "forbidden", "forbidden/raw-sql",
                                    "raw SQL drops a trigger, which can remove a storage enforcer",
                                    s.owner, s.line, s.col))
    return out


def _reach(starts: Iterable[str], edges: list[_Edge], blocked: set[str]) -> set[str]:
    """Functions reachable from starts over every call edge (name-matched ones too) without entering
    or leaving a blocked function (the starts themselves are included unless blocked)."""
    succ: dict[str, set[str]] = collections.defaultdict(set)
    for e in edges:
        succ[e.caller].add(e.callee)
    seen = {s for s in starts if s not in blocked}
    stack = list(seen)
    while stack:
        for n in succ[stack.pop()]:
            if n not in seen and n not in blocked:
                seen.add(n)
                stack.append(n)
    return seen


def door_only(idx: _Index, edges: list[_Edge], sites: list[_Site], entries: list[str],
              trusted: set[str]) -> set[str]:
    """Helpers every path to which passes a door or sink: reachable from them, not reachable from any
    entrypoint without them, and never used as a value (a value can be called from anywhere)."""
    if not trusted:
        return set()
    free = _reach(entries, edges, trusted)
    from_trusted = _reach([n for e in edges if e.caller in trusted for n in [e.callee]], edges, trusted)
    escaped = {s.detail for s in sites if s.what == "ref"}
    return {f for f in from_trusted - free - escaped if f in idx.funcs and idx.funcs[f].node is not None}


def _consts_used_only_by(sites: list[_Site], owners: set[str]) -> set[str]:
    """Module-level string constants used at least once, and only inside the given functions."""
    uses: dict[str, set[str]] = collections.defaultdict(set)
    for s in sites:
        if s.what == "const":
            uses[s.detail].add(s.owner)
    return {c for c, who in uses.items() if who and who <= owners}


def sink_writes(idx: _Index, med: Mediation, edges: list[_Edge],
                sites: list[_Site]) -> dict[str | None, set[str]]:
    """Tables (None: a computed name) and statement kinds the rule's sink code writes with raw SQL:
    SQL in the sinks, in what they call (not through a door), and in the constants they use."""
    sinks = {c for c in (_canon_py(idx, p) for p in med.sinks) if c in idx.funcs}
    doors = {c for c in (_canon_py(idx, p) for p in med.doors) if c in idx.funcs}
    owners = _reach(sinks, [e for e in edges if not e.inferred], doors - sinks)
    consts = {s.detail for s in sites if s.what == "const" and s.owner in owners}
    out: dict[str | None, set[str]] = collections.defaultdict(set)
    for s in sites:
        if s.what == "sql" and (s.owner in owners or (s.const and s.const in consts)):
            for w in sql_writes(s.detail):
                out[w.table] |= w.ops
    return dict(out)


def _uncovered(writes: dict[str | None, set[str]], tables: dict[str, frozenset[str]]) -> dict[str, list[str]]:
    return {("(computed)" if t is None else t): sorted(ops - (tables.get(t, frozenset()) if t else frozenset()))
            for t, ops in sorted(writes.items(), key=lambda kv: str(kv[0]))
            if t is None and not tables or t is not None and ops - tables.get(t, frozenset())}


def _shortest(start: str, goals: set[str], succ: dict[str, list[_Edge]],
              inferred_ok: bool) -> tuple[list[str], _Edge] | None:
    """BFS: the shortest call path from start to a goal, and its last edge."""
    prev: dict[str, _Edge | None] = {start: None}
    queue = collections.deque([start])
    while queue:
        n = queue.popleft()
        for e in succ.get(n, ()):
            if e.inferred and not inferred_ok or e.callee in prev:
                continue
            prev[e.callee] = e
            if e.callee in goals:
                names, last = [e.callee], e
                cur: _Edge | None = e
                while cur is not None:
                    names.append(cur.caller)
                    cur = prev[cur.caller]
                return list(reversed(names)), last
            queue.append(e.callee)
    return None


# --- output -------------------------------------------------------------------------------------
_LEVEL = {"bypass": "error", "forbidden": "error", "missing": "error", "unresolved": "warning"}


def to_text(report: Report) -> str:
    """Human-readable report, one finding per line."""
    lines = []
    for f in report.findings:
        where = f"{f.file}:{f.line}:{f.col}" if f.file else "-"
        lines.append(f"{f.kind:<10} {f.rule:<8} {where}  {f.message}")
    for rid in report.scanned:
        c = report.counts(rid)
        summary = ", ".join(f"{n} {k}" for k, n in c.items()) or "no findings"
        lines.append(f"{rid}: {report.rule_result(rid)} ({summary})")
    for rid, why in report.not_scanned.items():
        lines.append(f"{rid}: not scanned ({why})")
    for rid, why in report.warnings.items():
        lines.append(f"warning    {rid:<8} {why}")
    lines.append(f"scanned {report.files} file(s), {report.functions} function(s)")
    return "\n".join(lines)


def to_sarif(report: Report, version: str = "0") -> dict[str, Any]:
    """SARIF 2.1.0."""
    codes = sorted({f.code for f in report.findings})
    rules = [{"id": c, "name": c.replace("/", "-"), "shortDescription": {"text": c},
              "defaultConfiguration": {"level": _LEVEL[c.split("/")[0]]}} for c in codes]
    results = []
    for f in report.findings:
        res: dict[str, Any] = {
            "ruleId": f.code, "ruleIndex": codes.index(f.code), "level": _LEVEL[f.kind],
            "message": {"text": f"{f.rule}: {f.message}"},
            "properties": {"invRule": f.rule, "kind": f.kind, "path": f.path},
        }
        if f.file:
            res["locations"] = [{"physicalLocation": {
                "artifactLocation": {"uri": f.file},
                "region": {"startLine": max(f.line, 1), "startColumn": max(f.col, 1)}}}]
        if f.path:
            res["codeFlows"] = [{"threadFlows": [{"locations": [
                {"location": {"message": {"text": step}}} for step in f.path]}]}]
        results.append(res)
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "inv scan", "version": version,
                                      "informationUri": "https://example.invalid/invariants", "rules": rules}},
                  "results": results}],
    }
