"""`inv-scan` (invariants_scan.__main__): the scan over a JSON rule document, with its exit codes."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from invariants_scan.__main__ import FAILED, OK, UNRESOLVED, USAGE, main
from scan_support import write_project

SHOP = {
    "shop/__init__.py": "",
    "shop/gateway.py": "def charge(order_id, cents):\n    ...\n",
    "shop/service.py": "from shop.gateway import charge\n\ndef checkout(o):\n    charge(o, 1)\n",
    "shop/admin.py": "from shop import gateway\n\ndef fix(o):\n    gateway.charge(o, -5)\n",
}


def _rules(tmp_path, **med):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps({"rules": [{"id": "PAY-1", "mediation": {
        "sink": "py:shop.gateway.charge", "door": "py:shop.service.checkout", **med}}]}))
    return p


def test_a_bypass_fails_and_an_allowed_one_passes(tmp_path, capsys):
    src = write_project(tmp_path / "src", SHOP)
    assert main([str(_rules(tmp_path)), "--root", str(src)]) == FAILED
    out = capsys.readouterr().out
    assert "bypass" in out and "shop.admin.fix -> shop.gateway.charge" in out and "PAY-1: fail" in out
    assert main([str(_rules(tmp_path, allow=["py:shop.admin.fix"])), "--root", str(src)]) == OK


def test_unresolved_only_is_its_own_exit_code(tmp_path):
    src = write_project(tmp_path / "src", {**SHOP, "shop/admin.py": "def fix(gw, o):\n    gw.charge(o, -5)\n"})
    assert main([str(_rules(tmp_path)), "--root", str(src)]) == UNRESOLVED


@pytest.mark.parametrize("fmt", ["json", "sarif"])
def test_json_and_sarif_to_a_file(tmp_path, fmt):
    src = write_project(tmp_path / "src", SHOP)
    out = tmp_path / f"report.{fmt}"
    assert main([str(_rules(tmp_path)), "--root", str(src), "--format", fmt, "--output", str(out)]) == FAILED
    data = json.loads(out.read_text())
    if fmt == "sarif":
        assert data["version"] == "2.1.0" and data["runs"][0]["results"][0]["ruleId"] == "bypass"
    else:
        assert [f["kind"] for f in data["findings"]] == ["bypass"]


def test_the_default_root_is_the_rule_documents_directory(tmp_path):
    write_project(tmp_path, SHOP)
    assert main([str(_rules(tmp_path))]) == FAILED


@pytest.mark.parametrize("text, err", [
    (None, "No such file"),
    ("not json", "Expecting value"),
    ("[1]", "a JSON object"),
    ('{"rules": [{"id": "X", "mediation": {"sink": 5}}]}', "mediation.sink"),
])
def test_usage_and_document_errors(tmp_path, capsys, text, err):
    p = tmp_path / "rules.json"
    if text is not None:
        p.write_text(text)
    assert main([str(p), "--root", str(tmp_path)]) == USAGE
    assert err in capsys.readouterr().err


def test_a_missing_root_is_a_usage_error(tmp_path, capsys):
    assert main([str(_rules(tmp_path)), "--root", str(tmp_path / "nope")]) == USAGE
    assert "does not exist" in capsys.readouterr().err


def test_python_dash_m_runs_it(tmp_path):
    src = write_project(tmp_path / "src", SHOP)
    r = subprocess.run([sys.executable, "-m", "invariants_scan", str(_rules(tmp_path)), "--root", str(src)],
                       capture_output=True, text=True)
    assert r.returncode == FAILED and "PAY-1: fail" in r.stdout
