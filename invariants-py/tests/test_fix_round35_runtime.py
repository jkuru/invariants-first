"""Fix round 35 (external audit), runtime: each test failed before its fix (or pins a rejected claim).

H1. An @inv.edge state annotated with a TypedDict took any object (a TypedDict is a dict subclass, so
    _check_state returned early), and _with_field copied a dataclass posing as the dict with its own
    reads intact. A TypedDict state is now a dict, else a TypeError.
H2. inv verify failed every model_call rule without model_policy.schema (None != the schema's name).
H3. Under `inv mcp` (INV_CONFINE_ROOT), inv check's source read of the guards (static_guards) parsed
    modules outside --root (a symlink, a namespace portion, the standard library) and named what it
    found there. In practice the rules step failed first and hid the echo; the read itself is now
    confined as the child's imports are.
- monitor() set ev["key"] after report() had handed the event to observers and exporters.
- falsified_by was not checked to be a list of test ids at load.
- `model_policy: {}` (not changed): refused at load on purpose, pinned in round 33 (M8).
- `inv mcp <dir>/invariants.yaml --root <dir>` joined the rule file to --root twice.
- propose_rules reported "pass" over an unresolved scan.
- action.yml kept a Windows python.exe's \\r in --site (and --path) values.
- inv init drafted private (_-prefixed) pydantic models, which a pydantic contract ref cannot name.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil
import subprocess
from typing import Any

import pytest

import invariants as inv
import invariants.testing
import round24 as r24

REPO = pathlib.Path(__file__).resolve().parents[2]


# --- H1: a TypedDict-annotated edge state is a dict, never a dataclass or model posing as one --------
def test_a_typeddict_state_refuses_a_dataclass_posing_as_it():
    with inv.isolated():
        inv.load(r24.plan_doc())
        good = r24.plan()
        forged = inv.testing.make_checked(r24.CPlan, {"action": "delete", "n": 1})

        @dataclasses.dataclass
        class Pose:
            plan: Any

            def __getitem__(self, k: Any) -> Any:
                return forged
        r24.SEEN.clear()
        with pytest.raises(TypeError, match="accepts only a dict state"):
            r24.act_state(Pose(good))
        assert r24.SEEN == []
        r24.act_state({"plan": good})
        assert r24.SEEN == [(dict, {"action": "read", "n": 1})]


# --- H2: model_policy.schema is optional; verify does not demand it ---------------------------------
def test_verify_accepts_a_model_call_rule_without_model_policy_schema():
    with inv.isolated():
        reg = inv.load(r24.plan_doc())
        assert "schema" not in reg.rules["P"].policy
        assert inv.verify(reg, modules=()) >= 1
        doc = r24.plan_doc()
        doc["rules"][0]["model_policy"]["schema"] = "Nope"
        reg = inv.load(doc)
        with pytest.raises(inv.CoverageError, match="model_policy.schema is 'Nope'"):
            inv.verify(reg, modules=())


# --- monitor: observers and exporters get the row's key ---------------------------------------------
def test_monitor_events_carry_the_key_when_observers_see_them():
    import fixround as fx
    from invariants.registry import observe
    seen: list[Any] = []
    with inv.isolated() as iso:
        inv.load(fx.doc(fx.rule("D-2", "probe2", "big", fx.enf(view={"big": "args.obj"}),
                         fx.enf("monitor", timing="post_effect", mode="detect", view={"big": "args.x"}))))
        stop = observe(lambda rid, kind, ev: seen.append(dict(ev)))
        try:
            inv.monitor("D-2", [("a", {"args": {"x": True}})], iso)
        finally:
            stop()
    assert [e.get("key") for e in seen] == ["a"]


# --- loader: falsified_by is a list of test ids; model_policy: {} is a policy -----------------------
@pytest.mark.parametrize("bad", ["tests/test_x.py::test_a", [1], [""], {"a": 1}, ["ok", None]])
def test_falsified_by_must_be_a_list_of_test_ids(bad):
    doc = r24.doc("pay", "args.amount < 0")
    doc["rules"][0]["falsified_by"] = bad
    with inv.isolated(), pytest.raises(inv.LoadError, match="falsified_by must be a list"):
        inv.load(doc)


def test_an_empty_model_policy_is_still_refused_at_load():
    """Rejected claim: `model_policy: {}` is refused on purpose (round 33, M8), before any check where
    truthiness and `is not None` could disagree."""
    with inv.isolated():
        doc = r24.plan_doc()
        doc["rules"][0]["model_policy"] = {}
        with pytest.raises(inv.LoadError, match="needs model_policy"):
            inv.load(doc)


# --- mcp: `inv mcp <dir>/invariants.yaml --root <dir>` reads that file, not <dir>/<dir>/... ---------
def test_inv_mcp_rulefile_is_relative_to_the_current_directory(tmp_path, monkeypatch):
    import invariants.mcp.protocol as protocol
    from invariants.cli import main
    app = tmp_path / "app"
    app.mkdir()
    (app / "invariants.yaml").write_text("rules: []\n")
    got: list[Any] = []
    monkeypatch.setattr(protocol, "serve", lambda server, i, o: got.append(server))
    monkeypatch.chdir(tmp_path)
    assert main(["mcp", "app/invariants.yaml", "--root", "app"]) == 0
    assert got[0].rules == (app / "invariants.yaml").resolve()
    got.clear()
    assert protocol.main(["--root", "app", "--rules", "invariants.yaml"]) == 0      # inv-mcp: --rules under --root
    assert got[0].rules == (app / "invariants.yaml").resolve()


# --- mcp: propose_rules never reports pass over a scan that did not pass -----------------------------
@pytest.mark.parametrize("result", ["unresolved", "fail"])
def test_propose_rules_verdict_is_not_pass_when_a_draft_is_not_proved(tmp_path, monkeypatch, result):
    from cli_support import write_project
    from invariants.mcp.propose import tool_propose_rules
    from invariants_scan.contracts import Report
    from test_mcp import _bank
    root = _bank(tmp_path, rules=False)
    write_project(root, {"bank/batch.py": "from bank.store import write_balance\n\n"
                                          "def refund_all(conn, rows):\n    for a, c in rows:\n"
                                          "        write_balance(conn, a, c)\n"})
    monkeypatch.delenv("INV_ABLATE", raising=False)
    data = tool_propose_rules(root, None, {})
    assert data["scan"]["verdict"] == "pass" and data["door_rules"][0]["drafted"] == "rule"
    real = Report.as_dict

    def as_dict(self: Report) -> dict[str, Any]:     # what propose_rules reads; inv init's own check is unchanged
        d = real(self)
        for row in d["rules"].values():
            row["result"] = result
        return d
    monkeypatch.setattr(Report, "as_dict", as_dict)
    assert tool_propose_rules(root, None, {})["scan"]["verdict"] == result


# --- bootstrap: inv init drafts only the public models a pydantic contract ref can name ---------------
def test_init_never_drafts_a_private_model_as_a_contract(tmp_path):
    from cli_support import write_project
    from invariants import bootstrap
    from invariants.integrations.pydantic import check_contracts
    root = write_project(tmp_path, {
        "shop/__init__.py": "",
        "shop/models.py": "from pydantic import BaseModel, ConfigDict, Field\n\n\nclass _Req(BaseModel):\n"
                          "    model_config = ConfigDict(extra='forbid')\n    n: int = Field(gt=0)\n\n\nclass Pub(_Req):\n    m: int = Field(ge=1)\n"})
    found = bootstrap.discover(root, [root])
    assert [(m.module, m.name) for m in found.models] == [("shop.models", "Pub")]
    doc = {"spec": "invariant/v0.1", "rules": [],
           "contracts": {n: bootstrap.contract_record(m) for n, m in bootstrap.contracts(found)}}
    import sys
    sys.path.insert(0, str(root))
    try:
        assert set(check_contracts(doc, inv.Registry())) == {"pub"}
    finally:
        sys.path.remove(str(root))
        for k in [k for k in sys.modules if k.split(".")[0] == "shop"]:
            del sys.modules[k]


# --- action.yml: a Windows python.exe's \r\n never reaches --site or --path ----------------------------
@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_action_strips_carriage_returns_from_the_lines_it_reads(tmp_path):
    import yaml
    action = REPO / "action.yml"
    if not action.exists():
        pytest.skip("action.yml lives at the monorepo root, not in the sdist")
    run = yaml.safe_load(action.read_text())["runs"]["steps"][-1]["run"]
    gate = tmp_path / "invariants-gate" / "bin"
    gate.mkdir(parents=True)
    log = tmp_path / "argv"
    (gate / "python").write_text(f'#!/bin/bash\nprintf "%s|" "$@" > {log}\n')
    fake = tmp_path / "pyexe"
    fake.write_text("#!/bin/bash\nprintf 'C:/site/purelib\\r\\nC:/site/platlib\\r\\n'\n")
    for f in (gate / "python", fake):
        f.chmod(0o755)
    env = {"PATH": os.environ["PATH"], "RUNNER_TEMP": str(tmp_path), "INV_BASE": "", "INV_RULE_FILE": "",
           "INV_PYTHON": str(fake), "INV_PATH": "src\r\nlib\r\n"}
    r = subprocess.run(["bash", "-c", run], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert log.read_text() == ("-I|-m|invariants.cli|check|--path|src|--path|lib|"
                               "--site|C:/site/purelib|--site|C:/site/platlib|")


# --- H3: under `inv mcp`, inv check's source read of the guards stays inside --root ---------------------
GUARDED = "import invariants as inv\n\n\n@inv.guard('IN-1')\ndef pay(x):\n    return x\n"


def _probe_project(tmp_path: Any, kind: str) -> tuple[Any, dict[str, Any]]:
    from cli_support import write_project
    vault = tmp_path / "outside" / "vault"
    vault.mkdir(parents=True)
    (vault / "evil.py").write_text("def refund(x):\n    return x\n\n\ndef AKIA_SECRET(x):\n    return x\n")
    proj = write_project(tmp_path / "proj", {"app/__init__.py": "", "app/svc.py": GUARDED,
                                             "pyproject.toml": '[project]\nname = "p"\nversion = "0"\n'})
    if kind == "module file":
        (proj / "app" / "leak.py").symlink_to(vault / "evil.py")
        mod = "app.leak"
    else:                                         # a package directory
        (vault / "__init__.py").write_text("")
        (proj / "app" / "leak").symlink_to(vault, target_is_directory=True)
        mod = "app.leak.evil"
    rules = [{"id": f"P-{n}", "status": "enforce", "bad": "args.x > 1", "subject": {"target": f"py:{t}"},
              "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}]}
             for n, t in enumerate([f"{mod}.AKIA_SECRET", f"{mod}.nothere", "json.dumps"])]
    rules.append({"id": "IN-1", "status": "enforce", "bad": "args.x > 1", "subject": {"target": "py:app.svc.pay"},
                  "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}]})
    return proj, {"spec": "invariant/v0.1", "rules": rules}


@pytest.mark.parametrize("kind", ["module file", "package dir"])
def test_static_guards_never_read_a_module_outside_the_confined_root(tmp_path, monkeypatch, kind):
    from invariants.cli.static_guards import guard_problems
    from invariants.confinement import CONFINE_ENV
    proj, doc = _probe_project(tmp_path, kind)
    rule_file = proj / "invariants.yaml"
    rule_file.write_text("rules: []\n")
    n, open_problems = guard_problems(rule_file, doc, [])        # plain inv check: reads them, as before
    assert n == 4 and "line 5" in open_problems[0] and "no 'nothere'" in open_problems[1]
    monkeypatch.setenv(CONFINE_ENV, str(proj))
    n, problems = guard_problems(rule_file, doc, [])
    assert n == 4 and len(problems) == 3                         # IN-1, inside the root, still passes
    assert all("not read" in p and "fails closed" in p for p in problems), problems
    assert "line" not in problems[0] and "nothere'" not in problems[1]   # nothing of the outside file echoed
    assert problems[0].replace("AKIA_SECRET", "X").split(":", 1)[1] == \
        problems[1].replace("nothere", "X").split(":", 1)[1]


def test_static_guards_never_follow_a_reexport_into_a_namespace_portion_outside_the_root(tmp_path, monkeypatch):
    """A namespace package with a portion inside the root is the project's (ConfinedFinder's
    _project_package): its submodule in a portion outside the root is not read."""
    from cli_support import write_project
    from invariants.cli.static_guards import guard_problems
    from invariants.confinement import CONFINE_ENV
    outside = write_project(tmp_path / "outside", {"ns_r35/evil.py": "def refund(x):\n    return x\n"})
    proj = write_project(tmp_path / "proj", {"ns_r35/mine.py": "X = 1\n", "app/__init__.py": "",
                                             "app/svc.py": "from ns_r35.evil import refund as pay\n"})
    rule_file = proj / "invariants.yaml"
    rule_file.write_text("rules: []\n")
    doc = {"rules": [{"id": "P-1", "subject": {"target": "py:app.svc.pay"},
                      "enforcers": [{"point": "decorator"}]}]}
    _, problems = guard_problems(rule_file, doc, [str(proj), str(outside)])
    assert "line 1" in problems[0], problems                    # unconfined: the re-export is followed
    monkeypatch.setenv(CONFINE_ENV, str(proj))
    _, problems = guard_problems(rule_file, doc, [str(proj), str(outside)])
    assert "ns_r35.evil resolves outside" in problems[0] and "line" not in problems[0], problems


def test_static_guards_never_follow_a_reexport_through_a_top_level_symlink_out_of_the_root(tmp_path, monkeypatch):
    """A top-level module reached from the root (proj/leak_r35.py -> outside) is the project's, though
    no package of the project holds it (ConfinedFinder's _through)."""
    from cli_support import write_project
    from invariants.cli.static_guards import guard_problems
    from invariants.confinement import CONFINE_ENV
    outside = write_project(tmp_path / "outside", {"evil.py": "def refund(x):\n    return x\n"})
    proj = write_project(tmp_path / "proj", {"app/__init__.py": "",
                                             "app/svc.py": "from leak_r35 import refund as pay\n"})
    (proj / "leak_r35.py").symlink_to(outside / "evil.py")
    rule_file = proj / "invariants.yaml"
    rule_file.write_text("rules: []\n")
    doc = {"rules": [{"id": "P-1", "subject": {"target": "py:app.svc.pay"},
                      "enforcers": [{"point": "decorator"}]}]}
    _, problems = guard_problems(rule_file, doc, [str(proj)])
    assert "line 1" in problems[0], problems
    monkeypatch.setenv(CONFINE_ENV, str(proj))
    _, problems = guard_problems(rule_file, doc, [str(proj)])
    assert "leak_r35 resolves outside" in problems[0] and "line" not in problems[0], problems


def test_a_dataclass_state_parameter_still_takes_a_dict():
    """Pinned while fixing H1: a dict reaches a dataclass-annotated edge as a plain dict, as before."""
    with inv.isolated():
        inv.load(r24.plan_doc())
        r24.SEEN.clear()
        with pytest.raises(AttributeError):          # the consumer reads state.plan; a dict has no attribute
            r24.act_dc({"plan": r24.plan()})
        assert r24.SEEN == []
