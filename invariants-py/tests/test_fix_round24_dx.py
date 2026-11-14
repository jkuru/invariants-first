"""Round 24: adoption friction found by a review on real codebases (jupyter_server, google-adk).

1. a missing third-party package is reported as itself, not as a missing app module;
2. `pytest --inv-ablate` with no rules is a usage error, and finds invariants.yaml in the rootdir;
3. `inv init` comments out a contract `inv lock` cannot pin, and `inv lock` names the contract;
4. verify on a second rule of an already guarded function says which decorator to extend;
5. loader messages: did-you-mean, CEL hints, file and line, duplicate ids, spec, rule ids;
6. CLI vocabulary: both weakening spellings, plain `inv status`, `inv scan` with no door rules;
7. [tool.invariants.scan] extend_exclude adds to the default excludes;
8. `inv init` noise: private methods, self asserts, preconditions after inert statements, dict.update
   and set.add, read-only doors.
"""
from __future__ import annotations

import argparse
import ast
import itertools
import json
import sys
import textwrap

import pytest

import invariants as inv
from cli_support import run_inv, write_project
from invariants import bootstrap, cli, doors, lock
from invariants.cli import project

pytest_plugins = ["pytester"]

_n = itertools.count()


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A fresh importable package per test: app(files) -> its package name."""
    made: list[str] = []

    def make(files: dict[str, str]) -> str:
        name = f"r24app{next(_n)}"
        for rel, text in files.items():
            p = tmp_path / name / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(text).replace("PKG", name))
        (tmp_path / name / "__init__.py").touch()
        made.append(name)
        return name

    monkeypatch.syspath_prepend(str(tmp_path))
    yield make
    for m in [m for m in sys.modules if any(m == n or m.startswith(n + ".") for n in made)]:
        del sys.modules[m]


def load_error(doc: dict) -> str:
    with inv.isolated(), pytest.raises(inv.LoadError) as ei:
        inv.load(doc)
    return str(ei.value)


SERVICE = """
import invariants as inv

def refunded_so_far(charge_id: str) -> int:
    return 0

class BillingService:
    @inv.guard("REFUND")
    def refund(self, charge_id: str, charged: int, refunded: int, amount: int) -> None:
        pass
