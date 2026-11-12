"""The registry of loaded rules, its reach counters, and which registry is current."""
from __future__ import annotations

import collections
import contextlib
import contextvars
import dataclasses
import json
import logging
import os
import pathlib
import sys
import threading
import time
import uuid
import weakref
from typing import Any, Callable, Iterable, Iterator, Protocol

from .errors import CoverageError, LoadError, ReviewNotOpened
from .model import GUARD_POINTS, HOLDS_RACE, Provider, Rule
from .resolve import resolve

ABLATE_ENV = "INV_ABLATE"
ABLATE_ARM_ENV = "INV_ABLATE_TESTING"     # INV_ABLATE is honoured only while this is "1" (the pytest plugin)
ABLATE_TRACE_ENV = "INV_ABLATE_TRACE"     # file the plugin reads: rule ids whose ablation was reached
SINK_ENV = "INV_SINK_SIZE"
DEBUG_ENV = "INV_DEBUG"
REPLY_SIZE_ENV = "INV_MAX_REPLY_SIZE"
DEFAULT_REPLY_SIZE = 1_000_000           # longest model reply text inv.parse accepts (characters or bytes)
DEFAULT_SINK_SIZE = 1000                 # recent events kept per registry; counters are separate
Observer = Callable[[str, str, dict[str, Any]], None]
OBSERVERS: list[Observer] = []          # called on every record() in any registry
log = logging.getLogger("invariants")
log.addHandler(logging.NullHandler())


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


_warned_unarmed = False
_warned_armed: set[str] = set()


_pytest_sessions = 0       # pytest sessions configured in this process (the plugin counts them)


def pytest_session_started() -> None:
    """Called by the pytest plugin's pytest_configure: this process is running a test session."""
    global _pytest_sessions
    _pytest_sessions += 1


def pytest_session_ended() -> None:
    """Called by the pytest plugin's pytest_unconfigure."""
    global _pytest_sessions
    _pytest_sessions = max(0, _pytest_sessions - 1)


def under_pytest() -> bool:
    """True while this process runs a pytest session with the invariants plugin configured.

    Neither `import pytest` (a dependency may do it in production) nor PYTEST_CURRENT_TEST
    (inherited or settable) is proof: only the plugin's pytest_configure counts.
    """
    return _pytest_sessions > 0


def _has_real_handler(lg: logging.Logger) -> bool:
    """True if a record on lg reaches a handler other than a NullHandler (lastResort is off then)."""
    c: logging.Logger | None = lg
    while c is not None:
        if any(not isinstance(h, logging.NullHandler) for h in c.handlers):
            return True
        c = c.parent if c.propagate else None
    return False


def ablation_level() -> int:
    """Log level for honoured env ablation: INFO under pytest, ERROR anywhere else (guards are off)."""
    return logging.INFO if under_pytest() else logging.ERROR


def env_ablated() -> set[str]:
    """Rules named in INV_ABLATE, honoured only when INV_ABLATE_TESTING=1 too; else empty (and a warning).

    Honoured outside pytest (an `inv scan` or app subprocess of a falsifier), it logs at ERROR once
    per rule set: in production it means those rules' in-process guards are off.
    """
    global _warned_unarmed
    names = set(filter(None, (x.strip() for x in os.environ.get(ABLATE_ENV, "").split(","))))
    if not names:
        return set()
    if os.environ.get(ABLATE_ARM_ENV) != "1":
        if not _warned_unarmed:
            _warned_unarmed = True
            log.warning("invariants: %s=%s is ignored: ablation through the environment needs %s=1 "
                        "(set by the pytest plugin)", ABLATE_ENV, ",".join(sorted(names)), ABLATE_ARM_ENV)
        return set()
    key = ",".join(sorted(names))
    if not under_pytest() and key not in _warned_armed:
        _warned_armed.add(key)
        log.error("invariants: %s=%s with %s=1: the in-process guards of these rules are OFF in this "
                  "process (pid %d). This is for test runs only; unset both outside tests.",
                  ABLATE_ENV, key, ABLATE_ARM_ENV, os.getpid())
        if not _has_real_handler(log) or not log.isEnabledFor(logging.ERROR):
            try:                                 # the app set no logging: say it on stderr anyway
                sys.stderr.write(f"invariants: ERROR: {ABLATE_ENV}={key} with {ABLATE_ARM_ENV}=1: the "
                                 f"in-process guards of these rules are OFF in this process "
                                 f"(pid {os.getpid()}). Unset both outside tests.\n")
            except Exception:
                pass
    return names


