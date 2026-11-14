"""The scan's shared data types and constants: findings, the report, the index records."""
from __future__ import annotations

import ast
import collections
import dataclasses
import pathlib
from typing import TYPE_CHECKING, Any, Callable, Iterable

if TYPE_CHECKING:
    from .reachability import _Pass

KINDS = ("bypass", "forbidden", "unresolved", "missing")
SKIP_DIRS = frozenset({"__pycache__", "node_modules", "site-packages", "build", "dist", "venv"})
REFLECTION = frozenset({"eval", "exec", "compile", "__import__", "globals", "vars", "locals"})
DYNAMIC_IMPORT = frozenset({"importlib.import_module", "importlib.__import__"})


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
    owner: str | None = None       # the function the finding is in (module.qualname), when known

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Mediation:
    """The scan fields of one rule."""

    rule: str
    sinks: list[str]
    doors: list[str]
    allow: list[str] = dataclasses.field(default_factory=list)   # known back doors, let through


@dataclasses.dataclass
class Report:
    """Everything one scan found."""

    findings: list[Finding]
    scanned: dict[str, Mediation]
    not_scanned: dict[str, str]
    files: int = 0
    functions: int = 0
    # per rule: every function on a path that reaches the sink without the door, from any entrypoint
    # (a finding shows one path per call site; `inv init` freezes all of them); worked out when read
    on_paths: dict[str, set[str]] = dataclasses.field(default_factory=lambda: _LazyOnPaths())
    # fix round 23 (files and roots): files per always-skipped directory name, and notes on the roots and
    # excludes (an exclude glob that matches a file from some of its roots only); shown, never findings
    skipped: dict[str, int] = dataclasses.field(default_factory=dict)
    notes: list[str] = dataclasses.field(default_factory=list)

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
        out: dict[str, Any] = {
            "files": self.files, "functions": self.functions,
            "rules": {rid: {"result": self.rule_result(rid), "sinks": m.sinks, "doors": m.doors,
                            "allow": m.allow, "counts": self.counts(rid)}
                      for rid, m in self.scanned.items()},
            "not_scanned": self.not_scanned,
            "findings": [f.as_dict() for f in self.findings],
        }
        if self.skipped:
            out["skipped"] = dict(self.skipped)
        if self.notes:
            out["notes"] = list(self.notes)
        return out


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


class _OnPaths:
    """The functions on a path that reaches the sink without the door, for one rule: those a finding
    shows are known as the checks run; the rest come from the path searches, run only when asked."""

    def __init__(self) -> None:
        self.known: set[str] = set()
        self._later: list[Callable[[str | None], set[str]]] = []

    def update(self, names: Iterable[str]) -> None:
        self.known.update(names)

    def defer(self, passes: list[_Pass], starts: list[str], allowed: set[str]) -> None:
        from .reachability import _every_path_node
        self._later.append(lambda only: _every_path_node(passes, starts, allowed, only))

    def __contains__(self, name: object) -> bool:
        if name in self.known:
            return True
        if not isinstance(name, str):
            return False
        for later in self._later:
            self.known |= later(name)
            if name in self.known:
                return True
        return False

    def all(self) -> set[str]:
        for later in self._later:
            self.known |= later(None)
        self._later = []
        return self.known


class _LazyOnPaths(dict[str, Any]):
    """Report.on_paths: rule -> its on-path functions (a set), each worked out on first read."""

    def __getitem__(self, rule: str) -> Any:
        got = dict.__getitem__(self, rule)
        return got.all() if isinstance(got, _OnPaths) else got

    def get(self, rule: str, default: Any = None) -> Any:
        return self[rule] if rule in self else default

    def items(self) -> Any:
        return {k: self[k] for k in self}.items()

    def values(self) -> Any:
        return {k: self[k] for k in self}.values()


@dataclasses.dataclass(slots=True)
class _Edge:
    caller: str
    callee: str
    line: int
    col: int
    inferred: bool               # a name-match edge (unknown receiver), not an extracted one
    via_getattr: bool = False    # getattr(obj, "literal"): reported as forbidden when it names the sink


@dataclasses.dataclass(slots=True)
class _Fan:
    """A name-matched call on an unknown receiver (`x.save()`), kept as one record: it stands for an
    inferred edge to each of its callees, in this order (every scanned method of that name; the tuple is
    shared by every call of the name)."""

    caller: str
    callees: tuple[str, ...]
    line: int
    col: int
    via_getattr: bool = False
    inferred: bool = True

    def edges(self) -> list[_Edge]:
        return [_Edge(self.caller, c, self.line, self.col, True, self.via_getattr) for c in self.callees]


@dataclasses.dataclass(slots=True)
class _Site:
    """Something the second pass saw at one place, for the per-rule checks."""

    what: str                    # getattr | setattr | ref | attrcall | wrapped | reflection | dynimport | patch
    #                              | excluded (a reference into a module the exclude globs, or the
    #                                directories the scan always skips, leave out)
    owner: str
    line: int
    col: int
    detail: str = ""
