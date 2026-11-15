"""Path search over the call edges: entrypoints, reachability and the shortest paths to a sink.

A call graph here is a list of call records: an _Edge (one call, one callee) or a _Fan (a name-matched
call on an unknown receiver, standing for one inferred edge to each of its callees, in their order).
Every search treats a fan exactly as those edges in that place, without making them: a name like
`forward` or `save` can match thousands of methods, at thousands of call sites."""
from __future__ import annotations

import collections
from typing import Any, Iterable, Iterator, Union

from .contracts import _Edge, _Fan, _Site
from .indexer import _Index

_Call = Union[_Edge, _Fan]
_MODULE = ".<module>"                            # a module body: never a frozen back door of its own


def _flat(calls: Iterable[_Call]) -> Iterator[_Edge]:
    """The edges the call records stand for, in order (a fan as one inferred edge per callee)."""
    for c in calls:
        if type(c) is _Fan:
            yield from c.edges()
        else:
            yield c  # type: ignore[misc]


def _entrypoints(idx: _Index, edges: list[_Call]) -> list[str]:
    """Functions with no extracted caller, module bodies, then anything those cannot reach.

    A name-matched caller is not proof, so it does not stop a function being an entrypoint.
    """
    proven = [e for e in edges if type(e) is _Edge and not e.inferred]
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


def _reach(starts: Iterable[str], edges: Iterable[_Call], blocked: set[str]) -> set[str]:
    """Functions reachable from starts over every call edge (name-matched ones too) without entering
    or leaving a blocked function (the starts themselves are included unless blocked)."""
    succ: dict[str, set[str]] = collections.defaultdict(set)
    fans: dict[str, list[tuple[str, ...]]] = collections.defaultdict(list)
    for e in edges:
        if type(e) is _Fan:
            fans[e.caller].append(e.callees)
        else:
            succ[e.caller].add(e.callee)  # type: ignore[union-attr]
    seen = {s for s in starts if s not in blocked}
    stack = list(seen)
    spread: set[int] = set()                     # a fan's callees are the same from every caller: once
    while stack:
        n = stack.pop()
        for c in succ.get(n, ()):
            if c not in seen and c not in blocked:
                seen.add(c)
                stack.append(c)
        for cs in fans.get(n, ()):
            if id(cs) in spread:
                continue
            spread.add(id(cs))
            for c in cs:
                if c not in seen and c not in blocked:
                    seen.add(c)
                    stack.append(c)
    return seen


def door_only(idx: _Index, edges: list[_Call], sites: list[_Site], entries: list[str],
              trusted: set[str]) -> set[str]:
    """Helpers every path to which passes a door or sink: reachable from them, not reachable from any
    entrypoint without them, and never used as a value (a value can be called from anywhere)."""
    if not trusted:
        return set()
    free = _reach(entries, edges, trusted)
    from_trusted = _reach([e.callee for e in _flat(c for c in edges if c.caller in trusted)], edges, trusted)
    escaped = {s.detail for s in sites if s.what == "ref"}
    # `.m` taken as a value on a receiver the scan cannot type may be any scanned method named m (fix
    # round 35: `f = obj.run` let Helper.run's own sites go unreported)
    escaped.update(m for s in sites if s.what == "attrref" for m in idx.fan(s.detail))
    return {f for f in from_trusted - free - escaped if f in idx.funcs and idx.funcs[f].node is not None}


