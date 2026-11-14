"""Fix round 29: a real-codebase adoption review of `inv init` and the scan.

1. `inv init` worked out the functions on the door-free paths once per name it asked about: each allowed
   function re-ran the path search. They are worked out once; the search spreads a name-matched fan
   once per search and keeps no path it does not need; the source read for shas is kept for every scan
   of one graph; a long freeze prints progress lines. An allow entry is in use when it is on any
   door-free path (not only the shortest one from each entrypoint): `inv init` froze such a function,
   then called its entry unused, and deleting it failed the rule.
2. A site's sha hashes the syntax tree of its statement (a compound one's header), not its tokens: a
   formatter (quotes, parentheses, number case, line breaks) never changes it, a change of what the
   statement means does. An entry with a sha from before is told the new sha of the same site.
3. The result of calling a value class of the standard library (pathlib.Path(...), str literals) is typed:
   its methods never name-match a scanned method (`pathlib.Path(__file__).resolve()` is not
   Pipeline.resolve). A wrapper class outside the scan still is name-matched (it may delegate).
4. An import of a name that is not installed and that several modules below a root may be says first
   to run the scan where the dependency is installed.
5. The text report shows each finding's code, and says how to see an unresolved finding's sha.
"""
from __future__ import annotations

import random

import pytest
from cli_support import write_project

import invariants_scan as scan
from invariants import doors
from invariants_scan import accept, contracts, reachability
from invariants_scan.contracts import _Edge, _Fan

PAY = "def send_to_gateway(c, a):\n    pass\n\ndef refund(c, a):\n    send_to_gateway(c, a)\n"


def _rule(sink, *door, **med):
    return {"rules": [{"id": "R", "mediation": {"sink": sink, "door": list(door), **med}}]}


PAY_RULE = _rule("py:payments.send_to_gateway", "py:payments.refund")


def _scan(tmp_path, files, doc=PAY_RULE):
    return scan.scan(doc, [write_project(tmp_path, {"payments.py": PAY, **files})])


# --- 1. the on-path functions are worked out once, and on any path ------------------------------------------
LONG = ("import payments\ndef x():\n    payments.send_to_gateway(1, 2)\ndef z():\n    x()\ndef y():\n    z()\n"
        "def main():\n    x()\n    y()\n")


def test_an_allowed_function_on_a_longer_door_free_path_is_in_use(tmp_path):
    # main -> x is the shortest path to the sink; main -> y -> z -> x is a door-free path too
    allow = ["py:ops.main", "py:ops.x", "py:ops.y", "py:ops.z"]
    rep = _scan(tmp_path / "a", {"ops.py": LONG}, _rule("py:payments.send_to_gateway", "py:payments.refund",
                                                        allow=allow))
    assert rep.findings == [], rep.findings
    # and deleting y from allow is a bypass, so its entry was in use
    rep = _scan(tmp_path / "b", {"ops.py": LONG}, _rule("py:payments.send_to_gateway", "py:payments.refund",
                                                        allow=[a for a in allow if a != "py:ops.y"]))
    assert [f.code for f in rep.findings] == ["bypass"], rep.findings


def test_an_allowed_function_on_a_longer_name_matched_path_is_in_use(tmp_path):
    # fact-pipeline: cli.main -> rules_path -> (an untyped p).resolve() name-matches Pipeline.resolve, a
    # path as long as main -> Pipeline.process -> Pipeline.resolve, searched first: inv init froze rules_path,
    # then called it unused
    files = {"app/__init__.py": "", "app/pipeline.py": PIPE + "    def process(self, x):\n        self.resolve(x)\n",
             "app/cli.py": "from app.pipeline import Pipeline\ndef rules_path(p):\n    return p.resolve(1)\n"
                           "def main(p):\n    Pipeline().process(1)\n    rules_path(p)\n"}
    allow = ["py:app.cli.main", "py:app.cli.rules_path", "py:app.pipeline.Pipeline.resolve",
             "py:app.pipeline.Pipeline.process"]
    rep = scan.scan(_rule("py:app.pipeline.Store.add", "py:app.pipeline.Pipeline.intake", allow=allow),
                    [write_project(tmp_path, files)])
    assert not [f for f in rep.findings if f.code == "missing/allow-unused"], rep.findings


