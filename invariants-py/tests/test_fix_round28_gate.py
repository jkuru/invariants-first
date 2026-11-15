"""Round 28, the weakening gate: the process that decides WEAKENED never imports the app (a module
setting INV_ALLOW_WEAKENING or patching invariants.lock.changes at import no longer passes `inv
check --base` or lets plain `inv lock` write a weakened lock), attribute paths under confinement,
opaque `python -Ic`, YAML aliases, and the adoption-review items (lock refuses an invalid rule file,
validate's message order, unresolved-site advice, init's closing note, per-entry allow_unresolved diffs)."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from cli_support import run_inv, write_project

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

SERVICE = '''\
import invariants as inv


@inv.guard("PAY")
def refund(amount: int) -> None:
    pass
'''
RULES = '''\
spec: invariant/v0.1
rules:
  - id: PAY
    guard: "py:r28pay.service.refund"
    bad: [{when: "args.amount <= 0", message: "refund must be positive"}]
'''
WEAKER = RULES.replace("args.amount <= 0", "args.amount < 0")
# what a pull request's own module can do at import, in the process that imports it
SETS_ENV = 'import os\nos.environ["INV_ALLOW_WEAKENING"] = "1"\n'
PATCHES = ('import invariants.lock as _l\n_r = _l.changes\n'
           '_l.changes = lambda *a, **k: [c for c in _r(*a, **k) if not c.weakened]\n'
           'import invariants.cli.commands as _c\nimport invariants.cli.git_base as _g\n'
           '_c.lock = _l\n_g.lock = _l\n')
VARIANTS = {"sets-env": SETS_ENV, "patches-changes": PATCHES}


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, env=GIT_ENV, check=True, capture_output=True)


def _project(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "p"
    write_project(root, {"r28pay/__init__.py": "", "r28pay/service.py": SERVICE, "invariants.yaml": RULES})
    assert run_inv("lock", cwd=root).returncode == 0
    return root


def _weakened_branch(tmp_path: pathlib.Path, payload: str) -> pathlib.Path:
    """main pins `<= 0`; the branch loosens it to `< 0`, re-pins with --accept-weakening, and adds payload."""
    root = _project(tmp_path)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-qb", "weak")
    (root / "invariants.yaml").write_text(WEAKER)
    assert run_inv("lock", "--accept-weakening", cwd=root).returncode == 0
    (root / "r28pay" / "service.py").write_text(SERVICE + payload)
    return root


@needs_git
@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_an_imported_module_cannot_pass_check_base(tmp_path, variant):
    root = _weakened_branch(tmp_path, VARIANTS[variant])
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("INV_ALLOW_WEAKENING", None)
    r = subprocess.run([sys.executable, "-m", "invariants.cli", "check", "--base", "main"], cwd=root, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL  since main" in r.stdout and "WEAKENED" in r.stdout and "allowed by a reviewer" not in r.stdout


@needs_git
def test_the_reviewer_switch_still_passes_check_base(tmp_path):
    root = _weakened_branch(tmp_path, "")
    env = {**os.environ, "INV_ALLOW_WEAKENING": "1"}
    r = subprocess.run([sys.executable, "-m", "invariants.cli", "check", "--base", "main"], cwd=root, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "(allowed by a reviewer)" in r.stdout, r.stdout + r.stderr


def test_an_imported_module_cannot_make_plain_lock_write_a_weakening(tmp_path):
    root = _project(tmp_path)
    before = (root / "invariants.lock").read_text()
    (root / "invariants.yaml").write_text(WEAKER)
    (root / "r28pay" / "service.py").write_text(SERVICE + PATCHES)
    r = run_inv("lock", cwd=root)
    assert r.returncode == 1 and "refused:" in r.stderr and "WEAKENED" in r.stdout, r.stdout + r.stderr
    assert (root / "invariants.lock").read_text() == before
    for args in (("lock", "--check"), ("check",), ("check", "--relock")):
        r = run_inv(*args, cwd=root)
        assert r.returncode == 1 and "WEAKENED" in r.stdout + r.stderr, (args, r.stdout + r.stderr)
        assert (root / "invariants.lock").read_text() == before


def test_the_stop_hook_is_not_fooled_by_an_imported_module(tmp_path):
    root = _project(tmp_path)
    (root / "invariants.yaml").write_text(WEAKER)
    (root / "r28pay" / "service.py").write_text(SERVICE + PATCHES + SETS_ENV)
    r = subprocess.run([sys.executable, "-m", "invariants.cli", "hook", "stop"], cwd=root, capture_output=True,
                       text=True, input=json.dumps({"cwd": str(root)}), timeout=60)
    assert r.returncode == 2 and "WEAKENED" in r.stderr, r.stdout + r.stderr


SPY = '''\
import sys
from invariants import cli
code = cli.main(sys.argv[1:])
print("CODE", code, "IMPORTED", sorted(m for m in sys.modules if m.split(".")[0] == "r28pay"))
'''


@needs_git
@pytest.mark.parametrize("args", [["check"], ["check", "--base", "main"], ["lock"], ["lock", "--check"],
                                  ["validate"], ["check", "--relock"]])
def test_the_deciding_process_never_imports_the_app(tmp_path, args):
    root = _weakened_branch(tmp_path, "")
    spy = tmp_path / "spy.py"
    spy.write_text(SPY)
    r = subprocess.run([sys.executable, str(spy), *args], cwd=root, capture_output=True, text=True, timeout=60)
    assert "IMPORTED []" in r.stdout, r.stdout + r.stderr
    assert "CODE" in r.stdout


def test_one_child_runs_per_check(tmp_path, monkeypatch):
    from invariants.cli import worker
    root = _project(tmp_path)
    runs: list[dict] = []
    real = worker.run_child

    def counting(path, extra, want):
        runs.append(want)
        return real(path, extra, want)
    monkeypatch.setattr(worker, "run_child", counting)
    monkeypatch.chdir(root)
    from invariants import cli
    assert cli.main(["check"]) == 0
    assert len(runs) == 1 and runs[0]["load"] and runs[0]["build"] is not None and runs[0]["verify"] == []
    assert "r28pay" not in sys.modules


def test_the_clause_types_come_back_from_the_child(tmp_path):
    """`args.flag` -> `args.flag && true` is the same condition only when the signature says flag is a
    bool: the child's typer, replayed in the deciding process, must reach the comparison."""
    root = tmp_path / "t"
    rules = 'rules:\n  - id: PAY\n    guard: "py:r28ty.service.refund"\n    bad: "args.flag"\n'
    write_project(root, {"r28ty/__init__.py": "", "invariants.yaml": rules, "r28ty/service.py": (
        'import invariants as inv\n\n\n@inv.guard("PAY")\ndef refund(amount: int, flag: bool) -> None:\n    pass\n')})
    assert run_inv("lock", cwd=root).returncode == 0
    (root / "invariants.yaml").write_text(rules.replace('"args.flag"', '"args.flag && true"'))
    r = run_inv("lock", "--check", cwd=root)
    assert "WEAKENED" not in r.stdout + r.stderr, r.stdout + r.stderr
    r = run_inv("check", "--relock", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr


def test_child_errors_print_as_before(tmp_path):
    root = _project(tmp_path)
    (root / "r28pay" / "service.py").write_text(SERVICE + "raise RuntimeError('boom at import')\n")
    r = run_inv("lock", cwd=root)
    assert r.returncode == 1
    assert "inv lock: RuntimeError: boom at import (raised while importing r28pay.service" in r.stderr, r.stderr
    r = run_inv("check", cwd=root)
    assert r.returncode == 1 and "FAIL  rules" in r.stdout and "boom at import" in r.stdout, r.stdout


# the child's word is taken only for what the imported code alone can say: a module that fakes the
# whole lock, or rewrites a file the verdict reads, changes nothing
FAKES_BUILD = ('import json, pathlib, invariants.lock as _l\n'
               '_l.build = lambda *a, **k: json.loads(pathlib.Path("invariants.lock").read_text())\n')
REWRITES_RULES = f'import pathlib\npathlib.Path("invariants.yaml").write_text({RULES!r})\n'


@needs_git
@pytest.mark.parametrize("payload", [FAKES_BUILD, REWRITES_RULES], ids=["fakes-build", "rewrites-rules"])
def test_the_child_cannot_stand_in_for_the_rule_file(tmp_path, payload):
    root = _weakened_branch(tmp_path, "")
    (root / "r28pay" / "service.py").write_text(SERVICE + payload)
    (root / "invariants.lock").write_text(subprocess.run(["git", "show", "main:invariants.lock"], cwd=root,
                                                         capture_output=True, text=True, check=True).stdout)
    for args in (("check", "--base", "main"), ("lock", "--check")):
        (root / "invariants.yaml").write_text(WEAKER)
        r = run_inv(*args, cwd=root)
        assert r.returncode == 1 and "WEAKENED" in r.stdout + r.stderr, (args, r.stdout + r.stderr)


FLEET = {
    "r28fleet/__init__.py": "",
    "r28fleet/store.py": "def set_size(conn, n):\n    conn.execute(n)\n",
    "r28fleet/service.py": ('import invariants as inv\nfrom r28fleet import store\n\n\n@inv.guard("F-1")\n'
                            'def resize(conn, n: int) -> None:\n    store.set_size(conn, n)\n'),
    "r28fleet/retry.py": ('from r28fleet import store\n\n\ndef retry(conn, n):\n'
                          '    store.set_size(conn, n)\n'),
    "invariants.yaml": ('rules:\n  - id: F-1\n    guard: "py:r28fleet.service.resize"\n    bad: "args.n < 0"\n'
                        '    mediation: {sink: "py:r28fleet.store.set_size", door: "py:r28fleet.service.resize"}\n'),
}
CLEANS_BYPASS = ('\nimport pathlib\npathlib.Path(__file__).with_name("retry.py").write_text("")\n')


def test_the_scan_reads_the_sources_before_the_app_is_imported(tmp_path):
    root = write_project(tmp_path / "f", FLEET)
    assert run_inv("lock", cwd=root).returncode == 0
    (root / "r28fleet" / "service.py").write_text(FLEET["r28fleet/service.py"] + CLEANS_BYPASS)
    r = run_inv("check", cwd=root)
    assert r.returncode == 1 and "FAIL  scan" in r.stdout and "retry" in r.stdout, r.stdout + r.stderr


def test_a_file_contract_is_pinned_by_the_deciding_process(tmp_path):
    root = _project(tmp_path)
    (root / "api.json").write_text('{"openapi": "3.0.0", "paths": {}}')
    (root / "invariants.yaml").write_text(RULES.replace("rules:", "contracts:\n  api: {type: openapi, ref: api.json}\n"
                                                                    "rules:"))
    assert run_inv("lock", cwd=root).returncode == 0
    from invariants import lock
    stored = json.loads((root / "invariants.lock").read_text())
    assert set(stored["contracts"]) == {"api"} and "hash" in stored["contracts"]["api"]
    assert not lock.schema_problems(stored)
    doc = __import__("invariants.loader", fromlist=["read_document"]).read_document(root / "invariants.yaml")
    static, errors = lock.static_pins(doc, root)
    assert not errors and static["contracts"]["api"] == stored["contracts"]["api"]
    fake = {**stored, "contracts": {"api": {"type": "openapi", "ref": "api.json", "hash": "sha256:0"}}}
    assert lock.with_static(fake, static)["contracts"]["api"] == stored["contracts"]["api"]
    with pytest.raises(Exception, match="pinned other targets"):
        lock.with_static({**stored, "targets": {}}, static)


def test_the_trust_boundary_is_documented():
    docs = pathlib.Path(__file__).resolve().parents[1] / "docs"
    sec = (pathlib.Path(__file__).resolve().parents[1] / "SECURITY.md").read_text()
    for text in ((docs / "agents.md").read_text(), (docs / "lockfile.md").read_text(), sec):
        assert "never imports" in text and "trusts the code it imports" in text
    start = (docs / "start.md").read_text()
    assert ".github/workflows/" in start and "CODEOWNERS" in start


# --- 2. an attribute path stays in the project ---------------------------------------------------
CONF_MOD = '''\
import os
import sys
from os import environ


class Model:
    class Inner:
        x: int


def f(x: int) -> None:
    pass


D = {"a": int}
'''


@pytest.fixture
def conf_pkg(tmp_path, monkeypatch):
    write_project(tmp_path / "c", {"r28conf/__init__.py": "from . import mod\n", "r28conf/mod.py": CONF_MOD})
    monkeypatch.syspath_prepend(str(tmp_path / "c"))
    yield tmp_path / "c"
    for m in [m for m in sys.modules if m.split(".")[0] == "r28conf"]:
        del sys.modules[m]


@pytest.mark.parametrize("ref", ["r28conf.mod:os.environ._data", "r28conf.mod:sys.modules",
                                 "r28conf:mod.Model"])
def test_a_contract_ref_never_goes_through_a_module(conf_pkg, ref):
    from invariants.errors import LoadError
    from invariants.resolve import resolve_ref
    with pytest.raises(LoadError, match="goes through the module"):
        resolve_ref(ref, "contract leak")


@pytest.mark.parametrize("ref", ["r28conf.mod:environ._data", "r28conf.mod:D.keys"])
def test_under_confinement_a_ref_goes_only_through_project_objects(conf_pkg, ref, monkeypatch):
    from invariants.errors import LoadError
    from invariants.resolve import resolve_ref
    resolve_ref(ref, "contract leak")                                   # plain inv follows it
    monkeypatch.setenv("INV_CONFINE_ROOT", str(conf_pkg))
    with pytest.raises(LoadError, match="is not defined inside"):
        resolve_ref(ref, "contract leak")


@pytest.mark.parametrize("target", ["py:r28conf.mod.os.environ", "py:r28conf.mod.sys.modules",
                                    "py:r28conf.mod.environ.copy"])
def test_under_confinement_a_target_goes_only_through_project_objects(conf_pkg, target, monkeypatch):
    from invariants.errors import LoadError
    from invariants.resolve import resolve
    monkeypatch.setenv("INV_CONFINE_ROOT", str(conf_pkg))
    with pytest.raises(LoadError, match="is not defined inside"):
        resolve(target)


def test_project_paths_still_resolve_under_confinement(conf_pkg, monkeypatch):
    from invariants.resolve import resolve, resolve_ref
    monkeypatch.setenv("INV_CONFINE_ROOT", str(conf_pkg))
    assert resolve_ref("r28conf.mod:Model.Inner", "contract c").__name__ == "Inner"
    assert resolve_ref("r28conf.mod:D", "contract c") == {"a": int}
    assert resolve_ref("r28conf.mod", "contract c").__name__ == "r28conf.mod"
    assert resolve("py:r28conf.mod.f").__name__ == "f"
    assert resolve("py:r28conf.mod.Model.Inner").__name__ == "Inner"


def test_the_check_tool_does_not_list_environment_names(tmp_path):
    root = write_project(tmp_path / "p3", {
        "app3/__init__.py": "", "app3/mod.py": "import os\n\n\ndef f(x: int) -> None:\n    pass\n",
        "invariants.yaml": ('contracts:\n  leak: {type: schema, ref: "app3.mod:os.environ._data"}\nrules:\n'
                            '  - id: R\n    guard: "py:app3.mod.f"\n    bad: "args.x <= 0"\n')})
    env = {**os.environ, "INV_CONFINE_ROOT": str(root), "R28_SECRET_NAME": "hunter2"}
    r = subprocess.run([sys.executable, "-m", "invariants.cli", "check"], cwd=root, env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 1 and "R28_SECRET_NAME" not in r.stdout + r.stderr, r.stdout + r.stderr
    assert "goes through the module app3.mod.os" in r.stdout + r.stderr


# --- 3. python code from a string, whatever the options before -c --------------------------------
WRITE = "\"open('invariants'+'.lock','w').write('{}')\""


@pytest.mark.parametrize("command", [
    f"python -c {WRITE}", f"python -I -c {WRITE}", f"python3 -Ic {WRITE}", f"python3 -u -c {WRITE}",
    f"python3 -I -u -B -c {WRITE}", f"python3 -W ignore -c {WRITE}", f"python3 -Wignore -c {WRITE}",
    f"python3 -X utf8 -c {WRITE}", f"python3.12 -IBc {WRITE}", f"/usr/bin/python3 -E -s -c {WRITE}",
    f"python3 --check-hash-based-pycs never -c {WRITE}", f"python3 -I '-c' {WRITE}",
    "echo \"open('invariants'+'.lock','w')\" | python3 -I -", "cat x.py | python3 -u - invariants.lock",
    f"cd /tmp && python3 -O -c {WRITE}",
])
def test_python_code_from_a_string_is_opaque(command):
    from invariants.cli import hooks
    assert hooks._python_code(command), command
    assert hooks._shell_writes(command) == "invariants.lock", command


@pytest.mark.parametrize("command", [
    "python3 app.py -c lock", "python3 -m pytest -c pytest.ini", "python3 -I -m pytest -c x", "python3 -u app.py",
    "python3 -W ignore app.py -c", "pytest -c lock.ini",
])
def test_python_running_a_script_or_module_is_not_code_from_a_string(command):
    from invariants.cli import hooks
    assert not hooks._python_code(command), command


def test_the_pretool_hook_blocks_python_isolated_code(tmp_path):
    r = subprocess.run([sys.executable, "-m", "invariants.cli", "hook", "pretool"], cwd=tmp_path, capture_output=True,
                       text=True, timeout=60, input=json.dumps({"tool_name": "Bash", "tool_input": {
                           "command": f"python3 -Ic {WRITE}"}}))
    assert r.returncode == 2, r.stdout + r.stderr


# --- 4. YAML aliases ----------------------------------------------------------------------------
BOMB = "spec: invariant/v0.1\nrules:\n  - id: R\n    door: \"py:a.b\"\n    sink: \"py:a.c\"\n    extra: &top\n" + "".join(
    f"      a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10) if i else ', '.join(['x'] * 10)}]\n" for i in range(9))


@pytest.mark.parametrize("text,line,kind", [
    (BOMB, 6, "an anchor"),
    ("rules:\n  - &r {id: R, guard: \"py:a.b\", bad: \"args.x\"}\n  - *r\n", 2, "an anchor"),
    ("base: &b {x: 1}\nrules:\n  - <<: *b\n    id: R\n", 1, "an anchor"),
])
def test_a_rule_file_with_yaml_aliases_is_refused_fast(tmp_path, text, line, kind):
    import time
    from invariants.errors import LoadError
    from invariants.loader import read_document
    f = tmp_path / "invariants.yaml"
    f.write_text(text)
    t = time.monotonic()
    with pytest.raises(LoadError, match=f"invariants.yaml:{line}: {kind} \\(YAML &name or \\*name\\) is not allowed"):
        read_document(f)
    assert time.monotonic() - t < 2
    r = run_inv("validate", f)
    assert r.returncode == 1 and "is not allowed in a rule file" in r.stderr


def test_an_alias_bomb_in_an_openapi_yaml_contract_is_refused(tmp_path):
    from invariants.errors import LoadError
    from invariants.lock import contract_hash
    lines = ["openapi: 3.0.0", "paths: {}", "x-bomb:"] + [
        f"  a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10) if i else ', '.join(['x'] * 10)}]" for i in range(9)]
    (tmp_path / "api.yaml").write_text("\n".join(lines) + "\n")
    with pytest.raises(LoadError, match="once its YAML aliases are expanded"):
        contract_hash("api", {"type": "openapi", "ref": "api.yaml"}, tmp_path, ["getX"])
    (tmp_path / "ok.yaml").write_text("openapi: 3.0.0\npaths: {}\nx-a: &a {k: 1}\nx-b: *a\n")
    assert "hash" in contract_hash("ok", {"type": "openapi", "ref": "ok.yaml"}, tmp_path)


# --- A, B. `inv lock` pins only a rule file validate accepts; the loader's words come first ----------
def _fleet(tmp_path: pathlib.Path, accept: str) -> pathlib.Path:
    files = dict(FLEET)
    files["r28fleet/retry.py"] = "from r28fleet import store\n\n\ndef retry(conn, n, name):\n    getattr(store, name)(conn, n)\n"
    files["invariants.yaml"] = FLEET["invariants.yaml"].replace("door: \"py:r28fleet.service.resize\"}",
                                                                 "door: \"py:r28fleet.service.resize\"" + accept + "}")
    return write_project(tmp_path / "f", files)


@pytest.mark.parametrize("accept", [
    ', allow_unresolved: [{at: "nowhere", code: bypass, sha: "zz", why: ""}]',
    ', allow_unresolved: [{at: "r28fleet/retry.py:5:4", code: unresolved/computed-attribute, sha: "f85270fe02", '
    'why: "reviewed"}]',
])
def test_lock_refuses_a_rule_file_validate_rejects(tmp_path, accept):
    root = _fleet(tmp_path, accept)
    for args in (("lock",), ("lock", "--accept-weakening")):
        r = run_inv(*args, cwd=root)
        assert r.returncode == 1 and r.stderr.startswith("invalid: "), r.stdout + r.stderr
        assert "`inv lock` pins only a rule file `inv validate` accepts" in r.stderr
        assert "refused:" not in r.stderr and not (root / "invariants.lock").exists()
    v = run_inv("validate", cwd=root)
    lines = v.stderr.splitlines()
    assert v.returncode == 1 and lines[0] == "invalid: invariants.yaml"
    assert lines[1].startswith("  scan: F-1: mediation.allow_unresolved[0]"), lines
    assert all(len(line) <= 202 for line in lines if "schema:" in line), lines
    assert [i for i, x in enumerate(lines) if "schema:" in x] > [1]


# --- C. unresolved sites: accept them in allow_unresolved, not allow -------------------------------
def test_unresolved_findings_say_how_to_accept_a_reviewed_site(tmp_path):
    root = _fleet(tmp_path, "")
    assert run_inv("lock", cwd=root).returncode == 0
    s = run_inv("status", cwd=root)
    assert "allow_unresolved (inv scan --format json gives its sha)" in s.stdout, s.stdout
    assert "freeze a known one in allow:" not in s.stdout
    c = run_inv("check", "--strict-scan", cwd=root)   # unresolved sites block only when strict
    assert c.returncode == 1 and "FAIL  scan" in c.stdout
    assert "hint: a person who reviewed an unresolved site can accept it in mediation.allow_unresolved" in c.stdout


def test_a_bypass_still_says_allow(tmp_path):
    root = write_project(tmp_path / "f", FLEET)
    assert run_inv("lock", cwd=root).returncode == 0
    s = run_inv("status", cwd=root)
    assert "freeze a known one in allow:" in s.stdout and "allow_unresolved (inv scan" not in s.stdout, s.stdout
    assert "hint:" not in run_inv("check", cwd=root).stdout


# --- D. init names door drafts only when it wrote one ----------------------------------------------
def test_init_mentions_door_drafts_only_when_there_are_some(tmp_path):
    root = write_project(tmp_path / "i", {"r28init/__init__.py": "", "r28init/calc.py": "def add(a, b):\n    return a + b\n"})
    r = run_inv("init", "--root", root, cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no path law" in r.stdout and "commented-out door drafts" not in r.stdout, r.stdout
    from invariants.cli import commands
    f = tmp_path / "x.yaml"
    f.write_text("rules:\n  # not written as a rule: why\n  # - id: DOOR-1\n  #   door: \"py:a.b\"\n")
    assert commands._has_door_draft(f)
    f.write_text("rules:\n  # - id: R-1\n  #   guard: \"py:a.b\"\n")
    assert not commands._has_door_draft(f)


# --- E. allow_unresolved changes are listed entry by entry -----------------------------------------
def _e(at: str, sha: str = "0123456789abcdef", why: str = "reviewed") -> dict:
    return {"at": at, "code": "unresolved/reflection", "sha": sha, "why": why}


def test_allow_unresolved_changes_name_each_entry():
    from invariants.lock.rule_diff import _accept_changes
    a, b = _e("m.py:3:1"), _e("n.py:9:2", "fedcba9876543210")
    dup = [str(c) for c in _accept_changes("rule R", [a, b], [a, b, a])]
    assert dup == ["rule R: mediation.allow_unresolved: another entry for m.py:3:1 (unresolved/reflection, "
                   "sha 0123456789abcdef)  WEAKENED (an unresolved site is now accepted without the scan's proof: "
                   "m.py:3:1 (unresolved/reflection) (2 entries for it instead of 1: each accepts one finding of "
                   "that statement))"], dup
    assert "(none) ->" not in dup[0]
    gone = [str(c) for c in _accept_changes("rule R", [a, b], [a])]
    assert gone == ["rule R: mediation.allow_unresolved: removed n.py:9:2 (unresolved/reflection, sha "
                    "fedcba9876543210) (n.py:9:2 (unresolved/reflection) is no longer accepted: the scan reports "
                    "it again)"], gone
    moved = [str(c) for c in _accept_changes("rule R", [a, b], [_e("m.py:5:1"), b])]
    assert moved == ["rule R: mediation.allow_unresolved: moved m.py:3:1 -> m.py:5:1 (unresolved/reflection, sha "
                     "0123456789abcdef) (the code above it moved)"], moved
    why = [str(c) for c in _accept_changes("rule R", [a], [_e("m.py:3:1", why="looked again")])]
    assert why[0].startswith("rule R: mediation.allow_unresolved: new reason for m.py:3:1") and "WEAKENED" not in why[0]
    bad = _accept_changes("rule R", [a], [a, {"at": "x"}])
    assert len(bad) == 1 and bad[0].weakened and "allow_unresolved[1] (new)" in str(bad[0])
