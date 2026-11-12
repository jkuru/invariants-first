"""`inv init` with a Graphify graph: communities and resolved calls propose doors the naming heuristics
miss; the scan still checks every one, and `inv check` never reads the graph."""
from __future__ import annotations

import json
import pathlib

import pytest

from invariants import doors, graphify, scan
from invariants.loader import expand_short_forms
from cli_support import run_inv, write_project

# Nothing here is named Service, Repo or Store, and the middle layer has one caller each: the
# heuristics alone find a writer but no door.
APP = {
    "pyproject.toml": '[project]\nname = "books"\nversion = "0"\n',
    "books/__init__.py": "",
    "books/ledger.py": "def post_entry(session, entry):\n    session.add(entry)\n",
    "books/flows.py": ("from books.ledger import post_entry\n\n"
                       "def settle(session, entry):\n"
                       "    if entry.amount <= 0:\n"
                       "        raise ValueError('positive only')\n"
                       "    post_entry(session, entry)\n"),
    "books/routes.py": ("from books.flows import settle\n\n"
                        "def handle_settle(session, entry):\n    settle(session, entry)\n"),
    "books/admin.py": ("from books.ledger import post_entry\n\n"
                       "def force_post(session, entry):\n    post_entry(session, entry)\n"),
}
SPOTS = {  # node id (opaque, as Graphify's own scheme is not interpreted) -> (file, line, community)
    "n-ledger": ("books/ledger.py", "L1", 1),
    "n-settle": ("books/flows.py", "L3", 2),
    "n-route": ("books/routes.py", "L3", 3),
    "n-admin": ("books/admin.py", "L3", 3),
}
CALLS = [("n-settle", "n-ledger"), ("n-route", "n-settle"), ("n-admin", "n-ledger")]


def _graph(spots=SPOTS, calls=CALLS, confidence="EXTRACTED", **extra):
    return {"directed": False, "multigraph": False, "graph": {"schema_version": 1},
            "nodes": [{"id": i, "label": i, "source_file": f, "source_location": loc, "community": c}
                      for i, (f, loc, c) in spots.items()],
            "links": [{"source": a, "target": b, "relation": "calls", "confidence": confidence,
                       "confidence_score": 1.0} for a, b in calls],
            "hyperedges": [], **extra}


def _app(tmp_path: pathlib.Path, graph: dict | None = None, files: dict | None = None) -> pathlib.Path:
    root = write_project(tmp_path / "app", {**APP, **(files or {})})
    if graph is not None:
        (root / "graphify-out").mkdir()
        (root / "graphify-out" / "graph.json").write_text(json.dumps(graph))
    return root


def test_without_the_graph_there_is_no_door(tmp_path):
    root = _app(tmp_path)
    found = doors.discover([root])
    assert found.doors == [] and "books.ledger" in found.no_door


def test_the_graph_proposes_the_function_between_two_layers(tmp_path):
    root = _app(tmp_path, _graph())
    found = doors.discover([root], graph=root / "graphify-out" / "graph.json", project=root)
    (d,) = found.doors
    assert d.doors == ["books.flows.settle"] and d.sinks == ["books.ledger.post_entry"]
    assert d.allow == ["books.admin.force_post"] and d.active             # the admin back door, frozen
    assert found.graph and "4 node(s), 4 matched" in found.graph
    rep = scan.scan(expand_short_forms({"rules": [d.rule()]}), [root])
    assert rep.rule_result(d.rid) == "pass"                                # checked by the scan, not the graph


def test_a_caller_in_the_writes_own_community_is_not_a_door(tmp_path):
    root = _app(tmp_path)
    same = dict(SPOTS, **{"n-settle": ("books/flows.py", "L3", 1)})       # flows clustered with the ledger
    p = root / "g.json"
    p.write_text(json.dumps(_graph(spots=same)))
    assert doors.discover([root], graph=p, project=root).doors == []


def test_a_call_only_the_graph_resolved_counts_as_a_caller(tmp_path):
    files = {"books/flows.py": APP["books/flows.py"] + "\nTOOLS = {'settle': settle}\n",
             "books/routes.py": ("from books.flows import TOOLS\n\n"
                                 "def dispatch(session, name, entry):\n    TOOLS[name](session, entry)\n")}
    root = _app(tmp_path, files=files)
    calls = [("n-settle", "n-ledger"), ("n-route", "n-settle")]
    graph_path = root / "g.json"
    graph_path.write_text(json.dumps(_graph(calls=[calls[0]]) | {"links": []}))
    assert doors.discover([root], graph=graph_path, project=root).doors == []   # settle has no known caller
    graph_path.write_text(json.dumps(_graph(calls=calls, confidence="EXTRACTED")))
    (d,) = doors.discover([root], graph=graph_path, project=root).doors
    assert d.doors == ["books.flows.settle"]
    for guess in ("INFERRED", "AMBIGUOUS"):                       # only deterministic edges count
        graph_path.write_text(json.dumps(_graph(calls=calls, confidence=guess)))
        assert doors.discover([root], graph=graph_path, project=root).doors == []


