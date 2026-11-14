"""Fix round 25: the static scan on real codebases (jupyter_server, google-adk, sqlalchemy, torch, transformers).

P. Performance, with the same output: one search per rule (only from the first entrypoint that reaches each
   call into the sink; the on-path functions of every entrypoint worked out only when read), a name-matched
   call kept as one record (a fan) instead of one edge per method of that name, each module's tree numbered
   once (breadth-first order derived from it), per-function sets made once, and a progress line on stderr
   for a long scan.
1. Provably safe narrowing: a computed getattr on an instance (or the class) of a class whose whole family
   is seen and harmless, or on an object outside the scanned code, is not reported; reading only a
   namespace's names is not reflection. Reflection is never narrowed by which modules a module imports.
2. mediation.allow_unresolved (short form allow_unresolved): reviewed unresolved sites, accepted one by
   one by file, code and sha; shown as accepted (text, JSON, SARIF suppressions, status, MCP explain),
   never failing; moved sites follow, edited ones do not; unused entries are missing; bypass and
   forbidden findings can never be accepted; a new or widened entry is a lock weakening.
3. forbidden/unwrap only for a guarded door; another unwrap that bears on the rule is unresolved/unwrap;
   hasattr(x, "__wrapped__") and an object outside the scanned code are neither.
5. A write of a package's __path__ (pkgutil.extend_path) is reported; sys.path through a name is tracked.
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


# --- 1. provably safe narrowing -------------------------------------------------------------------------
DISPATCH = """
    class Handler:
        def get(self):
            return 1
        def post(self):
            return 2
        def dispatch(self, verb):
            return getattr(self, verb)()
"""


def _computed(rep):
    return [f for f in rep.findings if f.code == "unresolved/computed-attribute"]


def test_a_getattr_dispatch_on_a_class_that_cannot_reach_the_sink_is_not_reported(tmp_path):
    rep = scan.scan(DOC, [_project(tmp_path, {"web.py": DISPATCH})])
    assert _computed(rep) == [] and rep.rule_result("PAY-1") == "pass", rep.findings


@pytest.mark.parametrize("extra", [
    # a method of the class reaches the sink: the dispatch may call it
    "def delete(self):\n    from payments import send_to_gateway\n    send_to_gateway(1, 2)\n",
    # a method named like the sink
    "def send_to_gateway(self):\n    pass\n",
    # its own __getattr__ answers any name
    "def __getattr__(self, name):\n    return name\n",
    # an if block may bind any name
    "if True:\n    x = 1\n",
    # an instance attribute named like the door
    "def setup(self):\n    self.refund = None\n",
    # a decorator the scan cannot name
    "@registry[0]\ndef other(self):\n    pass\n",
])
def test_a_getattr_dispatch_stays_unresolved_when_the_family_may_reach_the_sink(tmp_path, extra):
    rep = scan.scan(DOC, [_project(tmp_path, {"web.py": DISPATCH + textwrap.indent(extra, "        ")})])
    assert [f.line for f in _computed(rep)] == [8], rep.findings


SUBCLASS = """
    class Admin(Handler):
        def wipe(self):
            from payments import send_to_gateway
            send_to_gateway(1, 2)
