"""The nightly monitor: evaluates rules over stored rows. It reports; it never blocks (detect)."""
from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any

import invariants as inv

from .store import Store

# rule id -> what the monitor feeds it: one base environment per stored row
MONITORED = ("K-12", "K-15", "K-16")


@dataclasses.dataclass
class Finding:
    """One monitored rule over one row."""

    rule: str
    key: str
    outcome: str
    messages: list[str]
    error: str = ""


def _rows(store: Store, rule_id: str) -> Iterator[tuple[str, dict[str, Any]]]:
    if rule_id in ("K-12", "K-16"):                      # every BELIEVED fact
        for f in store.facts("BELIEVED"):
            yield f["id"], {"args": {"fact_id": f["id"]}, "self": store}
    elif rule_id == "K-15":                              # the deletion reconciler
        for (sid,) in store.conn.execute("SELECT id FROM sources WHERE deleted_at IS NOT NULL ORDER BY id"):
            yield sid, {"args": {"source_id": sid}, "self": store}


def run(store: Store, reg: inv.Registry | None = None) -> list[Finding]:
    """Evaluate each monitored rule over its rows inside one read transaction; inv.monitor records each."""
    findings = []
    with store.txn():
        for rid in MONITORED:
            for key, out in inv.monitor(rid, _rows(store, rid), reg, in_txn=True):
                if out.kind != "ok":
                    findings.append(Finding(rid, key, out.kind, out.messages, out.error))
    return findings
