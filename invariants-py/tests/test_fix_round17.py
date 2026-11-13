"""Fix round 17: ablation, MCP, providers.

1. --inv-ablate: a falsifier whose re-runs fail because of state a broader fixture carries (a
   module-scoped budget) is inconclusive, never caught. A second control re-run follows a failing
   ablated one; the result names the fixtures broader than the function.
2. inv-mcp: the root, rules, source_roots and graphify arguments stay inside the server's --root
   (after resolving symlinks); a filesystem root is never a project root.
3. Async providers run one at a time in order unless every one involved opted in to concurrency
   (`concurrent: true` in the rule file, or @inv.provider(..., concurrent=True)).
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
from typing import Any, Iterator

import jsonschema
import pytest

import invariants as inv
import round16 as r16
import round17 as r17
from invariants import mcp
from invariants import lockfile

pytest_plugins = ["pytester"]


# --- 1. ablation and state carried between runs ----------------------------------------------------
APP = '''
import invariants as inv

@inv.guard("R-1")
def halve(n: int) -> int:
    return n // 2
'''

RULES = {"spec": "invariant/v0.1", "targets": {"py:plug_app.halve": {"effects": "none"}},
         "rules": [{"id": "R-1", "status": "enforce", "subject": {"target": "py:plug_app.halve"},
                    "bad": "args.n < 0", "enforcers": [{"point": "decorator", "timing": "pre_effect",
                                                        "mode": "enforce"}],
                    "falsified_by": ["test_app.py::test_f"]}]}

# the reviewer's case: the guard is not load-bearing (the test passes with or without it), but each
# run spends a module-scoped budget of 2, so the third run (the ablated one) fails
BUDGET = '''
import pytest, invariants as inv, plug_app

@pytest.fixture(scope="module")
def budget():
    return {"left": BUDGET_LEFT}

def test_f(budget):
    try:
        plug_app.halve(-4)
    except inv.Rejected:
        pass
    budget["left"] -= 1
    assert budget["left"] >= 0
'''

# a real load-bearing falsifier that also reads a module-scoped fixture (read-only)
LOAD_BEARING = '''
import pytest, invariants as inv, plug_app

@pytest.fixture(scope="module")
def config():
    return {"n": -4}

def test_f(config):
    with pytest.raises(inv.Rejected):
        plug_app.halve(config["n"])
'''


@pytest.fixture
def abl(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeini("[pytest]\npythonpath = .\n")
    pytester.makepyfile(plug_app=APP)
    (pytester.path / "rules.json").write_text(json.dumps(RULES))
    return pytester


def _ablate(p: pytest.Pytester, *args: str) -> tuple[pytest.RunResult, dict[str, Any]]:
    r = p.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:randomly", "--inv-rules", "rules.json",
                               "--inv-ablate", "--inv-report-json", "rep.json", *args)
    rep = p.path / "rep.json"
    return r, (json.loads(rep.read_text()) if rep.exists() else {})


def _result(rep: dict[str, Any]) -> str:
    return str(rep["ablation"]["rules"]["R-1"][0]["result"])


def test_state_carried_by_a_module_fixture_is_not_caught(abl):
    abl.makepyfile(test_app=BUDGET.replace("BUDGET_LEFT", "2"))
    r, rep = _ablate(abl)
    res = _result(rep)
    assert res != "caught" and res.startswith("inconclusive (state carried between runs"), res
    assert "budget (module)" in res, res
    assert rep["ablation"]["unverified"] == ["R-1"]
    assert r.ret == pytest.ExitCode.TESTS_FAILED
    r.stdout.fnmatch_lines(["*no falsifier caught the ablation of R-1*", "*state carried between runs*"])


def test_a_failing_first_control_fails_closed_and_names_the_fixture(abl):
    abl.makepyfile(test_app=BUDGET.replace("BUDGET_LEFT", "1"))     # the first control run overspends
    r, rep = _ablate(abl)
    res = _result(rep)
    assert res.startswith("inconclusive (failed on a control re-run without ablation"), res
    assert "budget (module)" in res, res
    assert r.ret == pytest.ExitCode.TESTS_FAILED and rep["ablation"]["unverified"] == ["R-1"]


def test_a_load_bearing_falsifier_with_a_module_fixture_is_caught(abl):
    abl.makepyfile(test_app=LOAD_BEARING)
    r, rep = _ablate(abl)
    assert _result(rep) == "caught", r.stdout.str()
    assert r.ret == 0, r.stdout.str()


def test_a_control_failure_without_broad_fixtures_says_so(abl):
    abl.makepyfile(test_app='''
import plug_app
RUNS = []

def test_f():
    RUNS.append(1)
    assert len(RUNS) == 1
''')
    _, rep = _ablate(abl)
    res = _result(rep)
    assert res.startswith("inconclusive (failed on a control re-run") and "no fixture broader" in res, res


# --- 2. inv-mcp path arguments stay inside --root --------------------------------------------------
@pytest.fixture
def escape(tmp_path: pathlib.Path) -> tuple[mcp.Server, pathlib.Path, pathlib.Path]:
    server_root = tmp_path / "project"
    server_root.mkdir()
    outside = tmp_path / "elsewhere"
    (outside / "evil").mkdir(parents=True)
    (outside / "evil" / "__init__.py").write_text(
        "import pathlib; pathlib.Path(__file__).parent.parent.joinpath('PWNED').write_text('code ran')\n"
        "def f(x: int) -> None: pass\n")
    (outside / "invariants.yaml").write_text('rules:\n  - id: R-1\n    guard: "py:evil.f"\n    bad: "args.x > 1"\n')
    (server_root / "invariants.yaml").write_text(
        'rules:\n  - id: R-1\n    door: "py:app.d"\n    sink: "py:app.s"\n')
    (server_root / "app.py").write_text("def s():\n    pass\n\ndef d():\n    s()\n")
    return mcp.Server(server_root), server_root, outside


def _call(s: mcp.Server, name: str, **args: Any) -> dict[str, Any]:
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    assert r is not None
    return r["result"]  # type: ignore[no-any-return]


@pytest.mark.parametrize("tool", ["check", "status", "scan", "explain"])
@pytest.mark.parametrize("args", [{"root": "../elsewhere"}, {"root": "ABS"}, {"rules": "../elsewhere/invariants.yaml"},
                                  {"rules": "ABS/invariants.yaml"}, {"source_roots": ["../elsewhere"]}])
def test_path_arguments_cannot_escape_the_server_root(escape, tool, args):
    s, _, outside = escape
    args = {k: (v.replace("ABS", str(outside)) if isinstance(v, str) else v) for k, v in args.items()}
    if tool == "explain":
        args["id"] = "R-1"
    if tool == "status" and "source_roots" in args:
        pytest.skip("status takes no source_roots")
    res = _call(s, tool, **args)
    assert res["isError"] is True, res
    assert "outside the server's --root" in res["content"][0]["text"], res
    assert not (outside / "PWNED").exists()


def test_a_symlink_cannot_escape_the_server_root(escape):
    s, server_root, outside = escape
    os.symlink(outside, server_root / "link")
    for args in ({"root": "link"}, {"rules": "link/invariants.yaml"}):
        res = _call(s, "check", **args)
        assert res["isError"] is True and "outside the server's --root" in res["content"][0]["text"], res
    (server_root / "rules.yaml").symlink_to(outside / "invariants.yaml")
    res = _call(s, "check", rules="rules.yaml")
    assert res["isError"] is True and "outside the server's --root" in res["content"][0]["text"], res
    assert not (outside / "PWNED").exists()


def test_propose_rules_paths_stay_inside(escape):
    s, _, outside = escape
    for args in ({"root": str(outside)}, {"source_roots": [str(outside)]}, {"graphify": str(outside / "g.json")}):
        res = _call(s, "propose_rules", **args)
        assert res["isError"] is True and "outside the server's --root" in res["content"][0]["text"], (args, res)


def test_scan_with_the_filesystem_root_is_rejected(escape):
    s, _, outside = escape
    res = _call(s, "scan", root="/", rules=str(outside / "invariants.yaml"), source_roots=[str(outside)])
    assert res["isError"] is True, res
    anchor = mcp.Server(pathlib.Path("/"))
    res = _call(anchor, "scan", rules=str(outside / "invariants.yaml"))
    assert res["isError"] is True and "filesystem root" in res["content"][0]["text"], res


def test_paths_inside_the_server_root_still_work(escape):
    s, server_root, _ = escape
    (server_root / "sub").mkdir()
    assert _call(s, "scan")["isError"] is False
    assert _call(s, "scan", rules="invariants.yaml", source_roots=["."])["structuredContent"]["passed"] is True
    assert _call(s, "scan", root=str(server_root))["isError"] is False      # an absolute path inside is fine
    assert _call(s, "scan", root="sub/..")["isError"] is False


# --- 3. async providers: sequential unless every one opted in -------------------------------------
@pytest.fixture
def iso() -> Iterator[inv.Registry]:
    with inv.isolated() as reg:
        r16.PROVIDER_CALLS.clear()
        yield reg


def _overlapped(calls: list[tuple[str, int]], a: str, b: str) -> bool:
    kinds = [k for k, _ in calls]
    return max(kinds.index(f"{a}-start"), kinds.index(f"{b}-start")) < min(kinds.index(f"{a}-end"),
                                                                          kinds.index(f"{b}-end"))


def _prov(module: str, names: tuple[str, ...], **extra: Any) -> dict[str, Any]:
    return {n: {"type": "int", "from": f"py:{module}.{n}", "args": {"x": "args.x"}, **extra.get(n, {})}
            for n in names}


def test_async_providers_run_one_at_a_time_by_default(iso):
    inv.load(r16.guard_doc("G-AP", "apf", "slow1 + slow2 > 100", providers=_prov("round16", ("slow1", "slow2"))))
    asyncio.run(r16.apf(1))
    assert [k for k, _ in r16.PROVIDER_CALLS] == ["slow1-start", "slow1-end", "slow2-start", "slow2-end"]


def test_async_providers_that_all_opted_in_in_the_rule_file_run_together(iso):
    on = {"concurrent": True}
    inv.load(r16.guard_doc("G-AP", "apf", "slow1 + slow2 > 100",
                           providers=_prov("round16", ("slow1", "slow2"), slow1=on, slow2=on)))
    asyncio.run(r16.apf(1))
    assert _overlapped(r16.PROVIDER_CALLS, "slow1", "slow2"), r16.PROVIDER_CALLS


def test_one_provider_that_did_not_opt_in_never_overlaps(iso):
    inv.load(r16.guard_doc("G-AP", "apf", "slow1 + slow2 > 100",
                           providers=_prov("round16", ("slow1", "slow2"), slow1={"concurrent": True})))
    asyncio.run(r16.apf(1))
    assert [k for k, _ in r16.PROVIDER_CALLS] == ["slow1-start", "slow1-end", "slow2-start", "slow2-end"]


def test_the_decorator_opts_in(iso):
    r17.CALLS.clear()
    doc = r16.guard_doc("G-AP", "apf", "fast1 + fast2 > 100", providers=_prov("round17", ("fast1", "fast2")))
    inv.load(doc)
    asyncio.run(r16.apf(1))
    assert _overlapped(r17.CALLS, "fast1", "fast2"), r17.CALLS
    r17.CALLS.clear()
    doc["providers"]["fast2"]["concurrent"] = False     # the rule file can say no for one of them
    inv.load(doc)
    asyncio.run(r16.apf(1))
    assert [k for k, _ in r17.CALLS] == ["fast1-start", "fast1-end", "fast2-start", "fast2-end"]


def test_concurrent_must_be_a_boolean(iso):
    doc = r16.guard_doc("G-AP", "apf", "slow1 > 100",
                        providers=_prov("round16", ("slow1",), slow1={"concurrent": "yes"}))
    with pytest.raises(inv.LoadError, match="concurrent must be true or false"):
        inv.load(doc)
    with pytest.raises(TypeError):
        inv.provider("x", concurrent="yes")  # type: ignore[arg-type]


def test_the_schema_takes_concurrent():
    schema = json.loads(inv.schema_path().read_text())
    v = jsonschema.Draft202012Validator(schema["$defs"]["provider"])
    base = {"type": "int", "from": "py:m.f", "args": {}}
    assert v.is_valid({**base, "concurrent": True})
    assert not v.is_valid({**base, "concurrent": "yes"})


def test_the_lock_pins_concurrent_as_a_change_that_is_not_a_weakening(tmp_path):
    doc = r16.guard_doc("G-AP", "apf", "slow1 > 100", providers=_prov("round16", ("slow1",)))
    old = lockfile.build(doc, tmp_path)
    doc["providers"]["slow1"]["concurrent"] = True
    new = lockfile.build(doc, tmp_path)
    changes = lockfile.changes(old, new)
    hit = [c for c in changes if "concurrent" in c.what]
    assert hit and not any(c.weakened for c in hit), changes
    assert "concurrent" not in old["providers"]["slow1"]              # an existing lock does not churn