"""


def test_a_subclass_whose_method_reaches_the_sink_keeps_the_dispatch_unresolved(tmp_path):
    rep = scan.scan(DOC, [_project(tmp_path, {"web.py": DISPATCH + SUBCLASS})])
    assert [f.line for f in _computed(rep)] == [8], rep.findings


def test_a_receiver_built_as_exactly_its_class_has_no_subclass(tmp_path):
    rep = scan.scan(DOC, [_project(tmp_path, {"web.py": DISPATCH.replace("getattr(self, verb)", "getattr(self, 'get')")
                                              + SUBCLASS + "    def run(verb):\n        return getattr(Handler(), verb)()\n"})])
    assert _computed(rep) == [], rep.findings


@pytest.mark.parametrize("base", ["make_base()", "Unknown", "excluded_mod.Base"])
def test_a_base_the_scan_cannot_place_keeps_the_dispatch_unresolved(tmp_path, base):
    root = _project(tmp_path, {"web.py": "from pkg import excluded_mod\n" + textwrap.dedent(DISPATCH).replace(
        "class Handler:", f"class Handler({base}):"), "pkg/__init__.py": "",
        "pkg/excluded_mod.py": "class Base:\n    pass\n"})
    rep = scan.scan(DOC, [root], exclude=["pkg/excluded_mod.py"])
    assert any(f.code == "unresolved/computed-attribute" for f in rep.findings), rep.findings


def test_a_third_party_base_is_foreign_code(tmp_path):
    rep = scan.scan(DOC, [_project(tmp_path, {"web.py": "import tornado.web\n" + textwrap.dedent(DISPATCH).replace(
        "class Handler:", "class Handler(tornado.web.RequestHandler):")})])
    assert _computed(rep) == [], rep.findings


def test_a_value_taken_inside_the_door_keeps_every_dispatch_unresolved(tmp_path):
    root = _project(tmp_path, {"web.py": DISPATCH})
    (root / "payments.py").write_text(textwrap.dedent(PAYMENTS).replace(
        "    send_to_gateway(charge_id, amount)", "    holder['cb'] = send_to_gateway") + "holder = {}\n")
    rep = scan.scan(DOC, [root])
    assert [f.line for f in _computed(rep)] == [8], rep.findings


@pytest.mark.parametrize("src, reported", [
    ("import logging\ndef level(name):\n    return getattr(logging, name)\n", False),      # the standard library
    ("import requests\ndef f(name):\n    return getattr(requests, name)\n", False),     # a third-party package
    ("import payments\ndef f(name):\n    return getattr(payments, name)(1, 2)\n", True),  # a scanned module
    ("def f(obj, name):\n    return getattr(obj, name)()\n", True),                     # an unknown receiver
])
def test_a_computed_getattr_on_an_object_outside_the_scan_is_not_reported(tmp_path, src, reported):
    rep = scan.scan(DOC, [_project(tmp_path, {"ops.py": src})])
    assert bool(_computed(rep)) is reported, rep.findings


def test_reflection_is_not_narrowed_by_what_a_module_imports(tmp_path):
    # util.py imports nothing, yet call(payments, "send_to_gateway") calls the sink through the value it is given
    rep = scan.scan(DOC, [_project(tmp_path, {
        "util.py": "def call(obj, name, *a):\n    return getattr(obj, name)(*a)\n",
        "ops.py": "import payments, util\ndef f():\n    util.call(payments, 'send_to_gateway', 1, 2)\n"})])
    assert [(f.file, f.code) for f in _computed(rep)] == [("util.py", "unresolved/computed-attribute")]


def test_a_computed_setattr_is_still_reported(tmp_path):
    rep = scan.scan(DOC, [_project(tmp_path, {"web.py": DISPATCH.replace("getattr(self, verb)()", "1")
                                              + "        def put(self, k, v):\n            setattr(self, k, v)\n"})])
    assert [f.message.split(" ")[0] for f in _computed(rep)] == ["setattr"], rep.findings


@pytest.mark.parametrize("src, reported", [
    ("def f():\n    return list(globals().keys())\n", False),
    ("def f(n):\n    return n in globals()\n", False),
    ("import payments\ndef f():\n    return sorted(vars(payments).keys())\n", False),
    ("def f(n):\n    return globals()[n]\n", True),
    ("import payments\ndef f():\n    return payments.__dict__.items()\n", True),
])
def test_reading_only_the_names_of_a_namespace_is_not_reflection(tmp_path, src, reported):
    rep = scan.scan(DOC, [_project(tmp_path, {"ops.py": src})])
    assert any(f.code == "unresolved/reflection" for f in rep.findings) is reported, rep.findings


# --- 2. mediation.allow_unresolved ---------------------------------------------------------------------
PLUGINS = """
    import importlib

    def load(name):
        return importlib.import_module(name)
