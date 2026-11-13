"""Where an evaluation's event goes: the bounded sink of recent events, the observer, exporter and
pager hooks, the error-spike setting, and the invariants log."""
from __future__ import annotations

import collections
import dataclasses
import logging
import os
from typing import Any, Callable, Iterator

from .reviews import Handoff

SINK_ENV = "INV_SINK_SIZE"
DEBUG_ENV = "INV_DEBUG"
REPLY_SIZE_ENV = "INV_MAX_REPLY_SIZE"
DEFAULT_REPLY_SIZE = 1_000_000           # longest model reply text inv.parse accepts (characters or bytes)
DEFAULT_SINK_SIZE = 1000                 # recent events kept per registry; counters are separate
Observer = Callable[[str, str, dict[str, Any]], None]
log = logging.getLogger("invariants")
log.addHandler(logging.NullHandler())


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


class EventSink(collections.deque[dict[str, Any]]):
    """The recent events of one registry: a ring buffer (oldest dropped). Slices give lists.

    Iterating it (list(reg.sink), `for e in reg.sink`) walks a snapshot taken under the registry's
    lock, so another thread recording meanwhile never breaks the iteration; so do the searches
    (`e in reg.sink`, count, index), which deque runs as an iteration that a concurrent append
    breaks ("deque mutated during iteration"). One-step reads (len, reg.sink[i]) need no snapshot.
    """

    _lock: Any = None                     # the owning registry's lock (set by Registry)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        if self._lock is None:
            return super().__iter__()
        with self._lock:
            return iter(tuple(super().__iter__()))

    def __reversed__(self) -> Iterator[dict[str, Any]]:
        return reversed(tuple(self))

    def __getitem__(self, i: Any) -> Any:
        if isinstance(i, slice):
            return list(self)[i]
        return super().__getitem__(i)

    def __contains__(self, x: object) -> bool:
        return x in tuple(self)

    def count(self, x: Any) -> int:
        return tuple(self).count(x)

    def index(self, x: Any, *args: Any) -> int:
        return collections.deque(self).index(x, *args)   # deque's bounds and message, on a snapshot

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return list(self) == other
        return super().__eq__(other)

    __hash__ = None

    def __reduce__(self) -> Any:            # the owning registry's lock is not part of the events
        return (EventSink, (list(self), self.maxlen))


@dataclasses.dataclass(frozen=True)
class ErrorSpike:
    """errors rule_errors within window seconds demote a rule's effective mode to detect and page."""

    errors: int = 20
    window: float = 60.0


Pager = Callable[[Handoff], None]
Exporter = Callable[[str, str, dict[str, Any]], None]
