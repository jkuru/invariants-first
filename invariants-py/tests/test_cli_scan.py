"""`inv scan`: static call-graph mediation check. Pure ast, so these run in-process."""
from __future__ import annotations

import json
import textwrap

import jsonschema
import pytest

import invariants as inv
from invariants import cli, scan
from cli_support import BYPASS, CLEAN, run_inv, write_project


def _doc(sink="py:app.store.write", door="py:app.svc.door", tables=None, **extra):
    med = {"chokepoint": "door", "residual_paths": [], "sink": sink}
    if door is not None:
        med["door"] = door
    if tables:
        med["tables"] = tables
    return {"rules": [{"id": "R-9", "status": "enforce", "subject": {"target": "py:app.svc.door"},
                       "bad": "false", "mediation": {**med, **extra}}]}


BASE = {
    "app/__init__.py": "",
    "app/store.py": "def write(conn, v):\n    conn.execute('UPDATE things SET v = ?', (v,))\n",
    "app/svc.py": "from app import store\n\ndef door(conn, v):\n    store.write(conn, v)\n",
}


def _scan(tmp_path, extra, doc=None):
    files = {**BASE, **{k: textwrap.dedent(v) for k, v in extra.items()}}
    root = write_project(tmp_path, files)
    return scan.scan(doc or _doc(tables=["things"]), [root])


def _codes(report):
    return sorted((f.kind, f.code, f.file, f.line) for f in report.findings)


# --- the two fixture apps -----------------------------------------------------------------------
def test_clean_app_passes():
    r = scan.scan(inv.read_document(CLEAN / "invariants.yaml"), [CLEAN])
    assert r.findings == []
    assert r.rule_result("L-1") == "pass"
    assert r.rule_result("L-2") == "not_scanned" and r.not_scanned["L-2"] == "mediation declares no sink"


def test_bypass_app_every_finding():
    r = scan.scan(inv.read_document(BYPASS / "invariants.yaml"), [BYPASS])
    assert _codes(r) == [
        ("bypass", "bypass", "fleet/retry.py", 11),
        ("forbidden", "forbidden/getattr-sink", "fleet/ops.py", 6),
        ("forbidden", "forbidden/raw-sql", "fleet/ops.py", 10),
        ("forbidden", "forbidden/unwrap", "fleet/ops.py", 14),
        ("unresolved", "unresolved/computed-attribute", "fleet/ops.py", 18),
        ("unresolved", "unresolved/sink-escapes", "fleet/ops.py", 21),
    ]
    bypass = r.findings[0]
    assert bypass.path == ["fleet.retry.main", "fleet.retry.retry_resize", "fleet.store.set_size"]
    assert r.rule_result("F-1") == "fail"
    assert r.counts("F-1") == {"bypass": 1, "forbidden": 3, "unresolved": 2}


def test_cli_exit_codes_and_text(capsys):
    assert cli.main(["scan", str(CLEAN / "invariants.yaml")]) == cli.OK
    assert "L-1: pass" in capsys.readouterr().out
    assert cli.main(["scan", str(BYPASS / "invariants.yaml")]) == cli.FAILED
    out = capsys.readouterr().out
    assert "fleet/retry.py:11" in out and "F-1: fail" in out


def test_unresolved_only_exits_3(tmp_path, capsys):
    root = write_project(tmp_path / "p", {**BASE, "app/odd.py": "from app import store\nH = [store.write]\n"})
    (tmp_path / "rules.json").write_text(json.dumps(_doc()))
    assert cli.main(["scan", str(tmp_path / "rules.json"), "--root", str(root)]) == cli.UNRESOLVED
    capsys.readouterr()


def test_missing_root_is_usage_error(tmp_path, capsys):
    (tmp_path / "rules.json").write_text(json.dumps(_doc()))
    assert cli.main(["scan", str(tmp_path / "rules.json"), "--root", str(tmp_path / "nope")]) == cli.USAGE
    assert "does not exist" in capsys.readouterr().err


def test_json_format(tmp_path):
    out = tmp_path / "r.json"
    assert cli.main(["scan", str(BYPASS / "invariants.yaml"), "--format", "json", "--output", str(out)]) == 1
    data = json.loads(out.read_text())
    assert data["rules"]["F-1"]["result"] == "fail" and len(data["findings"]) == 6


SARIF_MIN = {   # the parts of SARIF 2.1.0 that code-scanning uploads need
    "type": "object", "required": ["version", "runs"],
    "properties": {"version": {"const": "2.1.0"}, "runs": {"type": "array", "minItems": 1, "items": {
        "type": "object", "required": ["tool", "results"],
        "properties": {"tool": {"required": ["driver"], "properties": {"driver": {"required": ["name", "rules"]}}},
                       "results": {"type": "array", "items": {
                           "required": ["ruleId", "level", "message"],
                           "properties": {"level": {"enum": ["error", "warning", "note", "none"]},
                                          "message": {"required": ["text"]}}}}}}}},
}