"""


def _doc(entries=None, **med):
    m = {"sink": "py:payments.send_to_gateway", "door": "py:payments.refund", **med}
    if entries is not None:
        m["allow_unresolved"] = entries
    return {"rules": [{"id": "PAY-1", "mediation": m}]}


def _entry(rep, why="plugins never import payments"):
    f = next(f for f in rep.findings if f.kind == "unresolved")
    return {"at": f"{f.file}:{f.line}", "code": f.code, "sha": f.sha, "why": why}


def test_a_reviewed_unresolved_site_is_accepted_and_shown(tmp_path):
    root = _project(tmp_path, {"plugins.py": PLUGINS})
    rep = scan.scan(_doc(), [root])
    assert rep.rule_result("PAY-1") == "unresolved" and rep.findings[0].sha and len(rep.findings[0].sha) == 10
    assert "[sha" not in scan.to_text(rep)                 # shown only where the rule opts in
    assert f"[sha {rep.findings[0].sha}]" in scan.to_text(scan.scan(_doc([]), [root]))
    e = _entry(rep)
    rep = scan.scan(_doc([e]), [root])
    assert rep.findings == [] and rep.rule_result("PAY-1") == "pass"
    assert [(f.file, f.line, f.why) for f in rep.accepted] == [("plugins.py", 5, "plugins never import payments")]
    assert rep.counts("PAY-1") == {"accepted": 1}
    text = scan.to_text(rep)
    assert "accepted   PAY-1    plugins.py:5:" in text and "(allow_unresolved: plugins never import payments)" in text
    assert "PAY-1: pass (1 accepted)" in text
    data = rep.as_dict()
    assert data["accepted"][0]["why"] == e["why"] and data["rules"]["PAY-1"]["allow_unresolved"][0]["sha"] == e["sha"]
    sarif = scan.to_sarif(rep)["runs"][0]["results"]
    assert sarif[0]["suppressions"] == [{"kind": "external", "status": "accepted", "justification": e["why"]}]


def test_an_entry_follows_its_site_when_code_above_moves_it(tmp_path):
    root = _project(tmp_path, {"plugins.py": PLUGINS})
    e = _entry(scan.scan(_doc(), [root]))
    (root / "plugins.py").write_text("# a comment\n\n" + (root / "plugins.py").read_text())
    rep = scan.scan(_doc([e]), [root])
    assert rep.findings == [] and [(f.line, f.moved_from) for f in rep.accepted] == [(7, 5)]
    assert "moved from line 5: update its at to plugins.py:7" in scan.to_text(rep)


def test_an_edit_of_the_site_itself_fails_again_and_the_entry_is_unused(tmp_path):
    root = _project(tmp_path, {"plugins.py": PLUGINS})
    e = _entry(scan.scan(_doc(), [root]))
    (root / "plugins.py").write_text((root / "plugins.py").read_text().replace("import_module(name)",
                                                                                "import_module('x.' + name)"))
    rep = scan.scan(_doc([e]), [root])
    assert rep.accepted == [] and _codes(rep) == ["missing/allow-unresolved-unused", "unresolved/reflection"]
    assert rep.rule_result("PAY-1") == "fail"


def test_one_entry_accepts_one_site(tmp_path):
    root = _project(tmp_path, {"plugins.py": PLUGINS + "\n    def load2(name):\n        return importlib.import_module(name)\n"})
    rep = scan.scan(_doc(), [root])
    assert len(rep.findings) == 2
    rep = scan.scan(_doc([_entry(rep)]), [root])
    assert len(rep.accepted) == 1 and _codes(rep) == ["unresolved/reflection"]


def test_an_entry_that_matches_nothing_is_missing(tmp_path):
    root = _project(tmp_path, {"plugins.py": PLUGINS})
    e = {**_entry(scan.scan(_doc(), [root])), "at": "plugins.py:5:99"}
    rep = scan.scan(_doc([e]), [root])
    # a column that is not the finding's: the nearest finding with the same sha still matches
    assert rep.findings == [] and len(rep.accepted) == 1
    e = {**e, "sha": "0" * 10}
    rep = scan.scan(_doc([e]), [root])
    assert _codes(rep) == ["missing/allow-unresolved-unused", "unresolved/reflection"]


@pytest.mark.parametrize("entry, why", [
    ({"at": "ops.py:3", "code": "bypass", "sha": "0" * 10, "why": "x"}, "only an unresolved finding"),
    ({"at": "ops.py:3", "code": "forbidden/getattr-sink", "sha": "0" * 10, "why": "x"}, "only an unresolved finding"),
    ({"at": "ops.py:3", "code": "unresolved/reflection", "why": "x"}, "missing sha"),
    ({"at": "ops.py", "code": "unresolved/reflection", "sha": "0" * 10, "why": "x"}, "path:line"),
    ({"at": "ops.py:3", "code": "unresolved/reflection", "sha": "xyz", "why": "x"}, "10 hex digits"),
    ({"at": "ops.py:3", "code": "unresolved/reflection", "sha": "0" * 10, "why": " "}, "why must say"),
    ({"at": "ops.py:3", "code": "unresolved/reflection", "sha": "0" * 10, "why": "x", "glob": "*"}, "unknown field"),
])
def test_a_bypass_or_forbidden_finding_can_never_be_accepted(tmp_path, entry, why):
    with pytest.raises(scan.ScanConfigError, match=why):
        scan.scan(_doc([entry]), [_project(tmp_path, {})])


def test_the_short_form_and_the_loader_accept_the_field():
    from invariants.loader import expand_short_forms
    entry = {"at": "plugins.py:5", "code": "unresolved/reflection", "sha": "0" * 10, "why": "x"}
    doc = expand_short_forms({"rules": [{"id": "D", "door": "py:payments.refund", "sink": "py:payments.send_to_gateway",
                                         "allow_unresolved": [entry]}]})
    assert doc["rules"][0]["mediation"]["allow_unresolved"] == [entry]
    assert scan.mediations(doc)[0]["D"].accept[0].sha == "0" * 10


def test_status_names_accepted_sites_and_withholds_held(tmp_path):
    from invariants import status_report
    root = _project(tmp_path, {"plugins.py": PLUGINS})
    rep = scan.scan(_doc([_entry(scan.scan(_doc(), [root]))]), [root])
    assert "1 unresolved site(s) accepted after review" in status_report._scan_reason(rep, "PAY-1")


def test_the_lock_reports_a_new_or_widened_entry_as_a_weakening():
    from invariants.lock.rule_diff import _accept_changes
    e = {"at": "a.py:5:3", "code": "unresolved/reflection", "sha": "0123456789", "why": "x"}

    def weak(old, new):
        return [c.weakened for c in _accept_changes("rule R", old, new)]
    assert weak(None, [e]) == [True]                                  # added
    assert weak([e], None) == [False]                                 # removed: a tightening
    assert weak([e], [{**e, "at": "a.py:9:3", "why": "y"}]) == [False]   # moved, reworded: the same site
    assert weak([e], [{**e, "at": "a.py:5"}]) == [True]               # drops its column: matches more
    assert weak([{**e, "at": "a.py:5"}], [e]) == [False]              # names a column: matches less
    assert weak([e], [{**e, "sha": "9876543210"}]) == [True, False]   # another site
    assert weak([e], ["junk"]) == [True]                              # malformed: a possible weakening


RULES = ("spec: invariant/v0.1\nrules:\n  - id: PAY-DOOR\n    door: py:payments.refund\n"
         "    sink: py:payments.send_to_gateway\n")


def test_inv_lock_refuses_an_added_entry_without_accept_weakening(tmp_path):
    import json
    from cli_support import run_inv
    root = _project(tmp_path, {"plugins.py": PLUGINS, "invariants.yaml": RULES,
                               "pyproject.toml": '[project]\nname = "x"\nversion = "0"\n'})
    assert run_inv("lock", cwd=root).returncode == 0
    r = run_inv("scan", "--format", "json", cwd=root)
    f = next(x for x in json.loads(r.stdout)["findings"] if x["kind"] == "unresolved")
    (root / "invariants.yaml").write_text(RULES + f'    allow_unresolved:\n      - {{at: "{f["file"]}:{f["line"]}", '
                                          f'code: {f["code"]}, sha: "{f["sha"]}", why: "plugins only"}}\n')
    r = run_inv("validate", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    r = run_inv("lock", cwd=root)
    assert r.returncode == 1 and "WEAKENED" in r.stdout and "allow_unresolved" in r.stdout, r.stdout + r.stderr
    assert run_inv("lock", "--accept-weakening", cwd=root).returncode == 0
    r = run_inv("scan", cwd=root)
    assert r.returncode == 0 and "PAY-DOOR: pass (1 accepted)" in r.stdout, r.stdout
    r = run_inv("check", cwd=root)
    assert r.returncode == 0 and "ok    scan" in r.stdout, r.stdout + r.stderr
    assert "1 reviewed unresolved site(s) accepted by allow_unresolved" in r.stdout, r.stdout
    assert "        accepted   PAY-DOOR" in r.stdout and "plugins only" in r.stdout, r.stdout


def test_mcp_explain_shows_accepted_sites_and_the_entry_to_write(tmp_path):
    from invariants.mcp import tools
    root = _project(tmp_path, {"plugins.py": PLUGINS, "invariants.yaml": RULES})
    out = tools.tool_explain(root, root / "invariants.yaml", {"id": "PAY-DOOR"})
    assert any('mediation.allow_unresolved ({at: "plugins.py:5:' in s for s in out["fix_options"]), out
    f = out["findings"][0]
    (root / "invariants.yaml").write_text(RULES + (
        f'    allow_unresolved:\n      - {{at: "{f["file"]}:{f["line"]}", code: {f["code"]}, sha: "{f["sha"]}", '
        f'why: "plugins only"}}\n'))
    out = tools.tool_explain(root, root / "invariants.yaml", {"id": "PAY-DOOR"})
    assert out["scan_result"] == "pass" and out["accepted"][0]["why"] == "plugins only"
    assert "accepted after review" in out["explanation"]
    one = tools.tool_explain(root, root / "invariants.yaml", {"id": out["accepted"][0]["id"]})
    assert "A person accepted this site after review" in one["explanation"]


# --- 3. unwrapping -------------------------------------------------------------------------------------
GUARDED = """
    import functools
    import invariants as inv
    from payments import send_to_gateway

    @inv.guard("G-1")
    def checked(c, a):
        send_to_gateway(c, a)