_noted: set[tuple[str, str]] = set()


def note_ablation_reached(rule_id: str) -> None:
    """Append rule_id to the INV_ABLATE_TRACE file (once per file): the ablation changed behaviour.

    The pytest plugin sets INV_ABLATE_TRACE for each ablated re-run, so it can tell a falsifier that
    failed because the rule was off from one that failed for an unrelated reason. Subprocesses
    inherit the variable, so an app or `inv scan` they start reports reach too.
    """
    path = os.environ.get(ABLATE_TRACE_ENV)
    if not path or (path, rule_id) in _noted:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(rule_id + "\n")
        _noted.add((path, rule_id))
    except OSError:
        pass


class EventSink(collections.deque[dict[str, Any]]):
    """The recent events of one registry: a ring buffer (oldest dropped). Slices give lists."""

    def __getitem__(self, i: Any) -> Any:
        if isinstance(i, slice):
            return list(self)[i]
        return super().__getitem__(i)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return list(self) == other
        return super().__eq__(other)

    __hash__ = None


@dataclasses.dataclass
class Handoff:
    """What route, escalate and page hand to the review store or the pager."""

    rule: str
    action: str                                   # route | escalate | page
    outcome: Any = None                           # the Outcome, when there is one
    bound: dict[str, Any] = dataclasses.field(default_factory=dict)   # the names bad read, as bound
    attempts: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    event: dict[str, Any] = dataclasses.field(default_factory=dict)
    reason: str = ""


class ReviewStore(Protocol):
    """Where route and escalate open a review. open() returns the review id."""

    def open(self, handoff: Handoff) -> str: ...


class MemoryReviews:
    """The default review store: in memory and bounded (the oldest are dropped). Not durable."""

    def __init__(self, maxlen: int = 1000) -> None:
        self.items: collections.deque[dict[str, Any]] = collections.deque(maxlen=maxlen)
        self._n = 0
        self._lock = threading.Lock()

    def open(self, handoff: Handoff) -> str:
        """Record the hand-off and return review-N."""
        with self._lock:
            self._n += 1
            rid = f"review-{self._n}"
            self.items.append({"id": rid, "rule": handoff.rule, "action": handoff.action,
                               "outcome": handoff.outcome, "bound": handoff.bound})
            return rid


def _jsonable(v: Any, depth: int = 0) -> Any:
    """v as JSON data: plain values as they are, containers walked, anything else its repr (never raises)."""
    try:
        if v is None or type(v) in (bool, int, float, str):
            return v
        if depth > 16:
            return "<nested too deeply>"
        if isinstance(v, dict):
            return {str(k): _jsonable(x, depth + 1) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_jsonable(x, depth + 1) for x in v]
        if dataclasses.is_dataclass(v) and not isinstance(v, type):
            return {f.name: _jsonable(getattr(v, f.name), depth + 1) for f in dataclasses.fields(v)}
        return repr(v)
    except Exception:
        return f"<unserialisable {type(v).__name__}>"


class JsonlReviews:
    """A durable review store: each hand-off is appended as one JSON line to path and fsynced.

    Use it as the review store, or as the review_fallback that catches hand-offs when the main
    store fails (inv.configure(review_fallback=JsonlReviews("reviews-fallback.jsonl"))). Values
    that are not JSON data are written as their repr. The file holds the bound names (arguments,
    replies), so give it the protection the review store has.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = pathlib.Path(path)
        self._lock = threading.Lock()

    def open(self, handoff: Handoff) -> str:
        """Append the hand-off and return its id (jsonl-<uuid>)."""
        rid = f"jsonl-{uuid.uuid4().hex}"
        out = handoff.outcome
        line = json.dumps({"id": rid, "time": time.time(), "rule": handoff.rule, "action": handoff.action,
                           "reason": handoff.reason,
                           "outcome": None if out is None else {"kind": getattr(out, "kind", None),
                                                                "messages": _jsonable(getattr(out, "messages", [])),
                                                                "error": _jsonable(getattr(out, "error", ""))},
                           "bound": _jsonable(handoff.bound), "attempts": _jsonable(handoff.attempts),
                           "event": _jsonable(handoff.event)}, sort_keys=True)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        return rid


@dataclasses.dataclass(frozen=True)
class ErrorSpike:
    """errors rule_errors within window seconds demote a rule's effective mode to detect and page."""

    errors: int = 20
    window: float = 60.0


