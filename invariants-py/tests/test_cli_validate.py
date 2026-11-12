"""`inv validate`: schema, CLI-owned mediation fields, applies_to, and every load-time check."""
from __future__ import annotations

import yaml

from cli_support import BYPASS, CLEAN, run_inv


def _variant(tmp_path, mutate):
    doc = yaml.safe_load((CLEAN / "invariants.yaml").read_text())
    mutate(doc)
    p = tmp_path / "invariants.yaml"
    p.write_text(yaml.safe_dump(doc))
    return p


def test_both_apps_validate():
    for app in (CLEAN, BYPASS):
        proc = run_inv("validate", app / "invariants.yaml")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("ok: ")


def test_default_rule_file_in_cwd():
    proc = run_inv("validate", cwd=CLEAN)
    assert proc.returncode == 0 and "L-1, L-2, L-3" in proc.stdout


def test_schema_errors_are_listed(tmp_path):
    def bad(doc):
        doc["rules"][0]["status"] = "approved"
        doc["rules"][1]["colour"] = "red"
    proc = run_inv("validate", _variant(tmp_path, bad), "--path", CLEAN)
    assert proc.returncode == 1
    assert "schema: rules/0/status:" in proc.stderr
    assert "schema: rules/1:" in proc.stderr and "colour" in proc.stderr
    assert "load: " in proc.stderr


def test_load_check_error(tmp_path):
    def bad(doc):
        doc["rules"][0]["bad"] = "args.nope <= 0"
    proc = run_inv("validate", _variant(tmp_path, bad), "--path", CLEAN)
    assert proc.returncode == 1
    assert "args.nope does not resolve" in proc.stderr and "schema:" not in proc.stderr


def test_mediation_and_applies_to_checks(tmp_path):
    def bad(doc):
        doc["rules"][0]["mediation"]["sink"] = "ledger.store.write_balance"
        doc["rules"][2]["applies_to"] = ["py:nothing.*"]
    proc = run_inv("validate", _variant(tmp_path, bad), "--path", CLEAN)
    assert proc.returncode == 1
    assert "scan: L-1:" in proc.stderr and "lockfile: L-3: applies_to pattern" in proc.stderr


def test_missing_file_and_bad_yaml(tmp_path):
    proc = run_inv("validate", tmp_path / "none.yaml")
    assert proc.returncode == 1 and "invalid:" in proc.stderr
    (tmp_path / "x.yaml").write_text("rules: [\n")
    assert run_inv("validate", tmp_path / "x.yaml").returncode == 1


def test_no_rule_file_is_usage_error(tmp_path):
    proc = run_inv("validate", cwd=tmp_path)
    assert proc.returncode == 2 and "no rule file" in proc.stderr


def test_no_command_is_usage_error():
    proc = run_inv()
    assert proc.returncode == 2 and "choose a command" in proc.stderr
    assert run_inv("--help").returncode == 0
    assert run_inv("frobnicate").returncode == 2
