"""Fix round 20 (registry, exporter): restart after a timed-out close, close's drain within the timeout,
locks reset in a forked child, and the sink / verify paths safe under concurrent recording and decoration."""
from __future__ import annotations

import os
import sys
import threading
import time
import warnings
from typing import Any

import pytest

import invariants as inv
from invariants import registry
from invariants.integrations.exporters import BufferedExporter
from invariants.registry import JsonlReviews, MemoryReviews, Registry


def wait_until(cond: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


# --- 1. BufferedExporter: start() after a timed-out close(), close()'s drain within the timeout ---

def test_start_after_a_timed_out_close_keeps_the_running_flusher_going():
    gate, got = threading.Event(), []

    def target(rule_id: str, kind: str, event: dict[str, Any]) -> None:
        gate.wait(10)
        got.append(event["i"])

    exp = BufferedExporter(target).start()
    try:
        exp("R-1", "ok", {"i": 0})
        assert wait_until(lambda: exp.pending == 1 and not exp._queue)   # the flusher holds the batch
        assert exp.close(timeout=0.05) is False                          # still exporting: timed out
        exp.start()                                                      # the host wants it running again
        gate.set()
        assert wait_until(lambda: got == [0])
        time.sleep(0.1)                     # the old flusher would exit here if it still saw the stop
        assert exp.running
        exp("R-1", "ok", {"i": 1})
        assert wait_until(lambda: got == [0, 1])     # exported by the flusher, not by a drain() here
        assert exp.running
    finally:
        gate.set()
        assert exp.close(5)
    assert not exp.running and exp.exported == 2


def test_start_while_a_close_still_waits_leaves_that_close_to_finish():
    gate = threading.Event()
    exp = BufferedExporter(lambda rule_id, kind, event: gate.wait(10)).start()
    exp("R-1", "ok", {"i": 0})
    assert wait_until(lambda: exp.pending == 1 and not exp._queue)
    closer = threading.Thread(target=exp.close)                  # no timeout: waits for the flusher
    closer.start()
    assert wait_until(lambda: exp._closing == 1)
    exp.start()                                                  # a no-op: that close() still ends
    gate.set()
    closer.join(5)
    assert not closer.is_alive() and not exp.running and exp.exported == 1


def test_close_applies_the_remaining_timeout_to_its_final_drain():
    gate = threading.Event()

    def target(rule_id: str, kind: str, event: dict[str, Any]) -> None:
        if event.get("slow"):
            gate.wait(10)

    class Racing(BufferedExporter):
        """Another thread starts the flusher again, with a slow event, just as close() drains."""

        raced = False

        def drain(self, timeout: float | None = None) -> bool:
            if not self.raced:
                self.raced = True
                self.start()
                self("R-1", "ok", {"slow": True})
                assert wait_until(lambda: self._busy == 1)
            return super().drain(timeout)

    exp = Racing(target).start()
    threading.Timer(4, gate.set).start()             # never hang the suite before the fix
    try:
        t0 = time.monotonic()
        exp.close(timeout=0.5)
        took = time.monotonic() - t0
        assert took < 2, f"close(timeout=0.5) took {took:.1f}s"
    finally:
        gate.set()
        exp.close(5)


# --- 2. fork: every library lock is fresh in the child ---

def _fork_while_held(lock: Any, child: Any) -> None:
    """Hold lock in another thread, fork, and run child() in the child; the child must not deadlock."""
    held, release = threading.Event(), threading.Event()

    def holder() -> None:
        with lock:
            held.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(5)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)   # fork() with threads alive
            pid = os.fork()
        if pid == 0:                                              # pragma: no cover - the child
            code = 1
            try:
                done, failed = threading.Event(), []

                def run() -> None:
                    try:
                        child()
                    except BaseException as e:
                        failed.append(e)
                    done.set()
                threading.Thread(target=run, daemon=True).start()
                code = (4 if failed else 0) if done.wait(3) else 3  # 3: deadlocked; 4: child() failed
            except BaseException:
                code = 2
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(status)
        assert code != 3, "the child deadlocked on a lock held at the fork"
        assert code == 0, f"the child failed (exit {code})"
    finally:
        release.set()
        t.join()


fork_only = pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")


@fork_only
def test_registry_lock_is_fresh_in_a_forked_child():
    reg = Registry()

    def child() -> None:
        reg.record("R-1", "ok", {"rule": "R-1"})
        assert reg.counts("R-1")["runs"] == 1 and list(reg.sink) == [{"rule": "R-1"}]
    _fork_while_held(reg._lock, child)


