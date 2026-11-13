"""BufferedExporter: hand events to a slow exporter (OpenTelemetry, a log shipper) off the guarded call.

    from invariants.integrations import exporters, otel

    buffered = exporters.BufferedExporter(otel.exporter())
    inv.configure(exporter=buffered)
    with buffered:                          # the host starts the flusher thread ...
        serve()                             # ... and close() on exit flushes what is queued

A registry calls its exporters inline, on the thread of the guarded call. BufferedExporter is such
an exporter that only appends the event to a bounded queue; a background thread calls the wrapped
exporter. The host owns that thread: it exists only between start() (or `with`) and close(), and
the library never starts it. Events recorded while it is not running wait in the queue (bounded)
until start() or drain().

on_full says what a full queue does with a new event: "drop_oldest" (the default) drops the oldest
queued event, "drop_new" drops the new one, "block" makes the guarded call wait for room while the
flusher runs (with no flusher running, or on the flusher's own thread, it drops the new event,
since nothing would make room). The
default never blocks the guarded call, and keeps the most recent events, as the registry's own
event sink does. Every dropped event is counted in .dropped; every event the wrapped exporter
raised on is counted in .failed (the flusher logs the first failure and goes on).

After os.fork the child's BufferedExporter is stopped, with an empty queue (the parent's queued
events stay the parent's to export): a child that exports calls start() again.

Review stores are never involved: a hand-off is durable on its own path (JsonlReviews fsyncs before
returning), never through an exporter's queue.
"""
from __future__ import annotations

import collections
import contextlib
import logging
import os
import threading
import time
import weakref
from typing import Any, AsyncIterator, Callable

Exporter = Callable[[str, str, dict[str, Any]], None]
ON_FULL = ("drop_oldest", "drop_new", "block")
log = logging.getLogger("invariants")
_LIVE: weakref.WeakSet[BufferedExporter] = weakref.WeakSet()