def test_the_on_path_search_runs_once_for_every_name_asked(monkeypatch):
    calls = []
    real = reachability._every_path_node
    monkeypatch.setattr(reachability, "_every_path_node", lambda *a, **k: calls.append(a) or real(*a, **k))
    succ = {"s": [_Edge("s", "a", 1, 1, False)], "a": [_Edge("a", "sink", 2, 1, False)]}
    ps = reachability._Pass({"sink"}, succ, True)
    on = contracts._OnPaths()
    on.defer([ps], ["s"], set())
    assert all(n in on for n in ("s", "a", "sink")) and "b" not in on and "c" not in on
    assert len(calls) == 1
    assert on.all() == {"s", "a", "sink"}


def _random_graph(rng):
    names = [f"f{i}" for i in range(rng.randint(3, 12))] + ["m.<module>"]
    goals = {"sink"}
    succ = {}
    fans = [tuple(rng.sample(names + ["sink"], rng.randint(1, 4))) for _ in range(3)]
    for n in names:
        es = []
        for _ in range(rng.randint(0, 4)):
            if rng.random() < 0.3:
                es.append(_Fan(n, rng.choice(fans), 1, 1))
            else:
                es.append(_Edge(n, rng.choice(names + ["sink"]), 1, 1, False))
        succ[n] = es
    return names, goals, succ


def _on_some_path(names, goals, succ, starts, allowed):
    """By brute force: every search state (function, left `allowed`) reached from a start that reaches a call
    into a goal made having left `allowed` (or from a module body), and the goals so called."""
    def nxt(st):
        n, left = st
        return [(c, left or c not in allowed) for e in succ.get(n, ())
                for c in (e.callees if isinstance(e, _Fan) else (e.callee,)) if c not in goals]

    def calls(st):
        n, left = st
        return [c for e in succ.get(n, ()) for c in (e.callees if isinstance(e, _Fan) else (e.callee,))
                if c in goals and (left or n.endswith(".<module>"))]
    states = [(n, b) for n in names for b in (False, True)]
    fwd = {(s, s not in allowed) for s in starts}
    while True:
        more = {t for st in fwd for t in nxt(st)} - fwd
        if not more:
            break
        fwd |= more
    good = {st for st in fwd if calls(st)}
    while True:
        more = {st for st in states if st in fwd and st not in good and any(t in good for t in nxt(st))}
        if not more:
            break
        good |= more
    return {n for n, _ in good} | {c for st in good for c in calls(st)}


@pytest.mark.parametrize("seed", range(300))
def test_the_on_path_functions_are_those_on_any_door_free_path(seed):
    rng = random.Random(seed)
    names, goals, succ = _random_graph(rng)
    ps = reachability._Pass(goals, succ, True)
    starts = rng.sample(names, rng.randint(1, len(names)))
    for allowed in (set(), set(rng.sample(names, rng.randint(1, len(names))))):
        got = ps.on_any_path(starts, allowed)
        assert got == _on_some_path(names, goals, succ, starts, allowed), (seed, allowed)
        # the path the search yields from each start is one of them
        assert {x for s in starts for p, _ in ps.paths(s, allowed) for x in p} <= got


def _paths_before(start, goals, succ, inferred_ok, allowed=frozenset()):
    """_paths as it was before fix round 29 (every fan spread from every state)."""
    import collections
    first = (start, start not in allowed)
    prev = {first: None}
    queue = collections.deque([first])
    while queue:
        st = queue.popleft()
        n, out = st
        for e in succ.get(n, ()):
            if e.inferred and not inferred_ok:
                continue
            for callee in (e.callees if type(e) is _Fan else (e.callee,)):
                if callee in goals:
                    if not out and not n.endswith(".<module>"):
                        continue
                    names = [callee]
                    cur = st
                    while cur is not None:
                        names.append(cur[0])
                        cur = prev[cur]
                    yield list(reversed(names)), (e if type(e) is _Edge else
                                                  _Edge(n, callee, e.line, e.col, True, e.via_getattr))
                    continue
                nxt = (callee, out or callee not in allowed)
                if nxt in prev:
                    continue
                prev[nxt] = st
                queue.append(nxt)