"""


def _unwraps(rep):
    return sorted((f.code, f.message) for f in rep.findings if f.code.endswith("/unwrap"))


@pytest.mark.parametrize("src", [
    "def f(m):\n    return hasattr(m, '__wrapped__')\n",                       # it only asks
    "import functools\ndef f():\n    return getattr(functools.reduce, '__wrapped__', None)\n",  # outside the scan
])
def test_an_unwrap_that_lifts_nothing_from_the_scanned_code_is_not_reported(tmp_path, src):
    rep = scan.scan(_doc(), [_project(tmp_path, {"ops.py": src})])
    assert _unwraps(rep) == [], rep.findings


def test_an_unknown_unwrap_is_unresolved_when_no_guarded_door_has_a_decorator(tmp_path):
    rep = scan.scan(_doc(), [_project(tmp_path, {"ops.py": "def f(fn):\n    return fn.__closure__\n"})])
    assert [c for c, _ in _unwraps(rep)] == ["unresolved/unwrap"]
    assert "calls a guarded function" not in _unwraps(rep)[0][1]


def test_an_unknown_unwrap_is_forbidden_when_a_guarded_function_reaches_the_sink(tmp_path):
    rep = scan.scan(_doc(), [_project(tmp_path, {"guarded.py": GUARDED,
                                                 "ops.py": "def f(fn):\n    return fn.__wrapped__(1, 2)\n"})])
    assert [(c, m.split(" without")[0]) for c, m in _unwraps(rep)] == [
        ("forbidden/unwrap", "fn.__wrapped__ may call a guarded function (guarded.checked)")]


def test_a_named_unwrap_of_a_guarded_function_is_forbidden_and_of_a_helper_unresolved(tmp_path):
    rep = scan.scan(_doc(), [_project(tmp_path, {"guarded.py": GUARDED + """
    def helper(c, a):
        send_to_gateway(c, a)
    def f():
        checked.__wrapped__(1, 2)
        helper.__wrapped__(1, 2)
    """})])
    assert _unwraps(rep) == [
        ("forbidden/unwrap", "checked.__wrapped__ calls the guarded function guarded.checked without its guard"),
        ("unresolved/unwrap", "helper.__wrapped__ reaches the function guarded.helper wraps, which can reach the "
                              "sink without a door: the scan cannot follow that call")]


def test_setattr_of_wrapped_on_a_door_is_a_monkeypatch(tmp_path):
    rep = scan.scan(_doc(), [_project(tmp_path, {"ops.py": "import payments\ndef f(g):\n"
                                                           "    setattr(payments.refund, '__wrapped__', g)\n"})])
    assert "forbidden/monkeypatch" in _codes(rep), rep.findings


# --- 5. where Python finds modules -------------------------------------------------------------------------
@pytest.mark.parametrize("files", [
    {"pkg/__init__.py": "__path__.append('/elsewhere')\n"},
    {"pkg/__init__.py": "from pkgutil import extend_path\n__path__ = extend_path(__path__, __name__)\n"},
    {"pkg/__init__.py": "import pkgutil\n__path__ = pkgutil.extend_path(__path__, __name__)\n"},
    {"pkg/__init__.py": "", "ops.py": "import pkg\npkg.__path__ += ['/elsewhere']\n"},
    {"pkg/__init__.py": "p = __path__\np.insert(0, '/elsewhere')\n"},
])
def test_a_write_of_a_package_path_is_reported(tmp_path, files):
    rep = scan.scan(_doc(), [_project(tmp_path, files)])
    assert [f.code for f in rep.findings] == ["unresolved/reflection"], rep.findings
    assert "__path__" in rep.findings[0].message and "any directory" in rep.findings[0].message


@pytest.mark.parametrize("src, reported", [
    ("import sys\np = sys.path\np.insert(0, '/x')\n", True),
    ("import sys\np = sys.path\nq = p\nq.append('/x')\n", True),
    ("from sys import path\nalias = path\nalias.extend(['/x'])\n", True),
    ("import sys\np = sys.path\nprint(p.index('/x'))\n", False),             # a read
    ("import sys\np = list(sys.path)\np.insert(0, '/x')\n", False),          # a copy
])
def test_sys_path_through_a_name_is_tracked(tmp_path, src, reported):
    rep = scan.scan(_doc(), [_project(tmp_path, {"ops.py": src})])
    assert any("changes where Python finds modules" in f.message for f in rep.findings) is reported, rep.findings


def test_a_skipped_file_that_changes_sys_path_through_a_name_is_not_third_party_quiet(tmp_path):
    root = _project(tmp_path, {"venv/lib/python3.12/site-packages/tool.py":
                               "import sys\np = sys.path\np.insert(0, '.')\nimport payments\n",
                               "venv/pyvenv.cfg": "home = /usr\n"})
    rep = scan.scan(_doc(), [root])
    assert [f.code for f in rep.findings] == ["unresolved/skipped-importer"], rep.findings


def test_a_name_that_may_be_a_file_under_the_roots_is_not_foreign(tmp_path):
    # with the root ".", cli/fleet/store.py is cli.fleet.store; run with cli on sys.path it is fleet.store
    rep = scan.scan(DOC, [_project(tmp_path, {"cli/fleet/__init__.py": "", "cli/fleet/store.py": "def put():\n    pass\n",
                                              "ops.py": "import fleet.store\ndef f(n):\n    return getattr(fleet.store, n)()\n"})])
    assert [f.file for f in _computed(rep)] == ["ops.py"], rep.findings


# --- a nested scope's bindings never type the enclosing function's names -----------------------------------
NESTED_DOC = {"rules": [{"id": "REPO-1", "subject": {"target": "py:repo.Repo.door"},
                         "mediation": {"sink": "py:repo.Repo.save"}}]}
NESTED_REPO = """
    class Repo:
        def save(self, x):
            pass

        def door(self, x):
            self.save(x)

    class Other:
        def save(self, x):
            pass
