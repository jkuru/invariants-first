"""Path search over the call edges: entrypoints, reachability and the shortest paths to a sink."""
from __future__ import annotations

import collections
from typing import Iterable, Iterator

from .contracts import _Edge, _Site
from .indexer import _Index


def _entrypoints(idx: _Index, edges: list[_Edge]) -> list[str]:
    """Functions with no extracted caller, module bodies, then anything those cannot reach.

    A name-matched caller is not proof, so it does not stop a function being an entrypoint.
    """
    proven = [e for e in edges if not e.inferred]
    called = {e.callee for e in proven if e.callee != e.caller}
    out = sorted(f for f in idx.funcs if f not in called)
    succ = collections.defaultdict(set)
    for e in proven:
        succ[e.caller].add(e.callee)
    seen = set(out)
    stack = list(out)
    for fid in sorted(idx.funcs):
        if not stack and fid not in seen:
            out.append(fid)
            seen.add(fid)
            stack.append(fid)
        while stack:
            for n in succ[stack.pop()]:
                if n not in seen:
                    seen.add(n)
                    stack.append(n)
    return out


def _reach(starts: Iterable[str], edges: list[_Edge], blocked: set[str]) -> set[str]:
    """Functions reachable from starts over every call edge (name-matched ones too) without entering
    or leaving a blocked function (the starts themselves are included unless blocked)."""
    succ: dict[str, set[str]] = collections.defaultdict(set)
    for e in edges:
        succ[e.caller].add(e.callee)
    seen = {s for s in starts if s not in blocked}
    stack = list(seen)
    while stack:
        for n in succ[stack.pop()]:
            if n not in seen and n not in blocked:
                seen.add(n)
                stack.append(n)
    return seen


def door_only(idx: _Index, edges: list[_Edge], sites: list[_Site], entries: list[str],
              trusted: set[str]) -> set[str]:
    """Helpers every path to which passes a door or sink: reachable from them, not reachable from any
    entrypoint without them, and never used as a value (a value can be called from anywhere)."""
    if not trusted:
        return set()
    free = _reach(entries, edges, trusted)
    from_trusted = _reach([n for e in edges if e.caller in trusted for n in [e.callee]], edges, trusted)
    escaped = {s.detail for s in sites if s.what == "ref"}
    return {f for f in from_trusted - free - escaped if f in idx.funcs and idx.funcs[f].node is not None}


def _shortest(start: str, goals: set[str], succ: dict[str, list[_Edge]], inferred_ok: bool,
              allowed: frozenset[str] | set[str] = frozenset()) -> tuple[list[str], _Edge] | None:
    """The shortest of _paths: the first call path from start to a goal, or None."""
    return next(_paths(start, goals, succ, inferred_ok, allowed), None)


def _paths(start: str, goals: set[str], succ: dict[str, list[_Edge]], inferred_ok: bool,
           allowed: frozenset[str] | set[str] = frozenset()) -> Iterator[tuple[list[str], _Edge]]:
    """BFS: for each call into a goal reachable from start, the shortest call path to it that passes at
    least one function outside `allowed` (a path of allowed functions alone is a frozen back door), and
    that last edge, shortest first."""
    State = tuple[str, bool]                     # (function, the path so far left `allowed`)
    first: State = (start, start not in allowed)
    prev: dict[State, tuple[State, _Edge] | None] = {first: None}
    queue = collections.deque([first])
    while queue:
        n, out = queue.popleft()
        for e in succ.get(n, ()):
            if e.inferred and not inferred_ok:
                continue
            if e.callee in goals:
                if not out:
                    continue                     # allowed functions all the way: frozen
                names, last = [e.callee], e
                cur: tuple[State, _Edge] | None = ((n, out), e)
                while cur is not None:
                    st, edge = cur
                    names.append(edge.caller)
                    cur = prev[st]
                yield list(reversed(names)), last
                continue
            nxt: State = (e.callee, out or e.callee not in allowed)
            if nxt in prev:
                continue
            prev[nxt] = ((n, out), e)
            queue.append(nxt)