def test_sarif_output(tmp_path):
    out = tmp_path / "scan.sarif"
    proc = run_inv("scan", BYPASS / "invariants.yaml", "--format", "sarif", "--output", out)
    assert proc.returncode == 1, proc.stderr
    sarif = json.loads(out.read_text())
    jsonschema.validate(sarif, SARIF_MIN)
    run = sarif["runs"][0]
    ids = [r["id"] for r in run["tool"]["driver"]["rules"]]
    assert "bypass" in ids and "forbidden/raw-sql" in ids
    res = {r["ruleId"]: r for r in run["results"]}
    b = res["bypass"]
    assert b["level"] == "error" and b["properties"]["invRule"] == "F-1"
    assert b["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "fleet/retry.py"
    assert b["locations"][0]["physicalLocation"]["region"]["startLine"] == 11
    assert len(b["codeFlows"][0]["threadFlows"][0]["locations"]) == 3
    assert res["unresolved/sink-escapes"]["level"] == "warning"
    assert all(run["tool"]["driver"]["rules"][r["ruleIndex"]]["id"] == r["ruleId"] for r in run["results"])


# --- graph semantics ----------------------------------------------------------------------------
def test_helper_called_only_by_door_is_not_a_bypass(tmp_path):
    r = _scan(tmp_path, {"app/svc.py": """
        from app import store
        def door(conn, v):
            _helper(conn, v)
        def _helper(conn, v):
            store.write(conn, v)
        def api(conn):
            door(conn, 1)
        """})
    assert r.findings == []


def test_direct_call_from_entrypoint_is_a_bypass(tmp_path):
    r = _scan(tmp_path, {"app/job.py": """
        from app.store import write as w
        def nightly(conn):
            w(conn, 0)
        """})
    assert [(f.kind, f.path) for f in r.findings] == [("bypass", ["app.job.nightly", "app.store.write"])]


def test_module_level_call_is_a_bypass(tmp_path):
    r = _scan(tmp_path, {"app/boot.py": "import app.store\napp.store.write(None, 1)\n"})
    assert [f.path for f in r.findings] == [["app.boot.<module>", "app.store.write"]]


def test_reexport_and_star_import_are_followed(tmp_path):
    r = _scan(tmp_path, {
        "app/api.py": "from app.store import write\n",
        "app/a.py": "from app.api import write\ndef f(c):\n    write(c, 1)\n",
        "app/b.py": "from app.store import *\ndef g(c):\n    write(c, 2)\n",
    })
    assert sorted(f.path[0] for f in r.findings if f.kind == "bypass") == ["app.a.f", "app.b.g"]


def test_relative_import(tmp_path):
    r = _scan(tmp_path, {"app/sub/__init__.py": "", "app/sub/x.py": "from .. import store\ndef f(c):\n    store.write(c, 1)\n"})
    assert [f.path[0] for f in r.findings] == ["app.sub.x.f"]


def test_method_sink_through_self_and_inheritance(tmp_path):
    files = {
        "app/store.py": """
            class Base:
                def write(self, v):
                    pass
            class Store(Base):
                def flush(self):
                    self.write(1)
            """,
        "app/svc.py": "def door(s, v):\n    s.write(v)\n",
    }
    r = _scan(tmp_path, files, _doc(sink="py:app.store.Store.write"))
    kinds = sorted((f.kind, f.code, f.path[0] if f.path else None) for f in r.findings)
    # Store.write resolves to Base.write; flush calls it through self: an extracted edge, so a bypass.
    assert ("bypass", "bypass", "app.store.Store.flush") in kinds
    # door calls .write on an unknown receiver: allowed inside the door, so nothing there.
    assert all(f.path[:1] != ["app.svc.door"] for f in r.findings)


def test_name_matched_path_is_unresolved_not_bypass(tmp_path):
    files = {
        "app/store.py": "def write(c, v):\n    pass\n",
        "app/worker.py": """
            from app import store
            class Worker:
                def run(self, c):
                    store.write(c, 1)
            def main(w):
                w.run(None)
            """,
    }
    r = _scan(tmp_path, files)
    by_start = {f.path[0]: f for f in r.findings if f.path}
    assert by_start["app.worker.main"].kind == "unresolved"
    assert by_start["app.worker.main"].code == "unresolved/inferred-path"
    assert by_start["app.worker.Worker.run"].kind == "bypass"   # only an inferred caller: still an entrypoint


def test_unknown_receiver_with_sink_name_is_unresolved(tmp_path):
    r = _scan(tmp_path, {"app/x.py": "def f(thing):\n    thing.write(None, 1)\n"})
    assert [f.code for f in r.findings] == ["unresolved/unknown-receiver"]


def test_cycle_unreachable_from_roots_still_checked(tmp_path):
    r = _scan(tmp_path, {"app/x.py": """
        from app import store
        def a(c):
            b(c)
        def b(c):
            a(c)
            store.write(c, 1)
        """})
    assert [f.kind for f in r.findings] == ["bypass"]


def test_getattr_literal_non_sink_is_followed(tmp_path):
    r = _scan(tmp_path, {
        "app/helpers.py": "from app import store\ndef h(c):\n    store.write(c, 1)\n",
        "app/x.py": "from app import helpers\ndef f(c):\n    getattr(helpers, 'h')(c)\n",
    })
    assert [f.path for f in r.findings] == [["app.x.f", "app.helpers.h", "app.store.write"]]


def test_sink_equal_to_door_has_no_paths(tmp_path):
    r = _scan(tmp_path, {"app/x.py": "from app import svc\ndef f(c):\n    svc.door(c, 1)\n"},
              _doc(sink="py:app.svc.door", door="py:app.svc.door"))
    assert r.findings == [] and r.rule_result("R-9") == "pass"


def test_door_defaults_to_subject_target(tmp_path):
    doc = _doc(door=None)
    r = _scan(tmp_path, {}, doc)
    assert r.scanned["R-9"].doors == ["py:app.svc.door"] and r.findings == []


# --- idioms -------------------------------------------------------------------------------------
@pytest.mark.parametrize("src,code", [
    ("from app import store\ndef f():\n    store.write = None\n", "forbidden/monkeypatch"),
    ("from app import svc\ndef f():\n    setattr(svc, 'door', None)\n", "forbidden/monkeypatch"),
    ("from app import svc\ndef f(c):\n    svc.door.__wrapped__(c, 1)\n", "forbidden/unwrap"),
    ("from app import svc\ndef f(c):\n    getattr(svc.door, '__wrapped__')(c, 1)\n", "forbidden/unwrap"),
    ("import operator\ndef f(s):\n    operator.methodcaller('write')(s)\n", "forbidden/getattr-sink"),
    ("def f(c):\n    c.execute('DELETE FROM things')\n", "forbidden/raw-sql"),
    ("def f(c):\n    c.execute('insert into \"things\" (v) values (1)')\n", "forbidden/raw-sql"),
    ("def f(c, t):\n    c.execute(f'UPDATE things SET v = {t}')\n", "forbidden/raw-sql"),
    ("def f(c):\n    c.execute('DROP TRIGGER guard_things')\n", "forbidden/raw-sql"),
    ("def f(code):\n    eval(code)\n", "unresolved/reflection"),
    ("def f(code):\n    exec(code)\n", "unresolved/reflection"),
    ("def f():\n    return globals()['x']\n", "unresolved/reflection"),
    ("import importlib\ndef f(n):\n    importlib.import_module(n)\n", "unresolved/reflection"),
    ("def f(o, n):\n    setattr(o, n, None)\n", "unresolved/computed-attribute"),
    ("from app import store\ndef f():\n    return lambda c: store.write\n", "unresolved/sink-escapes"),
    ("def f(:\n", "unresolved/parse-error"),
])
def test_idioms(tmp_path, src, code):
    r = _scan(tmp_path, {"app/x.py": src})
    assert [f.code for f in r.findings] == [code], r.findings


@pytest.mark.parametrize("src", [
    '"""UPDATE things SET v = 1 in a docstring is not SQL."""\n',
    "def f(c):\n    c.execute('SELECT v FROM things')\n",
    "def f(c):\n    c.execute('UPDATE other SET v = 1')\n",
    "def f(o):\n    return hasattr(o, 'anything')\n",
    "def f(o):\n    return getattr(o, 'colour', None)\n",
    "def f(c):\n    print('please update things soon')\n",
])
def test_harmless_code_passes(tmp_path, src):
    assert _scan(tmp_path, {"app/x.py": src}).findings == []


def test_sql_in_sink_and_door_is_allowed(tmp_path):
    r = _scan(tmp_path, {"app/svc.py": "from app import store\ndef door(c, v):\n    c.execute('DELETE FROM things')\n    store.write(c, v)\n"})
    assert r.findings == []


def test_missing_sink_and_door(tmp_path):
    r = _scan(tmp_path, {}, _doc(sink="py:app.store.nope", door="py:app.svc.gone"))
    assert sorted(f.code for f in r.findings) == ["missing/door", "missing/sink"]
    assert r.rule_result("R-9") == "fail"


@pytest.mark.parametrize("med,msg", [
    ({"sink": 3}, "mediation.sink"),
    ({"sink": []}, "mediation.sink"),
    ({"sink": "app.store.write"}, "py:"),
    ({"sink": "py:a.b", "door": [1]}, "mediation.door"),
    ({"sink": "py:a.b", "tables": "pools"}, None),
    ({"door": "py:a.b"}, "need mediation.sink"),
])
def test_mediation_config(med, msg):
    doc = {"rules": [{"id": "R-9", "status": "enforce", "subject": {"target": "py:a.c"}, "bad": "x",
                      "mediation": med}]}
    if msg is None:
        assert scan.mediations(doc)[0]["R-9"].tables == ["pools"]
    else:
        with pytest.raises(scan.ScanConfigError, match=msg):
            scan.mediations(doc)


def test_retired_rules_are_not_scanned():
    doc = {"rules": [{"id": "R-9", "status": "retired", "mediation": {"sink": "py:a.b"}}]}
    assert scan.mediations(doc) == ({}, {"R-9": "retired"})


def test_scan_never_imports(tmp_path):
    root = write_project(tmp_path, {**BASE, "app/boom.py": "raise SystemExit('imported!')\n"})
    assert scan.scan(_doc(), [root]).findings == []
