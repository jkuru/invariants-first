"""Diff-first scan gating: two tiers in `inv check`'s scan step, and with --base only new unresolved sites block.

1. Bypass, forbidden and missing findings block; an unresolved site (a call the scan cannot follow) is a
   WARN line and the step passes, unless the rule says mediation.strict: true or `inv check --strict-scan`.
2. With --base REF the scan also runs on REF's copy of the scan roots (git archive), with the rule file as
   it is now; an unresolved site not on REF blocks ("new since REF"), one on REF warns ("on REF"). Sites
   are a multiset of (rule, code, file, sha): code that only moved is the same site.
3. A base that cannot be read fails closed: every unresolved site blocks.
4. An ablated enforce rule still fails the step; `inv scan` keeps its own exit codes.
5. The MCP check tool and the stop hook read the new verdict: warnings never block them.
6. mediation.strict: true -> false (or removed) is WEAKENED in the lock; false -> true is a tightening.
7. `inv check --metrics FILE` appends one JSON line per run.
8. `inv init` writes a rule whose findings are all unresolved active, the sites as comments; it drafts
   allow_unresolved entries only with --strict-scan.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from cli_support import run_inv, write_project

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
GIT_ENV.pop("GIT_DIR", None)
GIT_ENV.pop("GIT_WORK_TREE", None)

FILES = {
    "df/__init__.py": "",
    "df/store.py": "def set_size(conn, n):\n    conn.execute(n)\n",
    "df/service.py": "from df import store\n\n\ndef resize(conn, n: int) -> None:\n    store.set_size(conn, n)\n",
    "df/retry.py": "from df import store\n\n\ndef retry(conn, n, name):\n    getattr(store, name)(conn, n)\n",
}
RULES = 'rules:\n  - id: F-1\n    door: "py:df.service.resize"\n    sink: "py:df.store.set_size"\n'
NEW_SITE = "from df import store\n\n\ndef again(conn, n, name):\n    getattr(store, name + 'x')(conn, n)\n"
BYPASS = "from df import store\n\n\ndef rogue(conn):\n    store.set_size(conn, 1)\n"


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, env=GIT_ENV, capture_output=True)


def _repo(tmp_path: pathlib.Path, rules: str = RULES, files: dict[str, str] | None = None) -> pathlib.Path:
    """A git repo on branch main: the project with one unresolved site (df/retry.py), locked and committed."""
    root = write_project(tmp_path / "p", {**FILES, **(files or {}), "invariants.yaml": rules})
    assert run_inv("lock", cwd=root).returncode == 0
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _check(root: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run_inv("check", *args, cwd=root)


# --- 1. tiers -------------------------------------------------------------------------------------
def test_an_unresolved_site_warns_and_the_check_passes(tmp_path):
    root = write_project(tmp_path / "p", {**FILES, "invariants.yaml": RULES})
    assert run_inv("lock", cwd=root).returncode == 0
    c = _check(root)
    assert c.returncode == 0 and c.stdout.rstrip().endswith("pass"), c.stdout
    assert "  ok    scan: no path skips a door or guard; 1 unresolved site(s) to review" in c.stdout
    warn = [line for line in c.stdout.splitlines() if line.strip().startswith("WARN")]
    assert len(warn) == 1 and "df/retry.py:5:5" in warn[0] and "unresolved/computed-attribute" in warn[0], warn
    # `inv scan` itself is unchanged: exit 3 on an unresolved finding
    assert run_inv("scan", cwd=root).returncode == 3


def test_a_bypass_still_blocks_beside_a_warning(tmp_path):
    root = write_project(tmp_path / "p", {**FILES, "df/rogue.py": BYPASS, "invariants.yaml": RULES})
    assert run_inv("lock", cwd=root).returncode == 0
    c = _check(root)
    assert c.returncode == 1 and "FAIL  scan" in c.stdout, c.stdout
    assert any(line.strip().startswith("bypass ") and "df/rogue.py" in line for line in c.stdout.splitlines())
    assert any(line.strip().startswith("WARN  unresolved") for line in c.stdout.splitlines()), c.stdout


def test_strict_scan_blocks_every_unresolved_site(tmp_path):
    root = write_project(tmp_path / "p", {**FILES, "invariants.yaml": RULES})
    assert run_inv("lock", cwd=root).returncode == 0
    c = _check(root, "--strict-scan")
    assert c.returncode == 1 and "FAIL  scan" in c.stdout and "[strict]" in c.stdout, c.stdout
    assert "WARN" not in c.stdout


@pytest.mark.parametrize("rules", [RULES + "    strict: true\n",
                                   RULES.replace('    door:', '    mediation: {strict: true}\n    door:')])
def test_a_strict_rule_blocks_every_unresolved_site(tmp_path, rules):
    root = write_project(tmp_path / "p", {**FILES, "invariants.yaml": rules})
    assert run_inv("validate", cwd=root).returncode == 0
    assert run_inv("lock", cwd=root).returncode == 0
    c = _check(root)
    assert c.returncode == 1 and "FAIL  scan" in c.stdout and "[strict]" in c.stdout, c.stdout


def test_strict_on_one_rule_leaves_the_other_advisory(tmp_path):
    two = RULES + "    strict: true\n" + RULES.replace("F-1", "F-2").replace("rules:\n", "")
    root = write_project(tmp_path / "p", {**FILES, "invariants.yaml": two})
    assert run_inv("lock", cwd=root).returncode == 0
    c = _check(root)
    assert c.returncode == 1, c.stdout
    lines = c.stdout.splitlines()
    assert any("F-1" in x and "[strict]" in x and "WARN" not in x for x in lines), c.stdout
    assert any(x.strip().startswith("WARN  unresolved F-2") for x in lines), c.stdout


def _gate(root: pathlib.Path, **kw: object) -> tuple[int, str, dict]:
    from invariants.cli.common import _captured
    from invariants.cli.scan_gate import cmd_scan_gate
    ns = argparse.Namespace(rulefile=str(root / "invariants.yaml"), root=[str(root)], exclude=[],
                            base=kw.get("base"), strict_scan=kw.get("strict_scan", False), gate={})
    code, text = _captured(cmd_scan_gate, ns)
    return code, text, ns.gate


def test_an_ablated_enforce_rule_still_fails_the_scan_step(tmp_path, monkeypatch):
    import invariants_scan.hooks as hooks
    root = write_project(tmp_path / "p", {**FILES, "invariants.yaml": RULES})
    code, text, _ = _gate(root)
    assert code == 0, text
    monkeypatch.setattr(hooks, "ablated", lambda: {"F-1"})
    code, text, _ = _gate(root)
    assert code == 1 and "ablated" in text and "not a pass" in text, text


def test_rule_strict_must_be_a_boolean(tmp_path):
    root = write_project(tmp_path / "p", {**FILES, "invariants.yaml": RULES + "    strict: yes please\n"})
    v = run_inv("validate", cwd=root)
    assert v.returncode == 1 and "strict" in v.stdout + v.stderr, v.stdout + v.stderr


def test_the_loader_refuses_a_strict_that_is_not_a_boolean():
    from invariants.errors import LoadError
    from invariants.loader.shapes import _check_mediation
    _check_mediation("F-1", {"mediation": {"sink": "py:a.s", "door": "py:a.d", "strict": True}})
    with pytest.raises(LoadError, match="mediation.strict must be true or false"):
        _check_mediation("F-1", {"mediation": {"sink": "py:a.s", "door": "py:a.d", "strict": "yes"}})


def test_the_base_copy_skips_links_and_names_that_leave_it(tmp_path):
    import io
    import tarfile
    from invariants.cli.scan_gate import _extract
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in (("ok/a.py", b"x = 1\n"), ("../evil.py", b"bad\n"), ("/abs.py", b"bad\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("ok/link.py")
        link.type, link.linkname = tarfile.SYMTYPE, "../../outside.py"
        tar.addfile(link)
    dest = tmp_path / "dest"
    dest.mkdir()
    _extract(buf.getvalue(), dest)
    assert (dest / "ok" / "a.py").read_text() == "x = 1\n"
    assert not (tmp_path / "evil.py").exists() and not (dest / "ok" / "link.py").exists()
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*")) == ["ok", "ok/a.py"]


def test_the_short_form_strict_expands_into_mediation():
    from invariants.loader import expand_short_forms
    doc = expand_short_forms({"rules": [{"id": "F-1", "door": "py:a.d", "sink": "py:a.s", "strict": True}]})
    assert doc["rules"][0]["mediation"]["strict"] is True and "strict" not in doc["rules"][0]


# --- 2. diff-first with --base ----------------------------------------------------------------------
@needs_git
def test_a_site_on_the_base_warns_with_base(tmp_path):
    root = _repo(tmp_path)
    c = _check(root, "--base", "main")
    assert c.returncode == 0, c.stdout
    assert "on main, not new" in c.stdout and "[on main]" in c.stdout, c.stdout


@needs_git
def test_a_new_site_since_the_base_blocks(tmp_path):
    root = _repo(tmp_path)
    (root / "df" / "again.py").write_text(NEW_SITE)
    c = _check(root, "--base", "main")
    assert c.returncode == 1 and "FAIL  scan" in c.stdout, c.stdout
    lines = c.stdout.splitlines()
    assert any("df/again.py" in x and "[new since main]" in x and "WARN" not in x for x in lines), c.stdout
    assert any("df/retry.py" in x and "[on main]" in x and x.strip().startswith("WARN") for x in lines), c.stdout
    # without --base the same tree warns only
    assert _check(root).returncode == 0


@needs_git
def test_a_site_that_only_moved_is_still_on_the_base(tmp_path):
    root = _repo(tmp_path)
    text = (root / "df" / "retry.py").read_text()
    (root / "df" / "retry.py").write_text("# a comment\n\n\ndef helper():\n    return 1\n\n\n" + text)
    c = _check(root, "--base", "main")
    assert "df/retry.py:12:5" in c.stdout and "[on main]" in c.stdout, c.stdout
    assert c.returncode == 0, c.stdout


@needs_git
def test_a_second_copy_of_a_base_site_is_new(tmp_path):
    # the same statement in the same function twice: one is on the base, the other is new (a multiset)
    root = _repo(tmp_path)
    (root / "df" / "retry.py").write_text(FILES["df/retry.py"] + "    getattr(store, name)(conn, n)\n")
    c = _check(root, "--base", "main")
    assert c.returncode == 1 and c.stdout.count("[new since main]") == 1 and c.stdout.count("[on main]") == 1, \
        c.stdout


@needs_git
def test_an_edited_site_is_new(tmp_path):
    root = _repo(tmp_path)
    (root / "df" / "retry.py").write_text(FILES["df/retry.py"].replace("(conn, n)", "(conn, n + 1)"))
    c = _check(root, "--base", "main")
    assert c.returncode == 1 and "[new since main]" in c.stdout, c.stdout


@needs_git
def test_a_strict_rule_blocks_a_site_on_the_base(tmp_path):
    root = _repo(tmp_path, RULES + "    strict: true\n")
    c = _check(root, "--base", "main")
    assert c.returncode == 1 and "[on main]  [strict]" in c.stdout, c.stdout
    root2 = _repo(tmp_path / "two")
    c = _check(root2, "--base", "main", "--strict-scan")
    assert c.returncode == 1 and "[on main]  [strict]" in c.stdout, c.stdout


@needs_git
def test_an_accepted_site_never_counts(tmp_path):
    root = _repo(tmp_path)
    r = run_inv("scan", "--format", "json", cwd=root)
    sha = json.loads(r.stdout)["findings"][0]["sha"]
    (root / "df" / "again.py").write_text(NEW_SITE)
    r = run_inv("scan", "--format", "json", cwd=root)
    new_sha = next(f["sha"] for f in json.loads(r.stdout)["findings"] if f["file"] == "df/again.py")
    (root / "invariants.yaml").write_text(
        RULES + f'    allow_unresolved:\n      - {{at: "df/again.py:5:5", code: unresolved/computed-attribute, '
                f'sha: "{new_sha}", why: "reviewed"}}\n')
    assert run_inv("lock", "--accept-weakening", cwd=root).returncode == 0
    c = _check(root, "--base", "main", "--accept-weakening")
    assert c.returncode == 0, c.stdout
    assert "1 reviewed unresolved site(s) accepted" in c.stdout and "[on main]" in c.stdout, c.stdout
    assert sha != new_sha


# --- 3. fail closed ---------------------------------------------------------------------------------
@needs_git
def test_an_unknown_base_fails_closed(tmp_path):
    root = _repo(tmp_path)
    code, text, gate = _gate(root, base="no-such-ref")
    assert code == 1 and "[no-such-ref unreadable]" in text and "fail closed" in text, text
    assert "fetch-depth: 0" in text and gate["new"] == 1


@needs_git
def test_a_shallow_clone_without_the_base_fails_closed(tmp_path):
    root = _repo(tmp_path)
    (root / "df" / "noop.py").write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "two")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{root}", str(clone))
    c = _check(clone, "--base", "HEAD~1")
    assert c.returncode == 1 and "FAIL  scan" in c.stdout and "fail closed" in c.stdout, c.stdout


@needs_git
def test_a_root_outside_the_repository_fails_closed(tmp_path):
    from invariants.cli.git_base import BaseUnavailable
    from invariants.cli.scan_gate import base_sites
    from invariants.loader import read_document
    root = _repo(tmp_path)
    outside = write_project(tmp_path / "outside", {"o.py": "x = 1\n"})
    with pytest.raises(BaseUnavailable, match="outside the git repository"):
        base_sites(root / "invariants.yaml", "main", [outside], [], read_document(root / "invariants.yaml"))


@needs_git
def test_a_root_the_base_lacks_makes_its_sites_new(tmp_path):
    root = _repo(tmp_path, files={"pyproject.toml": "[project]\nname = 'df'\n"})
    (root / "extra").mkdir()
    (root / "extra" / "more.py").write_text(NEW_SITE)
    from invariants.cli.common import _captured
    from invariants.cli.scan_gate import cmd_scan_gate
    ns = argparse.Namespace(rulefile=str(root / "invariants.yaml"), root=[str(root), str(root / "extra")],
                            exclude=[], base="main", strict_scan=False, gate={})
    code, text = _captured(cmd_scan_gate, ns)
    assert code == 1 and "more.py:5:5" in text and "[new since main]" in text, text


# --- 5. callers -------------------------------------------------------------------------------------
def test_the_mcp_check_tool_passes_with_warnings(tmp_path):
    from invariants.mcp import tools
    root = write_project(tmp_path / "p", {**FILES, "invariants.yaml": RULES})
    assert run_inv("lock", cwd=root).returncode == 0
    got = tools.tool_check(root.resolve(), None, {})
    assert got["passed"] is True and got["exit_code"] == 0, got
    assert len(got["warnings"]) == 1 and "df/retry.py" in got["warnings"][0], got
    scan = next(s for s in got["steps"] if s["step"] == "scan")
    assert scan["result"] == "ok"
    (root / "df" / "rogue.py").write_text(BYPASS)
    got = tools.tool_check(root.resolve(), None, {})
    assert got["passed"] is False and got["warnings"], got


def test_the_stop_hook_is_not_blocked_by_a_warning(tmp_path):
    root = write_project(tmp_path / "p", {**FILES, "invariants.yaml": RULES})
    assert run_inv("lock", cwd=root).returncode == 0

    def hook() -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "invariants.cli", "hook", "stop"],
                              input=json.dumps({"cwd": str(root)}), capture_output=True, text=True, cwd=root,
                              timeout=60)
    h = hook()
    assert h.returncode == 0, h.stderr
    (root / "df" / "rogue.py").write_text(BYPASS)
    h = hook()
    assert h.returncode == 2 and "bypass" in h.stderr, h.stderr


# --- 6. the lock ------------------------------------------------------------------------------------
@pytest.mark.parametrize("old,new,weak", [("true", "false", True), ("true", None, True),
                                          ("false", "true", False), (None, "true", False)])
def test_strict_turned_off_is_weakened(tmp_path, old, new, weak):
    def rules(v: str | None) -> str:
        return RULES + (f"    strict: {v}\n" if v is not None else "")
    root = write_project(tmp_path / "p", {**FILES, "invariants.yaml": rules(old)})
    assert run_inv("lock", cwd=root).returncode == 0
    (root / "invariants.yaml").write_text(rules(new))
    c = run_inv("lock", "--check", cwd=root)
    assert c.returncode == 1 and "mediation.strict" in c.stderr, c.stderr
    assert ("WEAKENED" in c.stderr) is weak, c.stderr
    w = run_inv("lock", cwd=root)
    assert (w.returncode != 0) is weak, w.stdout + w.stderr


def test_strict_false_and_left_out_are_the_same():
    from invariants.lock.rule_diff import _mediation_changes
    assert _mediation_changes("rule F-1", {"sink": "py:a.s", "strict": False}, {"sink": "py:a.s"}) == []


# --- 7. metrics -------------------------------------------------------------------------------------
@needs_git
def test_metrics_appends_one_line_per_run(tmp_path):
    root = _repo(tmp_path)
    (root / "df" / "again.py").write_text(NEW_SITE)
    m = tmp_path / "out" / "metrics.jsonl"
    m.parent.mkdir()
    assert _check(root, "--base", "main", "--metrics", str(m)).returncode == 1
    assert _check(root, "--metrics", str(m)).returncode == 0
    lines = [json.loads(x) for x in m.read_text().splitlines()]
    assert len(lines) == 2
    first, second = lines
    assert first["base"] == "main" and first["scan_new_unresolved"] == 1 and first["scan_legacy_unresolved"] == 1
    assert first["scan_blocking"] == 0 and first["accepted"] == 0 and first["scan_unresolved"] == 2
    assert first["value_weakenings"] == {"schema": 0, "rule": 0}
    assert "T" in first["time"] and first["time"].endswith("+00:00")
    assert second["base"] is None and second["scan_new_unresolved"] is None and second["scan_unresolved"] == 2


@needs_git
def test_metrics_count_weakenings_since_the_base(tmp_path):
    root = _repo(tmp_path, RULES + "    strict: true\n")
    (root / "invariants.yaml").write_text(RULES)
    assert run_inv("lock", "--accept-weakening", cwd=root).returncode == 0
    m = tmp_path / "m.jsonl"
    _check(root, "--base", "main", "--metrics", str(m))
    line = json.loads(m.read_text())
    assert line["value_weakenings"] == {"schema": 0, "rule": 1}, line


def test_metrics_count_weakenings_against_the_stored_lock_without_base(tmp_path):
    root = write_project(tmp_path / "p", {**FILES, "invariants.yaml": RULES + "    strict: true\n"})
    assert run_inv("lock", cwd=root).returncode == 0
    (root / "invariants.yaml").write_text(RULES)
    m = tmp_path / "m.jsonl"
    assert _check(root, "--metrics", str(m)).returncode == 1          # the lock is stale: a weakening
    line = json.loads(m.read_text())
    assert line["value_weakenings"] == {"schema": 0, "rule": 1} and line["base"] is None, line


def test_metrics_counts_blocking_findings(tmp_path):
    root = write_project(tmp_path / "p", {**FILES, "df/rogue.py": BYPASS, "invariants.yaml": RULES})
    assert run_inv("lock", cwd=root).returncode == 0
    m = tmp_path / "m.jsonl"
    assert _check(root, "--metrics", str(m)).returncode == 1
    line = json.loads(m.read_text())
    assert line["scan_blocking"] == 1 and line["scan_unresolved"] == 1, line


def test_metrics_on_an_unwritable_path_is_a_clear_error(tmp_path):
    root = write_project(tmp_path / "p", {**FILES, "invariants.yaml": RULES})
    assert run_inv("lock", cwd=root).returncode == 0
    c = _check(root, "--metrics", str(tmp_path / "no" / "such" / "dir" / "m.jsonl"))
    assert c.returncode == 2 and "--metrics: cannot append to" in c.stdout + c.stderr, c.stdout + c.stderr


def test_metrics_split_schema_and_rule_weakenings():
    from invariants import lock
    from invariants.cli.commands import write_metrics
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = pathlib.Path(d) / "m.jsonl"
        write_metrics(str(f), "main", [lock.Change("schema m.Row", "x", weakened=True),
                                       lock.Change("contract api", "x", weakened=True),
                                       lock.Change("rule R-1", "bad", weakened=True),
                                       lock.Change("rule R-2", "rev")], {"blocking": 2, "accepted": 1})
        line = json.loads(f.read_text())
    assert line["value_weakenings"] == {"schema": 2, "rule": 1} and line["scan_blocking"] == 2 and line["accepted"] == 1


# --- 8. inv init ------------------------------------------------------------------------------------
INIT = {
    "app/__init__.py": "", "app/repo.py": "class OrderRepo:\n    def save_order(self, o):\n        pass\n",
    "app/order_service.py": "from app.repo import OrderRepo\ndef place(o):\n    OrderRepo().save_order(o)\n"
                            "def place2(o):\n    OrderRepo().save_order(o)\n",
    "app/api.py": "from app.order_service import place, place2\ndef a(o):\n    place(o)\ndef b(o):\n    place2(o)\n",
    "app/plug.py": "import importlib\ndef load(n):\n    importlib.import_module(n)\n"}


def test_init_writes_a_rule_with_only_unresolved_sites_active(tmp_path):
    from invariants import doors
    root = write_project(tmp_path, INIT)
    found = doors.discover([root])
    d = found.doors[0]
    assert d.back_doors == [] and d.unresolved and d.active, d
    text = "\n".join(doors.render(found))
    assert f"\n  - id: {d.rid}" in "\n" + text and "# not written as a rule" not in text, text
    assert "unresolved site(s) to review" in text and "allow_unresolved" not in text, text
    assert doors.render_shared(found) == []
    doors.apply_strict(found)
    text = "\n".join(doors.render(found))
    assert not d.active and "# not written as a rule" in text and 'why: ""' in text, text


def test_init_drafts_no_shared_entry_without_strict_scan(tmp_path):
    from invariants import doors
    root = write_project(tmp_path, {
        "app/__init__.py": "",
        "app/repo.py": "class OrderRepo:\n    def save_order(self, o):\n        pass\n"
                       "class UserRepo:\n    def save_user(self, u):\n        pass\n",
        "app/order_service.py": "from app.repo import OrderRepo\ndef place(o):\n    OrderRepo().save_order(o)\n",
        "app/user_service.py": "from app.repo import UserRepo\ndef join(u):\n    UserRepo().save_user(u)\n",
        "app/api.py": "from app.order_service import place\nfrom app.user_service import join\n"
                      "def a(o):\n    place(o)\ndef b(o):\n    place(o)\ndef c(u):\n    join(u)\ndef d(u):\n    join(u)\n",
        "app/plug.py": "import importlib\ndef load(n):\n    importlib.import_module(n)\n"})
    found = doors.discover([root])
    assert len(found.doors) == 2 and all(d.active and d.unresolved for d in found.doors), found.doors
    body = "\n".join(doors.render(found))
    assert doors.render_shared(found) == [] and "allow_unresolved" not in body, body
    doors.apply_strict(found)
    assert "rules: [" in "\n".join(doors.render_shared(found))


def test_inv_init_strict_scan_drafts_allow_unresolved(tmp_path):
    plain = write_project(tmp_path / "plain", INIT)
    r = run_inv("init", "--no-graphify", "--no-lock", cwd=plain)
    assert r.returncode == 0, r.stdout + r.stderr
    text = (plain / "invariants.yaml").read_text()
    assert "allow_unresolved" not in text and "\n  - id: " in text, text
    v = run_inv("lock", cwd=plain)
    assert v.returncode == 0, v.stdout + v.stderr
    assert _check(plain).returncode == 0
    strict = write_project(tmp_path / "strict", INIT)
    r = run_inv("init", "--no-graphify", "--no-lock", "--strict-scan", cwd=strict)
    assert r.returncode == 0, r.stdout + r.stderr
    text = (strict / "invariants.yaml").read_text()
    assert "allow_unresolved:" in text and 'why: ""' in text and "# not written as a rule" in text, text


# --- end to end: a copy of apps/pool-service ---------------------------------------------------------
POOL = pathlib.Path(__file__).resolve().parents[2] / "apps" / "pool-service"
LEGACY = "def legacy(mod, conn):\n    getattr(mod, 'insert_' + 'lease')(conn, 'p', 'h', 1)\n"
UNTYPED = "def poke(m, conn):\n    m.insert_lease(conn, 'p', 'h', 1)\n"


@needs_git
@pytest.mark.skipif(not (POOL / "invariants.yaml").is_file(), reason="needs the monorepo's apps/")
def test_pool_service_end_to_end(tmp_path):
    pytest.importorskip("fastapi")
    root = tmp_path / "pool"
    shutil.copytree(POOL, root, ignore=shutil.ignore_patterns(".venv", "__pycache__", "build", "*.egg-info"))
    (root / "src" / "pool_service" / "legacy.py").write_text(LEGACY)
    env = {**GIT_ENV, "PYTHONPATH": str(root / "src")}

    def check(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "invariants.cli", "check", *args], cwd=root, env=env,
                              capture_output=True, text=True, timeout=300)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "legacy site")
    c = check("--base", "main")
    assert c.returncode == 0 and "[on main]" in c.stdout and "WARN" in c.stdout, c.stdout + c.stderr
    (root / "src" / "pool_service" / "poke.py").write_text(UNTYPED)
    c = check("--base", "main")
    assert c.returncode == 1 and "FAIL  scan" in c.stdout, c.stdout + c.stderr
    assert any("pool_service/poke.py" in x and "[new since main]" in x for x in c.stdout.splitlines()), c.stdout
