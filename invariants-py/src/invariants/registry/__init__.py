"""The registry of loaded rules, its reach counters, and which registry is current.

The package splits by owner:
- reviews.py: the hand-off and the review stores
- session.py: the process-wide state (pytest sessions, warn-once flags, trace files)
- events.py: the event sink, the observer/exporter/pager hooks, the error-spike setting, the log
- ablation.py: INV_ABLATE and the rules ablated everywhere or in this context
- coverage.py: what verify() and standing() check beyond the registry, and WRAPPED
- context.py: which registry is current, and the module-level calls on reg or the current one
This module keeps Registry and the state its one fork reset owns: _GLOBAL_LOCK (rebound in a forked
child, so it lives where every reader is: Registry.record, Registry.is_ablated, ablated_everywhere,
observe), OBSERVERS and _REGISTRIES. Every name the modules held as registry.<name> is re-exported.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import os
import threading
import time
import typing
import weakref
from typing import Any, Callable, Iterable, Iterator, Mapping

from ..errors import CoverageError, LoadError, ReviewNotOpened
from ..model import GUARD_POINTS, Provider, Rule  # noqa: F401  (GUARD_POINTS: re-exported)
from ..resolve import resolve
from .ablation import _CTX_ABLATED, GLOBAL_ABLATED, _has_real_handler, _warn_guards_off, ablation_level, env_ablated
from .coverage import (WRAPPED, _ablation_problems, _chokepoint_problems, _edge_problem, _function_names,
                       _mediation_problems, _resolves, _scope, _target_problems, _targetless_problems, _wraps,
                       _Wrapped)
from .events import (DEBUG_ENV, DEFAULT_REPLY_SIZE, DEFAULT_SINK_SIZE, REPLY_SIZE_ENV, SINK_ENV, ErrorSpike,
                     EventSink, Exporter, Observer, Pager, _env_int, log)
from .reviews import Handoff, JsonlReviews, MemoryReviews, ReviewStore, _jsonable
from .session import (ABLATE_ARM_ENV, ABLATE_ENV, ABLATE_TRACE_ENV, DECISION_TRACE_ENV, _SESSION, _trace,
                      is_decision, note_ablation_reached, note_decision, pytest_session_ended,
                      pytest_session_started, under_pytest)

OBSERVERS: list[Observer] = []          # called on every record() in any registry
_GLOBAL_LOCK = threading.Lock()
_REGISTRIES: weakref.WeakSet[Registry] = weakref.WeakSet()   # every registry, for the fork reset


class _View(typing.NamedTuple):
    """The rules, targets and contracts of one loaded generation (install() swaps them together)."""

    rules: dict[str, Rule]
    targets: dict[str, dict[str, Any]]
    contracts: dict[str, Any]


class Registry:
    """Loaded rules, providers and targets, plus the event sink, reviews and reach counters."""

    def __init__(self, sink_size: int | None = None) -> None:
        self._lock = threading.Lock()
        self.rules: dict[str, Rule] = {}
        self.providers: dict[str, Provider] = {}
        self.targets: dict[str, dict[str, Any]] = {}
        self.notes: list[str] = []
        size = _env_int(SINK_ENV, DEFAULT_SINK_SIZE) if sink_size is None else sink_size
        self.sink: EventSink = EventSink(maxlen=size)
        self.sink._lock = self._lock
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
        # extra checks verify() runs, each raising CoverageError: integrations add them (the FastAPI
        # route check of an app given to integrations.fastapi.install)
        self.verifiers: list[Callable[[Registry], Any]] = []
        try:
            _REGISTRIES.add(self)
        except TypeError:                          # a subclass made itself unhashable: no fork reset
            pass

    def _after_fork_in_child(self) -> None:
        """A fresh lock (shared with the sink, as always): one another parent thread held at the fork
        would never be released in the child."""
        self._lock = threading.Lock()
        self.sink._lock = self._lock

    def install(self, other: Registry) -> Registry:
        """Take the rules, providers, targets and notes of another loaded registry.

        A spike demotion survives the reload only for a rule that did not change: a rule whose
        record differs (a new rev, a fixed clause) or that is gone starts again at its own mode.
        """
        with self._lock:
            for rid in list(self.demoted):
                old, new = self.rules.get(rid), other.rules.get(rid)
                if old is None or new is None or old.raw != new.raw:
                    self.demoted.pop(rid, None)
                    self._errors.pop(rid, None)
            self.rules, self.providers, self.targets = other.rules, other.providers, other.targets
            self.notes, self.backend_name = other.notes, other.backend_name
            self.contracts = other.contracts
        return self

    def _view(self) -> _View:
        with self._lock:
            return _View(self.rules, self.targets, self.contracts)

    def add_verifier(self, check: Callable[[Registry], Any], key: Any = None) -> bool:
        """Have verify() run check(registry) too (it raises CoverageError for what it finds); under the
        registry's lock, as verify() copies the list. With a key, a check already added under an equal
        key is kept and this one is not added. True when check was added."""
        with self._lock:
            if key is not None and any(getattr(c, "__inv_verifier_key__", None) == key for c in self.verifiers):
                return False
            if key is not None:
                check.__inv_verifier_key__ = key  # type: ignore[attr-defined]
            self.verifiers.append(check)
            return True

    @property
    def reviews(self) -> list[dict[str, Any]]:
        """Reviews held by the default in-memory store (empty for another store)."""
        store = self.review_store
        snap = getattr(store, "snapshot", None)
        if callable(snap):
            return list(snap())
        items = getattr(store, "items", None)
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
            exporters = list(self.exporters)      # capture() and configure() change it from other threads
        if is_decision(kind, event):
            note_decision(rule_id)
        if spiked:
            self.page(Handoff(rule_id, "page", event=event, reason=spiked))
        with _GLOBAL_LOCK:
            fns = [*OBSERVERS]
        for fn in [*fns, *exporters]:
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
        if m != "enforce":
            return m
        with self._lock:                           # record() demotes from other threads
            demoted = rule.id in self.demoted
        return "detect" if demoted else m

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

    async def ahand_off(self, h: Handoff) -> str:
        """hand_off() for async callers: a store with an async aopen() is awaited (JsonlReviews runs its
        fsync in a worker thread); one with only open() has it run in a worker thread
        (asyncio.to_thread), never on the event loop."""
        errors: list[BaseException] = []
        for which, store in (("review store", self.review_store), ("review fallback", self.review_fallback)):
            if store is None:
                continue
            try:
                aopen = getattr(store, "aopen", None)
                rid = await aopen(h) if callable(aopen) else await asyncio.to_thread(store.open, h)
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

        Asking is not acting: this never counts as reaching the rule for --inv-ablate (a test
        asserting `not inv.is_ablated(...)` is not a falsifier). Enforcement points call skips().
        """
        if rule_id in self.ablated or self._ctx_ablated(rule_id):
            return True
        with self._lock:                           # ablate(scope="registry") changes it from other threads
            here = self._reg_ablated.get(rule_id, 0) > 0
        if here:
            return True
        with _GLOBAL_LOCK:                         # ablated_everywhere() changes it from other threads
            return GLOBAL_ABLATED.get(rule_id, 0) > 0

    def skips(self, rule_id: str) -> bool:
        """is_ablated() for an enforcement point about to skip the rule: a True answer is noted as
        reached for the pytest plugin's --inv-ablate (see note_ablation_reached)."""
        if self.is_ablated(rule_id):
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
        """Top-level packages of the loaded targets, edge targets and python contracts."""
        return _scope(self._view())

    def verify(self, modules: Iterable[str] | None = None) -> int:
        """Every rule's target carries its guard, and every guard names a rule in the file.

        The second check covers guards in `modules` (module names or package prefixes);
        by default, the packages of the loaded targets (see scope()). It reads one loaded
        generation: a reload meanwhile does not mix two rule files.
        """
        return self._verify(self._view(), modules)

    def _verify(self, view: _View, modules: Iterable[str] | None) -> int:
        prefixes = tuple(_scope(view) if modules is None else modules)
        problems = [p for rule in view.rules.values() for p in _target_problems(rule)]
        for w in WRAPPED.snapshot():
            mod = getattr(w, "__module__", "") or ""
            if any(mod == p or mod.startswith(p + ".") for p in prefixes):
                problems.extend(self._guard_problems(w, view.rules))
        with self._lock:                           # integrations append to it from other threads
            checks = list(self.verifiers)
        for check in checks:
            try:
                check(self)
            except CoverageError as e:
                problems.extend(str(e).split("; "))
        if problems:
            raise CoverageError("; ".join(problems))
        return sum(1 for r in view.rules.values() if r.target is not None)

    def _guard_problems(self, w: Any, rules: dict[str, Rule]) -> list[str]:
        """Why one guarded callable is miswired against rules: a key no rule names, or another target."""
        problems = []
        for k in getattr(w, "__inv_rules__", ()):
            if k.startswith("py:"):
                why = self._py_key_problem(k, w)
                if why:
                    problems.append(f"{w.__qualname__}: guards {k}, {why}")
            elif k not in rules:
                problems.append(f"{w.__qualname__}: guards {k}, which is not in the rule file")
            elif getattr(w, "__inv_kind__", "") == "edge":
                why = _edge_problem(rules[k], w)
                if why:
                    problems.append(f"{w.__qualname__}: {why}")
            elif rules[k].target is not None and not _wraps(rules[k].target, w):
                problems.append(f"{w.__qualname__}: guards {k}, whose target is {rules[k].target_key}")
            elif rules[k].target is None and rules[k].status != "retired":
                problems.extend(f"{w.__qualname__}: {why}" for why in _targetless_problems(self, rules[k], w))
        return problems

    def _py_key_problem(self, key: str, w: Any) -> str | None:
        """Why a guard keyed by a 'py:' target is miswired (the key resolves to another function than
        the one the guard wraps), else None. A key no rule of this file names is not a problem here:
        it may belong to another rule file of the process (calling it under this one is a LoadError)."""
        try:
            obj = resolve(key)
        except LoadError as e:
            return f"which does not resolve ({e})"
        if not _wraps(obj, w):
            return f"but {key} is another function (put the guard on {key}, or name the function it wraps)"
        return None

    def standing(self, rule_id: str, scan: bool = False,
                 ablation: Mapping[str, Any] | None = None) -> tuple[str, list[str]]:
        """Computed standing: held | enforce(known-callers) | monitored | candidate | retired, with reasons.

        Effective modes count (a rule demoted by an error spike is monitored). held is withheld while
        the rule is ablated, its mediation names no chokepoint (or a chokepoint that names none of the
        rule's functions, or a py: path that does not resolve), lists residual paths, keeps live
        mediation.allow back doors that residual_paths does not declare, verify fails, the scan has
        not run, or (given ablation: the pytest plugin's --inv-report-json data, or its "ablation"
        part) no falsifier of the rule was caught by the ablation. The rule and verify are read
        from one loaded generation.
        """
        view = self._view()
        rule = view.rules[rule_id]
        if rule.status == "retired":
            return "retired", []
        modes = [self.mode(rule, e) for e in rule.enforcers]
        if rule.status in ("candidate", "contested") or not any(m != "off" for m in modes):
            return "candidate", []
        if "enforce" not in modes:
            return "monitored", ["no enforcer in effective mode enforce"]
        why = []
        if self.is_ablated(rule_id):
            why.append("ablated: its in-process guards are off (INV_ABLATE or inv.ablated)")
        why.extend(_mediation_problems(rule))
        if ablation is not None:
            why.extend(_ablation_problems(rule, ablation))
        try:
            if getattr(self.verify, "__func__", None) is Registry.verify:
                self._verify(view, None)          # the same rules generation as the rest of this answer
            else:
                self.verify()                     # a subclass or patch that replaces verify is honoured
        except CoverageError as e:
            why.append(f"verify fails: {e}")
        if not scan:
            why.append("inv scan not run")
        return ("held" if not why else "enforce(known-callers)"), why


def _after_fork_in_child() -> None:
    """Fresh locks in a forked child: a lock another parent thread held at the fork is never released
    there. The values stay the parent's (ProcessSession, BufferedExporter and the review stores do
    the same for theirs)."""
    global _GLOBAL_LOCK
    _GLOBAL_LOCK = threading.Lock()
    WRAPPED._after_fork_in_child()
    for reg in list(_REGISTRIES):
        reg._after_fork_in_child()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


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


# Defined after Registry and observe, which it imports from this package.
from .context import (_CURRENT, _DEFAULT, _UNSET, ablated, capture, configure, current,  # noqa: E402
                      default_registry, is_ablated, isolated, standing, using, verify, verify_coverage)

# Classes keep the module name they had before the split: a pinned signature (`invariants.lock`) names
# an annotation's class by its module, and pickles name it too, so a moved __module__ would make an
# upgrade alone re-pin the lock and make new pickles unreadable by older versions.
for _obj in list(globals().values()):
    if isinstance(_obj, type) and (_obj.__module__ == __name__ or _obj.__module__.startswith(__name__ + ".")):
        _obj.__module__ = 'invariants.registry'
del _obj
