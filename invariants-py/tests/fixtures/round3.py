"""Fixture app for the fix-round-3 tests: py: keys, provider types, classmethods."""
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
            f"{M}.pykey": {"effects": "none"}, f"{M}.pure": {"effects": "none"}, f"{M}.sized": {"effects": "none"},
            **(targets or {}),
        },
        "providers": {
            "label": {"type": "str", "from": f"{M}.label", "args": {}},
            "maybe": {"type": "int | null", "from": f"{M}.maybe", "args": {}},
        },
        "rules": list(rules),
    }


def rule(rid: str, target: str, bad: Any, timing: str = "pre_effect", **kw: Any) -> dict[str, Any]:
    return {"id": rid, "status": "enforce", "subject": {"target": f"{M}.{target}"}, "bad": bad,
            "enforcers": [{"point": "decorator", "timing": timing, "mode": "enforce"}],
            "falsified_by": ["tests/test_fix_round3.py"], **kw}
