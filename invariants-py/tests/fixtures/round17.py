"""Fix round 17 fixtures: async providers that opt in to running concurrently."""
from __future__ import annotations

import asyncio

import invariants as inv

CALLS: list[tuple[str, int]] = []


@inv.provider("fast1", concurrent=True)
async def fast1(x: int) -> int:
    CALLS.append(("fast1-start", x))
    await asyncio.sleep(0.05)
    CALLS.append(("fast1-end", x))
    return x


@inv.provider("fast2", concurrent=True)
async def fast2(x: int) -> int:
    CALLS.append(("fast2-start", x))
    await asyncio.sleep(0.05)
    CALLS.append(("fast2-end", x))
    return x
