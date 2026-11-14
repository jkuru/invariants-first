"""Round 33 (ci): the MCP `check` and `status` tools take `path` and `import` as `inv check` and
`inv status` do (each path confined to the server's --root, each module a dotted name); the CI
workflow's core-only job installs no extras and its build job smoke-tests the installed wheel; the
support-agent example's private venv installs the `langgraph` extra."""
from __future__ import annotations

import json
import pathlib

import pytest
import yaml

from cli_support import run_inv, write_project
from invariants.mcp import Server, ToolError, protocol
from invariants.mcp.tools import tool_check, tool_status

REPO = pathlib.Path(__file__).resolve().parents[2]
# a targetless rule, and a guard naming a rule no file has, in a module under lib/ (off the import path)
GUARDED = {"lib/app/__init__.py": "",
           "lib/app/svc.py": "import invariants as inv\n\n@inv.guard('NOPE')\ndef f(x):\n    return x\n"}
TARGETLESS = {"id": "R-1", "status": "candidate", "bad": "false"}


@pytest.fixture
def proj(tmp_path: pathlib.Path) -> pathlib.Path:
    root = write_project(tmp_path / "proj", GUARDED)
    (root / "invariants.json").write_text(json.dumps({"rules": [TARGETLESS]}))
    r = run_inv("lock", root / "invariants.json", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    return root


def _guards(data: dict) -> dict:
    return next(s for s in data["steps"] if s["step"] == "guards")


# --- L2: path and import reach inv check and inv status ------------------------------------------
def test_check_imports_a_module_from_a_path(proj):
    plain = tool_check(proj, None, {})
    assert plain["passed"] is True and _guards(plain)["result"] == "skip", plain["output"]
    data = tool_check(proj, None, {"path": ["lib"], "import": ["app.svc"]})
    assert data["passed"] is False and data["exit_code"] == 1, data["output"]
    assert _guards(data)["result"] == "fail" and "guards NOPE" in data["output"]
    nopath = tool_check(proj, None, {"import": ["app.svc"]})          # the module is only under lib/
    assert nopath["passed"] is False and "cannot import app.svc" in nopath["output"] + nopath["stderr"]


def test_status_takes_path_and_import(proj):
    assert tool_status(proj, None, {})["status"]["verify"] == {"passed": True, "problems": []}
    verify = tool_status(proj, None, {"path": ["lib"], "import": ["app.svc"]})["status"]["verify"]
    assert verify["passed"] is False and any("guards NOPE" in p for p in verify["problems"])


@pytest.mark.parametrize("tool", [tool_check, tool_status])
def test_a_path_outside_the_root_is_refused(proj, tmp_path, tool):
    outside = tmp_path / "outside"
    outside.mkdir()
    (proj / "out").symlink_to(outside)
    for given in ("..", str(outside), "../outside", "out"):
        with pytest.raises(ToolError, match="outside the server's --root"):
            tool(proj, None, {"path": [given]})
    with pytest.raises(ToolError, match="list of strings"):
        tool(proj, None, {"path": "lib"})


@pytest.mark.parametrize("tool", [tool_check, tool_status])
@pytest.mark.parametrize("name", ["", "-x", "--relock", "app/svc", "../app", ".app", "app.", "app svc",
                                  "app..svc", "1app"])
def test_an_import_that_is_not_a_dotted_name_is_refused(proj, tool, name):
    with pytest.raises(ToolError, match="not a dotted module name"):
        tool(proj, None, {"import": [name]})


def test_tools_list_and_call_accept_path_and_import(proj):
    tools = {t["name"]: t["inputSchema"]["properties"] for t in protocol.tools_list()}
    for name in ("check", "status"):
        assert tools[name]["path"]["type"] == "array" and tools[name]["import"]["items"] == {"type": "string"}
    assert "path" not in tools["scan"] and "import" not in tools["scan"]
    reply = Server(proj).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "check", "arguments": {"path": ["lib"], "import": ["app.svc"]}}})
    assert reply is not None and reply["result"]["isError"] is False
    assert reply["result"]["structuredContent"]["passed"] is False
    bad = Server(proj).handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": "check", "arguments": {"path": ["/"]}}})
    assert bad is not None and bad["result"]["isError"] is True