Pager = Callable[[Handoff], None]
Exporter = Callable[[str, str, dict[str, Any]], None]
GLOBAL_ABLATED: collections.Counter[str] = collections.Counter()   # rules ablated in every registry
# (registry, rule id) pairs ablated in this context only: inv.ablated()'s default scope
_CTX_ABLATED: contextvars.ContextVar[tuple[tuple[Registry, str], ...]] = contextvars.ContextVar(
    "inv_ctx_ablated", default=())
_GLOBAL_LOCK = threading.Lock()


class Registry:
    """Loaded rules, providers and targets, plus the event sink, reviews and reach counters."""

    def __init__(self, sink_size: int | None = None) -> None:
        self.rules: dict[str, Rule] = {}
        self.providers: dict[str, Provider] = {}
        self.targets: dict[str, dict[str, Any]] = {}
        self.notes: list[str] = []
        size = _env_int(SINK_ENV, DEFAULT_SINK_SIZE) if sink_size is None else sink_size
        self.sink: EventSink = EventSink(maxlen=size)
        self.review_store: ReviewStore = MemoryReviews()
        self.review_fallback: ReviewStore | None = None   # tried when review_store fails (e.g. JsonlReviews)
        self.max_reply_size = _env_int(REPLY_SIZE_ENV, DEFAULT_REPLY_SIZE)   # 0: no limit
        self.pager: Pager | None = None            # None: page logs at ERROR
        self.exporters: list[Exporter] = []        # called on every record() in this registry
        self.error_spike: ErrorSpike | None = None
        self.demoted: dict[str, str] = {}          # rule id -> why its effective mode dropped to detect
        self._errors: dict[str, collections.deque[float]] = {}
        self.violation_log_level = logging.WARNING
        self.debug = os.environ.get(DEBUG_ENV, "") not in ("", "0")
        self.allow_non_finite = False              # True: a NaN/Infinity read by a rule is data, not a rule_error
        self.evidence: collections.defaultdict[str, collections.Counter[str]] = (
            collections.defaultdict(collections.Counter))           # reach counters
        self.ablated: set[str] = env_ablated()      # INV_ABLATE (testing only): for the registry's life
        # inv.ablated(scope="registry") blocks open now, per rule: a count, so overlapping blocks
        # (two threads, or nested) keep the rule ablated until the last one exits
        self._reg_ablated: collections.Counter[str] = collections.Counter()
        self.backend_name: str | None = None
        self.contracts: dict[str, Any] = {}
        self._lock = threading.Lock()

    def install(self, other: Registry) -> Registry:
        """Take the rules, providers, targets and notes of another loaded registry."""
        with self._lock:
            self.rules, self.providers, self.targets = other.rules, other.providers, other.targets
            self.notes, self.backend_name = other.notes, other.backend_name
            self.contracts = other.contracts
        return self

    @property
    def reviews(self) -> list[dict[str, Any]]:
        """Reviews held by the default in-memory store (empty for another store)."""
        items = getattr(self.review_store, "items", None)
        return list(items) if items is not None else []

    def record(self, rule_id: str, kind: str, event: dict[str, Any]) -> None:
        """Count one evaluation and keep its event in the bounded sink (thread-safe).

        Observers and exporters that raise are logged and never reach the guarded call.
        """
        spiked = None
        with self._lock:
            c = self.evidence[rule_id]
            if kind != "ablated":              # a skipped evaluation is not a run
                c["runs"] += 1
            c[kind] += 1
            if self.sink.maxlen != 0:
                self.sink.append(event)
            if kind == "rule_error" and self.error_spike is not None and rule_id not in self.demoted:
                spiked = self._spike(rule_id)
        if spiked:
            self.page(Handoff(rule_id, "page", event=event, reason=spiked))
        with _GLOBAL_LOCK:
            fns = [*OBSERVERS]
        for fn in [*fns, *self.exporters]:
            try:
                fn(rule_id, kind, event)
            except Exception:
                log.exception("invariants observer %r failed; the call goes on", fn)

    def _spike(self, rule_id: str) -> str | None:
        """Track one rule_error; on a spike, demote the rule (call with the lock held)."""
        now, sp = time.monotonic(), self.error_spike
        if sp is None:
            return None
        q = self._errors.setdefault(rule_id, collections.deque())
        q.append(now)
        while q and q[0] < now - sp.window:
            q.popleft()
        if len(q) < sp.errors:
            return None
        why = f"rule-error spike: {len(q)} rule_errors in {sp.window:g}s; effective mode dropped to detect"
        self.demoted[rule_id] = why
        return why

    def mode(self, rule: Rule, enf: dict[str, Any]) -> str:
        """Effective mode of an enforcer here: rule.mode(enf), dropped to detect while the rule is demoted."""
        m = rule.mode(enf)
        return "detect" if m == "enforce" and rule.id in self.demoted else m

    def restore(self, rule_id: str) -> None:
        """End a spike demotion (after review)."""
        with self._lock:
            self.demoted.pop(rule_id, None)
            self._errors.pop(rule_id, None)

    def hand_off(self, h: Handoff) -> str:
        """Open a review for route or escalate and return its id.

        A store that raises (or returns no id) is logged, then review_fallback is tried. When no
        review can be opened, ReviewNotOpened is raised carrying the hand-off: the work is never
        dropped silently, and the call does not proceed.
        """
        errors: list[BaseException] = []
        for which, store in (("review store", self.review_store), ("review fallback", self.review_fallback)):
            if store is None:
                continue
            try:
                rid = store.open(h)
                if not isinstance(rid, str) or not rid:
                    raise TypeError(f"{type(store).__name__}.open returned {type(rid).__name__}, not a review id")
            except Exception as e:
                errors.append(e)
                log.error("invariants %s %r failed for %s %s: %s%s", which, type(store).__name__, h.rule, h.action,
                          type(e).__name__, f": {e}" if self.debug else "", exc_info=self.debug)
                continue
            if errors:
                log.warning("invariants %s opened %s for %s %s after the review store failed",
                            which, rid, h.rule, h.action)
            return rid
        raise ReviewNotOpened(
            f"{h.rule} {h.action}: no review was opened (" + "; ".join(type(e).__name__ for e in errors)
            + "); the hand-off is on .handoff and the call does not proceed", h,
            outcome=h.outcome, attempts=h.attempts, rule=h.rule,
            point=(h.event or {}).get("point")) from (errors[-1] if errors else None)

    def page(self, h: Handoff) -> None:
        """Page the owner: the configured pager, else an ERROR log. A failing pager is logged."""
        if self.pager is None:
            log.error("invariants page %s: %s", h.rule, h.reason or (h.outcome.kind if h.outcome else ""))
            return
        try:
            self.pager(h)
        except Exception:
            log.exception("invariants pager failed for %s", h.rule)

    def is_ablated(self, rule_id: str) -> bool:
        """True if the rule is ablated here or everywhere (ablated_everywhere).

        A True answer means a guard, a DDL builder or a scan acted on the ablation, so it is noted
        as reached for the pytest plugin's --inv-ablate (see note_ablation_reached).
        """
        if (rule_id in self.ablated or self._reg_ablated[rule_id] > 0 or GLOBAL_ABLATED[rule_id] > 0
                or self._ctx_ablated(rule_id)):
            note_ablation_reached(rule_id)
            return True
        return False

    def _ctx_ablated(self, rule_id: str) -> bool:
        return any(r is self and rid == rule_id for r, rid in _CTX_ABLATED.get())

    def open_review(self, rule_id: str, outcome: Any) -> str:
        """Open a review in the review store and return its id."""
        return self.review_store.open(Handoff(rule_id, "route", outcome))

    def counts(self, rule_id: str) -> dict[str, int]:
        """A copy of one rule's reach counters."""
        with self._lock:
            return dict(self.evidence.get(rule_id, {}))

    def generation(self, keys: Any) -> tuple[dict[str, Provider], list[Rule]]:
        """(providers, rules for keys) read together: one loaded generation, never half of a reload."""
        with self._lock:
            return self.providers, [r for k in keys for r in self.rules_for(k)]

    def rules_for(self, key: str) -> list[Rule]:
        """Rules a guard key names: a rule id, or 'py:...' for every rule on that target."""
        if key.startswith("py:"):
            found = [r for r in self.rules.values() if r.target_key == key]
            if not found and not self.rules:
                raise LoadError(f"guard names target {key!r}, but no rule file is loaded in this registry "
                                f"(call inv.load first)")
            if not found and key not in self.targets:
                raise LoadError(f"guard names target {key!r}, which no loaded rule or target declares")
            return found
        if key not in self.rules:
            raise LoadError(f"guard names rule {key!r}, which is not loaded")
        return [self.rules[key]]

    @contextlib.contextmanager
    def ablate(self, rule_id: str, scope: str = "context") -> Iterator[None]:
        """Disable one rule's in-process guards for the block.

        scope "context" (the default): only in this thread or task (and tasks it starts inside the
        block, which copy the context); other threads and tasks still enforce the rule.
        scope "registry": for every thread and task that uses this registry, for falsifiers that
        call the guard from a worker thread. (For subprocesses and every registry, the pytest
        plugin's --inv-ablate uses ablated_everywhere and INV_ABLATE.)
        """
        if scope == "context":
            token = _CTX_ABLATED.set(_CTX_ABLATED.get() + ((self, rule_id),))
            try:
                yield
            finally:
                _CTX_ABLATED.reset(token)
            return
        if scope != "registry":
            raise ValueError(f"ablation scope must be 'context' or 'registry', not {scope!r}")
        with self._lock:
            self._reg_ablated[rule_id] += 1
        try:
            yield
        finally:
            with self._lock:
                self._reg_ablated[rule_id] -= 1
                if self._reg_ablated[rule_id] <= 0:
                    del self._reg_ablated[rule_id]

    def scope(self) -> set[str]:
        """Top-level packages of the loaded targets, edge targets, boundaries and python contracts."""
        out: set[str] = set()
        keys = list(self.targets) + [t.get("transaction") for t in self.targets.values()]
        for rule in self.rules.values():
            keys.append(rule.target_key)
            keys.extend(e.get("target") for e in rule.enforcers)
        for key in keys:
            if isinstance(key, str) and key.startswith("py:"):
                out.add(key[3:].split(".")[0])
        for c in self.contracts.values():
            if isinstance(c, dict) and c.get("type") == "python" and isinstance(c.get("ref"), str):
                out.add(c["ref"].split(":")[0].split(".")[0])
        return out

    def verify(self, modules: Iterable[str] | None = None) -> int:
        """Every rule's target carries its guard, and every guard names a rule in the file.

        The second check covers guards in `modules` (module names or package prefixes);
        by default, the packages of the loaded targets (see scope()).
        """
        problems = []
        prefixes = tuple(self.scope() if modules is None else modules)
        for rule in self.rules.values():
            if rule.status == "retired" or rule.target is None:
                continue
            points = {e["point"] for e in rule.enforcers} & GUARD_POINTS
            if rule.policy or points:
                keys = getattr(rule.target, "__inv_rules__", ())
                if rule.id not in keys and rule.target_key not in keys:
                    problems.append(f"{rule.id}: {rule.target_key} is not wrapped by its guard")
                elif not rule.policy and points - set(getattr(rule.target, "__inv_points__", ("decorator",))):
                    missing = sorted(points - set(getattr(rule.target, "__inv_points__", ("decorator",))))
                    problems.append(f"{rule.id}: the guard on {rule.target_key} does not evaluate point "
                                    f"{', '.join(missing)}")
                elif rule.policy and getattr(rule.target, "__inv_kind__", "") != "model_call":
                    problems.append(f"{rule.id}: has model_policy but target is not @inv.model_call")
                elif rule.policy and getattr(rule.target.__inv_schema__, "__name__", "") != rule.policy.get("schema"):
                    problems.append(f"{rule.id}: decorator schema differs from model_policy.schema")
            for e in rule.enforcers:
                if e["point"] == "edge":
                    t = resolve(e["target"])
                    if getattr(t, "__inv_kind__", "") != "edge" or rule.id not in getattr(t, "__inv_rules__", ()):
                        problems.append(f"{rule.id}: edge target {e['target']} is not wrapped by @inv.edge({rule.id!r})")
        for w in list(WRAPPED):
            mod = getattr(w, "__module__", "") or ""
            if not any(mod == p or mod.startswith(p + ".") for p in prefixes):
                continue
            for k in getattr(w, "__inv_rules__", ()):
                if not k.startswith("py:") and k not in self.rules:
                    problems.append(f"{w.__qualname__}: guards {k}, which is not in the rule file")
                elif (not k.startswith("py:") and getattr(w, "__inv_kind__", "") != "edge"
                      and self.rules[k].target is not None and not _wraps(self.rules[k].target, w)):
                    problems.append(f"{w.__qualname__}: guards {k}, whose target is {self.rules[k].target_key}")
        if problems:
            raise CoverageError("; ".join(problems))
        return sum(1 for r in self.rules.values() if r.target is not None)

    def standing(self, rule_id: str, scan: bool = False) -> tuple[str, list[str]]:
        """Computed standing: held | enforce(known-callers) | monitored | candidate | retired, with reasons."""
        rule = self.rules[rule_id]
        if rule.status == "retired":
            return "retired", []
        modes = [rule.mode(e) for e in rule.enforcers]
        if rule.status in ("candidate", "contested") or not any(m != "off" for m in modes):
            return "candidate", []
        if "enforce" not in modes:
            return "monitored", ["no enforcer in effective mode enforce"]
        why = []
        if (rule_id in self.ablated or self._reg_ablated[rule_id] > 0 or GLOBAL_ABLATED[rule_id] > 0
                or self._ctx_ablated(rule_id)):
            why.append("ablated: its in-process guards are off (INV_ABLATE or inv.ablated)")
        med = rule.raw.get("mediation") or {}
        if not med.get("chokepoint"):
            why.append("mediation names no chokepoint")
        if med.get("residual_paths"):
            why.append(f"residual paths: {med['residual_paths']}")
        weak = sorted({p.split(".")[1] for m in rule.names_for.values() for p in m.values()
                       if p.startswith("provider.") and self.providers[p.split(".")[1]].consistency not in HOLDS_RACE})
        if weak and not any(e["point"] == "storage" and rule.mode(e) == "enforce" for e in rule.enforcers):
            why.append("reads shared state through "
                       + ", ".join(f"{p} ({self.providers[p].consistency})" for p in weak)
                       + " with no storage enforcer: the guard is an early refusal only")
        try:
            self.verify()
        except CoverageError as e:
            why.append(f"verify fails: {e}")
        if not scan:
            why.append("inv scan not run")
        return ("held" if not why else "enforce(known-callers)"), why