class BufferedExporter:
    """An exporter that queues events (bounded) for a host-started thread to pass to target."""

    def __init__(self, target: Exporter, *, maxsize: int = 10_000, on_full: str = "drop_oldest") -> None:
        if not callable(target):
            raise TypeError(f"BufferedExporter target must be an exporter (a callable), got {target!r}")
        if not isinstance(maxsize, int) or isinstance(maxsize, bool) or maxsize < 1:
            raise ValueError(f"BufferedExporter maxsize must be a positive int, got {maxsize!r}")
        if on_full not in ON_FULL:
            raise ValueError(f"BufferedExporter on_full must be one of {', '.join(ON_FULL)}, got {on_full!r}")
        self.target, self.maxsize, self.on_full = target, maxsize, on_full
        self.dropped = 0                  # events dropped because the queue was full
        self.failed = 0                   # events the target exporter raised on
        self.exported = 0                 # events the target exporter took
        self._reset()
        _LIVE.add(self)

    def _reset(self) -> None:
        """Fresh lock, empty queue, no flusher (construction, and the child after os.fork)."""
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._queue: collections.deque[tuple[str, str, dict[str, Any]]] = collections.deque()
        self._busy = 0                    # events taken by a flush and not yet exported
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._closing = 0                 # close() calls waiting for the flusher to stop now

    @property
    def running(self) -> bool:
        """True while the flusher thread runs (between start() and close())."""
        with self._lock:
            return self._thread is not None

    @property
    def pending(self) -> int:
        """Events queued or being exported now."""
        with self._lock:
            return len(self._queue) + self._busy

    def __call__(self, rule_id: str, kind: str, event: dict[str, Any]) -> None:
        """Queue one event (the registry calls this inline); never raises, never waits unless "block"."""
        with self._lock:
            if len(self._queue) >= self.maxsize and not self._make_room():
                self.dropped += 1
                return
            self._queue.append((rule_id, kind, event))
            self._changed.notify_all()

    def _make_room(self) -> bool:
        """With the lock held and the queue full: True when the new event may be appended."""
        if self.on_full == "drop_oldest":
            self._queue.popleft()
            self.dropped += 1
            return True
        if self.on_full == "drop_new":
            return False
        if self._thread is threading.current_thread():
            return False                          # the target recorded an event: never wait for ourselves
        while len(self._queue) >= self.maxsize and self._thread is not None:
            self._changed.wait()
        return len(self._queue) < self.maxsize

    def start(self) -> BufferedExporter:
        """Start the flusher thread (a no-op while it runs). Returns self.

        After a close(timeout) that timed out (the flusher still exporting its last batch), start()
        cancels that stop: the same flusher thread keeps running and exports what comes next. While
        a close() is still waiting for the flusher, start() leaves the stop alone (that close ends).
        """
        with self._lock:
            if self._thread is not None:
                if not self._closing:
                    self._stopping = False
                return self
            self._stopping = False
            self._thread = threading.Thread(target=self._run, name="invariants-exporter", daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        """The flusher: take every queued event, export them, until close() and the queue is empty.

        However it ends (stopped, or a BaseException from the target), it clears itself, so a close()
        that timed out still leaves an exporter that start() can restart and drain() can empty.
        """
        try:
            while True:
                with self._lock:
                    while not self._queue and not self._stopping:
                        self._changed.wait()
                    if not self._queue:
                        return
                    batch = self._take()
                self._export(batch)
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread, self._stopping = None, False
                self._changed.notify_all()

    def _take(self) -> list[tuple[str, str, dict[str, Any]]]:
        """With the lock held: move the queue into a batch the caller exports."""
        batch = list(self._queue)
        self._queue.clear()
        self._busy += len(batch)
        self._changed.notify_all()
        return batch

    def _export(self, batch: list[tuple[str, str, dict[str, Any]]]) -> None:
        """Call the target for each event outside the lock; count what it took and what it raised on."""
        ok = failed = 0
        try:
            for rule_id, kind, event in batch:
                try:
                    self.target(rule_id, kind, event)
                    ok += 1
                except Exception:
                    failed += 1
                    if self.failed + failed == 1:
                        log.exception("invariants BufferedExporter: %r failed; later failures are counted in "
                                      ".failed", self.target)
        finally:                                  # a BaseException still settles the batch's count
            with self._lock:
                self.exported += ok
                self.failed += failed + (len(batch) - ok - failed)
                self._busy -= len(batch)
                self._changed.notify_all()

    def drain(self, timeout: float | None = None) -> bool:
        """Export everything queued now; True when nothing is left (within timeout; None: no limit).

        With the flusher running it waits for it; without one it exports in the calling thread.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while self._thread is not None and (self._queue or self._busy):
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._changed.wait(left)
            batch = self._take() if self._thread is None else []
        if batch:
            self._export(batch)
        return self.pending == 0

    def close(self, timeout: float | None = None) -> bool:
        """Flush the queue, then stop the flusher thread; True when it stopped within timeout.

        Idempotent: closing a stopped exporter only exports what is queued (in this thread). The final
        drain gets what is left of timeout (a start() from another thread meanwhile runs a flusher it
        then waits for).
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            thread = self._thread
            self._stopping = True
            if thread is not None:
                self._closing += 1
            self._changed.notify_all()
        if thread is not None:
            try:
                thread.join(timeout)
            finally:
                with self._lock:
                    self._closing -= 1
            with self._lock:
                if thread.is_alive():
                    return False
                if self._thread is thread:
                    self._thread, self._stopping = None, False
                self._changed.notify_all()        # a "block" caller waiting for room stops waiting
        # what was queued after the flusher's last batch
        self.drain(None if deadline is None else max(0.0, deadline - time.monotonic()))
        return True

    def __enter__(self) -> BufferedExporter:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _after_fork_in_child() -> None:
    for exp in list(_LIVE):
        exp._reset()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def lifespan(*exporters: BufferedExporter) -> Callable[[Any], contextlib.AbstractAsyncContextManager[None]]:
    """An ASGI lifespan (Starlette/FastAPI `lifespan=`) that runs the exporters' flushers while the app runs.

        app = FastAPI(lifespan=exporters.lifespan(buffered))

    close() runs in a worker thread, so the final flush never blocks the event loop.
    """
    import asyncio

    @contextlib.asynccontextmanager
    async def run(app: Any) -> AsyncIterator[None]:
        for exp in exporters:
            exp.start()
        try:
            yield
        finally:
            for exp in exporters:
                await asyncio.to_thread(exp.close)
    return run


__all__ = ["BufferedExporter", "lifespan", "ON_FULL"]
