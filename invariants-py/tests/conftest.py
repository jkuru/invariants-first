"""Fixtures: the example apps live in tests/fixtures, separate from the library."""
from __future__ import annotations

import sys
from typing import Iterator

import pytest

from support import FIXTURES, RULE_FILE

if str(FIXTURES) not in sys.path:
    sys.path.insert(0, str(FIXTURES))

import invariants as inv  # noqa: E402


@pytest.fixture
def reg() -> Iterator[inv.Registry]:
    """An isolated registry with the fixture rule file loaded into it."""
    with inv.isolated() as r:
        inv.load(RULE_FILE)
        yield r


@pytest.fixture
def pool_svc(reg: inv.Registry):
    """A PoolService on a fresh database with pools p1 and p2 (size 100)."""
    import pool
    conn = pool.connect()
    conn.execute("INSERT INTO pools VALUES ('p1', 100), ('p2', 100)")
    svc = pool.PoolService(conn)

    def resize(pid: str, new: int) -> None:
        svc.resize_door(pool.ResizePoolRequest(pid, new))
    svc.resize = resize  # type: ignore[attr-defined]
    return svc
