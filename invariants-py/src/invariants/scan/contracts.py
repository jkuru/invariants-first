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
    # an unresolved finding in a file: the key a person writes in mediation.allow_unresolved to accept it
    # after review (a short hash of its code, function and normalized source line)
    sha: str | None = None
    why: str | None = None         # accepted: the reason its allow_unresolved entry gives
    moved_from: int | None = None  # accepted at another line than its entry says (the code above moved)
    moved_col: int | None = None   # and the column the entry gave, when it gave one (fix round 28)
    # an unresolved/inferred-path finding: what reaches its site, which its sha covers too (the entrypoints
    # and the name-matched calls on the paths through it)
    reached_by: list[str] | None = None
    # accepted by an entry of the file's top-level allow_unresolved (shared by several rules): which one
    accepted_by: str | None = None

    def __getattribute__(self, name: str) -> Any:
        # sha and reached_by are worked out when first read (accept.defer_sha; fix round 28)
        if name == "sha" or name == "reached_by":
            fill = object.__getattribute__(self, "__dict__").get("_fill")
            if fill is not None:
                fill()
        return object.__getattribute__(self, name)

    def as_dict(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        for k in ("sha", "why", "moved_from", "moved_col", "reached_by", "accepted_by"):     # only where they say something
            if out[k] is None:
                del out[k]
        return out


@dataclasses.dataclass(frozen=True)
class Accept:
    """One mediation.allow_unresolved entry: a person reviewed this unresolved finding and accepts it."""

    at: str                        # as written: path:line or path:line:col
    file: str
    line: int
    col: int | None
    code: str                      # unresolved/... (bypass and forbidden findings are never accepted)
    sha: str
    why: str
    # an entry of the file's top-level allow_unresolved: every rule it lists (it accepts its site once
    # under each); () for an entry of the rule's own list
    rules: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at, "code": self.code, "sha": self.sha, "why": self.why,
                **({"rules": list(self.rules)} if self.rules else {})}

    def label(self) -> str | None:
        """Which entry accepted a finding, for a shared one (a rule's own entry is named by its at)."""
        return f"the allow_unresolved entry {self.at} shared by rules {', '.join(self.rules)}" if self.rules else None


@dataclasses.dataclass
class Mediation:
    """The scan fields of one rule."""

    rule: str
    sinks: list[str]
    doors: list[str]
    allow: list[str] = dataclasses.field(default_factory=list)   # known back doors, let through
    # reviewed unresolved findings, accepted one by one (None: the rule has no allow_unresolved)
    accept: list[Accept] | None = None
    # the door-to-sink pairing, when mediation.sink maps each sink to its doors (None: the list form, in
    # which any door may reach any sink); doors is then every door the mapping names
    pairs: dict[str, list[str]] | None = None

    def groups(self) -> list[Mediation]:
        """What the scan checks one at a time: the rule itself (the list form), or one part per set of
        doors (the mapping), holding the sinks that set may reach: a door of another sink is no door there."""
        if self.pairs is None:
            return [self]
        by: dict[tuple[str, ...], list[str]] = {}
        for s in self.sinks:
            by.setdefault(tuple(dict.fromkeys(self.pairs[s])), []).append(s)
        return [Mediation(self.rule, sinks, list(ds), self.allow) for ds, sinks in by.items()]


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
    # unresolved findings a rule's mediation.allow_unresolved accepts after review: shown, never failing
    accepted: list[Finding] = dataclasses.field(default_factory=list)

    def for_rule(self, rule_id: str) -> list[Finding]:
        return [f for f in self.findings if f.rule == rule_id]

    def counts(self, rule_id: str | None = None) -> dict[str, int]:
        c = collections.Counter(f.kind for f in self.findings if rule_id is None or f.rule == rule_id)
        out = {k: c[k] for k in KINDS if c[k]}
        n = sum(1 for f in self.accepted if rule_id is None or f.rule == rule_id)
        if n:
            out["accepted"] = n
        return out

    @property
    def ablated_enforce(self) -> list[str]:
        """Enforce rules the scan skipped because they are ablated: the scan does not pass."""
        return [rid for rid, why in self.not_scanned.items() if why.startswith("ablated") and "enforce" in why]

    def rule_result(self, rule_id: str) -> str:
        """pass | fail | unresolved | not_scanned."""
        if rule_id not in self.scanned:
            return "not_scanned"
        c = self.counts(rule_id)                     # an accepted finding never fails a rule
        if c.get("bypass") or c.get("forbidden") or c.get("missing"):
            return "fail"
        return "unresolved" if c.get("unresolved") else "pass"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "files": self.files, "functions": self.functions,
            "rules": {rid: {"result": self.rule_result(rid), "sinks": m.sinks, "doors": m.doors,
                            **({"pairs": m.pairs} if m.pairs is not None else {}),
                            "allow": m.allow, "counts": self.counts(rid),
                            **({"allow_unresolved": [a.as_dict() for a in m.accept]} if m.accept else {})}
                      for rid, m in self.scanned.items()},
            "not_scanned": self.not_scanned,
            "findings": [f.as_dict() for f in self.findings],
        }
        if self.accepted:
            out["accepted"] = [f.as_dict() for f in self.accepted]
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
    aliases: dict[str, str] = dataclasses.field(default_factory=dict)       # module-scope imports and defs
    star: list[str] = dataclasses.field(default_factory=list)
    # imports inside a function or class body: they bind there, not in the module (fix round 27); a name
    # the module scope does not bind resolves through them, and alternatives() counts every binding
    nested: dict[str, str] = dataclasses.field(default_factory=dict)
    # fix round 28: an absolute import found through the importing file's own directory or a deeper
    # sys.path entry (`import fleet.store` in cli/main.py is cli/fleet/store.py): its first name -> the
    # name the scan indexes it under (`fleet` -> `cli.fleet`)
    rebase: dict[str, str] = dataclasses.field(default_factory=dict)


class _OnPaths:
    """The functions on a path that reaches the sink without the door, for one rule: those a finding
    shows are known as the checks run; the rest, every function on any door-free path to a reported call
    (what `inv init` freezes, and what makes an allow entry in use), come from one search, run when first
    asked (fix round 29: `inv init` asks for each allowed function, and searching again per name cost
    minutes on a large tree)."""

    def __init__(self) -> None:
        self.known: set[str] = set()
        self._later: list[tuple[list[_Pass], list[str], set[str]]] = []

    def update(self, names: Iterable[str]) -> None:
        self.known.update(names)

    def defer(self, passes: list[_Pass], starts: list[str], allowed: set[str]) -> None:
        self._later.append((passes, starts, allowed))

    def __contains__(self, name: object) -> bool:
        if name in self.known:
            return True
        return isinstance(name, str) and bool(self._later) and name in self.all()

    def all(self) -> set[str]:
        from .reachability import _every_path_node
        for later in self._later:
            self.known |= _every_path_node(*later)
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
    # getattr/setattr/delattr/hasattr: the class the receiver is an instance of, when the scan knows it
    # ("C", or "C!" when it is exactly C: built here, or the class object itself), else ""
    recv: str = ""
