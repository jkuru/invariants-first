"""Fixture app for the fix-round-5 tests: hostile mappings, rule reload mid-call, snapshot providers."""
from __future__ import annotations

import asyncio
from typing import Any, Callable

import invariants as inv

M = "py:round5"
SPEC = "invariant/v0.1"
HOOK: list[Callable[[], Any]] = []


class Evil(dict):  # type: ignore[type-arg]
    """A mapping whose lookups raise something that is not a CelError."""

    def __getitem__(self, k: Any) -> Any:
        raise RuntimeError("evil getitem")

    def __contains__(self, k: object) -> bool:
        return True


@inv.provider("lim")
def lim(k: int) -> int:
    return 10


@inv.provider("lim2")
def lim2(k: int) -> int:
    return 10


@inv.provider("snap")
def snap(k: int) -> int:
    return 10


@inv.guard("Q-1")
def door(d: dict) -> int:  # type: ignore[type-arg]
    return 1


@inv.guard("Q-2")
async def adoor(d: dict) -> int:  # type: ignore[type-arg]
    return 1


@inv.guard("Q-3")
def sdoor(d: dict) -> int:  # type: ignore[type-arg]
    return 1


@inv.guard("Q-4")
async def asdoor(d: dict) -> int:  # type: ignore[type-arg]
    return 1


@inv.guard(f"{M}.reload")
def reload(n: int) -> int:
    for h in HOOK:
        h()
    return n


@inv.guard(f"{M}.areload")
async def areload(n: int) -> int:
    for h in HOOK:
        h()
    await asyncio.sleep(0)
    return n


def _rule(rid: str, target: str, bad: str, timing: str = "pre_effect", on_error: str = "fail_closed") -> dict[str, Any]:
    return {"id": rid, "status": "enforce", "subject": {"target": f"{M}.{target}"}, "bad": bad,
            "enforcers": [{"point": "decorator", "timing": timing, "mode": "enforce", "on_error": on_error}],
            "falsified_by": ["tests/test_fix_round5.py"]}


def hostile_doc() -> dict[str, Any]:
    pa = {"args": {"k": "args.d.k"}}
    return {"spec": SPEC,
            "targets": {f"{M}.{t}": {"effects": "none"} for t in ("door", "adoor", "sdoor", "asdoor")},
            "providers": {"lim": {"type": "int", "from": f"{M}.lim", "consistency": "stale_ok", **pa},
                          "snap": {"type": "int", "from": f"{M}.snap", "consistency": "snapshot", **pa}},
            "rules": [_rule("Q-1", "door", "lim > 5"), _rule("Q-2", "adoor", "lim > 5"),
                      _rule("Q-3", "sdoor", "snap > 5", "post_effect"),
                      _rule("Q-4", "asdoor", "snap > 5", "post_effect")]}


def reload_doc(pname: str) -> dict[str, Any]:
    """S-1/S-2: result > <pname>; reloading with another pname swaps the provider table."""
    return {"spec": SPEC,
            "targets": {f"{M}.reload": {"effects": "none"}, f"{M}.areload": {"effects": "none"}},
            "providers": {pname: {"type": "int", "from": f"{M}.{pname}", "consistency": "stale_ok",
                                  "args": {"k": "args.n"}}},
            "rules": [_rule("S-1", "reload", f"result > {pname}", "post_effect", "fail_open_and_page"),
                      _rule("S-2", "areload", f"result > {pname}", "post_effect", "fail_open_and_page")]}
