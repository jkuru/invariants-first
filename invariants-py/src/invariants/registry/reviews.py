"""Review stores: the hand-off route, escalate and page carry, and where reviews are opened."""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import json
import os
import pathlib
import threading
import time
import uuid
import weakref
from typing import Any, Protocol

# Every review store with a lock, for the fork reset: JsonlReviews holds its lock across the write
# and fsync, so a fork meanwhile would leave the child's copy locked for good.
_LOCKED: weakref.WeakSet[Any] = weakref.WeakSet()


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
    """Where route and escalate open a review. open() returns the review id.

    A store may also define `async def aopen(handoff) -> str`: async guards, model calls and edges
    await it (Registry.ahand_off). A store without one has its open() run in a worker thread
    (asyncio.to_thread), so a blocking write never runs on the event loop.
    """

    def open(self, handoff: Handoff) -> str: ...


class MemoryReviews:
    """The default review store: in memory and bounded (the oldest are dropped). Not durable."""

    def __init__(self, maxlen: int = 1000) -> None:
        self._items: collections.deque[dict[str, Any]] = collections.deque(maxlen=maxlen)
        self._n = 0
        self._lock = threading.Lock()
        _LOCKED.add(self)

    @property
    def items(self) -> tuple[dict[str, Any], ...]:
        """The reviews held now, oldest first: a snapshot taken under the store's lock."""
        with self._lock:
            return tuple(self._items)

    def open(self, handoff: Handoff) -> str:
        """Record the hand-off and return review-N."""
        with self._lock:
            self._n += 1
            rid = f"review-{self._n}"
            self._items.append({"id": rid, "rule": handoff.rule, "action": handoff.action,
                                "outcome": handoff.outcome, "bound": handoff.bound})
            return rid

    async def aopen(self, handoff: Handoff) -> str:
        """open() for async callers: in memory, so it runs on the loop (no thread hop)."""
        return self.open(handoff)

    def snapshot(self) -> list[dict[str, Any]]:
        """The reviews held now, copied under the lock (open() may run in another thread)."""
        return list(self.items)


def _int_text(v: int) -> str:
    """str(v), or for an int too long for Python's decimal conversion (past sys.get_int_max_str_digits()
    digits: json.dumps would raise) its exact hexadecimal form, marked: '<int 0x...>'."""
    try:
        return str(v)
    except ValueError:
        return f"<int {hex(v)}>"


def _int(v: int) -> Any:
    """An int as JSON: itself, or, too long to print in decimal, _int_text's marked string."""
    t = _int_text(v)
    return v if not t.startswith("<") else t


def _jsonable(v: Any, depth: int = 0) -> Any:
    """v as JSON data: plain values as they are, containers walked, anything else its repr (never raises)."""
    try:
        if type(v) is int:
            return _int(v)
        if v is None or type(v) in (bool, float, str):
            return v
        if depth > 16:
            return "<nested too deeply>"
        if isinstance(v, dict):
            return {(_int_text(k) if type(k) is int else str(k)): _jsonable(x, depth + 1) for k, x in v.items()}
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
        _LOCKED.add(self)

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

    async def aopen(self, handoff: Handoff) -> str:
        """open() in a worker thread (asyncio.to_thread), so the write and fsync never block the event
        loop. Async guards, model calls and edges use it (Registry.ahand_off); the file is the same."""
        return await asyncio.to_thread(self.open, handoff)


def _after_fork_in_child() -> None:
    """A fresh lock for every review store in a forked child (the reviews held stay the parent's)."""
    for store in list(_LOCKED):
        store._lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)
