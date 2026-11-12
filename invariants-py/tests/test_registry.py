"""Registry object: default instance, isolation, using, install, thread-safe counters."""
from __future__ import annotations

import asyncio
import contextvars
import threading

import jobs

import invariants as inv
from support import RULE_FILE, raises


def test_default_registry_is_current_outside_blocks():
    assert inv.current() is inv.default_registry()


def test_isolated_gives_fresh_registry_and_restores():
    outer = inv.current()
    with inv.isolated() as r:
        assert inv.current() is r and r is not outer and r.rules == {}
        inv.load(RULE_FILE)
        assert len(r.rules) == 6
    assert inv.current() is outer
    assert "R-1" not in outer.rules or outer is not r


def test_isolated_registries_do_not_share_counters():
    with inv.isolated() as a:
        inv.load(RULE_FILE)
        jobs.split_work(2, [1, 1])
        with inv.isolated() as b:
            inv.load(RULE_FILE)
            jobs.split_work(2, [1, 1])
            jobs.split_work(2, [1, 1])
        assert a.evidence["R-1"]["runs"] == 1 and b.evidence["R-1"]["runs"] == 2


def test_guard_without_loaded_rule_raises_load_error():
    with inv.isolated():
        e = raises(inv.LoadError, jobs.split_work, 2, [1, 1])
    assert e is not None and "not loaded" in str(e)


def test_load_into_default_registry():
    d = inv.default_registry()
    saved = (d.rules, d.providers, d.targets, d.notes, d.backend_name)
    try:
        assert inv.load(RULE_FILE) is d and len(d.rules) == 6
        assert jobs.split_work(2, [1, 1]) == [1, 1]
    finally:
        d.rules, d.providers, d.targets, d.notes, d.backend_name = saved


def test_install_keeps_sink_and_counters(reg):
    jobs.split_work(2, [1, 1])
    inv.load(RULE_FILE)
    assert reg.evidence["R-1"]["runs"] == 1 and len(reg.sink) == 1


def test_using_is_context_local(reg):
    other = inv.Registry()
    seen = []

    def in_thread():
        seen.append(inv.current())
    with inv.using(other):
        t = threading.Thread(target=in_thread)
        t.start()
        t.join()
        assert inv.current() is other
    assert inv.current() is reg
    assert seen[0] is not other         # a plain thread does not inherit the context


def test_using_carries_into_asyncio(reg):
    async def main():
        return jobs.split_work(2, [1, 1])
    asyncio.run(main())
    assert reg.evidence["R-1"]["runs"] == 1


def test_reach_counters_are_thread_safe(reg):
    n_threads, per = 8, 250
    barrier = threading.Barrier(n_threads)

    def work():
        barrier.wait()
        for _ in range(per):
            jobs.split_work(10, [5, 5])
    threads = [threading.Thread(target=contextvars.copy_context().run, args=(work,)) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert reg.counts("R-1") == {"runs": n_threads * per, "ok": n_threads * per}
    assert len(reg.sink) == min(n_threads * per, reg.sink.maxlen)     # the sink is a ring buffer


def test_review_ids_are_unique_under_threads(reg):
    ids: list[str] = []
    lock = threading.Lock()

    def work():
        for _ in range(50):
            rid = reg.open_review("X", None)
            with lock:
                ids.append(rid)
    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 200


def test_rules_for_target_key(reg):
    assert [r.id for r in reg.rules_for("py:pool.PoolService.resize_door")] == ["R-21", "P-1"]
    assert raises(inv.LoadError, reg.rules_for, "Z-9") is not None


def test_registry_records_backend(reg):
    assert reg.backend_name == inv.cel.resolve_name()