@pytest.mark.parametrize("seed", range(300))
def test_the_path_search_yields_what_it_did(seed):
    rng = random.Random(seed)
    names, goals, succ = _random_graph(rng)
    for allowed in (set(), set(rng.sample(names, rng.randint(1, len(names))))):
        for s in names:
            got = [(p, e.caller, e.callee, e.line) for p, e in reachability._paths(s, goals, succ, True, allowed)]
            want = [(p, e.caller, e.callee, e.line) for p, e in _paths_before(s, goals, succ, True, allowed)]
            assert got == want, (seed, s, allowed)


@pytest.mark.parametrize("seed", range(400))
def test_the_first_path_is_the_first_the_search_yields(seed):
    rng = random.Random(seed)
    names, goals, succ = _random_graph(rng)
    for ok in (False, True):
        ps = reachability._Pass(goals, succ, ok)
        for s in names:
            first = next(ps.paths(s), None)
            assert ps.first_path(s) == (first[0] if first else None), (seed, s)


def test_paths_spreads_a_fan_once_and_yields_the_same():
    big = tuple(f"m{i}" for i in range(50))
    succ = {"s": [_Edge("s", f"c{i}", i, 1, False) for i in range(30)],
            **{f"c{i}": [_Fan(f"c{i}", big, 1, 1)] for i in range(30)},
            **{m: [_Edge(m, "sink", 9, 1, False)] for m in big}}
    got = [(p, (e.caller, e.line)) for p, e in reachability._paths("s", {"sink"}, succ, True)]
    assert got == [(p, (e.caller, e.line)) for p, e in _paths_before("s", {"sink"}, succ, True)]
    assert len(got) == 50 and got[0][0] == ["s", "c0", "m0", "sink"]


def test_the_freeze_stops_when_nothing_new_is_frozen(tmp_path, monkeypatch):
    # a back door and an unresolved site: the rule never passes, so every round re-ran the same scan
    root = write_project(tmp_path, {
        "app/__init__.py": "", "app/repo.py": "class OrderRepo:\n    def save_order(self, o):\n        pass\n",
        "app/order_service.py": "from app.repo import OrderRepo\ndef place(o):\n    OrderRepo().save_order(o)\n"
                                "def place2(o):\n    OrderRepo().save_order(o)\n",
        "app/api.py": "from app.order_service import place, place2\nfrom app.repo import OrderRepo\n"
                      "def a(o):\n    place(o)\ndef b(o):\n    place2(o)\ndef c(o):\n    OrderRepo().save_order(o)\n",
        "app/plug.py": "import importlib\ndef load(n):\n    importlib.import_module(n)\n"})
    calls = []
    real = doors._check
    monkeypatch.setattr(doors, "_check", lambda *a: calls.append(1) or real(*a))
    found = doors.discover([root])
    d = found.doors[0]
    assert d.allow == ["app.api.c"] and len(d.unresolved) == 1 and d.back_doors, d
    assert len(calls) == 2                         # the first scan, then one with c frozen


def test_a_long_freeze_prints_progress(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scan._Progress, "AFTER", 0.0)
    monkeypatch.setattr(scan._Progress, "EVERY", 0.0)
    monkeypatch.setenv("INV_PROGRESS", "1")
    root = write_project(tmp_path, {
        "app/__init__.py": "", "app/repo.py": "class OrderRepo:\n    def save_order(self, o):\n        pass\n",
        "app/order_service.py": "from app.repo import OrderRepo\ndef place(o):\n    OrderRepo().save_order(o)\n"
                                "def place2(o):\n    OrderRepo().save_order(o)\n",
        "app/api.py": "from app.order_service import place, place2\nfrom app.repo import OrderRepo\n"
                      "def a(o):\n    place(o)\ndef b(o):\n    place2(o)\ndef c(o):\n    OrderRepo().save_order(o)\n"})
    doors.discover([root])
    err = capsys.readouterr().err
    assert "freezing the back doors of drafted rule ORDER-REPO-DOOR, rule 0/" in err and ", round 1/" in err, err


# --- 2. the sha is the statement's syntax tree --------------------------------------------------------------
SVC = "import payments\ndef load(obj, name):\n    {stmt}\n"


def _sha(tmp_path, stmt, owner_line=3):
    rep = _scan(tmp_path, {"svc.py": SVC.replace("{stmt}", stmt)},
                _rule("py:payments.send_to_gateway", "py:payments.refund", allow_unresolved=[]))
    got = [f for f in rep.findings if f.code == "unresolved/computed-attribute"]
    assert len(got) == 1, rep.findings
    return got[0].sha


