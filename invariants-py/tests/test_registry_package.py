"""The registry package: its re-exported namespace, actor-safe reads, durable reviews, the process
session after fork, per-worker decision traces under pytest-xdist, and share-nothing provider fan-out."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import warnings
from typing import Any

import pytest

import invariants as inv
import round19 as r19
from invariants import registry
from invariants.model import Provider, Rule
from invariants.providers import ProviderFailed, prefetch_many
from invariants.registry import Handoff, JsonlReviews, MemoryReviews, reviews, session

pytest_plugins = ["pytester"]


# --- 1. the namespace ------------------------------------------------------------------------------
def test_moved_names_are_the_same_objects_under_registry():
    for name in ("Handoff", "ReviewStore", "MemoryReviews", "JsonlReviews", "_jsonable"):
        assert getattr(registry, name) is getattr(reviews, name)
    for name in ("pytest_session_started", "pytest_session_ended", "under_pytest", "note_decision",
                 "note_ablation_reached", "is_decision", "ABLATE_ENV", "DECISION_TRACE_ENV", "_SESSION"):
        assert getattr(registry, name) is getattr(session, name)
    assert not hasattr(registry, "_pytest_sessions") and not hasattr(registry, "_warned_armed")


# --- 2. actor-safe reads ---------------------------------------------------------------------------
def test_reads_return_snapshots_not_live_containers(iso):
    store = MemoryReviews()
    store.open(Handoff("G-1", "route"))
    assert isinstance(store.items, tuple) and store.items is not store.items
    with pytest.raises(AttributeError):
        store.items.append({})                   # type: ignore[attr-defined]
    iso.record("G-1", "ok", {"rule": "G-1"})
    it = iter(iso.sink)
    iso.record("G-1", "ok", {"rule": "G-1"})     # recording meanwhile never breaks an iteration
    assert len(list(it)) == 1 and len(list(reversed(iso.sink))) == 2


def test_record_mode_standing_reviews_hammered_while_loading_and_demoting(iso):
    doc = r19.guard_doc("G-1", "spend", "args.amount > 5")
    inv.load(doc)
    inv.configure(iso, error_spike=inv.ErrorSpike(errors=3, window=60))
    rule = iso.rules["G-1"]
    errors: list[BaseException] = []
    n = 300

    def run(fn: Any) -> None:
        try:
            for i in range(n):
                fn(i)
        except BaseException as e:               # anything at all fails the test
            errors.append(e)

    def capturer(i: int) -> None:
        with inv.capture(iso):
            pass

    def loader(i: int) -> None:
        iso.install(inv.load(doc, install=False))
        iso.restore("G-1")
    jobs = [lambda i: iso.record("G-1", "rule_error" if i % 2 else "ok", {"rule": "G-1", "i": i}),
            lambda i: iso.mode(rule, rule.enforcers[0]),
            lambda i: iso.standing("G-1"),
            lambda i: (iso.hand_off(Handoff("G-1", "route")), iso.reviews),
            lambda i: (list(iso.sink), iso.sink[-1:], iso.counts("G-1")),
            capturer,
            loader]
    threads = [threading.Thread(target=run, args=(j,)) for j in jobs for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    assert iso.counts("G-1")["runs"] == 2 * n
    assert len(iso.reviews) == min(2 * n, 1000) and len({r["id"] for r in iso.reviews}) == len(iso.reviews)


def test_jsonl_reviews_concurrent_open_is_unique_complete_and_fsynced_first(tmp_path, monkeypatch):
    path = tmp_path / "reviews.jsonl"
    store, order, lock = JsonlReviews(path), [], threading.Lock()
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        real_fsync(fd)
        last = path.read_text().splitlines()[-1]   # the store's lock is held: the line just written
        with lock:
            order.append(("fsync", json.loads(last)["id"]))
    monkeypatch.setattr(os, "fsync", fsync)

    def opener(k: int) -> None:
        for i in range(25):
            rid = store.open(Handoff("G-1", "route", bound={"k": k, "i": i, "x": "y" * 500}))
            with lock:
                order.append(("returned", rid))
    threads = [threading.Thread(target=opener, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    lines = path.read_text().splitlines()
    ids = [json.loads(line)["id"] for line in lines]   # every line is complete JSON
    assert len(ids) == 200 and len(set(ids)) == 200
    for rid in ids:
        assert order.index(("fsync", rid)) < order.index(("returned", rid))


# --- the process session ---------------------------------------------------------------------------
def test_process_session_counts_and_warns_once_under_threads():
    s = session.ProcessSession()
    threads = [threading.Thread(target=lambda: [s.started() for _ in range(500)]) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert s.pytest_sessions == 2000 and s.active()
    firsts = [s.first_armed("R-1") for _ in range(5)] + [s.first_unarmed() for _ in range(5)]
    assert firsts.count(True) == 2
    for _ in range(2001):
        s.ended()
    assert s.pytest_sessions == 0 and not s.active()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_process_session_after_fork_keeps_its_values_with_a_fresh_lock():
    s = registry._SESSION
    s._lock.acquire()                            # held at the fork, as by another parent thread
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)   # fork() with threads alive
            pid = os.fork()
        if pid == 0:                             # the child: under_pytest() must not deadlock
            os._exit(0 if registry.under_pytest() else 1)
    finally:
        s._lock.release()
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0


# --- 3. decision traces under pytest-xdist ---------------------------------------------------------
APP = '''
import invariants as inv

BROKEN = False

@inv.guard("R-1")
def halve(n: int) -> int:
    return -1 if BROKEN else n // 2

@inv.guard("R-3")
def third(n: int) -> int:
    return n // 3
'''

CONFTEST = '''
import os, pathlib

def note_trace():
    with pathlib.Path(__file__).with_name("traces.log").open("a") as f:
        f.write(f"{os.getpid()} {os.environ.get('PYTEST_XDIST_WORKER', 'main')} "
                f"{os.environ.get('INV_DECISION_TRACE', '-')}\\n")
'''

TESTS_A = '''
import pytest
import invariants as inv
import trace_app
from conftest import note_trace

def test_r1_falsifier(monkeypatch):
    note_trace()
    monkeypatch.setattr(trace_app, "BROKEN", True)
    with pytest.raises(inv.Rejected):
        trace_app.halve(4)
'''

TESTS_B = '''
import invariants as inv
import trace_app
from conftest import note_trace

def test_r3_falsifier():
    note_trace()
    assert trace_app.third(9) == 3           # the rule never decides anything here
    assert not inv.is_ablated("R-3")          # fails under ablation for another reason
'''


def _rule(rid: str, target: str, falsifier: str) -> dict[str, Any]:
    return {"id": rid, "status": "enforce", "subject": {"target": target}, "bad": "result < 0",
            "enforcers": [{"point": "decorator", "timing": "post_effect", "mode": "enforce"}],
            "falsified_by": [falsifier]}


@pytest.fixture
def traced(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeini("[pytest]\npythonpath = .\n")
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(trace_app=APP, test_a=TESTS_A, test_b=TESTS_B)
    doc = {"spec": "invariant/v0.1",
           "targets": {"py:trace_app.halve": {"effects": "none"}, "py:trace_app.third": {"effects": "none"}},
           "rules": [_rule("R-1", "py:trace_app.halve", "test_a.py::test_r1_falsifier"),
                     _rule("R-3", "py:trace_app.third", "test_b.py::test_r3_falsifier")]}
    (pytester.path / "rules.json").write_text(json.dumps(doc))
    return pytester


def _ablate(p: pytest.Pytester, *args: str) -> tuple[dict[str, Any], list[list[str]]]:
    log = p.path / "traces.log"
    log.unlink(missing_ok=True)
    p.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:randomly", "--inv-rules", "rules.json",
                           "--inv-ablate", "--inv-report-json", "rep.json", *args)
    return json.loads((p.path / "rep.json").read_text()), [line.split() for line in log.read_text().splitlines()]


@pytest.mark.skipif(not __import__("importlib.util").util.find_spec("xdist"), reason="needs pytest-xdist")
def test_decision_traces_are_per_worker_and_the_result_matches_a_serial_run(traced):
    serial, _ = _ablate(traced)
    parallel, notes = _ablate(traced, "-n", "2")
    assert serial["ablation"] == parallel["ablation"]
    from invariants.pytest_plugin import NEVER_FIRED
    assert serial["ablation"]["rules"] == {
        "R-1": [{"falsifier": "test_a.py::test_r1_falsifier", "result": "caught"}],
        "R-3": [{"falsifier": "test_b.py::test_r3_falsifier", "result": NEVER_FIRED}]}
    controls = [(pid, worker, trace) for pid, worker, trace in notes if trace != "-"]
    assert len(controls) == 2 and all(worker != "main" for _, worker, _ in controls)
    assert len({trace for _, _, trace in controls}) == 2     # each control run wrote its own file, in its worker


# --- 4. provider fan-out ---------------------------------------------------------------------------
def test_concurrent_providers_share_nothing_until_their_owner_stores_the_results():
    cache: dict[Any, Any] = {}
    seen: list[dict[Any, Any]] = []
    running, peak = [0], [0]

    async def body(ok: bool) -> int:
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        await asyncio.sleep(0.01)
        seen.append(dict(cache))                 # what a provider task sees mid-gather
        running[0] -= 1
        if not ok:
            raise RuntimeError("down")
        return 1

    async def one() -> int:
        return await body(True)

    async def two() -> int:
        return await body(False)
    reg = inv.Registry()
    reg.providers = {n: Provider(n, {"args": {}, "concurrent": True}, fn, int, False)
                     for n, fn in (("one", one), ("two", two))}
    rule = Rule({"id": "P-1"}, [], {"a", "b"})
    whys = asyncio.run(prefetch_many(reg, [(rule, {"a": "provider.one", "b": "provider.two"}, {})], cache))
    assert whys == [None] and peak[0] == 2 and seen == [{}, {}]
    got = {key[0]: v for key, v in cache.items()}
    assert got["one"] == 1 and isinstance(got["two"], ProviderFailed)


@pytest.fixture
def iso():
    with inv.isolated() as r:
        yield r
