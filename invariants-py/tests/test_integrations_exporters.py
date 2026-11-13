"""integrations.exporters.BufferedExporter: a bounded queue, a flusher thread the host starts and stops."""
from __future__ import annotations

import asyncio
import os
import threading
import time
import warnings
from typing import Any

import pytest

import invariants as inv
from invariants.integrations.exporters import BufferedExporter, lifespan


class Target:
    """An exporter that records what it got, optionally failing or waiting on a gate."""

    def __init__(self, fail_on: Any = (), gate: threading.Event | None = None) -> None:
        self.got: list[tuple[str, str, int]] = []
        self.threads: set[str] = set()
        self.fail_on, self.gate = set(fail_on), gate

    def __call__(self, rule_id: str, kind: str, event: dict[str, Any]) -> None:
        if self.gate is not None:
            self.gate.wait(10)
        self.threads.add(threading.current_thread().name)
        if event["i"] in self.fail_on:
            raise RuntimeError("collector down")
        self.got.append((rule_id, kind, event["i"]))


def flushers() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "invariants-exporter"]


def feed(exp: BufferedExporter, n: int, start: int = 0) -> None:
    for i in range(start, start + n):
        exp("R-1", "ok", {"i": i})


def test_no_thread_before_start_and_none_after_close():
    before = len(flushers())
    exp = BufferedExporter(Target())
    feed(exp, 3)
    assert len(flushers()) == before and not exp.running and exp.pending == 3
    with exp:
        assert exp.running and len(flushers()) == before + 1
    assert not exp.running and len(flushers()) == before and exp.pending == 0


def test_events_reach_the_target_on_the_flusher_thread_in_order():
    target = Target()
    with BufferedExporter(target) as exp:
        feed(exp, 50)
        assert exp.drain(10)
    assert [i for _, _, i in target.got] == list(range(50)) and target.threads == {"invariants-exporter"}
    assert exp.exported == 50 and exp.dropped == 0 and exp.failed == 0


def test_the_guarded_call_never_waits_for_a_slow_target():
    gate = threading.Event()
    target = Target(gate=gate)
    with inv.isolated() as reg, BufferedExporter(target) as exp:
        inv.configure(reg, exporter=exp)
        t0 = time.monotonic()
        for i in range(20):
            reg.record("R-1", "ok", {"rule": "R-1", "i": i})
        assert time.monotonic() - t0 < 1.0 and target.got == []
        gate.set()
        assert exp.drain(10)
    assert len(target.got) == 20


def test_drain_without_a_flusher_exports_in_the_calling_thread():
    target = Target()
    exp = BufferedExporter(target)
    feed(exp, 5)
    assert exp.drain() and target.threads == {threading.current_thread().name} and exp.exported == 5


def test_drain_times_out_while_the_target_is_stuck():
    gate = threading.Event()
    exp = BufferedExporter(Target(gate=gate)).start()
    try:
        feed(exp, 3)
        assert exp.drain(0.05) is False
        gate.set()
        assert exp.drain(10)
    finally:
        gate.set()
        exp.close()


def test_close_flushes_and_is_idempotent():
    target = Target()
    exp = BufferedExporter(target).start()
    feed(exp, 10)
    assert exp.close() and len(target.got) == 10
    assert exp.close() and exp.close() and len(target.got) == 10
    feed(exp, 2, start=10)                       # queued while stopped; a second close exports it here
    assert exp.close() and len(target.got) == 12
    exp.start()                                  # and it can be started again
    feed(exp, 1, start=12)
    exp.close()
    assert [i for _, _, i in target.got] == list(range(13))


@pytest.mark.parametrize("on_full, kept", [("drop_oldest", [5, 6, 7, 8, 9]), ("drop_new", [0, 1, 2, 3, 4])])
def test_overflow_is_counted(on_full, kept):
    target = Target()
    exp = BufferedExporter(target, maxsize=5, on_full=on_full)
    feed(exp, 10)
    assert exp.dropped == 5 and exp.pending == 5
    exp.close()
    assert [i for _, _, i in target.got] == kept


def test_block_waits_for_room_while_the_flusher_runs_and_drops_without_one():
    exp = BufferedExporter(Target(), maxsize=2, on_full="block")
    feed(exp, 3)                                 # not running: nothing would make room
    assert exp.dropped == 1
    with exp:
        feed(exp, 200, start=3)
        assert exp.drain(10)
    assert exp.dropped == 1 and exp.exported == 202          # 2 queued before start, then 200


def test_a_failing_target_is_counted_and_the_flusher_goes_on(caplog):
    target = Target(fail_on={1, 3})
    with caplog.at_level("WARNING", logger="invariants"), BufferedExporter(target) as exp:
        feed(exp, 5)
        assert exp.drain(10)
        assert exp.running
    assert exp.failed == 2 and [i for _, _, i in target.got] == [0, 2, 4]
    assert caplog.text.count("BufferedExporter") == 1   # the first failure is logged, the rest counted


def test_a_target_that_records_again_never_deadlocks_the_flusher():
    exp: BufferedExporter

    def again(rule_id: str, kind: str, event: dict[str, Any]) -> None:
        if event["i"] < 100:                     # twice into a queue of one: the second finds it full
            exp(rule_id, kind, {"i": event["i"] + 100})
            exp(rule_id, kind, {"i": event["i"] + 200})
    exp = BufferedExporter(again, maxsize=1, on_full="block")
    with exp:
        feed(exp, 1)
        assert exp.drain(10)
    assert exp.dropped == 1 and exp.exported == 2 and not exp.running


@pytest.mark.parametrize("kw", [{"maxsize": 0}, {"maxsize": True}, {"on_full": "wait"}])
def test_bad_settings_are_refused(kw):
    with pytest.raises(ValueError):
        BufferedExporter(Target(), **kw)
    with pytest.raises(TypeError):
        BufferedExporter("not callable")         # type: ignore[arg-type]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_the_child_after_fork_is_stopped_with_an_empty_queue():
    gate = threading.Event()
    target = Target(gate=gate)
    exp = BufferedExporter(target).start()
    feed(exp, 3)
    r, w = os.pipe()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)   # fork() with threads alive
        pid = os.fork()
    if pid == 0:                                 # the child
        ok = not exp.running and exp.pending == 0 and not flushers()
        exp.start()                              # the host restarts it
        feed(exp, 1, start=7)
        gate.set()
        ok = ok and exp.close(5) and [i for _, _, i in target.got] == [7]
        os.write(w, b"1" if ok else b"0")
        os._exit(0)
    os.close(w)
    _, status = os.waitpid(pid, 0)
    child_ok = os.read(r, 1)
    os.close(r)
    gate.set()
    assert exp.close(10) and [i for _, _, i in target.got] == [0, 1, 2]      # the parent's are the parent's
    assert os.waitstatus_to_exitcode(status) == 0 and child_ok == b"1"


def test_lifespan_runs_the_flusher_while_the_app_runs():
    target = Target()
    exp = BufferedExporter(target)

    async def app_life() -> None:
        async with lifespan(exp)(object()):
            assert exp.running
            feed(exp, 4)
        assert not exp.running
    asyncio.run(app_life())
    assert len(target.got) == 4