"""


def guard_rule(pkg: str, bad: object, **more: object) -> dict:
    return {"spec": "invariant/v0.1",
            "rules": [{"id": "REFUND", "guard": f"py:{pkg}.service.BillingService.refund", "bad": bad, **more}]}


# --- 1. a missing dependency is the real cause ----------------------------------------------------
def test_a_missing_third_party_package_is_reported_as_itself(app):
    pkg = app({"models.py": "import not_installed_pkg_r24\n", "service.py": "from PKG import models\ndef f(): pass\n"})
    msg = load_error({"targets": {f"py:{pkg}.service.f": {"effects": "none"}}})
    assert msg == (f"cannot import {pkg}.service: ModuleNotFoundError: No module named 'not_installed_pkg_r24' "
                   f"(while importing {pkg}.models)")


def test_a_module_that_does_not_exist_says_so_with_a_suggestion(app):
    pkg = app({"service.py": "def f(): pass\n"})
    msg = load_error({"targets": {f"py:{pkg}.servce.f": {"effects": "none"}}})
    assert msg == f"target py:{pkg}.servce.f: {pkg} has no 'servce'; did you mean 'service'?"
    msg = load_error({"targets": {f"py:{pkg}.service.g": {"effects": "none"}}})
    assert msg == f"target py:{pkg}.service.g: {pkg}.service has no 'g' (it has: f)"


def test_a_provider_from_typo_names_the_provider(app):
    pkg = app({"service.py": SERVICE})
    msg = load_error({"providers": {"so_far": {"type": "int", "from": f"py:{pkg}.service.refunded_sofar",
                                               "args": {"charge_id": "args.charge_id"}}}})
    assert msg.startswith(f"provider so_far: from py:{pkg}.service.refunded_sofar: {pkg}.service has no "
                          f"'refunded_sofar'; did you mean 'refunded_so_far'?")


# --- 2. pytest --inv-ablate with no rules ---------------------------------------------------------
PLAIN = "def test_plain():\n    assert 1 + 1 == 2\n"


def test_ablate_with_no_rules_exits_with_a_usage_error(pytester):
    pytester.makepyfile(test_plain=PLAIN)
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--inv-ablate")
    assert r.ret == pytest.ExitCode.USAGE_ERROR
    err = r.stderr.str()
    assert "--inv-ablate: no rules loaded, so nothing was checked: pass --inv-rules PATH" in err
    assert "or put invariants.yaml in the rootdir (.)" in err


def test_ablate_finds_invariants_yaml_in_the_rootdir(pytester):
    pytester.makeini("[pytest]\npythonpath = .\n")
    pytester.makepyfile(r24plug="import invariants as inv\n\n@inv.guard('R-1')\ndef halve(n: int) -> int:\n"
                                "    return n // 2\n",
                        test_app="import pytest\nimport invariants as inv\nimport r24plug\n\n"
                                 "def test_falsifier():\n    with pytest.raises(inv.Rejected):\n"
                                 "        r24plug.halve(-2)\n")
    (pytester.path / "invariants.yaml").write_text(
        "spec: invariant/v0.1\nrules:\n  - id: R-1\n    guard: py:r24plug.halve\n    bad: 'args.n < 0'\n"
        "    tests: [test_app.py::test_falsifier]\n")
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--inv-ablate")
    assert r.ret == 0
    r.stdout.fnmatch_lines(["*R-1: load-bearing*"])


# --- 3. init and lock on a model pydantic cannot give a JSON Schema for --------------------------
UNPINNABLE = {"pyproject.toml": '[project]\nname = "app24"\nversion = "0"\n',
              "src/app24/__init__.py": "",
              "src/app24/models.py": textwrap.dedent("""
                  from pydantic import BaseModel, ConfigDict, Field

                  class Client:
                      pass

                  class Agent(BaseModel):
                      model_config = ConfigDict(arbitrary_types_allowed=True)
                      client: Client
                      retries: int = Field(ge=0)

                  class Order(BaseModel):
                      qty: int = Field(gt=0)
                  """)}


def test_init_comments_out_a_contract_lock_cannot_pin(tmp_path):
    pytest.importorskip("pydantic")
    root = write_project(tmp_path / "p", UNPINNABLE)
    r = run_inv("init", cwd=root)
    assert r.returncode == 0, r.stderr
    text = (root / "invariants.yaml").read_text()
    assert ("  # not written as a contract: `inv lock` cannot pin it: contract agent (app24.models:Agent) cannot "
            "be pinned: PydanticInvalidForJsonSchema: Cannot generate a JsonSchema for core_schema.IsInstanceSchema"
            in text)
    assert '  # agent: {type: pydantic, ref: "app24.models:Agent"}' in text
    assert '\n  order: {type: pydantic, ref: "app24.models:Order"}' in text
    assert "left 1 model(s) commented out: `inv lock` cannot pin them (Agent; the file says why)" in r.stdout
    assert json.loads((root / "invariants.lock").read_text())["contracts"].keys() == {"order"}


def test_lock_names_the_contract_it_cannot_pin(tmp_path):
    pytest.importorskip("pydantic")
    root = write_project(tmp_path / "p", {**UNPINNABLE, "invariants.yaml":
                                          'spec: invariant/v0.1\ncontracts:\n'
                                          '  agent: {type: pydantic, ref: "app24.models:Agent"}\n'})
    r = run_inv("lock", cwd=root)
    assert r.returncode == 1
    assert ("inv lock: contract agent (app24.models:Agent) cannot be pinned: PydanticInvalidForJsonSchema: "
            "Cannot generate a JsonSchema for core_schema.IsInstanceSchema") in r.stderr
    assert "(comment out or delete it in invariants.yaml, or fix the model, then run `inv lock` again)" in r.stderr


# --- 4. a second rule on a guarded function ---------------------------------------------------------
def test_verify_names_the_decorator_to_extend(tmp_path):
    root = write_project(tmp_path / "p", {"bill24/__init__.py": "", "bill24/service.py": SERVICE.replace("PKG", "bill24"),
                                          "invariants.yaml": textwrap.dedent("""
                                              spec: invariant/v0.1
                                              rules:
                                                - id: REFUND
                                                  guard: "py:bill24.service.BillingService.refund"
                                                  bad: "args.amount <= 0"
                                                - id: REFUND-CAP
                                                  guard: "py:bill24.service.BillingService.refund"
                                                  bad: "args.amount > 10000"
                                              """)})
    r = run_inv("verify", cwd=root)
    assert r.returncode == 1
    assert ("  py:bill24.service.BillingService.refund is guarded by @inv.guard('REFUND'), which does not name "
            "REFUND-CAP: add 'REFUND-CAP' to the decorator (or use "
            "@inv.guard('py:bill24.service.BillingService.refund'))") in r.stderr


def test_verify_points_at_a_typo_in_the_decorator(app):
    pkg = app({"service.py": SERVICE.replace('@inv.guard("REFUND")', '@inv.guard("REFUN")')})
    with inv.isolated() as reg, pytest.raises(inv.CoverageError) as ei:
        inv.load(guard_rule(pkg, "args.amount <= 0"))
        reg.verify([pkg])
    assert (f"py:{pkg}.service.BillingService.refund is guarded by @inv.guard('REFUN'), which does not name REFUND: "
            f"add 'REFUND' to the decorator (or use @inv.guard('py:{pkg}.service.BillingService.refund')), or fix "
            f"'REFUN' if it is a typo of 'REFUND'") in str(ei.value)


# --- 5. loader messages -------------------------------------------------------------------------------
def test_unknown_fields_suggest_the_field(app):
    pkg = app({"service.py": SERVICE})
    doc = guard_rule(pkg, "args.amount <= 0", tset=["t.py::t"])
    assert load_error(doc) == "REFUND: unknown field(s) ['tset'] (did you mean 'tests'?)"
    doc = {"rules": [{"id": "R", "gaurd": f"py:{pkg}.service.BillingService.refund", "bad": "false"}]}
    assert load_error(doc) == "R: unknown field(s) ['gaurd'] (did you mean 'guard'?)"


def test_cel_hints(app):
    pkg = app({"service.py": SERVICE})
    target = f"py:{pkg}.service.BillingService.refund"
    assert load_error(guard_rule(pkg, "amount <= 0")) == (
        f"REFUND: bad reads undeclared name(s) ['amount']: amount is a parameter of {target}: write args.amount")
    assert load_error(guard_rule(pkg, "args.amout <= 0")) == (
        f"REFUND: args.amout does not resolve against {target} (did you mean args.amount? its parameters: "
        f"charge_id, charged, refunded, amount)")
    assert load_error(guard_rule(pkg, "args.amount <= 0 and not args.charged > 0")).endswith(
        "(in 'args.amount <= 0 and not args.charged > 0'); CEL writes `and` as &&, `not` as !")
    assert load_error(guard_rule(pkg, "len(args.charge_id) == 0")) == (
        "REFUND: bad calls len(), a Python function CEL does not have: write size(x) (or x.size())")
    two = [{"when": "args.amount > 0", "message": "first"}, {"when": "args.amount <== 0", "message": "second"}]
    assert load_error(guard_rule(pkg, two)).startswith("REFUND: bad clause 'second' does not parse: ")
    assert load_error(guard_rule(pkg, ["args.amount <= 0"])) == (
        'REFUND.bad: clause 1 is a bare string; in a list each clause is a mapping: '
        '- {when: "args.amount <= 0", message: "..."}')


def test_provider_names_in_cel(app):
    pkg = app({"service.py": SERVICE.replace("def refunded_so_far", "@inv.provider('so_far')\ndef refunded_so_far")})
    prov = {"so_far": {"type": "int", "from": f"py:{pkg}.service.refunded_so_far",
                       "args": {"charge_id": "args.charge_id"}}}
    doc = {**guard_rule(pkg, "provider.so_far > 0"), "providers": prov}
    assert load_error(doc) == ("REFUND: bad reads undeclared name(s) ['provider']: a provider is read by its "
                               "name: write so_far, not provider.so_far")
    doc = {**guard_rule(pkg, "provider.so_fr > 0"), "providers": prov}
    assert load_error(doc).endswith("no provider so_fr is declared (did you mean so_far?); a provider is read by "
                                    "its name, not provider.so_fr")


def test_contract_ref_needs_a_colon():
    assert load_error({"contracts": {"c": {"type": "pydantic", "ref": "billing.models.Order"}}}) == (
        "contract c: ref 'billing.models.Order' names a Python object: write it as module:attribute "
        "(billing.models:Order)")


def test_spec_and_rule_id_are_checked():
    assert load_error({"spec": "invariant/v0.2"}) == (
        "spec: 'invariant/v0.2' is not a rule-file version this library reads (it reads invariant/v0.1)")
    assert load_error({"rules": [{"id": "refund door", "status": "detect", "bad": "false"}]}) == (
        "'refund door': not a rule id: an id is letters, digits, '-', '_' and '.', starting with a letter or "
        "digit (no spaces); for example 'refund-door'")
    for good in ("R-1", "refund_door.v2", "PAY1"):
        assert inv.loader.rules.RULE_ID.match(good)
    for bad in ("a b", "-x", "py:x", "x,y", ""):
        assert not inv.loader.rules.RULE_ID.match(bad)


def test_file_errors_name_the_file_and_line(tmp_path, app):
    pkg = app({"service.py": SERVICE})
    f = tmp_path / "invariants.yaml"
    f.write_text(f"spec: invariant/v0.1\nrules:\n  - id: REFUND\n    guard: py:{pkg}.service.BillingService.refund\n"
                 f"    bad: 'args.amount <= 0'\n  - id: REFUND\n    guard: py:{pkg}.service.BillingService.refund\n"
                 f"    bad: 'args.amount > 9'\n")
    with inv.isolated(), pytest.raises(inv.LoadError) as ei:
        inv.load(f)
    assert str(ei.value) == (f"{f}:6: REFUND: duplicate id: rule 1 (line 3) and rule 2 (line 6) both use it "
                             f"(rule ids are unique)")
    f.write_text("rules:\n  - id: A\n   door2: x\n")
    with pytest.raises(inv.LoadError) as ei:
        inv.load(f)
    assert f'in "{f}", line 2' in str(ei.value) and "<unicode string>" not in str(ei.value)
    f.write_text("rules:\n  - id: A\n    status: candidate\n    bad: 'false'\n  - id: B\n    tset: []\n")
    with pytest.raises(inv.LoadError) as ei:
        inv.load(f)
    assert str(ei.value).startswith(f"{f}:5: B: unknown field(s) ['tset']")


def test_guard_called_before_load_says_to_call_load(app):
    pkg = app({"service.py": SERVICE})
    mod = __import__(f"{pkg}.service", fromlist=["x"])
    with inv.isolated(), pytest.raises(inv.LoadError) as ei:
        mod.BillingService().refund("c", 1, 0, 1)
    assert str(ei.value) == ("guard names rule 'REFUND', which is not loaded (no rule file is loaded in this "
                             "registry): call inv.load(\"invariants.yaml\") once at startup, before the first "
                             "guarded call (in tests: a conftest.py that calls it, or pytest --inv-rules "
                             "invariants.yaml)")


# --- 6. CLI vocabulary -------------------------------------------------------------------------------
def test_both_weakening_spellings_on_both_commands():
    p = cli.parser()
    for cmd, dest in (("lock", "accept_weakening"), ("check", "allow_weakening")):
        for flag in cli.WEAKENING_FLAGS:
            assert getattr(p.parse_args([cmd, flag]), dest) is True
    assert cli.WEAKENING_FLAGS == ("--accept-weakening", "--allow-weakening")
    lock_help = p._subparsers._group_actions[0].choices["check"].format_help()     # type: ignore[union-attr]
    assert "--accept-weakening" in lock_help and "--allow-weakening" not in lock_help


def test_status_is_plain_and_keeps_its_json_fields(tmp_path):
    root = write_project(tmp_path / "p", {"bill24s/__init__.py": "", "bill24s/repo.py":
                                          "class RefundRepo:\n    def save_refund(self, a: int) -> None:\n        pass\n",
                                          "bill24s/service.py": SERVICE.replace("PKG", "bill24s"),
                                          "invariants.yaml": textwrap.dedent("""
                                              spec: invariant/v0.1
                                              rules:
                                                - id: REFUND
                                                  guard: "py:bill24s.service.BillingService.refund"
                                                  bad: [{when: "args.amount <= 0", message: "refund must be positive"}]
                                                - id: REFUND-DOOR
                                                  door: "py:bill24s.service.BillingService.refund"
                                                  sink: "py:bill24s.repo.RefundRepo.save_refund"
                                              """)})
    r = run_inv("status", "--json", cwd=root)
    rows = {x["id"]: x for x in json.loads(r.stdout)["rules"]}
    for row in rows.values():
        assert {"id", "status", "standing", "reasons", "ablation_proven", "scan", "scan_counts", "checks",
                "guarded", "ablation", "next", "status_text"} <= set(row)
    assert rows["REFUND"]["guarded"] is True and rows["REFUND"]["ablation"] == "not run"
    assert rows["REFUND-DOOR"]["guarded"] is None and rows["REFUND-DOOR"]["ablation"] == "not needed"
    text = run_inv("status", cwd=root).stdout
    assert "REFUND (detect: logs a bad call, does not block it)" in text
    assert ("  checks:  py:bill24s.service.BillingService.refund refuses a call when: refund must be positive"
            in text)
    assert "  checks:  every call to py:bill24s.repo.RefundRepo.save_refund goes through " in text
    assert "  guard:   yes" in text and "  ablation proved it: not run" in text
    for jargon in ("enforce(known-callers)", "chokepoint", "mediation.sink", "monitored"):
        assert jargon not in text


def test_scan_with_no_door_rules_says_so(tmp_path):
    root = write_project(tmp_path / "p", {"a.py": "def f(): pass\n", "invariants.yaml": "spec: invariant/v0.1\n"})
    r = run_inv("scan", cwd=root)
    assert r.returncode == 0 and r.stdout.startswith("no door rules to check (1 file(s) scanned)")


# --- 7. extend_exclude --------------------------------------------------------------------------------
def test_extend_exclude_adds_to_the_defaults(tmp_path):
    f = tmp_path / "invariants.yaml"
    f.write_text("spec: invariant/v0.1\n")
    ns = argparse.Namespace(exclude=[], root=[])
    (tmp_path / "pyproject.toml").write_text('[tool.invariants.scan]\nextend_exclude = ["*/legacy/*"]\n')
    assert project._exclude(ns, f) == [*project.DEFAULT_SCAN_EXCLUDE, "*/legacy/*"]
    assert set(project.scan_settings(ns, f)["exclude"]) == {*project.DEFAULT_SCAN_EXCLUDE, "*/legacy/*"}
    (tmp_path / "pyproject.toml").write_text('[tool.invariants.scan]\nexclude = ["*/legacy/*"]\n')
    assert project._exclude(ns, f) == ["*/legacy/*"]
    (tmp_path / "pyproject.toml").write_text('[tool.invariants.scan]\nexclude = ["a"]\nextend_exclude = ["b"]\n')
    assert project._exclude(ns, f) == ["a", "b"]
    assert lock.scan_pin(["."], project._exclude(ns, f))["exclude"] == ["a", "b"]
    (tmp_path / "pyproject.toml").write_text('[tool.invariants.scan]\nextend_exclude = "b"\n')
    with pytest.raises(cli.Usage, match="extend_exclude must be a list of strings"):
        project._exclude(ns, f)


# --- 8. init noise -------------------------------------------------------------------------------------
def guards_of(src: str) -> dict[str, list[tuple[str, str]]]:
    return {g.qualname: g.clauses for g in bootstrap.finding._guards("m", ast.parse(textwrap.dedent(src)))}


def test_init_skips_private_methods_and_self_asserts():
    found = guards_of("""
        class Handler:
            def _helper(self, x: int):
                if x < 0:
                    raise ValueError("x")
            def get(self):
                assert self.app is not None
                assert self.request.uri is not None
            def put(self, n: int):
                assert self.app is not None
                if n < 0:
                    raise ValueError("negative")
        def _private(n: int):
            if n < 0:
                raise ValueError("n")
        """)
    assert found == {"Handler.put": [("args.n < 0", "negative")]}


def test_init_reads_preconditions_after_inert_statements():
    found = guards_of('''
        import logging
        log = logging.getLogger(__name__)

        def pay(amount: int, cap: int):
            """Pay."""
            log.debug("paying %s", amount)
            limit = 100
            if amount <= 0:
                raise ValueError("positive")
            if amount > limit:
                raise ValueError("reads a local")
        def later(n: int):
            record(n)
            if n < 0:
                raise ValueError("after a call")
        ''')
    assert found == {"pay": [("args.amount <= 0", "positive")]}


def test_dict_update_and_set_add_are_not_writes():
    def writes(src: str) -> list[str]:
        fn = ast.parse(textwrap.dedent(src)).body[0]
        return doors._writes_in(fn)
    assert writes("def f(self):\n    self._connection_args['headers'].update(x)\n") == []
    assert writes("def f(self):\n    self.session.state.update(x)\n") == []
    assert writes("def f(self):\n    Conn._open_sockets.add(x)\n") == []
    assert writes("def f(self):\n    self.logger.add(x)\n") == []
    assert writes("def f(self):\n    self.session.add(x)\n") == ["self.session.add"]
    assert writes("def f(sql_session):\n    sql_session.add(x)\n") == ["sql_session.add"]
    assert writes("def f(s: Session):\n    s.add(x)\n") == ["s.add"]
    assert writes("def f(db):\n    db.query(X).update(y)\n") == ["db.query(X).update"]
    assert not doors._is_write_name("add_done_callback") and doors._is_write_name("add_usage")


def test_a_read_only_function_is_never_a_door(tmp_path):
    root = write_project(tmp_path / "p", {
        "app/__init__.py": "",
        "app/repo.py": "class AccountRepo:\n    def save(self, a: int) -> None:\n        pass\n",
        "app/service.py": textwrap.dedent("""
            from app.repo import AccountRepo
            class AccountService:
                def __init__(self, repo: AccountRepo):
                    self.repo = repo
                def get_account(self, a: int) -> None:
                    self.repo.save(a)
                def deposit(self, a: int) -> None:
                    self.repo.save(a)
            """)})
    found = doors.discover([root])
    assert [d.doors for d in found.doors] == [["app.service.AccountService.deposit"]]
    assert doors._read_only("x.Svc.list_sessions") and doors._read_only("x.isReady")
    assert not doors._read_only("x.Svc.listen") and not doors._read_only("x.Svc.deposit")
