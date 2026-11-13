"""Layering of the scan package, and unit tests on its pure functions (no mocks, no files).

The scan is static analysis: it must not import the runtime enforcement layer (guard, registry) or the
CLI that drives it. It reads ablation through invariants._scan_hooks, which the package wires.
"""
import ast
import pathlib

import invariants
from invariants import _scan_hooks, scan
from invariants.scan import reachability, sarif
from invariants.scan.contracts import Finding, Mediation, Report, _Edge

SCAN = pathlib.Path(scan.__file__).parent
FORBIDDEN = ("invariants.guard", "invariants.registry", "invariants.cli")


def _imports(path: pathlib.Path) -> set[str]:
    """Every module an import statement in path names, made absolute (`from .. import registry` too)."""
    pkg = "invariants.scan"
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = pkg.split(".")[: len(pkg.split(".")) - (node.level - 1)] if node.level else []
            mod = ".".join([*base, *([node.module] if node.module else [])])
            out.add(mod)
            out.update(f"{mod}.{a.name}" for a in node.names)
    return out


def test_scan_package_replaces_the_module():
    assert (SCAN / "__init__.py").is_file()
    assert not (SCAN.parent / "scan.py").exists()


def test_scan_does_not_import_guard_registry_or_cli():
    files = sorted(SCAN.glob("*.py"))
    assert len(files) > 5
    for f in files:
        bad = sorted(m for m in _imports(f) if any(m == x or m.startswith(x + ".") for x in FORBIDDEN))
        assert not bad, f"{f.name} imports {bad}"


def test_ablation_hooks_are_wired_by_the_package():
    assert _scan_hooks.ablated is not _scan_hooks._none and _scan_hooks.reached is not _scan_hooks._ignore
    assert invariants._registry is invariants.registry




# --- _shortest / _paths ----------------------------------------------------------------------------
def _succ(*edges: _Edge) -> dict[str, list[_Edge]]:
    out: dict[str, list[_Edge]] = {}
    for e in edges:
        out.setdefault(e.caller, []).append(e)
    return out


def _e(a: str, b: str, inferred: bool = False) -> _Edge:
    return _Edge(a, b, 1, 0, inferred)


def test_shortest_finds_the_shortest_path_and_its_last_edge():
    succ = _succ(_e("a", "b"), _e("b", "sink"), _e("a", "c"), _e("c", "d"), _e("d", "sink"))
    names, last = reachability._shortest("a", {"sink"}, succ, False)
    assert names == ["a", "b", "sink"] and (last.caller, last.callee) == ("b", "sink")
    assert reachability._shortest("sink", {"a"}, succ, False) is None


def test_shortest_skips_inferred_edges_unless_asked():
    succ = _succ(_e("a", "sink", inferred=True))
    assert reachability._shortest("a", {"sink"}, succ, False) is None
    assert reachability._shortest("a", {"sink"}, succ, True)[0] == ["a", "sink"]


def test_shortest_lets_through_only_a_path_of_allowed_functions():
    succ = _succ(_e("job", "sink"), _e("new", "job"))
    assert reachability._shortest("job", {"sink"}, succ, False, {"job"}) is None      # frozen back door
    assert reachability._shortest("new", {"sink"}, succ, False, {"job"})[0] == ["new", "job", "sink"]


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