def _paths(start: str, goals: set[str], succ: dict[str, list[_Call]], inferred_ok: bool,
           allowed: frozenset[str] | set[str] = frozenset(),
           split: dict[int, tuple[tuple[str, ...], tuple[str, ...]]] | None = None
           ) -> Iterator[tuple[list[str], _Edge]]:
    """BFS: for each call into a goal reachable from start, the shortest call path to it that passes at
    least one function outside `allowed` (a path of allowed functions alone is a frozen back door), and
    that last edge, shortest first. A fan's callees that are not goals are spread once per search from
    states that left `allowed` (and once from those that did not): each gets the same state from any
    caller, so spreading them again adds nothing (fix round 29: a large fan, spread again from each of its
    callers, cost minutes on a large tree). split: a fan's (goal, other) callees, kept across searches."""
    State = tuple[str, bool]                     # (function, the path so far left `allowed`)
    first: State = (start, start not in allowed)
    prev: dict[State, State | None] = {first: None}
    queue = collections.deque([first])
    split = {} if split is None else split
    spread: set[tuple[int, bool]] = set()
    while queue:
        st = queue.popleft()
        n, out = st
        for e in succ.get(n, ()):
            if e.inferred and not inferred_ok:
                continue
            if type(e) is _Fan:
                key = id(e.callees)
                got = split.get(key)
                if got is None:
                    got = split[key] = (tuple(c for c in e.callees if c in goals),
                                        tuple(c for c in e.callees if c not in goals))
                into, rest = got
                if (key, out) in spread:
                    rest = ()
                else:
                    spread.add((key, out))
            else:
                c = e.callee  # type: ignore[union-attr]
                into, rest = ((c,), ()) if c in goals else ((), (c,))
            if into and (out or n.endswith(_MODULE)):    # else allowed functions all the way: frozen (a
                for callee in into:                       # module body's own call into the sink never is:
                    names = [callee]                      # fix round 28)
                    cur: State | None = st
                    while cur is not None:
                        names.append(cur[0])
                        cur = prev[cur]
                    yield list(reversed(names)), (e if type(e) is _Edge else
                                                  _Edge(n, callee, e.line, e.col, True, e.via_getattr))
            for callee in rest:
                nxt: State = (callee, out or callee not in allowed)
                if nxt in prev:
                    continue
                prev[nxt] = st
                queue.append(nxt)


# ---- one search per rule -------------------------------------------------------------------------------
# _paths from every entrypoint costs O(entrypoints x edges). The searches below give the same findings,
# paths and on-path functions in the same order: a search from one start never needs the functions that
# cannot reach a goal (pruning them keeps the search order of the others: every caller of a function
# that can reach a goal can reach it too), and only the first start that reaches a call into a goal
# reports it, so a search runs from those starts alone. The on-path functions of every start are worked
# out only when someone asks for them (`inv init`, an allow entry's use).

class _Callers:
    """Who calls a function, over call records: the extracted (and plain) edges, and the fans whose
    callees hold it (a fan's callers are found once, whichever of its callees is reached first)."""

    def __init__(self, calls: Iterable[_Call], goals: frozenset[str] | set[str] = frozenset()) -> None:
        self.pred: dict[str, set[str]] = collections.defaultdict(set)
        self.fan_callers: dict[int, list[str]] = collections.defaultdict(list)
        self.in_fans: dict[str, list[int]] = collections.defaultdict(list)
        self.into_goal: list[str] = []           # callers with a call into a goal, in order
        held: dict[int, tuple[str, ...]] = {}
        for e in calls:
            if type(e) is _Fan:
                key = id(e.callees)
                if key not in held:
                    held[key] = e.callees
                    for c in e.callees:
                        if c not in goals:
                            self.in_fans[c].append(key)
                self.fan_callers[key].append(e.caller)
                if goals and any(c in goals for c in e.callees):
                    self.into_goal.append(e.caller)
            elif e.callee in goals:  # type: ignore[union-attr]
                self.into_goal.append(e.caller)
            else:
                self.pred[e.callee].add(e.caller)  # type: ignore[union-attr]

    def back(self, starts: Iterable[str], blocked: set[str] | frozenset[str] = frozenset()) -> set[str]:
        """starts (less blocked ones) and every unblocked function with a path of unblocked functions to one."""
        seen = {s for s in starts if s not in blocked}
        stack = list(seen)
        fired: set[int] = set()
        while stack:
            n = stack.pop()
            for p in self.pred.get(n, ()):
                if p not in seen and p not in blocked:
                    seen.add(p)
                    stack.append(p)
            for key in self.in_fans.get(n, ()):
                if key in fired:
                    continue
                fired.add(key)
                for p in self.fan_callers[key]:
                    if p not in seen and p not in blocked:
                        seen.add(p)
                        stack.append(p)
        return seen


