"""Fixture app for the fix-round-3 tests: re-entrant and savepoint transactions, py: keys, classmethods."""
from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any, Iterator

import invariants as inv

M = "py:round3"
SPEC = "invariant/v0.1"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    c.execute("CREATE TABLE t (x INTEGER)")
    return c


class Store:
    """Re-entrant boundary: only the outermost block BEGINs and COMMITs (the common pattern)."""

    def __init__(self) -> None:
        self.conn, self.depth = _conn(), 0

    @inv.transaction
    def txn(self) -> Iterator[None]:
        self.depth += 1
        outer = self.depth == 1
        if outer:
            self.conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            if outer:
                self.conn.execute("ROLLBACK")
            raise
        finally:
            self.depth -= 1
        if outer:
            self.conn.execute("COMMIT")

    def total(self) -> int:
        return self.conn.execute("SELECT coalesce(sum(x), 0) FROM t").fetchone()[0]

    @inv.guard("S-1")
    def write(self, n: int) -> int:
        self.conn.execute("INSERT INTO t VALUES (?)", (n,))
        return n


class SpStore(Store):
    """Nested blocks are real savepoints."""

    @inv.transaction(savepoint=True)
    def sp(self) -> Iterator[None]:
        self.depth += 1
        name = f"sp{self.depth}"
        self.conn.execute("BEGIN" if self.depth == 1 else f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK" if self.depth == 1 else f"ROLLBACK TO {name}")
            if self.depth > 1:
                self.conn.execute(f"RELEASE {name}")
            raise
        finally:
            self.depth -= 1
        self.conn.execute("COMMIT" if self.depth == 0 else f"RELEASE {name}")

    @inv.guard("S-2")
    def spwrite(self, n: int) -> int:
        self.conn.execute("INSERT INTO t VALUES (?)", (n,))
        return n


@inv.guard("py:round3.pykey")
def pykey(n: int) -> int:
    return n


@inv.guard("P-1")
def pure(n: int) -> int:
    return n


@dataclasses.dataclass
class Req:
    pool: str
    size: int
    tags: list[int]


@inv.guard("P-2")
def sized(req: Req, anything: Any = None) -> int:
    return req.size


@inv.provider("label")
def label() -> str:
    return "x"


@inv.provider("maybe")
def maybe() -> int | None:
    return 1


class Typed:
    """Targets with annotations for the load-time type check."""

    @inv.guard("C-1")
    @classmethod
    def cm(cls, n: int) -> int:
        return n

    @classmethod
    @inv.guard("C-2")
    def cm2(cls, n: int) -> int:
        return n

    @inv.guard("C-3")
    @staticmethod
    def sm(n: int) -> int:
        return n


def doc(*rules: dict[str, Any], targets: dict[str, Any] | None = None) -> dict[str, Any]:
    """The fixture rule file with the given rules."""
    return {
        "spec": SPEC,
        "targets": {
            f"{M}.Store.write": {"effects": "transactional", "transaction": f"{M}.Store.txn"},
            f"{M}.SpStore.spwrite": {"effects": "transactional", "transaction": f"{M}.SpStore.sp"},
            f"{M}.pykey": {"effects": "none"}, f"{M}.pure": {"effects": "none"}, f"{M}.sized": {"effects": "none"},
            **(targets or {}),
        },
        "providers": {
            "label": {"type": "str", "from": f"{M}.label", "args": {}, "consistency": "stale_ok"},
            "maybe": {"type": "int | null", "from": f"{M}.maybe", "args": {}, "consistency": "stale_ok"},
        },
        "rules": list(rules),
    }


def rule(rid: str, target: str, bad: Any, timing: str = "pre_effect", **kw: Any) -> dict[str, Any]:
    return {"id": rid, "status": "enforce", "subject": {"target": f"{M}.{target}"}, "bad": bad,
            "enforcers": [{"point": "decorator", "timing": timing, "mode": "enforce"}],
            "falsified_by": ["tests/test_fix_round3.py"], **kw}
