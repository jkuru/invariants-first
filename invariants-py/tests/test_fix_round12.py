"""Fix round 12 (evaluation findings): contract-granularity lock pins, the semantic lock diff with
WEAKENED changes and --accept-weakening, an older lock version refused, and resolved
schema pins (a Literal alias counts). (Its raw-SQL checks were removed with mediation.tables: raw SQL
is below the API.)"""
from __future__ import annotations

import dataclasses
import json
import textwrap
import typing

import pydantic
import pytest

import invariants as inv
from invariants import cli, lock as lockmod, scan
from invariants.cel import subset
from cli_support import CLEAN, copy_app, run_inv, write_project


@pytest.fixture
def app(tmp_path):
    a = copy_app(CLEAN, tmp_path / "clean")
    assert run_inv("lock", a / "invariants.yaml").returncode == 0
    return a


def _check(app):
    return run_inv("lock", "--check", app / "invariants.yaml")


def _edit(path, old, new):
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))


# --- 1. no churn: body-only and unrelated edits -------------------------------------------------
def test_body_only_refactor_keeps_the_lock_fresh(app):
    _edit(app / "ledger" / "service.py",
          "    _apply(conn, account, store.read_balance(conn, account) - amount)",
          "    balance = store.read_balance(conn, account)\n    _apply(conn, account, balance - amount)")
    proc = _check(app)
    assert proc.returncode == 0, proc.stderr


def test_unrelated_edit_in_a_contract_module_keeps_the_lock_fresh(app):
    svc = app / "ledger" / "service.py"
    svc.write_text(svc.read_text() + "\n\ndef helper(x: int) -> int:\n    return x + 1\n")
    _edit(svc, '"""The door', '"""Edited docstring. The door')
    assert _check(app).returncode == 0


def test_signature_change_names_the_target_and_shows_old_and_new(app):
    _edit(app / "ledger" / "service.py", "def deposit(conn, account: str, amount: int)",
          "def deposit(conn, account: str, amount: float)")
    proc = _check(app)
    assert proc.returncode == 1
    assert ("target py:ledger.service.deposit: signature: 'deposit(conn, account: str, amount: int) -> None' -> "
            "'deposit(conn, account: str, amount: float) -> None'") in proc.stderr
    assert "WEAKENED" not in proc.stderr


# --- 2. weakenings: named, shown, and not re-pinned silently ------------------------------------
@pytest.mark.parametrize("old,new,expect", [
    ("bad: args.amount > 1000000", "bad: args.amount > 2000000", "rule L-2: bad: 'args.amount > 1000000' -> "
     "'args.amount > 2000000'  WEAKENED (the condition now matches fewer cases)"),
    ("status: detect\n", "status: candidate\n", "rule L-2: status: 'detect' -> 'candidate'  WEAKENED"),
    ("- {point: decorator, timing: pre_effect, mode: enforce}\n", "- {point: decorator, timing: pre_effect, "
     "mode: detect}\n", "rule L-3: enforcer decorator mode: 'enforce' -> 'detect'  WEAKENED"),
    ("on_violation: reject, on_error: fail_closed}", "on_violation: reject, on_error: fail_open_and_page}",
     "rule L-1: enforcer decorator on_error: 'fail_closed' -> 'fail_open_and_page'  WEAKENED (fail_open"),
    ('      sink: "py:ledger.store.write_balance"\n', "",
     'rule L-1: mediation.sink: ["py:ledger.store.write_balance"] -> (none)  WEAKENED'),
])
def test_weakening_is_called_out_and_refused(app, old, new, expect):
    rf = app / "invariants.yaml"
    if "status: detect" in old:          # L-2 is the only detect rule
        _edit(rf, "    status: detect\n", "    status: candidate\n")
    else:
        _edit(rf, old, new)
    proc = _check(app)
    assert proc.returncode == 1 and expect in proc.stderr, proc.stderr
    assert "--accept-weakening" in proc.stderr
    before = (app / "invariants.lock").read_text()
    refused = run_inv("lock", rf)
    assert refused.returncode == 1 and "refused:" in refused.stderr and expect in refused.stdout
    assert (app / "invariants.lock").read_text() == before            # not written
    accepted = run_inv("lock", rf, "--accept-weakening")
    assert accepted.returncode == 0 and "accepted 1 weakening(s)" in accepted.stdout
    assert _check(app).returncode == 0