def _wraps(target: Any, w: Any) -> bool:
    """True if w is target, or is reached from it through __wrapped__ / __func__ (stacked decorators)."""
    obj, seen = target, 0
    while obj is not None and seen < 32:
        if obj is w:
            return True
        # __func__ first: a bound method forwards __wrapped__ to the inner function, skipping w
        obj = getattr(obj, "__func__", None) or getattr(obj, "__wrapped__", None)
        seen += 1
    return False


# Every guarded callable in the process, for verify. Weak, so guards defined in a
# function (tests) drop out once they are garbage.
WRAPPED: weakref.WeakSet[Callable[..., Any]] = weakref.WeakSet()

_DEFAULT = Registry()
_CURRENT: contextvars.ContextVar[Registry | None] = contextvars.ContextVar("inv_registry", default=None)


def default_registry() -> Registry:
    """The process-wide default registry."""
    return _DEFAULT


def current() -> Registry:
    """The registry guards use now: the one set by using()/isolated(), else the default."""
    return _CURRENT.get() or _DEFAULT


@contextlib.contextmanager
def using(reg: Registry) -> Iterator[Registry]:
    """Run guards against reg inside the block (context-local), then restore."""
    token = _CURRENT.set(reg)
    try:
        yield reg
    finally:
        _CURRENT.reset(token)


