"""Fix round 25: the static scan on real codebases (jupyter_server, google-adk, sqlalchemy, torch, transformers).

P. Performance, with the same output: one search per rule (only from the first entrypoint that reaches each
   call into the sink; the on-path functions of every entrypoint worked out only when read), a name-matched
   call kept as one record (a fan) instead of one edge per method of that name, each module's tree numbered
   once (breadth-first order derived from it), per-function sets made once, and a progress line on stderr
   for a long scan.
"""
from __future__ import annotations

import ast
import collections
import random
import textwrap

import pytest
from cli_support import write_project

from invariants import scan
from invariants.scan import astutil, reachability
from invariants.scan.contracts import _Edge, _Fan

PAYMENTS = """
    def send_to_gateway(charge_id, amount):
        pass

    def refund(charge_id, amount):
        send_to_gateway(charge_id, amount)
"""
DOC = {"rules": [{"id": "PAY-1", "subject": {"target": "py:payments.refund"},
                  "mediation": {"sink": "py:payments.send_to_gateway"}}]}


def _project(tmp_path, files):
    return write_project(tmp_path, {"payments.py": textwrap.dedent(PAYMENTS),
                                    **{k: textwrap.dedent(v) for k, v in files.items()}})


def _codes(rep):
    return sorted(f.code for f in rep.findings)


# --- P. performance: the same results, faster ---------------------------------------------------------------
def _graph(rnd: random.Random, n: int):
    names = [f"f{i}" for i in range(n)]
    calls: list = []
    succ: dict = collections.defaultdict(list)
    fan_names = [tuple(sorted(rnd.sample(names, rnd.randint(1, min(4, n))))) for _ in range(3)]
    for a in names:
        for _ in range(rnd.randint(0, 3)):
            if rnd.random() < 0.2:
                c = _Fan(a, rnd.choice(fan_names), rnd.randint(1, 9), 0)
            else:
                c = _Edge(a, rnd.choice(names + ["sink", "sink2"]), rnd.randint(1, 9), rnd.randint(0, 2),
                          rnd.random() < 0.2)
            calls.append(c)
            succ[a].append(c)
    return names, calls, succ


def _old_search(starts, goals, succ, allowed):
    """What the search from every start found: each call site once (first start, shortest path), and every
    function on any path from any start."""
    on, out, reported = set(), [], set()
    for k, ok in enumerate((False, True)):
        for s in starts:
            for names, last in reachability._paths(s, goals, succ, ok, allowed):
                on.update(names)
                site = (last.caller, last.line, last.col)
                if site not in reported:
                    reported.add(site)
                    out.append((k, names, (last.caller, last.callee, last.line, last.col)))
    return out, on


@pytest.mark.parametrize("seed", range(60))
def test_one_search_per_rule_finds_what_a_search_from_every_entrypoint_finds(seed):
    rnd = random.Random(seed)
    names, calls, succ = _graph(rnd, rnd.randint(3, 25))
    goals = {"sink", "sink2"} if seed % 2 else {"sink"}
    allowed = set(rnd.sample(names, min(len(names), rnd.randint(0, 3))))
    starts = rnd.sample(names, len(names))
    passes = [reachability._Pass(goals, succ, ok) for ok in (False, True)]
    got = [(k, names_, (e.caller, e.callee, e.line, e.col))
           for k, names_, e in reachability._search_order(passes, starts, allowed)]
    want, on = _old_search(starts, goals, succ, allowed)
    assert got == want
    assert reachability._every_path_node(passes, starts, allowed) == on
    for f in names:                                    # one backward pass answers every _reach question
        assert (f in reachability._can_reach(goals, calls, set(allowed))) == \
            bool(reachability._reach([f], calls, set(allowed)) & goals)


def test_a_fan_is_one_inferred_edge_per_callee_in_order():
    fan = _Fan("a", ("m.A.save", "m.B.save"), 3, 4, via_getattr=True)
    assert [(e.caller, e.callee, e.line, e.col, e.inferred, e.via_getattr) for e in fan.edges()] == [
        ("a", "m.A.save", 3, 4, True, True), ("a", "m.B.save", 3, 4, True, True)]
    succ = {"a": [fan]}
    assert [n for n, _ in reachability._paths("a", {"m.B.save"}, succ, True)] == [["a", "m.B.save"]]
    assert list(reachability._paths("a", {"m.B.save"}, succ, False)) == []


def test_a_name_matched_call_is_one_record_in_the_graph_and_every_edge_when_read(tmp_path):
    root = _project(tmp_path, {"repo.py": """
        class A:
            def save(self):
                pass
        class B:
            def save(self):
                pass
        def use(x):
            x.save()
    """})
    g = scan.graph([root])
    fans = [c for c in g.calls if isinstance(c, _Fan)]
    assert [(f.caller, f.callees) for f in fans] == [("repo.use", ("repo.A.save", "repo.B.save"))]
    assert {(e.caller, e.callee, e.inferred) for e in g.edges if e.caller == "repo.use"} == {
        ("repo.use", "repo.A.save", True), ("repo.use", "repo.B.save", True)}
    assert len(g.edges) == sum(1 for _ in g.edges)


def test_breadth_first_order_comes_from_the_numbered_tree():
    src = textwrap.dedent("""
        import os
        class C:
            def m(self, a=1):
                return [x for x in a if (y := x)]
        def f(*a, **k):
            lambda q: q + 1
            global G
    """)
    tree = ast.parse(src)
    astutil._number(tree)
    assert astutil._walk(tree) == list(ast.walk(tree))
    fn = tree.body[1].body[0]
    assert set(astutil._under(fn)) == set(ast.walk(fn))
    assert set(astutil._own_under(fn)) == set(astutil._own_nodes(fn))


def test_inits_on_paths_are_worked_out_when_read(tmp_path):
    root = _project(tmp_path, {"ops.py": """
        from payments import send_to_gateway
        def a():
            b()
        def b():
            send_to_gateway(1, 2)
    """})
    rep = scan.scan(DOC, [root])
    assert dict.__getitem__(rep.on_paths, "PAY-1").__class__.__name__ == "_OnPaths"
    assert {"ops.a", "ops.b", "payments.send_to_gateway"} <= rep.on_paths["PAY-1"]
    assert rep.on_paths.get("nope", set()) == set()


def test_a_long_scan_says_how_far_it_got_on_stderr(tmp_path, capsys, monkeypatch):
    root = _project(tmp_path, {"ops.py": "from payments import refund\n"})
    monkeypatch.setattr(scan._Progress, "AFTER", 0.0)
    monkeypatch.setattr(scan._Progress, "EVERY", 0.0)
    rep = scan.scan(DOC, [root])
    err = capsys.readouterr().err
    assert "inv scan: reading files" in err and "inv scan: checking rule 0/1" in err
    assert rep.rule_result("PAY-1") == "pass"
    monkeypatch.setenv("INV_PROGRESS", "0")
    scan.scan(DOC, [root])
    assert capsys.readouterr().err == ""


def test_a_short_scan_says_nothing_on_stderr(tmp_path, capsys):
    root = _project(tmp_path, {"ops.py": "from payments import refund\n"})
    scan.scan(DOC, [root])
    assert capsys.readouterr().err == ""