@pytest.mark.parametrize("one, two", [
    ("return getattr(obj, name, 'x')", 'return getattr(obj, name, "x")'),                      # quotes
    ("return getattr(obj, name, f'{name!r}')", 'return getattr(obj, name, f"{name!r}")'),     # f-string quotes
    ("for (x) in getattr(obj, name):\n        pass", "for x in getattr(obj, name):\n        pass"),
    ("if (getattr(obj, name)):\n        pass", "if getattr(obj, name):\n        pass"),
    ("return getattr(obj, name) + 0XAB", "return getattr(obj, name) + 0xAB"),                  # number case
    ("return getattr(obj, name) + 1E5", "return getattr(obj, name) + 1e5"),
    ("return getattr(obj, name) + a + b", "return (\n        getattr(obj, name)\n        + a\n        + b\n    )"),
    ("return getattr(obj, name)  # why", "return getattr(obj,\n                   name)"),
    ("with getattr(obj, name)() as (h):\n        pass", "with getattr(obj, name)() as h:\n        pass"),
    ("x = u'a' + getattr(obj, name)", "x = 'a' + getattr(obj, name)"),
])
def test_a_reformat_keeps_the_sha(tmp_path, one, two):
    assert _sha(tmp_path / "a", one) == _sha(tmp_path / "b", two)


@pytest.mark.parametrize("one, two", [
    ("return getattr(obj, name, 'x')", "return getattr(obj, name, 'y')"),
    ("return getattr(obj, name) + 0xAB", "return getattr(obj, name) + 0xAC"),
    ("return getattr(obj, name) + 1", "return getattr(obj, name) + 1.0"),
    ("return getattr(obj, name) + 1", "return getattr(obj, name) + True"),
    ("x = (getattr(obj, name),)", "x = (getattr(obj, name))"),
    ("return getattr(obj, name) + a - b", "return getattr(obj, name) + (a - b)"),
    ("for x in getattr(obj, name):\n        pass", "for y in getattr(obj, name):\n        pass"),
])
def test_a_change_of_meaning_changes_the_sha(tmp_path, one, two):
    assert _sha(tmp_path / "a", one) != _sha(tmp_path / "b", two)


def test_a_compound_statement_is_hashed_by_its_header(tmp_path):
    a = _sha(tmp_path / "a", "for x in getattr(obj, name):\n        pass")
    assert a == _sha(tmp_path / "b", "for x in getattr(obj, name):\n        print(x)\n    else:\n        y = 1")


def test_the_tree_key_leaves_out_empty_fields_on_every_version():
    import ast
    key = accept._key(ast.parse("def f(): pass").body[0])
    assert key == "tree FunctionDef(name='f', args=arguments())", key
    assert accept._key("x = 1") == accept._key("x = (1)") != accept._key("x = 1.0")


def test_an_entry_with_a_sha_from_before_is_told_the_new_one(tmp_path):
    stmt = "return getattr(obj, name)"
    root = write_project(tmp_path, {"payments.py": PAY, "svc.py": SVC.replace("{stmt}", stmt)})
    new = scan.scan(_rule("py:payments.send_to_gateway", "py:payments.refund", allow_unresolved=[]),
                    [root]).findings[0]
    old = accept._text_sha(new.code, new.owner, stmt)
    assert old != new.sha
    entry = {"at": f"svc.py:{new.line}", "code": new.code, "sha": old, "why": "fields only"}
    rep = scan.scan(_rule("py:payments.send_to_gateway", "py:payments.refund", allow_unresolved=[entry]), [root])
    msgs = [f.message for f in rep.findings if f.code == "missing/allow-unresolved-unused"]
    assert len(msgs) == 1 and f"write sha {new.sha}" in msgs[0] and "earlier inv" in msgs[0], rep.findings
    entry["sha"] = new.sha
    rep = scan.scan(_rule("py:payments.send_to_gateway", "py:payments.refund", allow_unresolved=[entry]), [root])
    assert rep.findings == [] and len(rep.accepted) == 1


