"""Which registry is current (using, isolated), and the module-level calls that act on a given
registry or the current one: ablated, capture, configure, is_ablated, verify and standing.

It imports Registry and observe from the package, which defines them before it imports this module.
"""
from __future__ import annotations

import collections
import contextlib
import contextvars
import threading
from typing import Any, Iterable, Iterator, Mapping

from . import Registry, observe
from .events import DEFAULT_SINK_SIZE, SINK_ENV, ErrorSpike, EventSink, Exporter, Pager, _env_int
from .reviews import MemoryReviews, ReviewStore

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
        with reg._lock:
            reg.exporters.append(keep)

        def stop() -> None:
            with reg._lock:
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
            kept = collections.deque.__iter__(r.sink)    # the lock is held: not the snapshotting __iter__
            r.sink = EventSink(kept, maxlen=_env_int(SINK_ENV, DEFAULT_SINK_SIZE) if sink_size is None
                               else sink_size)
            r.sink._lock = r._lock
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
    """True while the rule is ablated (inv.ablated, INV_ABLATE or Registry.ablate)."""
    return (reg or current()).is_ablated(rule_id)


def verify(reg: Registry | None = None, modules: Iterable[str] | None = None) -> int:
    """`inv verify`: every target guarded, every guard names a rule. Returns the number of targeted rules."""
    return (reg or current()).verify(modules)


verify_coverage = verify


def standing(rule_id: str, reg: Registry | None = None, scan: bool = False,
             ablation: Mapping[str, Any] | None = None) -> tuple[str, list[str]]:
    """Computed standing of one rule, with the reasons it is not held (see Registry.standing)."""
    return (reg or current()).standing(rule_id, scan=scan, ablation=ablation)
