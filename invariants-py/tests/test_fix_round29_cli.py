"""Round 29, CLI messages and OpenAPI parameters. Each item below failed before its fix.

1. `inv check`'s hint for an unresolved site named mediation.allow_unresolved for every rule; in a
   door:/sink: rule, writing it there was refused ("give allow_unresolved at the top of the rule or
   under mediation, not both") though it was given once. The hint now names the spelling of the
   rule's form, the short form accepts mediation.allow_unresolved when it is not also at the top, and
   an entry error in a top-level allow_unresolved names allow_unresolved[0], not
   mediation.allow_unresolved[0].
2. `inv check --base` listed every accepted unresolved site, with nothing marking one accepted since
   the base.
3. `inv status --ablation` said `ablation proved it: no` for a stale report (now `stale (re-run)`), and
   `not needed` for a door-only rule whose falsifiers the report shows load-bearing (now says so).
4. An OpenAPI operation's parameters were compared by position: removing one shifted the rest, so a
   removed required parameter (or any removal) was never WEAKENED, and allowEmptyValue turned on was
   not either. They are now matched by (in, name). A Swagger 2.0 parameter's schema fields (type,
   maximum, enum, items, ...) sit on the parameter itself and were not pinned at all: they are now
   pinned as the schema they make, and judged (Draft 4).
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

import pytest

import invariants as inv
from cli_support import run_inv, write_project
from invariants import lock as lockmod
from invariants import status_report
from invariants.errors import LoadError
from invariants.loader import expand_short_forms
from invariants.registry.coverage import rule_digest

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

FILES = {
    "r29c/__init__.py": "",
    "r29c/store.py": "def set_size(conn, n):\n    conn.execute(n)\n",
    "r29c/service.py": ("from r29c import store\n\n\ndef resize(conn, n: int) -> None:\n"
                        "    store.set_size(conn, n)\n"),
    "r29c/retry.py": "from r29c import store\n\n\ndef retry(conn, n, name):\n    getattr(store, name)(conn, n)\n",
}
SHORT = 'rules:\n  - id: F-1\n    door: "py:r29c.service.resize"\n    sink: "py:r29c.store.set_size"\n'
LONG = ('rules:\n  - id: F-1\n    bad: "false"\n    enforcers: [{point: structure, timing: pre_effect, mode: enforce}]\n'
        '    status: enforce\n'
        '    mediation: {door: "py:r29c.service.resize", sink: "py:r29c.store.set_size"}\n')


def _project(tmp_path: pathlib.Path, rules: str, extra: dict[str, str] | None = None) -> pathlib.Path:
    return write_project(tmp_path / "p", {**FILES, **(extra or {}), "invariants.yaml": rules})


def _sha(root: pathlib.Path, file: str) -> str:
    r = run_inv("scan", "--format", "json", cwd=root)
    return next(f["sha"] for f in json.loads(r.stdout)["findings"] if f["file"] == file)


def _entry(root: pathlib.Path, file: str, why: str) -> str:
    return (f'{{at: "{file}:5:5", code: unresolved/computed-attribute, sha: "{_sha(root, file)}", '
            f'why: "{why}"}}')


# --- 1. the hint names the rule's spelling; the short form takes mediation.allow_unresolved ---------
@pytest.mark.parametrize("rules,where", [
    (SHORT, "accept it in allow_unresolved (at the top of the rule, beside door: and sink:)"),
    (LONG, "accept it in mediation.allow_unresolved (at, code, sha, why)")])
def test_the_hint_names_the_spelling_of_the_rules_form(tmp_path, rules, where):
    root = _project(tmp_path, rules)
    assert run_inv("lock", cwd=root).returncode == 0
    c = run_inv("check", "--strict-scan", cwd=root)   # unresolved sites block only when strict
    assert c.returncode == 1 and "FAIL  scan" in c.stdout and f"hint: a person who reviewed an unresolved site can {where}" \
        in c.stdout, c.stdout


def test_the_hint_names_both_spellings_when_both_forms_fail():
    from invariants.cli.commands import unresolved_hint
    got = unresolved_hint(["A", "B"], {"A"})
    assert "allow_unresolved at the top of A (door:/sink: rules) or mediation.allow_unresolved in B" in got


@pytest.mark.parametrize("place", ["top", "mediation"])
def test_a_short_form_rule_accepts_the_site_either_way(tmp_path, place):
    root = _project(tmp_path, SHORT)
    e = _entry(root, "r29c/retry.py", "only reads")
    add = f"    allow_unresolved: [{e}]\n" if place == "top" else f"    mediation:\n      allow_unresolved: [{e}]\n"
    (root / "invariants.yaml").write_text(SHORT + add)
    v = run_inv("validate", cwd=root)
    assert v.returncode == 0, v.stdout + v.stderr
    assert run_inv("lock", cwd=root).returncode == 0
    c = run_inv("check", cwd=root)
    assert c.returncode == 0 and "1 reviewed unresolved site(s) accepted" in c.stdout, c.stdout + c.stderr


def test_given_at_both_places_is_still_refused():
    doc = {"rules": [{"id": "F", "door": "py:a.b", "sink": "py:a.c", "allow_unresolved": [],
                      "mediation": {"allow_unresolved": []}}]}
    with pytest.raises(LoadError, match="give allow_unresolved at the top of the rule or under mediation, not both"):
        expand_short_forms(doc)


def test_half_of_each_form_says_which_half():
    doc = {"rules": [{"id": "F", "sink": "py:a.c", "mediation": {"door": "py:a.b"}}]}
    with pytest.raises(LoadError, match=r"gives sink: at the top \(the door:/sink: form\), so give door at the top"):
        expand_short_forms(doc)


def test_an_empty_why_at_the_top_names_the_field_as_written(tmp_path):
    root = _project(tmp_path, SHORT)
    e = _entry(root, "r29c/retry.py", "")
    (root / "invariants.yaml").write_text(SHORT + f"    allow_unresolved: [{e}]\n")
    for args in (("validate",), ("check",), ("status",)):
        r = run_inv(*args, cwd=root)
        out = r.stdout + r.stderr
        assert r.returncode != 0 and "F-1: allow_unresolved[0]: why must say why" in out, (args, out)
        assert "mediation.allow_unresolved[0]" not in out, (args, out)


def test_an_empty_why_under_mediation_keeps_the_long_name(tmp_path):
    root = _project(tmp_path, LONG.replace('sink: "py:r29c.store.set_size"}',
                                           'sink: "py:r29c.store.set_size", allow_unresolved: '
                                           '[{at: "r29c/retry.py:5:5", code: unresolved/computed-attribute, '
                                           'sha: "0123456789abcdef", why: ""}]}'))
    v = run_inv("validate", cwd=root)
    assert v.returncode == 1 and "F-1: mediation.allow_unresolved[0]: why must say why" in v.stderr, v.stderr


# --- 2. --base marks the sites accepted since the base ----------------------------------------------
def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, env=GIT_ENV, check=True, capture_output=True)


@needs_git
def test_check_base_marks_a_site_accepted_since_the_base(tmp_path):
    again = "from r29c import store\n\n\ndef again(conn, n, name):\n    getattr(store, name)(conn, n)\n"
    root = _project(tmp_path, SHORT, {"r29c/again.py": again})
    old = _entry(root, "r29c/again.py", "reviewed")
    new = _entry(root, "r29c/retry.py", "self-accepted")
    (root / "invariants.yaml").write_text(SHORT + f"    allow_unresolved:\n      - {old}\n")
    assert run_inv("lock", cwd=root).returncode == 0
    _git(root, "init", "-q", ".")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    (root / "invariants.yaml").write_text(SHORT + f"    allow_unresolved:\n      - {old}\n      - {new}\n")
    assert run_inv("lock", "--accept-weakening", cwd=root).returncode == 0
    c = run_inv("check", "--base", "HEAD", "--accept-weakening", cwd=root)
    assert c.returncode == 0, c.stdout + c.stderr
    lines = c.stdout.splitlines()
    assert any("2 reviewed unresolved site(s) accepted by allow_unresolved (1 new since HEAD)" in x for x in lines), lines
    (retry,) = [x for x in lines if x.strip().startswith("accepted") and "r29c/retry.py" in x]
    (kept,) = [x for x in lines if x.strip().startswith("accepted") and "r29c/again.py" in x]
    assert retry.endswith("[new since HEAD]") and "new since" not in kept, lines
    plain = run_inv("check", cwd=root)                       # without --base nothing is "new"
    assert "new since" not in plain.stdout, plain.stdout


# --- 3. inv status: a stale report, and a door-only rule's load-bearing falsifiers ------------------
GUARDED = ("import invariants as inv\n\n\n@inv.guard(\"R-1\")\ndef refund(amount: int) -> None:\n    pass\n")


def _rules(pkg: str) -> dict:
    door, sink = (f"py:{pkg}.service.refund", f"py:{pkg}.service.write") if pkg != "json" else \
        ("py:json.dumps", "py:json.loads")
    return {"rules": [
        {"id": "R-1", "guard": door, "bad": "false", "tests": ["tests/test_x.py::test_r1"]},
        {"id": "D-1", "door": door, "sink": sink, "tests": ["tests/test_x.py::test_d1"]}]}


DIGESTS = ("import json, sys\nimport invariants as inv\nfrom invariants.registry.coverage import rule_digest\n"
           "reg = inv.load(sys.argv[1], install=False)\n"
           "print(json.dumps({rid: rule_digest(r) for rid, r in reg.rules.items()}))\n")


def _report(digests: dict[str, str], *, stale: bool = False) -> dict:
    rows = {rid: [{"falsifier": f"tests/test_x.py::test_{rid.lower()}", "result": "caught"}] for rid in digests}
    return {"ablation": {"digests": {rid: ("0" * 64 if stale else d) for rid, d in digests.items()}, "rules": rows}}


def test_status_says_a_stale_report_is_stale_and_shows_a_door_rules_report():
    reg = inv.load(_rules("json"), install=False)          # json.dumps and json.loads stand in: nothing imported
    door = reg.rules["D-1"]
    digests = {rid: rule_digest(r) for rid, r in reg.rules.items()}
    assert status_report._door_only(door)
    assert status_report._report_says(door, _report(digests)) == "load-bearing"
    assert status_report._report_says(door, _report(digests, stale=True)) == "stale"
    assert status_report._report_says(door, {"ablation": {"rules": {}}}) is None
    assert status_report._report_says(door, None) is None
    stale_row = {"ablation": status_report.ablation_state(reg.rules["R-1"], _report(digests, stale=True)),
                 "reasons": ["ablation: the report is for another version of the rule (...)"]}
    assert stale_row["ablation"] == "no"                       # the JSON state is unchanged
    assert status_report._ablation_line(stale_row) == "stale (re-run)"
    assert status_report._ablation_line({"ablation": "no", "reasons": ["ablation: no falsifier caught it"]}) == "no"
    assert status_report._ablation_line({"ablation": "not needed", "ablation_report": "load-bearing"}) == \
        "not needed (the report shows it load-bearing)"
    assert status_report._ablation_line({"ablation": "not needed"}) == "not needed"


def test_status_text_with_an_ablation_report(tmp_path):
    pkg = "r29st2"
    root = write_project(tmp_path / "s", {f"{pkg}/__init__.py": "",
                                          f"{pkg}/service.py": GUARDED + "\n\ndef write(x):\n    pass\n",
                                          "invariants.json": json.dumps(_rules(pkg))})
    (root / "digests.py").write_text(DIGESTS)        # the app is imported in a child, never here
    got = subprocess.run([sys.executable, "digests.py", "invariants.json"], cwd=root, capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": os.pathsep.join([str(root), os.environ.get("PYTHONPATH", "")])})
    digests = json.loads(got.stdout)
    for stale, r1, d1 in ((False, "yes", "not needed (the report shows it load-bearing)"),
                          (True, "stale (re-run)", "not needed (the report for it is stale)")):
        (root / "abl.json").write_text(json.dumps(_report(digests, stale=stale)))
        s = run_inv("status", "invariants.json", "--import", f"{pkg}.service", "--ablation", "abl.json", cwd=root)
        lines = s.stdout.splitlines()
        rows = {x.split(" ")[0]: i for i, x in enumerate(lines) if not x.startswith(" ")}
        assert f"  ablation proved it: {r1}" in lines[rows["R-1"]:rows["D-1"]], s.stdout + s.stderr
        assert f"  ablation proved it: {d1}" in lines[rows["D-1"]:], s.stdout + s.stderr
        j = json.loads(run_inv("status", "invariants.json", "--import", f"{pkg}.service", "--ablation", "abl.json",
                               "--json", cwd=root).stdout)
        door = next(r for r in j["rules"] if r["id"] == "D-1")
        assert door["ablation"] == "not needed", door
        assert door["ablation_report"] == ("stale" if stale else "load-bearing")


# --- 4. OpenAPI parameters matched by (in, name); Swagger 2.0 inline schemas pinned ------------------
def _pin(spec: dict) -> dict:
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "api.json").write_text(json.dumps(spec))
    pins = lockmod.Schemas()
    pin = lockmod.contract_hash("api", {"type": "openapi", "ref": "api.json"}, d, ["a"], pins)
    shutil.rmtree(d)
    return {"contracts": {"api": pin}, "schemas": pins.table}


def _oa(params: list, version: str = "3.0.3") -> dict:
    return {"openapi": version, "paths": {"/a": {"get": {"operationId": "a", "responses": {}, "parameters": params}}}}


def _sw(params: list) -> dict:
    return {"swagger": "2.0", "paths": {"/a": {"get": {"operationId": "a", "responses": {}, "parameters": params}}}}


def _q(name: str, required: bool = False, **schema) -> dict:
    return {"name": name, "in": "query", "required": required, "schema": schema or {"type": "integer"}}


def _s2(name: str, required: bool = False, **fields) -> dict:
    return {"name": name, "in": "query", "required": required, **(fields or {"type": "integer"})}


def _changes(old: dict, new: dict) -> list:
    return lockmod.changes(_pin(old), _pin(new))


def _accepts(params: list, request: dict) -> bool:
    """Whether a request (a mapping of query values) passes the parameters as a validator reads them:
    every required parameter present, each value valid under its schema (an unknown one is ignored)."""
    jsonschema = pytest.importorskip("jsonschema")
    for p in params:
        schema = p.get("schema") or {k: v for k, v in p.items() if k not in ("name", "in", "required")}
        if p["name"] not in request:
            if p.get("required"):
                return False
            continue
        v = jsonschema.Draft4Validator(schema)
        if not v.is_valid(request[p["name"]]):
            return False
    return True


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0"])
@pytest.mark.parametrize("old,new,request_", [
    ([_q("a", True), _q("b")], [_q("b")], {}),                                     # required removed
    ([_q("a", type="integer", maximum=5), _q("b")], [_q("b")], {"a": 9}),          # optional removed
    ([_q("a", type="integer", maximum=5), _q("b", type="integer", maximum=5)],     # a removed: b not misread
     [_q("b", type="integer", maximum=5)], {"a": 9}),
    ([_q("a", True)], [_q("a", False)], {}),                                       # required true -> false
    ([_q("a", True, type="integer", maximum=5)], [_q("c")], {}),                   # renamed: a is gone
])
def test_a_removed_or_loosened_parameter_is_weakened(old, new, request_, version):
    assert not _accepts(old, request_) and _accepts(new, request_)               # the instance shows it
    cs = _changes(_oa(old, version), _oa(new, version))
    assert any(c.weakened for c in cs), [str(c) for c in cs]
    assert all("parameters[0]" not in str(c) and "parameters[1]" not in str(c) for c in cs), [str(c) for c in cs]


def test_a_removed_required_parameter_is_named():
    cs = _changes(_oa([_q("a", True), _q("b")]), _oa([_q("b")]))
    assert [str(c) for c in cs if c.weakened] == [
        'contract api: operations.a.parameters[query a] removed: {"in": "query", "name": "a", "required": true, '
        '"schema": {"type": "integer"}} -> (none)  WEAKENED (required query parameter a removed: a request '
        'without it now passes)']


def test_parameters_matched_by_name_judge_each_one_against_its_old_self():
    old = [_q("a", type="integer", maximum=1), _q("b", type="integer", maximum=5)]
    cs = _changes(_oa(old), _oa([_q("b", type="integer", maximum=9)]))
    loosened = [c for c in cs if "parameters[query b].schema.maximum" in str(c)]
    assert loosened and loosened[0].weakened, [str(c) for c in cs]
    cs = _changes(_oa(old), _oa([_q("b", type="integer", maximum=3)]))       # b tightened, a removed
    assert [c.what for c in cs if c.weakened] == ["operations.a.parameters[query a] removed"], [str(c) for c in cs]


@pytest.mark.parametrize("old,new", [
    ([_q("b")], [_q("a", True), _q("b")]),                                        # a required one added
    ([_q("b")], [_q("a"), _q("b")]),                                              # an optional one added
    ([_q("a", False)], [_q("a", True)]),                                          # now required
])
def test_an_added_or_required_parameter_only_tightens(old, new):
    cs = _changes(_oa(old), _oa(new))
    assert cs and not any(c.weakened for c in cs), [str(c) for c in cs]


def test_allow_empty_value_turned_on_is_weakened():
    old, new = {**_q("a"), "allowEmptyValue": False}, {**_q("a"), "allowEmptyValue": True}
    cs = _changes(_oa([old]), _oa([new]))
    assert [c.what for c in cs if c.weakened] == ["operations.a.parameters[query a].allowEmptyValue"], cs
    assert not any(c.weakened for c in _changes(_oa([new]), _oa([old])))


def test_swagger_2_parameter_fields_are_pinned_as_a_schema():
    pin = _pin(_sw([_s2("a", type="array", items={"type": "string", "maxLength": 3}, maxItems=2,
                        collectionFormat="csv")]))
    (p,) = pin["contracts"]["api"]["operations"]["a"]["parameters"]
    assert p["schema"] == {"type": "array", "maxItems": 2, "items": {"type": "string", "maxLength": 3}}
    assert p["collectionFormat"] == "csv"
    body = {"name": "b", "in": "body", "schema": {"type": "object", "maxProperties": 1}}
    (b,) = _pin(_sw([body]))["contracts"]["api"]["operations"]["a"]["parameters"]
    assert b["schema"] == {"type": "object", "maxProperties": 1}      # a body parameter keeps its schema
    (o,) = _pin(_oa([{"name": "a", "in": "query", "type": "integer"}]))["contracts"]["api"]["operations"]["a"]["parameters"]
    assert "schema" not in o                                          # only Swagger 2.0 reads those fields


@pytest.mark.parametrize("old,new,value", [
    (dict(type="integer", maximum=5), dict(type="integer", maximum=9), 7),
    (dict(type="integer", maximum=5, exclusiveMaximum=True), dict(type="integer", maximum=5), 5),
    (dict(type="string", enum=["x"]), dict(type="string", enum=["x", "y"]), "y"),
    (dict(type="string", pattern="^a$"), dict(type="string"), "b"),
    (dict(type="array", items={"type": "string", "maxLength": 3}), dict(type="array", items={"type": "string"}),
     ["abcd"]),
    (dict(type="integer", minimum=1), dict(type="number", minimum=1), 1.5),
])
def test_a_loosened_swagger_2_parameter_is_weakened(old, new, value):
    assert not _accepts([_s2("a", **old)], {"a": value}) and _accepts([_s2("a", **new)], {"a": value})
    cs = _changes(_sw([_s2("a", **old)]), _sw([_s2("a", **new)]))
    assert any(c.weakened for c in cs), [str(c) for c in cs]
    back = _changes(_sw([_s2("a", **new)]), _sw([_s2("a", **old)]))
    assert back and not any(c.weakened for c in back), [str(c) for c in back]


def test_a_removed_swagger_2_parameter_is_weakened():
    cs = _changes(_sw([_s2("a", True), _s2("b")]), _sw([_s2("b")]))
    assert [c.what for c in cs if c.weakened] == ["operations.a.parameters[query a] removed"], [str(c) for c in cs]