class _Pass:
    """One pass of the path search (extracted calls only, or name-matched ones too) over a rule's
    door-free graph: its calls, pruned to the functions that can reach a call into a goal (live)."""

    def __init__(self, goals: set[str], succ: dict[str, list[_Call]], inferred_ok: bool) -> None:
        self.goals = goals
        kept = {c: own for c, es in succ.items() if (own := [e for e in es if inferred_ok or not e.inferred])}
        callers = _Callers((e for es in kept.values() for e in es), goals)
        self.live = live = callers.back(callers.into_goal)
        pruned: dict[int, tuple[str, ...]] = {}

        def keep(cs: tuple[str, ...]) -> tuple[str, ...]:
            if id(cs) not in pruned:
                got = tuple(c for c in cs if c in goals or c in live)
                pruned[id(cs)] = cs if len(got) == len(cs) else got
            return pruned[id(cs)]
        self._split: dict[int, tuple[tuple[str, ...], tuple[str, ...]]] = {}   # a fan's (goal, other) callees
        self._dist: dict[str, int] | None = None                                 # first_path's distances
        self.succ: dict[str, list[_Call]] = {}
        for c, es in kept.items():
            if c not in live:
                continue
            mine: list[_Call] = []
            for e in es:
                if type(e) is _Fan:
                    cs = keep(e.callees)
                    if cs:
                        mine.append(e if cs is e.callees else _Fan(e.caller, cs, e.line, e.col, e.via_getattr))
                elif e.callee in goals or e.callee in live:  # type: ignore[union-attr]
                    mine.append(e)
            self.succ[c] = mine

    def paths(self, start: str, allowed: frozenset[str] | set[str] = frozenset()
              ) -> Iterator[tuple[list[str], _Edge]]:
        """What _paths(start, goals, succ, inferred_ok, allowed) yields."""
        if start not in self.live:
            return iter(())
        return _paths(start, self.goals, self.succ, True, allowed, self._split)

    def first_path(self, start: str) -> list[str] | None:
        """The functions on the first path paths(start) yields (no `allowed`), without a search from start
        (fix round 29: one search per function taken as a value cost minutes on a large tree). Breadth
        first, a search visits the functions at one distance from start in the order of the calls that
        lead to them, so the first path is the shortest to a call into a goal that takes, at each step,
        the first call (and a fan's first callee) one step nearer: worked out from every function's
        distance to such a call, found once for all."""
        if start not in self.live:
            return None
        dist = self._dist if self._dist is not None else self._distances()
        names, n = [start], start
        while True:
            d = dist[n]
            for _, into, rest in self._edges(n):
                if d == 0 and into:
                    return names + [into[0]]
                nxt = next((c for c in rest if dist.get(c) == d - 1), None) if d else None
                if nxt is not None:
                    break
            else:
                return None                      # (no live function lacks a distance)
            names.append(nxt)
            n = nxt

    def _distances(self) -> dict[str, int]:
        """Each live function's distance in calls to a call into a goal: a breadth-first search back from
        the functions that call one, a fan's callers one step beyond the nearest of its callees."""
        callers: dict[Any, list[Any]] = collections.defaultdict(list)    # a function or fan: who calls it
        for n in self.succ:
            for e, into, rest in self._edges(n):
                if type(e) is _Fan:
                    key = id(e.callees)
                    if ("", key) not in callers:
                        for c in rest:
                            callers[c].append(("", key))          # a fan is as near as its nearest callee
                    callers[("", key)].append(n)
                else:
                    for c in rest:
                        callers[c].append(n)
        dist: dict[Any, int] = {}
        line: collections.deque[Any] = collections.deque()
        for n in self.succ:
            if any(into for _, into, _ in self._edges(n)):
                dist[n] = 0
                line.append(n)
        while line:                              # 0-1 breadth first: a callee to its fan costs no step
            x = line.popleft()
            for p in callers.get(x, ()):
                d = dist[x] + (0 if isinstance(p, tuple) else 1)
                if p not in dist or d < dist[p]:
                    dist[p] = d
                    line.appendleft(p) if isinstance(p, tuple) else line.append(p)
        self._dist = {k: v for k, v in dist.items() if isinstance(k, str)}
        return self._dist

    def _edges(self, n: str) -> Iterator[tuple[_Call, tuple[str, ...], tuple[str, ...]]]:
        """n's calls, each with its callees that are goals and the others (a fan's split once)."""
        goals, split = self.goals, self._split
        for e in self.succ.get(n, ()):
            if type(e) is _Fan:
                key = id(e.callees)
                got = split.get(key)
                if got is None:
                    cs = e.callees
                    got = split[key] = (tuple(c for c in cs if c in goals), tuple(c for c in cs if c not in goals))
                yield e, got[0], got[1]
            else:
                c = e.callee  # type: ignore[union-attr]
                yield (e, (c,), ()) if c in goals else (e, (), (c,))

    def on_any_path(self, starts: list[str], allowed: frozenset[str] | set[str] = frozenset()) -> set[str]:
        """Every function on some call path from a start to a reported call into a goal (one made having
        left `allowed`, or from a module body), and the goals so called: the search states reached from
        the starts that reach such a call. One pass forward, one back; a fan spread from states that left
        `allowed` (or did not) is one node between its callers and its callees, so it is spread once."""
        State = tuple[str, bool]                 # (function, left `allowed`); a fan: ("", id, left)
        seen: set[Any] = {(s, s not in allowed) for s in starts if s in self.live}
        stack: list[Any] = list(seen)
        back: dict[Any, list[Any]] = collections.defaultdict(list)
        hits: list[Any] = []
        out: set[str] = set()
        while stack:
            st = stack.pop()
            if len(st) == 3:                     # a fan: its callees that are not goals
                for callee in self._split[st[1]][1]:
                    nxt: State = (callee, st[2] or callee not in allowed)
                    back[nxt].append(st)
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
                continue
            n, left = st
            hit = False
            for e, into, rest in self._edges(n):
                if into and (left or n.endswith(_MODULE)):
                    hit = True
                    out.update(into)
                if rest and type(e) is _Fan:
                    via = ("", id(e.callees), left)
                    back[via].append(st)
                    if via not in seen:
                        seen.add(via)
                        stack.append(via)
                    continue
                for callee in rest:
                    nxt = (callee, left or callee not in allowed)
                    back[nxt].append(st)
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            if hit:
                hits.append(st)
        done = set(hits)
        while hits:
            st = hits.pop()
            if len(st) == 2:
                out.add(st[0])
            for p in back.get(st, ()):
                if p not in done:
                    done.add(p)
                    hits.append(p)
        return out

    def into_goal(self, e: _Call) -> bool:
        if type(e) is _Fan:
            return any(c in self.goals for c in e.callees)
        return e.callee in self.goals  # type: ignore[union-attr]

    def first_starts(self, starts: list[str], allowed: frozenset[str] | set[str]) -> dict[str, int]:
        """For each function with a call into a goal, the index of the first start whose search reaches it
        having left `allowed` (where that search reports the call): each search state is visited from the
        first start that reaches it, so this is one pass over the graph."""
        label: set[tuple[str, bool]] = set()
        first: dict[str, int] = {}
        for i, s in enumerate(starts):
            st0 = (s, s not in allowed)
            if s not in self.live or st0 in label:
                continue
            label.add(st0)
            stack = [st0]
            while stack:
                n, out = stack.pop()
                es = self.succ.get(n, ())
                if (out or n.endswith(_MODULE)) and n not in first and any(self.into_goal(e) for e in es):
                    first[n] = i
                for e in es:
                    for callee in (e.callees if type(e) is _Fan else (e.callee,)):  # type: ignore[union-attr]
                        if callee in self.goals:
                            continue
                        nxt = (callee, out or callee not in allowed)
                        if nxt not in label:
                            label.add(nxt)
                            stack.append(nxt)
        return first