@contextlib.contextmanager
def isolated() -> Iterator[Registry]:
    """A fresh, empty registry for the block; inv.load() inside it installs there."""
    with using(Registry()) as reg:
        yield reg


def ablated(rule_id: str, reg: Registry | None = None, *,
            scope: str = "context") -> contextlib.AbstractContextManager[None]:
    """Disable one rule's guards (what `pytest --inv-ablate` does per rule); its falsifiers must then fail.

    scope "context" (default): this thread or task only (context-local, like the registry).
    scope "registry": every thread and task using the registry (a falsifier with worker threads).
    """
    if scope not in ("context", "registry"):
        raise ValueError(f"ablation scope must be 'context' or 'registry', not {scope!r}")
    return (reg or current()).ablate(rule_id, scope)


@contextlib.contextmanager
def ablated_everywhere(rule_id: str) -> Iterator[None]:
    """Disable one rule's guards in every registry, existing or new, for the block."""
    with _GLOBAL_LOCK:
        GLOBAL_ABLATED[rule_id] += 1
    try:
        yield
    finally:
        with _GLOBAL_LOCK:
            GLOBAL_ABLATED[rule_id] -= 1
            if GLOBAL_ABLATED[rule_id] <= 0:
                del GLOBAL_ABLATED[rule_id]