# --- L4: the CI workflow --------------------------------------------------------------------------
def _workflow() -> dict:
    return yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8"))


def _runs(job: dict) -> list[str]:
    return [s["run"] for s in job["steps"] if "run" in s]


def test_core_only_job_runs_the_core_tests_before_any_extra():
    runs = _runs(_workflow()["jobs"]["core-only"])
    first = next(i for i, r in enumerate(runs) if "-m pytest" in r)
    before = "\n".join(runs[:first + 1])
    assert "pip install $WORKSPACE -e . pytest" in before and "[" not in before, before    # no extra, no [dev]
    # the workspace libraries, editable, before invariants-first in every job that installs it
    assert _workflow()["env"]["WORKSPACE"].split() == [
        "-e", "../packages/invariants-cel", "-e", "../packages/invariants-scan", "-e", "../packages/invariants-schema-diff"]
    assert '"$CORE_IMPORTS"' in runs[first] and "$CORE_TESTS" in runs[first]
    assert "cel_expr_python" in "\n".join(runs[first + 1:])                  # the full suite, still no cel


def test_build_job_smoke_tests_the_installed_wheel():
    wf = _workflow()
    built = next(r for r in _runs(wf["jobs"]["build"]) if "python -m build" in r)
    assert "for lib in invariants-cel invariants-scan invariants-schema-diff" in built
    assert 'python -m build --outdir dist "../packages/$lib"' in built and "python -m build --outdir dist ." in built
    step = next(r for r in _runs(wf["jobs"]["build"]) if "dist/invariants_first-*.whl" in r)
    # invariants-first's wheel, its three libraries resolved from the wheels built beside it
    for want in ("pip install --find-links dist dist/invariants_first-*.whl pytest", "pip check", '"$CORE_IMPORTS"',
                 "inv --version", "inv-scan --help", "'site-packages' in m.__file__",
                 "-m pytest -q -p no:cacheprovider $CORE_TESTS"):
        assert want in step, want
    assert step.index("site-packages") < step.index("-m pytest")      # the tests import the wheel
    tests = wf["env"]["CORE_TESTS"].split()
    assert tests and all((REPO / "invariants-py" / t).is_file() for t in tests)
    compile(wf["env"]["CORE_IMPORTS"], "CORE_IMPORTS", "exec")


def test_each_library_is_tested_alone_and_every_job_installs_the_workspace_first():
    """Split phase 2: a job per library installs only it (and the libraries it depends on) and runs its
    tests; each job that installs invariants-first from the checkout installs the three libraries in
    the same command."""
    wf = _workflow()
    job = wf["jobs"]["library"]
    assert job["strategy"]["matrix"]["package"] == ["invariants-cel", "invariants-scan", "invariants-schema-diff"]
    runs = "\n".join(_runs(job))
    assert "find_spec('invariants') is not None" in runs and "python -m pytest tests" in runs
    seen = 0
    for name, j in wf["jobs"].items():
        for r in _runs(j):
            for line in r.splitlines():
                if "pip install" in line and ("-e ." in line or "-e '." in line):
                    seen += 1
                    assert "$WORKSPACE" in line, (name, line)
    assert seen >= 6


# --- L5: the support-agent example installs its langgraph extra --------------------------------------
def test_support_agent_ci_installs_the_langgraph_extra():
    text = (REPO / "apps/support-agent/ci.sh").read_text(encoding="utf-8")
    pyproject = (REPO / "apps/support-agent/pyproject.toml").read_text(encoding="utf-8")
    assert "langgraph = [" in pyproject and "'.[dev,langgraph]'" in text
    assert "REQUIRE_LANGGRAPH" in text
    assert _workflow()["jobs"]["apps"]["env"]["REQUIRE_LANGGRAPH"] == "1"