"""


def test_a_nested_def_binding_the_same_name_does_not_retype_the_outer_local(tmp_path):
    root = write_project(tmp_path, {"repo.py": textwrap.dedent(NESTED_REPO), "app.py": textwrap.dedent("""
        from repo import Other, Repo

        def handler(x):
            r = Other()
            def helper():
                r = Repo()
                return r
            r.save(x)
    """)})
    rep = scan.scan(NESTED_DOC, [root])
    assert rep.rule_result("REPO-1") == "pass", _codes(rep)


def test_the_outer_binding_alone_still_types_the_call(tmp_path):
    root = write_project(tmp_path, {"repo.py": textwrap.dedent(NESTED_REPO), "app.py": textwrap.dedent("""
        from repo import Repo

        def handler(x):
            r = Repo()
            def helper():
                r = 1
                return r
            r.save(x)
    """)})
    rep = scan.scan(NESTED_DOC, [root])
    assert "bypass" in {f.kind for f in rep.findings}, _codes(rep)


# --- a namespace package's portion installed outside the roots is third-party ------------------------------
@pytest.mark.parametrize("ops", ["import nspkg.lib\n", "import nspkg.lib.types\n", "from nspkg import lib\n",
                                 "from nspkg.lib import types\n", "import nspkg.ext\n"])
def test_an_installed_portion_of_a_namespace_package_is_third_party(tmp_path, monkeypatch, ops):
    site = tmp_path / "site" / "site-packages"
    (site / "nspkg" / "lib").mkdir(parents=True)
    (site / "nspkg" / "lib" / "__init__.py").write_text("")
    (site / "nspkg" / "lib" / "types.py").write_text("")
    (site / "nspkg" / "ext.py").write_text("")
    monkeypatch.syspath_prepend(str(site))
    root = _project(tmp_path / "app", {"nspkg/mine.py": "", "ops.py": ops})
    assert scan.scan(DOC, [root]).findings == []


@pytest.mark.parametrize("ops", ["import nspkg.gone\n", "from nspkg import gone\n"])
def test_a_namespace_portion_installed_nowhere_or_under_a_root_stays_unknown(tmp_path, monkeypatch, ops):
    root = _project(tmp_path / "app", {"nspkg/mine.py": "", "ops.py": ops, "elsewhere/nspkg/gone.py": ""})
    monkeypatch.syspath_prepend(str(root / "elsewhere"))      # a file under the root the scan reads as elsewhere.*
    rep = scan.scan(DOC, [root])
    assert _codes(rep) == ["unresolved/unknown-module"], rep.findings


def test_a_regular_package_never_gets_its_missing_submodule_from_site_packages(tmp_path, monkeypatch):
    site = tmp_path / "site" / "site-packages"
    (site / "pkg").mkdir(parents=True)
    (site / "pkg" / "admin.py").write_text("")
    monkeypatch.syspath_prepend(str(site))
    root = _project(tmp_path / "app", {"pkg/__init__.py": "", "ops.py": "import pkg.admin\n"})
    assert _codes(scan.scan(DOC, [root])) == ["unresolved/unknown-module"]