def _search_order(passes: list[_Pass], starts: list[str], allowed: frozenset[str] | set[str]
                  ) -> Iterator[tuple[int, list[str], _Edge]]:
    """(pass, path, last edge) for each call site into a goal the first time it is seen, as
    `for pass: for start in starts: for x in _paths(start, ...)` sees it (the same path, from the same
    start, in the same order), searching only from the first start that reaches each site."""
    reported: set[tuple[str, int, int]] = set()
    for k, ps in enumerate(passes):
        first = ps.first_starts(starts, allowed)
        todo = sorted({i for n, i in first.items()
                       if any(ps.into_goal(e) and (n, e.line, e.col) not in reported for e in ps.succ[n])})
        for i in todo:
            for names, last in ps.paths(starts[i], allowed):
                site = (last.caller, last.line, last.col)
                if site not in reported:
                    reported.add(site)
                    yield k, names, last


def _every_path_node(passes: list[_Pass], starts: list[str], allowed: frozenset[str] | set[str]) -> set[str]:
    """Every function on a door-free call path from a start to a reported call into a goal, in either pass,
    and the goals so called: what `inv init` freezes in allow, and what makes an allow entry in use (fix
    round 29: not only the functions on the path the search yields from each start, the shortest one,
    which depended on the order of the search, left a function on a longer path out, so that `inv init`
    froze it, then called its entry unused, and deleting it failed the rule; and cost one search per
    entrypoint, minutes on a large tree)."""
    out: set[str] = set()
    for ps in passes:
        out |= ps.on_any_path(starts, allowed)
    return out


def _can_reach(targets: set[str], edges: Iterable[_Call], blocked: set[str]) -> set[str]:
    """Every f with _reach([f], edges, blocked) & targets non-empty, in one backward pass: f is not blocked
    and a path of unblocked functions leads from f to a target."""
    return _Callers(edges).back(targets, blocked)
