"""Fixture app for the fix-round-4 tests: closed record types, an async provider, sync and async guards."""
from __future__ import annotations

import dataclasses
from typing import Any, Optional

import invariants as inv

M = "py:round4"
SPEC = "invariant/v0.1"


@dataclasses.dataclass
class Rec:
    size: int
    name: str

    @property
    def double(self) -> int:
        return self.size * 2


@inv.provider("rec")
def rec(n: int) -> Rec:
    return Rec(n, "r")


@inv.provider("maybe_rec")
def maybe_rec(n: int) -> Optional[Rec]:
    return Rec(n, "r") if n else None


@inv.provider("bag")
def bag(n: int) -> dict:
    return {"size": n}


@inv.provider("aprov")
async def aprov(n: int) -> int:
    return n


@inv.guard("G-1")
def door(n: int, anything: Any = None) -> int:
    return n


@inv.guard("G-2")
async def adoor(n: int) -> int:
    return n


def doc(*rules: dict[str, Any]) -> dict[str, Any]:
    prov = {"args": {"n": "args.n"}}
    return {
        "spec": SPEC,
        "targets": {f"{M}.door": {"effects": "none"}, f"{M}.adoor": {"effects": "none"}},
        "providers": {
            "rec": {"type": f"{M}.Rec", "from": f"{M}.rec", **prov},
            "maybe_rec": {"type": f"{M}.Rec | null", "from": f"{M}.maybe_rec", **prov},
            "bag": {"type": "dict", "from": f"{M}.bag", **prov},
            "aprov": {"type": "int", "from": f"{M}.aprov", **prov},
        },
        "rules": list(rules),
    }


def rule(rid: str, target: str, bad: Any, on_error: str = "fail_closed") -> dict[str, Any]:
    return {"id": rid, "status": "enforce", "subject": {"target": f"{M}.{target}"}, "bad": bad,
            "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce",
                           "on_error": on_error}],
            "falsified_by": ["tests/test_fix_round4.py"]}
