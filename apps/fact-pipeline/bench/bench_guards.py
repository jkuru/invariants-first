"""Guard overhead: each guarded path with guards on, with every rule ablated, and with the guard removed.

Run: python bench/bench_guards.py [--n 2000]. Prints p50/p99 in microseconds.
"bare" calls the undecorated function (functools.wraps' __wrapped__); this file is outside the
scanned roots on purpose, since that idiom is exactly what `inv scan` forbids in app code.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import statistics
import time
from collections.abc import Callable, Iterator
from typing import Any

import invariants as inv

from fact_pipeline import rules_path
from fact_pipeline.answer import compose_answer
from fact_pipeline.extract import propose_fact
from fact_pipeline.fake_model import FakeModel
from fact_pipeline.models import AnswerReply, EvidenceSpan, FactReply, Proposal
from fact_pipeline.pipeline import Pipeline
from fact_pipeline.store import Store, fact_id

TEXT = "Acme Corp said Tuesday it completed its acquisition of Beta Ltd for about $2.1 billion."


def timed(fn: Callable[[int], Any], n: int) -> dict[str, float]:
    for i in range(min(50, n)):          # warm up
        fn(-1 - i)
    xs = []
    for i in range(n):
        t = time.perf_counter_ns()
        fn(i)
        xs.append((time.perf_counter_ns() - t) / 1000)
    xs.sort()
    return {"p50": round(statistics.median(xs), 1), "p99": round(xs[int(len(xs) * 0.99) - 1], 1)}


@contextlib.contextmanager
def all_ablated() -> Iterator[None]:
    with contextlib.ExitStack() as stack:
        for rid in inv.current().rules:
            stack.enter_context(inv.ablated(rid))
        yield


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    n = ap.parse_args().n
    inv.load(rules_path())
    inv.verify()
    store = Store()
    pipe = Pipeline(store, FakeModel())
    src = store.add_source("bench://doc", TEXT, "pub-a")
    chunk = store.chunks_for(src.id)[0]
    acme = store.add_entity("Acme Corp")
    names = [store.add_entity(f"Target {i}") for i in range(-60, 3 * n + 1)]
    ev = [EvidenceSpan(src.id, src.hash, chunk.id, 0, 9, "Acme Corp", "ASSERTED")]
    counter = iter(range(10**9))

    def proposal() -> Proposal:
        obj = names[next(counter) % len(names)]
        return Proposal(fact_id(acme, "acquired", obj, "NONE", None, None), acme, "acquired", obj, "entity",
                        "NONE", None, None, ev)

    def ingest_guarded(_: int) -> None:
        store.ingest(proposal())

    def ingest_bare(_: int) -> None:
        with store.txn():
            Store.ingest_door.__wrapped__(store, proposal())   # type: ignore[attr-defined]

    client = FakeModel()

    def extract_guarded(_: int) -> None:
        propose_fact(chunk.id, chunk.text, chunk.source_hash, 1)

    def extract_bare(_: int) -> None:
        inv.parse(client.complete({"task": "extract", "text": chunk.text, "index": 1}), FactReply)

    ids = [chunk.id]
    ctx = [{"id": chunk.id, "text": chunk.text}]

    def answer_guarded(_: int) -> None:
        compose_answer("acquisition of Beta", ids, ctx)

    def answer_bare(_: int) -> None:
        inv.parse(client.complete({"task": "answer", "question": "acquisition of Beta", "chunks": ctx}), AnswerReply)

    out: dict[str, dict[str, dict[str, float]]] = {}
    for name, guarded, bare in (("ingest_door (K-1,4,7,11,18; sqlite txn)", ingest_guarded, ingest_bare),
                                ("propose_fact (K-2 model_call)", extract_guarded, extract_bare),
                                ("compose_answer (K-19 model_call)", answer_guarded, answer_bare)):
        row = {"guarded": timed(guarded, n)}
        with all_ablated():
            row["ablated"] = timed(guarded, n)
        row["bare"] = timed(bare, n)
        out[name] = row
    print(f"backend={inv.current().backend_name} n={n} (microseconds)")
    print(f"{'path':<42} {'guarded p50/p99':>18} {'ablated p50/p99':>18} {'bare p50/p99':>16}")
    for name, row in out.items():
        cells = [f"{row[k]['p50']:.1f}/{row[k]['p99']:.1f}" for k in ("guarded", "ablated", "bare")]
        print(f"{name:<42} {cells[0]:>18} {cells[1]:>18} {cells[2]:>16}")
    print(json.dumps(out))


if __name__ == "__main__":
    main()