def test_enforcer_removed_is_weakened():
    old = {"rules": {"R": {"semantic": {"enforcers": [{"point": "decorator", "mode": "enforce"},
                                                      {"point": "storage", "mode": "enforce"}]}}}}
    new = {"rules": {"R": {"semantic": {"enforcers": [{"point": "decorator", "mode": "enforce"}]}}}}
    [c] = lockmod.changes(old, new)
    assert c.weakened and c.what == "enforcer storage removed"


def test_strengthening_is_not_weakened(app):
    rf = app / "invariants.yaml"
    _edit(rf, "bad: args.amount > 1000000", "bad: args.amount >= 500000")
    proc = _check(app)
    assert proc.returncode == 1 and "WEAKENED" not in proc.stderr and "matches more cases" in proc.stderr
    written = run_inv("lock", rf)                                       # no flag needed
    assert written.returncode == 0 and "rule L-2: bad:" in written.stdout


def test_removed_rule_and_removed_clause_are_weakened():
    old = {"rules": {"R": {"semantic": {"bad": [{"when": "a > 1", "message": "m"}, {"when": "b", "message": "n"}]}},
                     "S": {}}}
    new = {"rules": {"R": {"semantic": {"bad": [{"when": "a > 1", "message": "m"}]}}}}
    got = {str(c) for c in lockmod.changes(old, new)}
    assert "rule R: bad clause removed: 'b [n]' -> (none)  WEAKENED" in got
    assert "rule S: removed  WEAKENED (the rule no longer exists)" in got


def test_prose_edits_are_named_but_not_weakened(app):
    _edit(app / "invariants.yaml", "A withdrawal is never for a non-positive amount.",
          "Withdrawals are positive.")
    proc = _check(app)
    # fix round 19: prose never makes the lock stale; the edit is reported for information
    assert proc.returncode == 0 and "rule L-1: statement text edited" in proc.stdout
    assert "WEAKENED" not in proc.stdout + proc.stderr and "not stale" in proc.stdout


def test_an_old_lock_field_that_is_gone_is_a_possible_weakening():
    old = {"providers": {"p": {"consistency": "same_txn", "signature": "f(conn) -> int"}}}
    new = {"providers": {"p": {"signature": "f(conn) -> str"}}}
    got = {str(c) for c in lockmod.changes(old, new)}
    assert any(g.startswith("provider p: consistency: 'same_txn' -> (none)  WEAKENED") for g in got), got
    assert "provider p: signature: 'f(conn) -> int' -> 'f(conn) -> str'" in got


@pytest.mark.parametrize("old,new,want", [
    ("a > 4 * b", "a > 10 * b", "unknown"),              # P04; b may be negative (round 14, R11-2)
    ("a > 10 * b", "a > 4 * b", "unknown"),
    ("a > 4 * size(b)", "a > 10 * size(b)", "weaker"),   # size() is non-negative
    ("a > 10 * size(b)", "a > 4 * size(b)", "stronger"),
    ("a > 4", "a >= 4", "stronger"),
    ("a >= 4", "a > 4", "weaker"),
    ("a > 10", "a >= 5", "stronger"),                    # both edits flag more
    ("a > 5", "a >= 10", "unknown"),                     # one each way
    ("a < 4", "a < 9", "stronger"),
    ("x - 3 > y", "x - 5 > y", "weaker"),
    ('!(p in ["a", "b"])', '!(p in ["a", "b", "c"])', "weaker"),   # F12: a wider vocabulary
    ('p in ["a"]', 'p in ["a", "b"]', "stronger"),
    ("a > 1 && b > 2", "a > 1 || b > 2", "unknown"),
    ("a == 1", "a == 2", "unknown"),
    ("xs.exists(x, x > 3)", "xs.exists(x, x > 5)", "weaker"),
    ("a > 1", "false", "weaker"),
    ("a > 1 && b > 1", "a > 2 && b > 0", "unknown"),     # mixed
])
def test_compare_bad(old, new, want):
    assert lockmod.compare_bad(old, new, subset.parse) == want


