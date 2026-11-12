"""Guard overhead: the same work with and without the invariants guards, p50/p99 in microseconds.

"without" calls the unguarded implementation (or runs with the rule ablated for HTTP), "with" the door.
Run: python bench/bench_guards.py [--n 2000] [--json]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import tempfile
import time
from typing import Callable

from fastapi.testclient import TestClient

import invariants as inv
from pool_service import api, db, service, store, triage, work
from pool_service.fake_model import FakeModelClient

RULES = pathlib.Path(__file__).resolve().parents[1] / "invariants.yaml"


def timed(fn: Callable[[int], object], n: int) -> dict[str, float]:
    for i in range(min(50, n)):
        fn(i)
    xs = []
    for i in range(n):
        t = time.perf_counter_ns()
        fn(i)
        xs.append((time.perf_counter_ns() - t) / 1000)
    xs.sort()
    return {"p50_us": round(statistics.median(xs), 2), "p99_us": round(xs[min(n - 1, int(n * 0.99))], 2)}


def pair(name: str, without: Callable[[int], object], with_: Callable[[int], object], n: int) -> dict[str, object]:
    a, b = timed(without, n), timed(with_, n)
    return {"case": name, "without": a, "with": b,
            "overhead_p50_us": round(b["p50_us"] - a["p50_us"], 2),
            "overhead_p99_us": round(b["p99_us"] - a["p99_us"], 2)}


def run(n: int) -> list[dict[str, object]]:
    api.load_rules(RULES)
    out = []
    caps = [3, 5, 7, 11, 13, 17, 19, 23]
    out.append(pair("R-1 split_work (pure)", lambda i: work._allocate(50, caps),
                    lambda i: work.split_work(50, caps), n))

    conn = db.memory()
    service.create_pool(conn, "bench", "B", 10 ** 4)

    def raw_lease(i: int) -> None:
        lid = store.insert_lease(conn, "bench", "b", 1)
        store.delete_lease(conn, lid)

    def door_lease(i: int) -> None:
        lid = service.lease_slots(conn, "bench", 1, "b")
        store.delete_lease(conn, lid)
    out.append(pair("R-20 lease (sqlite, 2 provider reads)", raw_lease, door_lease, n))

    svc = service.ResizeService(conn)
    pids = [service.propose_resize(conn, "bench", 10 ** 4 + 1 + (i % 2), "alice") for i in range(2 * n + 200)]
    for p in pids:
        service.approve_resize(conn, p, "bob")
    it = iter(pids)

    def raw_resize(i: int) -> None:
        with svc.txn():
            store.set_size(conn, "bench", 10 ** 4 + 1 + (i % 2))

    def door_resize(i: int) -> None:
        svc.apply(next(it))
    out.append(pair("R-21+R-22 resize (approval provider, current size)", raw_resize, door_resize, n))

    store.insert_job(conn, "J", "bench", True, 1, 10, 137)
    triage.set_client(FakeModelClient())
    prompt = triage.prompt_for("J", "Out of memory: Killed process 1")
    out.append(pair("R-7 model call + edge (fake model)",
                    lambda i: inv.parse(triage.client().complete(prompt), triage.CauseReply),
                    lambda i: triage.remediate(conn, triage.propose_cause(conn, "J", "Out of memory: Killed process 1"),
                                               "J"), n))

    with tempfile.TemporaryDirectory() as d:
        app = api.create_app(pathlib.Path(d) / "b.db", rules=RULES, load=False)
        with TestClient(app) as c:
            c.post("/pools", json={"pool_id": "http", "name": "H", "size": 10 ** 4})

            def http_lease(i: int) -> None:
                lid = c.post("/pools/http/leases", json={"holder": "b", "slots": 1}).json()["lease_id"]
                c.delete(f"/leases/{lid}")

            def http_ablated(i: int) -> None:
                with inv.ablated("R-20"), inv.ablated("H-1"):
                    http_lease(i)
            out.append(pair("HTTP lease (H-1 route + R-20 door, rules ablated vs on)", http_ablated, http_lease,
                            max(50, n // 10)))
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=2000)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    rows = run(a.n)
    if a.json:
        print(json.dumps(rows, indent=2))
        return
    print(f"{'case':<58} {'without p50/p99 us':>20} {'with p50/p99 us':>18} {'overhead p50/p99':>18}")
    for r in rows:
        w, g = r["without"], r["with"]
        print(f"{r['case']:<58} {w['p50_us']:>9}/{w['p99_us']:<10} {g['p50_us']:>8}/{g['p99_us']:<9} "
              f"{r['overhead_p50_us']:>8}/{r['overhead_p99_us']}")


if __name__ == "__main__":
    main()
