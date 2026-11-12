"""`inv lock` and `inv lock --check`."""
from __future__ import annotations

import json

import pytest

import invariants as inv
from invariants import lockfile
from cli_support import CLEAN, copy_app, run_inv


@pytest.fixture
def app(tmp_path):
    return copy_app(CLEAN, tmp_path / "clean")


def test_lock_contents(app):
    proc = run_inv("lock", app / "invariants.yaml")
    assert proc.returncode == 0, proc.stderr
    lock = json.loads((app / "invariants.lock").read_text())
    assert lock["lock_version"] == 3 and lock["spec"] == "invariant/v0.1"
    assert set(lock["rules"]) == {"L-1", "L-2", "L-3"}
    assert lock["rules"]["L-1"]["semantic"]["bad"] == "args.amount <= 0"
    assert lock["rules"]["L-1"]["doc"]["statement"].startswith("sha256:")
    assert lock["rules"]["L-3"]["targets"] == [
        "py:ledger.service.Ledger.close", "py:ledger.service.deposit", "py:ledger.service.withdraw"]
    assert "applies_to" not in lock["rules"]["L-3"]            # v3: the targets list holds the expansion
    t = lock["targets"]["py:ledger.service.withdraw"]
    assert t["signature"] == "withdraw(conn, account: str, amount: int) -> None"   # unwrapped through the guard
    assert t["effects"] == "external" and "signature_hash" not in t
    assert t["types"] == {"account": "str", "amount": "int", "return": "null"}   # resolved, not annotation text
    assert lock["contracts"]["ledger_py"] == {"ref": "ledger.service", "type": "python"}   # no module hash
    assert run_inv("lock", "--check", app / "invariants.yaml").returncode == 0


def test_lock_is_deterministic(app):
    run_inv("lock", app / "invariants.yaml")
    first = (app / "invariants.lock").read_text()
    run_inv("lock", app / "invariants.yaml")
    assert (app / "invariants.lock").read_text() == first


def test_check_without_lock_fails(app):
    proc = run_inv("lock", "--check", app / "invariants.yaml")
    assert proc.returncode == 1 and "no lockfile" in proc.stderr


def test_rule_change_makes_lock_stale(app):
    run_inv("lock", app / "invariants.yaml")
    rf = app / "invariants.yaml"
    rf.write_text(rf.read_text().replace("bad: args.amount <= 0", "bad: args.amount < 0"))
    proc = run_inv("lock", "--check", rf)
    assert proc.returncode == 1
    assert "rule L-1: bad: 'args.amount <= 0' -> 'args.amount < 0'  WEAKENED" in proc.stderr


def test_signature_change_makes_lock_stale(app):
    run_inv("lock", app / "invariants.yaml")
    svc = app / "ledger" / "service.py"
    svc.write_text(svc.read_text().replace("def deposit(conn, account: str, amount: int)",
                                           "def deposit(conn, account: str, amount: int, memo: str = '')"))
    proc = run_inv("lock", "--check", app / "invariants.yaml")
    assert proc.returncode == 1
    assert "target py:ledger.service.deposit: signature:" in proc.stderr
    assert "contract ledger_py" not in proc.stderr            # pinned per target, not per module


def test_effects_change_makes_lock_stale(app):
    run_inv("lock", app / "invariants.yaml")
    rf = app / "invariants.yaml"
    rf.write_text(rf.read_text().replace('"py:ledger.service.deposit": {effects: external}',
                                         '"py:ledger.service.deposit": {effects: none}'))
    proc = run_inv("lock", "--check", rf)
    assert proc.returncode == 1
    assert "target py:ledger.service.deposit: effects: 'external' -> 'none'  WEAKENED" in proc.stderr


def test_invalid_rule_file_is_not_locked(app):
    rf = app / "invariants.yaml"
    rf.write_text(rf.read_text().replace("args.amount <= 0", "args.nope <= 0"))
    proc = run_inv("lock", rf)
    assert proc.returncode == 1 and not (app / "invariants.lock").exists()


def test_custom_lock_path(app, tmp_path):
    out = tmp_path / "elsewhere.lock"
    assert run_inv("lock", app / "invariants.yaml", "--lock", out).returncode == 0
    assert out.is_file() and run_inv("lock", "--check", app / "invariants.yaml", "--lock", out).returncode == 0


# --- in-process unit tests (no app imports) -----------------------------------------------------
def test_rule_hash_ignores_evidence_only():          # the version-1 pin, still used to migrate
    r = {"id": "R", "bad": "x"}
    assert lockfile.rule_hash(r) == lockfile.rule_hash({**r, "evidence": {"runs": 3}})
    assert lockfile.rule_hash(r) != lockfile.rule_hash({**r, "bad": "y"})
    assert lockfile.rule_hash({"a": 1, "b": 2}) == lockfile.rule_hash({"b": 2, "a": 1})


def test_expand_applies_to():
    doc = {"targets": {"py:a.x": {}, "py:a.y": {}, "py:b.z": {}},
           "rules": [{"id": "R", "applies_to": "py:a.*"}, {"id": "S", "applies_to": ["py:b.*", "py:a.x"]},
                     {"id": "T"}]}
    assert lockfile.expand_applies_to(doc) == {"R": ["py:a.x", "py:a.y"], "S": ["py:a.x", "py:b.z"]}
    with pytest.raises(inv.LoadError, match="matches no entry"):
        lockfile.expand_applies_to({"targets": {}, "rules": [{"id": "R", "applies_to": "py:q.*"}]})
    with pytest.raises(inv.LoadError, match="applies_to must be"):
        lockfile.expand_applies_to({"targets": {}, "rules": [{"id": "R", "applies_to": []}]})


def test_diff():
    a = {"lock_version": 2, "spec": "s", "rules": {"R": {"semantic": {"bad": "x > 1"}}, "Gone": {}},
         "targets": {"py:t": {"signature": "f(a)"}}, "contracts": {}}
    b = {"lock_version": 2, "spec": "s", "rules": {"R": {"semantic": {"bad": "x > 2"}}, "New": {}},
         "targets": {"py:t": {"signature": "f(a, b)"}}, "contracts": {}}
    assert lockfile.diff(a, a) == []
    assert lockfile.diff(a, b) == [
        "rule Gone: removed  WEAKENED (the rule no longer exists)", "rule New: added",
        "rule R: bad: 'x > 1' -> 'x > 2'  WEAKENED (the condition now matches fewer cases)",
        "target py:t: signature: 'f(a)' -> 'f(a, b)'"]


def test_file_contract_hash(tmp_path):
    (tmp_path / "api.proto").write_text("syntax = 'proto3';")
    e = lockfile.contract_hash("api", {"type": "protobuf", "ref": "api.proto"}, tmp_path)
    assert e["hash"].startswith("sha256:")
    with pytest.raises(inv.LoadError, match="not found"):
        lockfile.contract_hash("api", {"type": "protobuf", "ref": "missing.proto"}, tmp_path)