def test_unknown_direction_is_treated_as_a_possible_weakening():
    [c] = lockmod.changes({"rules": {"R": {"semantic": {"bad": "a == 1"}}}},
                           {"rules": {"R": {"semantic": {"bad": "a == 2"}}}})
    assert c.weakened and "cannot be decided" in c.note


def test_explicit_door_equal_to_the_subject_is_not_a_change_of_doors():
    old = {"rules": {"R": {"semantic": {"subject": {"target": "py:a.d"}, "mediation": {"sink": "py:a.s"}}}}}
    new = {"rules": {"R": {"semantic": {"subject": {"target": "py:a.d"},
                                        "mediation": {"sink": "py:a.s", "door": "py:a.d"}}}}}
    assert not any(c.weakened for c in lockmod.changes(old, new))
    more = {"rules": {"R": {"semantic": {"subject": {"target": "py:a.d"},
                                         "mediation": {"sink": "py:a.s", "door": ["py:a.d", "py:a.x"]}}}}}
    assert any(c.weakened and c.what == "mediation.door" for c in lockmod.changes(old, more))


def test_accept_weakening_with_check_is_a_usage_error(app):
    assert cli.main(["lock", str(app / "invariants.yaml"), "--check", "--accept-weakening"]) == cli.USAGE


# --- 3. lock version -----------------------------------------------------------------------------
@pytest.mark.parametrize("args", [("lock", "--check"), ("lock",)])
def test_an_older_lock_version_is_refused(app, args):
    lock = json.loads((app / "invariants.lock").read_text())
    (app / "invariants.lock").write_text(json.dumps({**lock, "lock_version": 2}))
    proc = run_inv(*args, app / "invariants.yaml")
    assert proc.returncode == 1 and "lock_version 2 is not supported" in proc.stderr, proc.stderr
    assert "delete it and re-run `inv lock`" in proc.stderr


def test_newer_lock_version_is_rejected(app):
    lock = json.loads((app / "invariants.lock").read_text())
    (app / "invariants.lock").write_text(json.dumps({**lock, "lock_version": 99}))
    for args in (("lock", "--check"), ("lock",)):
        proc = run_inv(*args, app / "invariants.yaml")
        assert proc.returncode == 1 and "newer invariants" in proc.stderr


# --- 4. resolved schemas ------------------------------------------------------------------------
def _model(values):
    Pred = typing.Literal[values]                             # a module-level alias in real code
    return pydantic.create_model("FactReply", predicate=(Pred, ...), quote=(str, ...))


def test_pydantic_schema_pin_sees_a_literal_alias():
    a = lockmod.resolved_schema(_model(("acquired", "ceo_of")))
    b = lockmod.resolved_schema(_model(("acquired", "ceo_of", "founded_in")))
    assert a != b
    diffs = lockmod.contracts._json_paths(a, b)
    assert ("pydantic.properties.predicate.enum added", None, ["founded_in"]) in diffs


def test_pydantic_contract_pin_follows_the_alias(tmp_path, monkeypatch):
    for vals in (("a", "b"), ("a", "b", "c")):
        pkg = tmp_path / f"m{len(vals)}"
        write_project(pkg, {"models.py": textwrap.dedent(f"""
            import typing
            import pydantic
            Pred = typing.Literal{list(vals)!r}
            class Reply(pydantic.BaseModel):
                predicate: Pred
            """)})
        monkeypatch.syspath_prepend(str(pkg))
        import importlib
        import sys
        sys.modules.pop("models", None)
        importlib.invalidate_caches()
        e = lockmod.contract_hash("r", {"type": "pydantic", "ref": "models:Reply"}, tmp_path)
        assert e["schema"]["pydantic"]["properties"]["predicate"]["enum"] == list(vals)
        monkeypatch.syspath_prepend(str(tmp_path))     # keep the order deterministic
        sys.modules.pop("models", None)