def test_nodes_match_by_file_and_line_not_by_id(tmp_path):
    root = _app(tmp_path)
    g = scan.graph([root])
    spots = {"x" * 40: (str((root / "books/flows.py").resolve()), "L4", 2),   # absolute path, a line inside
             "y": ("./books/ledger.py", 1, 1),                                 # relative, a bare int line
             "z": ("elsewhere/ledger.py", "L1", 9),                            # another file
             "w": ("books/flows.py", "not-a-line", 2)}
    p = tmp_path / "g.json"
    p.write_text(json.dumps(_graph(spots=spots, calls=[])))
    h = graphify.hints(p, g, [root])
    assert h.community == {"books.flows.settle": 2, "books.ledger.post_entry": 1} and h.matched == 2


def test_an_ambiguous_file_tail_matches_nothing(tmp_path):
    root = write_project(tmp_path / "two", {"a/__init__.py": "", "a/m.py": "def f():\n    pass\n",
                                           "b/__init__.py": "", "b/m.py": "def f():\n    pass\n"})
    p = tmp_path / "g.json"
    p.write_text(json.dumps(_graph(spots={"n": ("m.py", "L1", 1)}, calls=[])))
    assert graphify.hints(p, scan.graph([root]), [tmp_path / "nowhere"]).matched == 0


@pytest.mark.parametrize("text,msg", [
    ("not json", "not JSON"),
    ("[]", "no nodes list"),
    ('{"nodes": [], "links": 3}', "links must be a list"),
    ('{"nodes": [], "links": [], "graph": {"schema_version": 2}}', "schema_version 2"),
])
def test_a_malformed_graph_is_refused(tmp_path, text, msg):
    p = tmp_path / "g.json"
    p.write_text(text)
    with pytest.raises(graphify.GraphifyError, match=msg):
        graphify.read(p)


def test_a_missing_graph_is_refused(tmp_path):
    with pytest.raises(graphify.GraphifyError):
        graphify.read(tmp_path / "nope.json")


def test_init_reads_graphify_out_when_it_exists_and_check_does_not(tmp_path):
    root = _app(tmp_path, _graph())
    r = run_inv("init", "--no-lock", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "read the Graphify graph graphify-out/graph.json" in r.stdout and "through books.flows.settle" in r.stdout
    text = (root / "invariants.yaml").read_text()
    assert 'door: "py:books.flows.settle"' in text and '"py:books.admin.force_post"' in text
    assert run_inv("lock", cwd=root).returncode == 0
    assert run_inv("check", cwd=root).returncode == 0
    # the graph cannot vouch for a path: a new direct call fails whatever graph.json says
    (root / "graphify-out" / "graph.json").write_text("garbage")
    (root / "books/promo.py").write_text("from books.ledger import post_entry\n\n"
                                         "def bonus(session, e):\n    post_entry(session, e)\n")
    r = run_inv("check", cwd=root)
    assert r.returncode == 1 and "books.promo.bonus -> books.ledger.post_entry" in r.stdout


def test_init_graph_options(tmp_path):
    root = _app(tmp_path, _graph())
    r = run_inv("init", "--no-lock", "--no-graphify", cwd=root)
    assert r.returncode == 0 and "Graphify" not in r.stdout
    assert 'door: "py:books.flows.settle"' not in (root / "invariants.yaml").read_text()
    (root / "invariants.yaml").unlink()
    r = run_inv("init", "--no-lock", "--graphify", "nope.json", cwd=root)
    assert r.returncode == 2 and "--graphify" in r.stderr and not (root / "invariants.yaml").exists()
    other = root / "elsewhere.json"
    other.write_text(json.dumps(_graph()))
    (root / "graphify-out" / "graph.json").unlink()
    r = run_inv("init", "--no-lock", "--graph", str(other), cwd=root)
    assert r.returncode == 0 and 'door: "py:books.flows.settle"' in (root / "invariants.yaml").read_text()
