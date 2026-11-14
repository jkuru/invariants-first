"""Review round 26: confinement under `inv mcp`, and what the pretool hook reads behind a Python run.

1. `check` and `status` imported and ran code outside --root through a symlinked subpackage: only the
   top-level package of a target was checked, so `app/leak -> ../../outside/vault` let
   `py:app.leak.evil.refund` run vault/evil.py. Under INV_CONFINE_ROOT every module of the project
   (a submodule, one a package's __path__ leads to, one the project's code imports) must resolve
   inside the root before its code runs; the standard library and installed packages load as before.
2. `scan`, `explain` and `propose_rules` followed a symlink whose target lies outside --root and
   returned the names and paths of the code there. Confined, such a link is not followed, and is an
   unresolved finding (unresolved/outside-root) naming only the link: the rule never passes on code
   it skipped. Plain `inv scan` still follows it.
3. `inv hook pretool` read only the text of the script a command runs: `python run.py` whose run.py
   imports a helper that writes the lock, or `pytest` whose conftest.py writes it, went through. It
   now also reads the project modules the script imports (transitively, bounded) and, for pytest, the
   conftest.py files and what they import; test modules themselves are not read.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from cli_support import run_inv, write_project
from invariants import lock
from invariants.confinement import CONFINE_ENV

REPO = pathlib.Path(__file__).resolve().parents[2]
SECRET_FN = "rotate_key_AKIAPLANTED26"
SECRET_MOD = "prod_secrets_r26"


def _guard_rules(target: str) -> str:
    return (f"spec: invariant/v0.1\nrules:\n  - id: PAY-1\n    guard: {target}\n"
            f"    bad: 'args.amount > 100'\n")


def _evil(marker: pathlib.Path) -> str:
    return (f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n\n\n"
            f"def refund(amount):\n    return amount\n")


def _mcp(tool: str, proj: pathlib.Path, args: dict | None = None) -> str:
    from invariants.mcp import propose, tools
    from invariants.mcp.confine import ToolError
    fn = {"check": tools.tool_check, "status": tools.tool_status, "scan": tools.tool_scan,
          "explain": tools.tool_explain, "propose_rules": propose.tool_propose_rules}[tool]
    try:
        return json.dumps(fn(proj.resolve(), None, args or {}))
    except ToolError as e:
        return f"ToolError: {e}"


# --- 1. check and status: no module of the project resolves outside --root -------------------------------
def _linked_project(tmp_path: pathlib.Path, kind: str) -> tuple[pathlib.Path, pathlib.Path, str]:
    """(project, marker, target): app/ inside the project, with code outside it reached four ways."""
    marker = tmp_path / "RAN_OUTSIDE"
    vault = tmp_path / "outside" / "vault"
    vault.mkdir(parents=True)
    (vault / "evil.py").write_text(_evil(marker))
    proj = write_project(tmp_path / "proj", {"app/__init__.py": "", "app/store.py": "X = 1\n"})
    target = "py:app.leak.evil.refund"
    if kind == "namespace dir":                      # a symlink to a directory with no __init__.py
        (proj / "app" / "leak").symlink_to(vault, target_is_directory=True)
    elif kind == "package dir":
        (vault / "__init__.py").write_text("")
        (proj / "app" / "leak").symlink_to(vault, target_is_directory=True)
    elif kind == "module file":                       # a symlink to one file
        (proj / "app" / "leak.py").symlink_to(vault / "evil.py")
        target = "py:app.leak.refund"
    elif kind == "package path":                      # app/__init__.py extends __path__ outside
        (proj / "app" / "__init__.py").write_text(f"__path__.append({str(vault)!r})\n")
        target = "py:app.evil.refund"
    elif kind == "imported by the project":           # the target is inside; its module imports the link
        (proj / "app" / "leak").symlink_to(vault, target_is_directory=True)
        (proj / "app" / "svc.py").write_text("import json\nimport app.leak.evil\n\n\ndef refund(amount):\n"
                                             "    return amount\n")
        target = "py:app.svc.refund"
    elif kind == "top-level link imported":           # a top-level name at the root that links out
        (proj / "leak26").symlink_to(vault, target_is_directory=True)
        (proj / "app" / "svc.py").write_text("import leak26.evil\n\n\ndef refund(amount):\n    return amount\n")
        target = "py:app.svc.refund"
    (proj / "invariants.yaml").write_text(_guard_rules(target))
    return proj, marker, target


KINDS = ["namespace dir", "package dir", "module file", "package path", "imported by the project",
         "top-level link imported"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("tool", ["check", "status"])
def test_mcp_never_runs_code_outside_root_through_the_project(tmp_path, tool, kind):
    proj, marker, _ = _linked_project(tmp_path, kind)
    out = _mcp(tool, proj)
    assert not marker.exists(), (kind, out)
    assert "resolves outside" in out and "confines imports" in out, (kind, out)


@pytest.mark.parametrize("kind", KINDS)
def test_plain_check_still_imports_through_the_link(tmp_path, kind):
    proj, marker, _ = _linked_project(tmp_path, kind)
    r = run_inv("check", cwd=proj)
    assert marker.exists(), (kind, r.stdout + r.stderr)
    assert "confines imports" not in r.stdout + r.stderr


def test_the_project_s_own_imports_of_the_stdlib_and_installed_packages_still_load(tmp_path):
    proj = write_project(tmp_path / "proj", {
        "app/__init__.py": "", "app/inner/__init__.py": "", "app/inner/deep.py": "Y = 2\n",
        "app/svc.py": "import json\nimport pytest\nimport invariants\nfrom app.inner import deep\n\n\n"
                      "def refund(amount):\n    return amount + deep.Y\n",
        "invariants.yaml": _guard_rules("py:app.svc.refund")})
    out = _mcp("check", proj)
    steps = {s["step"]: s["result"] for s in json.loads(out)["steps"]}
    assert steps["rules"] == "ok" and "resolves outside" not in out, out


def test_a_symlink_inside_root_to_a_directory_inside_root_still_loads(tmp_path):
    proj = write_project(tmp_path / "proj", {"app/__init__.py": "", "shared/evil.py": "def refund(amount):\n"
                                             "    return amount\n", "invariants.yaml": _guard_rules(
                                                 "py:app.leak.evil.refund")})
    (proj / "app" / "leak").symlink_to(proj / "shared", target_is_directory=True)
    out = _mcp("check", proj)
    steps = {s["step"]: s["result"] for s in json.loads(out)["steps"]}
    assert steps["rules"] == "ok" and "resolves outside" not in out, out


def test_the_finder_under_confinement(tmp_path, monkeypatch):
    from invariants.confinement import ConfinedFinder, import_module
    from invariants.errors import LoadError
    marker = tmp_path / "ran.txt"
    vault = tmp_path / "vault26"
    vault.mkdir()
    (vault / "evil.py").write_text(_evil(marker))
    root = tmp_path / "root"
    (root / "pkg_r26").mkdir(parents=True)
    (root / "pkg_r26" / "__init__.py").write_text("")
    (root / "pkg_r26" / "ok.py").write_text("Z = 3\n")
    (root / "pkg_r26" / "leak").symlink_to(vault, target_is_directory=True)
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv(CONFINE_ENV, str(root))
    try:
        assert import_module("pkg_r26.ok", "t").Z == 3
        with pytest.raises(LoadError, match="module pkg_r26.leak resolves outside"):
            import_module("pkg_r26.leak.evil", "t")
        assert not marker.exists()
        assert any(isinstance(f, ConfinedFinder) for f in sys.meta_path)
        import json as _json                                       # stdlib through the finder: as before
        assert ConfinedFinder().find_spec("json") is not None and _json.dumps(1) == "1"
        monkeypatch.delenv(CONFINE_ENV)
        assert ConfinedFinder().find_spec("pkg_r26.leak", [str(root / "pkg_r26")]) is None   # off: no-op
    finally:
        for m in [m for m in sys.modules if m.split(".")[0] == "pkg_r26"]:
            del sys.modules[m]


def test_the_repro_end_to_end_through_the_server(tmp_path):
    proj, marker, _ = _linked_project(tmp_path, "namespace dir")
    req = ('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
           '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"check","arguments":{}}}\n')
    r = subprocess.run([sys.executable, "-m", "invariants.cli", "mcp", "--root", str(proj)], input=req,
                       capture_output=True, text=True, cwd=tmp_path, timeout=120)
    assert not marker.exists() and "resolves outside" in r.stdout, r.stdout + r.stderr


# --- 2. scan, explain, propose_rules: a symlink out of --root is not followed ---------------------------
def _scan_project(tmp_path: pathlib.Path) -> pathlib.Path:
    vault = tmp_path / "outside" / "vault"
    vault.mkdir(parents=True)
    (vault / f"{SECRET_MOD}.py").write_text(f"def {SECRET_FN}(conn):\n    return conn.save('x')\n")
    (tmp_path / "outside" / "lone_r26.py").write_text("def hunter2_PLANTED(s):\n    return s.save(1)\n")
    proj = write_project(tmp_path / "proj", {
        "app/__init__.py": "",
        "app/store.py": "class Store:\n    def save(self, x):\n        return x\n",
        "app/svc.py": "from app.store import Store\n\n\ndef create(x):\n    return Store().save(x)\n",
        "invariants.yaml": ("spec: invariant/v0.1\nrules:\n  - id: SAVE-DOOR\n    door: py:app.svc.create\n"
                            "    sink: py:app.store.Store.save\n")})
    (proj / "app" / "leak").symlink_to(vault, target_is_directory=True)
    (proj / "app" / "lone.py").symlink_to(tmp_path / "outside" / "lone_r26.py")
    return proj


def _no_outside_names(out: str) -> None:
    for s in (SECRET_FN, SECRET_MOD, "hunter2_PLANTED", "lone_r26", "vault", "outside"):
        assert s not in out.replace("outside-root", "").replace("resolves outside", "").replace(
            "outside the", ""), (s, out)


def test_mcp_scan_does_not_follow_a_symlink_out_of_root(tmp_path):
    proj = _scan_project(tmp_path)
    out = _mcp("scan", proj)
    _no_outside_names(out)
    data = json.loads(out)
    assert data["verdict"] == "unresolved" and not data["passed"], out
    refused = sorted(f["file"] for f in data["findings"] if f["code"] == "unresolved/outside-root")
    assert refused == ["app/leak", "app/lone.py"], out
    assert data["rules"]["SAVE-DOOR"]["result"] != "pass", out


def test_mcp_explain_and_propose_rules_do_not_follow_it_either(tmp_path):
    proj = _scan_project(tmp_path)
    out = _mcp("explain", proj, {"id": "SAVE-DOOR"})
    _no_outside_names(out)
    assert "unresolved/outside-root" in out, out
    out = _mcp("propose_rules", proj)
    _no_outside_names(out)
    assert "read 3 source file(s)" in out, out                     # app/__init__, store, svc: not the links


def test_plain_scan_still_follows_the_link(tmp_path):
    proj = _scan_project(tmp_path)
    r = run_inv("scan", "--format", "json", cwd=proj)
    assert SECRET_FN in r.stdout and "outside-root" not in r.stdout, r.stdout + r.stderr


def test_scan_under_the_confine_env_does_not_follow_it(tmp_path):
    proj = _scan_project(tmp_path)
    env = {**os.environ, CONFINE_ENV: str(proj.resolve()), "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-m", "invariants.cli", "scan", "--format", "json"], cwd=proj, env=env,
                       capture_output=True, text=True, timeout=60)
    assert SECRET_FN not in r.stdout and "unresolved/outside-root" in r.stdout, r.stdout + r.stderr


def test_a_link_inside_root_or_excluded_is_not_reported(tmp_path):
    proj = _scan_project(tmp_path)
    (proj / "app" / "leak").unlink()
    (proj / "app" / "lone.py").unlink()
    write_project(proj, {"shared/helpers.py": "def h():\n    return 1\n"})
    (proj / "app" / "shared").symlink_to(proj / "shared", target_is_directory=True)
    (proj / "app" / "tests").symlink_to(tmp_path / "outside" / "vault", target_is_directory=True)
    (proj / "venv").mkdir()
    (proj / "venv" / "os.py").symlink_to(tmp_path / "outside" / "lone_r26.py")
    data = json.loads(_mcp("scan", proj))
    assert data["verdict"] == "pass" and not [f for f in data["findings"]
                                              if f["code"] == "unresolved/outside-root"], data


def test_the_server_s_own_rules_outside_root_are_not_confined(tmp_path):
    from invariants.mcp import tools
    proj = _scan_project(tmp_path)
    rules = tmp_path / "operator" / "invariants.yaml"
    rules.parent.mkdir()
    rules.write_text((proj / "invariants.yaml").read_text())
    data = tools.run_scan(proj.resolve(), rules, [str(proj.resolve())], (), proj.resolve())
    assert SECRET_FN in json.dumps(data)                             # the operator's roots, followed as before


# --- 3. the hook reads the project modules a Python run imports ---------------------------------------
def _hook(root: pathlib.Path, command: str) -> subprocess.CompletedProcess[str]:
    payload = {"tool_name": "Bash", "cwd": str(root), "tool_input": {"command": command}}
    return subprocess.run([sys.executable, "-m", "invariants.cli", "hook", "pretool"], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=root, timeout=60)


WRITES = "open('invariants.lock', 'w').write('{}')\n"
WRITES_PY = "import tomllib, pathlib\np = pathlib.Path('pyproject.toml')\np.write_text(p.read_text().replace('[tool.invariants.scan]', ''))\n"


def _hook_project(tmp_path: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    return write_project(tmp_path / "p", {lock.LOCK_NAME: "{}\n", "pyproject.toml": "[project]\n", **files})


@pytest.mark.parametrize("files,command", [
    ({"run.py": "import helper\n", "helper.py": WRITES}, "python run.py"),
    ({"run.py": "import a\n", "a.py": "from b import go\n", "b.py": WRITES}, "python3 run.py"),
    ({"run.py": "from tools import fix\n", "tools/__init__.py": "", "tools/fix.py": WRITES}, "python run.py"),
    ({"run.py": "import tools\n", "tools/__init__.py": WRITES}, "python run.py"),
    ({"tools/__init__.py": "", "tools/run.py": "from . import fix\n", "tools/fix.py": WRITES},
     "python -m tools.run"),
    ({"tools/__init__.py": "", "tools/run.py": "from .sub.fix import go\n", "tools/sub/__init__.py": "",
      "tools/sub/fix.py": WRITES}, "python -m tools.run"),
    ({"tools/__init__.py": WRITES, "tools/run.py": "print(1)\n"}, "python -m tools.run"),
    ({"src/pkg/__init__.py": "", "src/pkg/cli.py": "import pkg.fix\n", "src/pkg/fix.py": WRITES},
     "python -m pkg.cli"),
    ({"run.py": "import importlib\nimportlib.import_module('helper')\n", "helper.py": WRITES}, "python run.py"),
    ({"run.py": "import helper\n", "helper.py": "from invariants import lock\nopen(lock.LOCK_NAME, 'w')\n"},
     "uv run python run.py"),
    ({"run.py": "import helper\n", "helper.py": WRITES_PY}, "python run.py"),
    ({"tests/conftest.py": WRITES, "tests/test_a.py": "def test_a():\n    pass\n"}, "pytest -q"),
    ({"conftest.py": "import helper\n", "helper.py": WRITES}, "python -m pytest"),
    ({"tests/conftest.py": "pytest_plugins = ['myplug']\n", "myplug.py": WRITES}, "uv run pytest tests"),
    ({"plug.py": WRITES}, "pytest -p plug"),
    ({"sub/conftest.py": WRITES, "sub/x/test_a.py": ""}, "py.test sub/x"),
])
def test_hook_blocks_a_run_whose_project_modules_write_the_lock(tmp_path, files, command):
    root = _hook_project(tmp_path, files)
    r = _hook(root, command)
    assert r.returncode == 2, (command, r.stderr)
    assert "a project file it imports or loads" in r.stderr, r.stderr


def test_hook_blocks_pytest_from_a_subdirectory_with_a_conftest_above(tmp_path):
    root = _hook_project(tmp_path, {"conftest.py": WRITES, "tests/test_a.py": ""})
    assert _hook(root / "tests", "pytest").returncode == 2


@pytest.mark.parametrize("files,command", [
    ({"run.py": "import helper\n", "helper.py": "print('hi')\n"}, "python run.py"),
    ({"run.py": "import json, pathlib\nimport helper\n", "helper.py": "import tomllib, pathlib\n"
      "print(tomllib.loads(pathlib.Path('pyproject.toml').read_text()))\n"}, "python run.py"),
    ({"tests/conftest.py": "import pytest\n", "tests/test_lock.py":
      "def test_lock():\n    assert 'invariants.lock' in open('invariants.lock').name\n"}, "pytest -q"),
    ({"tests/conftest.py": "import helper\n", "helper.py": "X = 1\n",
      "tests/test_relock.py": "def test_r(tmp_path):\n    (tmp_path / 'invariants.lock').write_text('{}')\n"},
     "python -m pytest tests"),
    ({".venv/lib/conftest.py": WRITES, "node_modules/x/conftest.py": WRITES, "tests/test_a.py": ""}, "pytest"),
    ({"run.py": "import invariants, pytest\n"}, "python run.py"),
    ({"run.py": "import missing_mod\n"}, "python run.py"),
    ({"run.py": "def broken(:\n", "helper.py": WRITES}, "python run.py"),
])
def test_hook_allows_ordinary_runs(tmp_path, files, command):
    root = _hook_project(tmp_path, files)
    r = _hook(root, command)
    assert r.returncode == 0, (command, r.stderr)


def test_hook_does_not_read_imports_outside_the_locked_tree(tmp_path):
    write_project(tmp_path, {"shared_r26.py": WRITES})
    root = _hook_project(tmp_path, {"run.py": "import shared_r26\n"})
    assert _hook(root, f"PYTHONPATH={tmp_path} python run.py").returncode == 0
    write_project(root, {"pkg/__init__.py": "", "pkg/run.py": "from ... import up_r26\n"})
    write_project(tmp_path, {"up_r26.py": WRITES})                    # a relative import climbing out of the tree
    assert _hook(root, "python pkg/run.py").returncode == 0
    (root / "shared_r26.py").symlink_to(tmp_path / "shared_r26.py")    # found in the tree: read where it points
    assert _hook(root, "python run.py").returncode == 2
    nolock = write_project(tmp_path / "nolock", {"run.py": "import helper\n", "helper.py": WRITES,
                                                 "conftest.py": WRITES})
    assert _hook(nolock, "python run.py").returncode == 0 and _hook(nolock, "pytest").returncode == 0


@pytest.mark.parametrize("app", ["fact-pipeline", "pool-service", "support-agent"])
@pytest.mark.parametrize("command", ["pytest", "python -m pytest -q", "uv run pytest tests",
                                     "python -m pytest --inv-verify --inv-ablate --inv-report"])
def test_hook_allows_the_apps_test_runs(app, command):
    root = REPO / "apps" / app
    if not (root / lock.LOCK_NAME).is_file():
        pytest.skip("the apps are not in this checkout")
    for cwd in (root, root / "tests"):
        r = _hook(cwd, command)
        assert r.returncode == 0, (app, command, r.stderr)