Kind = typing.Literal["x", "y"]


@dataclasses.dataclass
class Inner:
    n: int


@dataclasses.dataclass
class Outer:
    kind: Kind
    inner: Inner
    note: str | None = None


def test_dataclass_and_dict_schemas_use_resolved_annotations():
    s = lockmod.resolved_schema(Outer)
    assert s["fields"]["kind"] == {"literal": ["x", "y"]}
    assert s["fields"]["inner"] == {"$ref": f"{__name__}.Inner"} and s["$defs"][f"{__name__}.Inner"]["fields"] == \
        {"n": "int"}
    assert s["required"] == ["inner", "kind"]
    d = lockmod.resolved_schema({"cause": typing.Literal["OOM", "TIMEOUT"], "n": int})
    assert d == {"fields": {"cause": {"literal": ["OOM", "TIMEOUT"]}, "n": "int"}}


def test_openapi_contract_pins_only_the_operations_rules_name(tmp_path):
    spec = {"openapi": "3.1.0", "paths": {
        "/a": {"post": {"operationId": "makeA", "requestBody": {"$ref": "#/components/requestBodies/A"}}},
        "/b": {"get": {"operationId": "listB"}}},
        "components": {"requestBodies": {"A": {"content": {"application/json": {"schema": {"type": "object"}}}}}}}
    f = tmp_path / "api.json"
    f.write_text(json.dumps(spec))
    pin = lockmod.contract_hash("api", {"type": "openapi", "ref": "api.json"}, tmp_path, ["makeA"])
    assert set(pin["operations"]) == {"makeA"} and "hash" not in pin
    assert pin["operations"]["makeA"]["requestBody"]["content"]   # $ref resolved
    spec["paths"]["/c"] = {"get": {"operationId": "newOne"}}                    # P10: a new endpoint
    f.write_text(json.dumps(spec))
    assert lockmod.contract_hash("api", {"type": "openapi", "ref": "api.json"}, tmp_path, ["makeA"]) == pin
    spec["components"]["requestBodies"]["A"]["content"]["application/json"]["schema"]["type"] = "array"
    f.write_text(json.dumps(spec))
    assert lockmod.contract_hash("api", {"type": "openapi", "ref": "api.json"}, tmp_path, ["makeA"]) != pin
    with pytest.raises(inv.LoadError, match="no operation nope"):
        lockmod.contract_hash("api", {"type": "openapi", "ref": "api.json"}, tmp_path, ["nope"])


# --- 6. the lock file itself --------------------------------------------------------------------
def test_lock_layout(app):                      # version 3 since fix round 13 (see test_fix_round13)
    lock = json.loads((app / "invariants.lock").read_text())
    assert lock["lock_version"] == lockmod.LOCK_VERSION == 3
    assert set(lock) == {"lock_version", "spec", "rules", "targets", "providers", "contracts", "schemas", "scan"}
    l1 = lock["rules"]["L-1"]
    assert set(l1["doc"]) == {"statement", "owner", "class", "mediation.chokepoint"}
    assert "statement" not in l1["semantic"] and "chokepoint" not in l1["semantic"]["mediation"]
    assert l1["semantic"]["enforcers"][0]["on_error"] == "fail_closed"
    assert l1["semantic"]["mediation"]["sink"] == "py:ledger.store.write_balance"


def test_defaulted_enforcer_fields_do_not_count_as_a_change(app):
    _edit(app / "invariants.yaml", "on_violation: reject, on_error: fail_closed}", "}")
    assert _check(app).returncode == 0