def observe(fn: Observer) -> Callable[[], None]:
    """Call fn(rule_id, kind, event) on every evaluation in any registry; returns an unsubscribe."""
    with _GLOBAL_LOCK:
        OBSERVERS.append(fn)

    def stop() -> None:
        with _GLOBAL_LOCK:
            if fn in OBSERVERS:
                OBSERVERS.remove(fn)
    return stop


@contextlib.contextmanager
def capture(reg: Registry | None = None) -> Iterator[list[dict[str, Any]]]:
    """Collect every event recorded in the block (in reg only, when given) into a list. For tests."""
    events: list[dict[str, Any]] = []
    lock = threading.Lock()

    def keep(rule_id: str, kind: str, event: dict[str, Any]) -> None:
        with lock:
            events.append(event)
    if reg is None:
        stop = observe(keep)
    else:
        reg.exporters.append(keep)

        def stop() -> None:
            if keep in reg.exporters:
                reg.exporters.remove(keep)
    try:
        yield events
    finally:
        stop()


_UNSET: Any = object()


def configure(reg: Registry | None = None, *, sink_size: int | None = _UNSET, review_store: ReviewStore | None = _UNSET,
              pager: Pager | None = _UNSET, exporter: Exporter | None = _UNSET,
              error_spike: ErrorSpike | None = _UNSET, violation_log_level: int = _UNSET,
              debug: bool = _UNSET, review_fallback: ReviewStore | None = _UNSET,
              max_reply_size: int = _UNSET, allow_non_finite: bool = _UNSET) -> Registry:
    """Set the operational hooks of reg (default: the current registry). Unnamed settings are kept.

    sink_size: recent events kept (0 keeps none). review_store: opens reviews for route and
    escalate (default: MemoryReviews). pager: called for page (default: an ERROR log).
    exporter: added to the exporters, called on every evaluation. error_spike: demote a rule to
    detect on a rule-error spike (None: off). violation_log_level: log level of violations.
    debug: put exception messages (which may hold secrets) into outcome errors, events and logs.
    review_fallback: a second review store tried when review_store fails (e.g. JsonlReviews);
    when both fail, route/escalate raise ReviewNotOpened with the hand-off. max_reply_size: the
    longest model reply text inv.parse accepts (default 1,000,000; 0 for no limit).
    allow_non_finite: let rules read NaN and +-Infinity as numbers (default False: a clause that
    reads one is a rule_error, because every comparison with NaN is false and a bound check such
    as `x < 0.0 || x > 1.0` would pass it). Model replies never carry them (inv.parse refuses them).
    """
    r = reg or current()
    with r._lock:
        if sink_size is not _UNSET:
            r.sink = EventSink(r.sink, maxlen=_env_int(SINK_ENV, DEFAULT_SINK_SIZE) if sink_size is None
                               else sink_size)
        if review_store is not _UNSET:
            r.review_store = review_store or MemoryReviews()
        if pager is not _UNSET:
            r.pager = pager
        if exporter is not _UNSET and exporter is not None:
            r.exporters.append(exporter)
        if error_spike is not _UNSET:
            r.error_spike = error_spike
        if violation_log_level is not _UNSET:
            r.violation_log_level = violation_log_level
        if debug is not _UNSET:
            r.debug = bool(debug)
        if review_fallback is not _UNSET:
            r.review_fallback = review_fallback
        if max_reply_size is not _UNSET:
            r.max_reply_size = max(0, int(max_reply_size))
        if allow_non_finite is not _UNSET:
            r.allow_non_finite = bool(allow_non_finite)
    return r


def is_ablated(rule_id: str, reg: Registry | None = None) -> bool:
    """True while the rule is ablated. Storage DDL builders call this to leave out that rule's enforcer."""
    return (reg or current()).is_ablated(rule_id)


def verify(reg: Registry | None = None, modules: Iterable[str] | None = None) -> int:
    """`inv verify`: every target guarded, every guard names a rule. Returns the number of targeted rules."""
    return (reg or current()).verify(modules)


verify_coverage = verify


def standing(rule_id: str, reg: Registry | None = None, scan: bool = False) -> tuple[str, list[str]]:
    """Computed standing of one rule, with the reasons it is not held."""
    return (reg or current()).standing(rule_id, scan=scan)