@fork_only
def test_registry_lock_after_configure_sink_size_is_fresh_in_a_forked_child():
    reg = Registry()
    inv.configure(reg, sink_size=5)

    def child() -> None:
        reg.record("R-1", "ok", {"rule": "R-1"})
        assert list(reg.sink) == [{"rule": "R-1"}] and reg.sink._lock is reg._lock
    _fork_while_held(reg._lock, child)


@fork_only
def test_global_lock_is_fresh_in_a_forked_child():
    def child() -> None:
        with registry.ablated_everywhere("R-FORK"):
            assert registry.Registry().is_ablated("R-FORK")
    _fork_while_held(registry._GLOBAL_LOCK, child)


@fork_only
def test_memory_reviews_lock_is_fresh_in_a_forked_child():
    store = MemoryReviews()

    def child() -> None:
        assert store.open(registry.Handoff("R-1", "route")) == "review-1"
    _fork_while_held(store._lock, child)


@fork_only
def test_jsonl_reviews_lock_is_fresh_in_a_forked_child(tmp_path):
    store = JsonlReviews(tmp_path / "r.jsonl")

    def child() -> None:
        assert store.open(registry.Handoff("R-1", "route")).startswith("jsonl-")
    _fork_while_held(store._lock, child)


@fork_only
def test_wrapped_lock_is_fresh_in_a_forked_child():
    def child() -> None:
        @inv.guard("R-FORK")
        def f() -> None: ...
        assert f in list(registry.WRAPPED)
    _fork_while_held(registry.WRAPPED._lock, child)


# --- 3. concurrent recording and decoration ---

class Slow:
    """A value whose comparison runs Python code, so the interpreter may switch threads inside it."""

    def __eq__(self, other: object) -> bool:
        sum(range(50))
        return False

    __hash__ = object.__hash__


@pytest.mark.parametrize("op", ["contains", "count", "index"])
def test_sink_searches_survive_concurrent_recording(op):
    reg = Registry(sink_size=2000)
    for _ in range(2000):
        reg.record("R-1", "ok", {"v": Slow()})
    stop = threading.Event()

    def writer() -> None:
        while not stop.is_set():
            reg.record("R-1", "ok", {"v": Slow()})

    t = threading.Thread(target=writer)
    t.start()
    errors = []
    try:
        for _ in range(30):
            probe = {"v": Slow()}
            try:
                if op == "contains":
                    assert probe not in reg.sink
                elif op == "count":
                    assert reg.sink.count(probe) == 0
                else:
                    with pytest.raises(ValueError):
                        reg.sink.index(probe)
            except RuntimeError as e:
                errors.append(e)
    finally:
        stop.set()
        t.join()
    assert not errors, errors[0]


def test_sink_searches_keep_their_answers():
    reg = Registry(sink_size=10)
    for i in range(3):
        reg.record("R-1", "ok", {"i": i})
    assert {"i": 1} in reg.sink and {"i": 9} not in reg.sink
    assert reg.sink.count({"i": 2}) == 1 and reg.sink.index({"i": 2}) == 2
    assert reg.sink.index({"i": 0}, 0, 2) == 0
    with pytest.raises(ValueError):
        reg.sink.index({"i": 2}, 0, 2)


def test_verify_survives_concurrent_decoration():
    """Reproduces on Python < 3.13 (WeakSet iteration there walks the live set)."""
    reg = Registry()
    keep = []
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)                  # switch threads often, inside the iteration
    for _ in range(20000):
        @inv.guard("R-RACE")
        def g() -> None: ...
        keep.append(g)
    stop = threading.Event()

    def decorate() -> None:
        while not stop.is_set():
            @inv.guard("R-RACE")
            def f() -> None: ...
            keep.append(f)

    t = threading.Thread(target=decorate)
    t.start()
    errors = []
    try:
        for _ in range(30):
            try:
                reg.verify(modules=["nothing_here"])
            except RuntimeError as e:
                errors.append(e)
    finally:
        stop.set()
        t.join()
        sys.setswitchinterval(old)
    assert not errors, errors[0]


def test_verify_reads_the_verifiers_under_the_registry_lock():
    reg = Registry()
    seen = []
    reg.verifiers.append(lambda r: seen.append(1))

    class Spy:
        """Stands for the registry lock: records whether verify took it to copy the verifiers."""

        def __init__(self, lock: Any) -> None:
            self.lock, self.entered = lock, 0

        def __enter__(self) -> Any:
            self.entered += 1
            return self.lock.__enter__()

        def __exit__(self, *exc: Any) -> Any:
            return self.lock.__exit__(*exc)

    view = reg._view()
    spy = Spy(reg._lock)
    reg._lock = spy                              # type: ignore[assignment]
    reg._verify(view, ["nothing_here"])
    assert spy.entered == 1 and seen == [1]
