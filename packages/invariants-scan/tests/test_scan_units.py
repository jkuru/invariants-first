"""Unit tests on the scan's pure functions (no mocks, no files): path finding and SARIF."""
from invariants_scan import reachability, sarif
from invariants_scan.contracts import Finding, Mediation, Report, _Edge


# --- _paths ----------------------------------------------------------------------------
def _shortest(*args):
    """The first path _paths finds (the shortest), or None."""
    return next(reachability._paths(*args), None)


def _succ(*edges: _Edge) -> dict[str, list[_Edge]]:
    out: dict[str, list[_Edge]] = {}
    for e in edges:
        out.setdefault(e.caller, []).append(e)
    return out


def _e(a: str, b: str, inferred: bool = False) -> _Edge:
    return _Edge(a, b, 1, 0, inferred)


def test_shortest_finds_the_shortest_path_and_its_last_edge():
    succ = _succ(_e("a", "b"), _e("b", "sink"), _e("a", "c"), _e("c", "d"), _e("d", "sink"))
    names, last = _shortest("a", {"sink"}, succ, False)
    assert names == ["a", "b", "sink"] and (last.caller, last.callee) == ("b", "sink")
    assert _shortest("sink", {"a"}, succ, False) is None


def test_shortest_skips_inferred_edges_unless_asked():
    succ = _succ(_e("a", "sink", inferred=True))
    assert _shortest("a", {"sink"}, succ, False) is None
    assert _shortest("a", {"sink"}, succ, True)[0] == ["a", "sink"]


def test_shortest_lets_through_only_a_path_of_allowed_functions():
    succ = _succ(_e("job", "sink"), _e("new", "job"))
    assert _shortest("job", {"sink"}, succ, False, {"job"}) is None      # frozen back door
    assert _shortest("new", {"sink"}, succ, False, {"job"})[0] == ["new", "job", "sink"]


def test_paths_yields_one_path_per_call_into_a_goal():
    succ = _succ(_e("a", "sink"), _e("a", "b"), _e("b", "sink"))
    assert [n for n, _ in reachability._paths("a", {"sink"}, succ, False)] == [["a", "sink"], ["a", "b", "sink"]]


def test_reach_stops_at_blocked_functions():
    edges = [_e("a", "door"), _e("door", "sink"), _e("a", "b")]
    assert reachability._reach(["a"], edges, {"door"}) == {"a", "b"}
    assert reachability._reach(["door"], edges, {"door"}) == set()


# --- to_sarif ------------------------------------------------------------------------------------
def _report() -> Report:
    findings = [Finding("bypass", "R-1", "bypass", "a -> sink", "app/a.py", 3, 5, "/r", ["a", "sink"], "app.a"),
                Finding("unresolved", "R-1", "unresolved/reflection", "eval runs code"),
                Finding("missing", "R-2", "missing/sink", "sink py:x is not defined")]
    return Report(findings, {"R-1": Mediation("R-1", ["py:x"], ["py:y"])}, {})


def test_sarif_rules_levels_and_locations():
    out = sarif.to_sarif(_report(), version="9.9")
    assert out["version"] == "2.1.0"
    run = out["runs"][0]
    assert run["tool"]["driver"]["version"] == "9.9"
    assert [r["id"] for r in run["tool"]["driver"]["rules"]] == ["bypass", "missing/sink", "unresolved/reflection"]
    levels = {r["ruleId"]: r["level"] for r in run["results"]}
    assert levels == {"bypass": "error", "unresolved/reflection": "warning", "missing/sink": "error"}
    first = run["results"][0]
    assert first["ruleIndex"] == 0 and first["message"]["text"] == "R-1: a -> sink"
    assert first["locations"][0]["physicalLocation"] == {
        "artifactLocation": {"uri": "app/a.py"}, "region": {"startLine": 3, "startColumn": 5}}
    assert [s["location"]["message"]["text"] for s in first["codeFlows"][0]["threadFlows"][0]["locations"]] \
        == ["a", "sink"]
    assert "locations" not in run["results"][1] and "codeFlows" not in run["results"][1]


def test_sarif_of_an_empty_report():
    out = sarif.to_sarif(Report([], {}, {}))
    assert out["runs"][0]["results"] == [] and out["runs"][0]["tool"]["driver"]["rules"] == []