# --- 3. a value class of the standard library is typed --------------------------------------------------------
PIPE = ("class Store:\n    def add(self, x):\n        pass\n"
        "class Pipeline:\n    def __init__(self):\n        self.store = Store()\n"
        "    def intake(self, x):\n        self.store.add(x)\n"
        "    def resolve(self, x):\n        self.store.add(x)\n")
PIPE_RULE = _rule("py:app.pipeline.Store.add", "py:app.pipeline.Pipeline.intake",
                  allow=["py:app.pipeline.Pipeline.resolve"])


def _pipe(tmp_path, cli):
    root = write_project(tmp_path, {"app/__init__.py": "", "app/pipeline.py": PIPE, "app/cli.py": cli})
    return scan.scan(PIPE_RULE, [root])


@pytest.mark.parametrize("cli", [
    "import pathlib\ndef where():\n    return pathlib.Path(__file__).resolve()\n",
    "from pathlib import Path\ndef where():\n    p = Path(__file__)\n    return p.resolve()\n",
])
def test_a_path_method_never_name_matches_a_scanned_method(tmp_path, cli):
    rep = _pipe(tmp_path, cli)
    assert not [f for f in rep.findings if f.code == "unresolved/inferred-path"], rep.findings


@pytest.mark.parametrize("cli", [
    "import io\ndef where(f):\n    return io.TextIOWrapper(f).resolve()\n",           # a wrapper may delegate
    "import types\ndef where(f):\n    return types.SimpleNamespace(resolve=f).resolve()\n",
    "import pathlib\ndef where(f):\n    p = pathlib.Path(f)\n    p.resolve = f\n    return p.resolve()\n",
    "import datetime\ndef where(f):\n    return datetime.date(2020, 1, 1).resolve()\n",   # date has no resolve
])
def test_another_class_outside_the_scan_is_still_name_matched(tmp_path, cli):
    rep = _pipe(tmp_path, cli)
    assert [f for f in rep.findings if f.code == "unresolved/inferred-path"], rep.findings


@pytest.mark.parametrize("go, found", [
    ("', '.join(xs)", False), ("f'{xs}'.join(xs)", False), ("str(xs).join(xs)", False),
    ("xs.join(xs)", True),                            # an untyped receiver: still name-matched
])
def test_a_str_method_never_name_matches(tmp_path, go, found):
    root = write_project(tmp_path, {"ops.py": "class J:\n    def join(self, x):\n        pass\n"
                                              f"def door():\n    J().join(1)\ndef go(xs):\n    return {go}\n"})
    rep = scan.scan(_rule("py:ops.J.join", "py:ops.door"), [root])
    assert bool([f for f in rep.findings if f.owner == "ops.go"]) == found, rep.findings


# --- 4. an import that is not installed ------------------------------------------------------------------------
def test_an_uninstalled_import_says_to_scan_where_it_is_installed(tmp_path):
    rep = _scan(tmp_path, {"a/websocket_x/__init__.py": "", "b/websocket_x/__init__.py": "",
                           "tools/main.py": "import websocket_x\n"})
    msgs = [f.message for f in rep.findings if f.code == "unresolved/unknown-module"]
    assert msgs and msgs[0].startswith("imports websocket_x, which is not installed where the scan runs: if it "
                                       "is a dependency, run the scan where it is installed"), msgs


# --- 5. the text report shows each finding's code -------------------------------------------------------------
UNRES = {"plug.py": "import importlib\ndef load(n):\n    importlib.import_module(n)\n"}


def test_the_text_report_shows_the_code_and_how_to_see_the_sha(tmp_path):
    rep = _scan(tmp_path / "a", UNRES)
    text = scan.to_text(rep)
    line = next(x for x in text.splitlines() if x.startswith("unresolved "))
    assert "  unresolved/reflection: " in line and "[sha " not in line, text
    assert "give the rule `allow_unresolved: []`" in text
    rep = _scan(tmp_path / "b", UNRES, _rule("py:payments.send_to_gateway", "py:payments.refund",
                                             allow_unresolved=[]))
    text = scan.to_text(rep)
    assert "[sha " in text and "allow_unresolved: []" not in text, text
    rep = _scan(tmp_path / "c", {"ops.py": "import payments\ndef f():\n    payments.send_to_gateway(1, 2)\n"})
    line = next(x for x in scan.to_text(rep).splitlines() if x.startswith("bypass "))
    assert "bypass:" not in line                       # a bypass's code is its kind
