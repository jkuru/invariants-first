"""Fix round 13 (certification round 10 findings): every field that changes what `bad` reads or how a
check runs is a possible weakening, and every rule-file field is classified (R10-1); `bad` is compared
as a canonical tree (R10-2); resolved types are pinned, through aliases and into dataclass, TypedDict
and pydantic fields (R10-3); lock version 3 with a content-addressed schema table and
no prose in pins (R10-6); per-key diffs, type-strict compares and malformed locks (R10-7)."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import textwrap
import typing

import pydantic
import pytest

from invariants import lock as lockmod, scan
from invariants.cel import subset
from invariants.loader import schema_path
from invariants import model
from cli_support import CLEAN, copy_app, run_inv, write_project


# --- R10-1: the classification covers the whole schema ------------------------------------------------
_DEF_PREFIX = {"target": "target", "provider": "provider", "clause": "clause", "enforcer": "enforcer",
               "modelPolicy": "modelPolicy", "rule": "rule"}


def _schema_fields() -> set[str]:
    schema = json.loads(schema_path().read_text())
    out: set[str] = set()

    def walk(node: dict, prefix: str) -> None:
        for name, sub in (node.get("properties") or {}).items():
            here = f"{prefix}.{name}"
            out.add(here)
            if isinstance(sub, dict) and sub.get("type") == "object" and "properties" in sub:
                walk(sub, here)
    walk(schema, "file")
    for name, d in schema["$defs"].items():
        if "properties" in d:
            assert name in _DEF_PREFIX, f"$defs.{name} is new: give it a prefix in this test and FIELD_POLICY"
            walk(d, _DEF_PREFIX[name])
    return out


def test_every_schema_field_is_classified():
    fields = _schema_fields()
    missing = sorted(f for f in fields if f not in lockmod.FIELD_POLICY)
    assert not missing, f"classify these in lockmod.FIELD_POLICY (weakening, ranked, neutral, ...): {missing}"
    allowed = {lockmod.WEAKENING, lockmod.RANKED, lockmod.MORE_IS_WEAKER, lockmod.PRESENCE, lockmod.ANALYSED,
               lockmod.NEUTRAL, lockmod.PROSE, lockmod.COMPUTED, lockmod.PIN}
    assert set(lockmod.FIELD_POLICY.values()) <= allowed
    for loc, pol in lockmod.FIELD_POLICY.items():
        if pol == lockmod.RANKED:
            assert loc in lockmod.RANKS, loc


def test_the_loader_field_sets_agree_with_the_classification():
    for prefix, names in (("rule", model.RULE_FIELDS), ("enforcer", model.ENFORCER_FIELDS),
                          ("modelPolicy", model.POLICY_FIELDS), ("target", model.TARGET_FIELDS),
                          ("provider", model.PROVIDER_FIELDS), ("file", model.FILE_FIELDS),
                          ("rule.mediation", model.MEDIATION_FIELDS)):
        for n in names:
            assert f"{prefix}.{n}" in lockmod.FIELD_POLICY, f"{prefix}.{n}"


def test_an_unclassified_field_is_a_possible_weakening():
    assert lockmod.policy("rule.something_new") == lockmod.WEAKENING
    [c] = lockmod.changes({"rules": {"R": {"semantic": {"something_new": 1}}}},
                           {"rules": {"R": {"semantic": {"something_new": 2}}}})
    assert c.weakened and "possible weakening" in c.note


def _rule(sem_a, sem_b):
    return lockmod.changes({"rules": {"R": {"semantic": sem_a}}}, {"rules": {"R": {"semantic": sem_b}}})


@pytest.mark.parametrize("a,b,what", [
    ({"bind": {"amt": "args.amount"}}, {"bind": {"amt": "args.fee"}}, "bind.amt"),
    ({"bind": {"amt": "args.amount"}}, {}, "bind"),
    ({"subject": {"contract": "api", "operation": "a"}}, {"subject": {"contract": "api", "operation": "b"}},
     "subject.operation"),
    ({"subject": {"contract": "a"}}, {"subject": {"contract": "b"}}, "subject.contract"),
    ({"scope": "call"}, {"scope": "transition"}, "scope"),
    ({"compares_to": "held_data"}, {}, "compares_to"),
    ({"compensable": False}, {"compensable": True}, "compensable"),
    ({"model_policy": {"semantic_resamples": 0}}, {"model_policy": {"semantic_resamples": 3}},
     "model_policy.semantic_resamples"),
    ({"model_policy": {"format_retries": 2}}, {"model_policy": {"format_retries": 5}}, "model_policy.format_retries"),
    ({"model_policy": {"schema": "A"}}, {"model_policy": {"schema": "B"}}, "model_policy.schema"),
    ({"model_policy": {"abstain": {"field": "c", "value": "X"}}}, {"model_policy": {}}, "model_policy.abstain"),
    ({"model_policy": {"feedback": {"format": "parser_error"}}}, {"model_policy": {"feedback": {"format": "none"}}},
     "model_policy.feedback.format"),
])
def test_rule_fields_that_change_what_bad_reads_are_weakened(a, b, what):
    got = _rule(a, b)
    assert got and all(c.weakened for c in got), got
    assert got[0].what == what


@pytest.mark.parametrize("field,old,new", [
    ("view", {"amt": "args.amount"}, {"amt": "args.fee"}),
    ("artifact", "schema.sql#sha256:aa", "schema.sql#sha256:bb"),
    ("conformance_test", "t::a", "t::b"),
    ("generated", False, True),
    ("timing", "pre_effect", "post_effect"),
])
def test_enforcer_fields_that_change_the_check_are_weakened(field, old, new):
    e = {"point": "decorator", "mode": "enforce"}
    got = _rule({"enforcers": [{**e, field: old}]}, {"enforcers": [{**e, field: new}]})
    assert got and all(c.weakened for c in got), got


@pytest.mark.parametrize("a,b", [
    ({"model_policy": {"semantic_resamples": 3}}, {"model_policy": {"semantic_resamples": 0}}),
    ({"model_policy": {"format_retries": 5}}, {"model_policy": {"format_retries": 2}}),
    ({}, {"compares_to": "held_data"}),
    ({"compensable": True}, {"compensable": False}),
    ({"falsified_by": ["a"]}, {"falsified_by": ["a", "b"]}),
    ({"mediation": {"residual_paths": []}}, {"mediation": {"residual_paths": ["x"]}}),
])
def test_strengthening_and_neutral_edits_are_not_weakened(a, b):
    got = _rule(a, b)
    assert got and not any(c.weakened for c in got), got


@pytest.mark.parametrize("field,old,new", [("args", {"c": "args.conn"}, {"c": "args.other"}),
                                           ("from", "py:m.f", "py:m.g"), ("type", "int", "double")])
def test_provider_fields_are_weakened(field, old, new):
    got = lockmod.changes({"providers": {"p": {field: old}}}, {"providers": {"p": {field: new}}})
    assert got and all(c.weakened for c in got), got


def test_written_out_model_policy_defaults_are_no_change():
    raw = {"id": "R", "status": "enforce", "bad": "x", "model_policy": {"schema": "S"}}
    full = {**raw, "model_policy": {"schema": "S", "format_retries": 2, "semantic_resamples": 1,
                                    "feedback": {"format": "parser_error", "semantic": "none"}}}
    assert lockmod.rule_semantics(raw) == lockmod.rule_semantics(full)
    held = lockmod.rule_semantics({**raw, "compares_to": "held_data"})
    assert held["model_policy"]["semantic_resamples"] == 0
    got = lockmod.changes({"rules": {"R": {"semantic": held}}},
                           {"rules": {"R": {"semantic": lockmod.rule_semantics(raw)}}})
    assert {c.what for c in got if c.weakened} == {"compares_to", "model_policy.semantic_resamples"}


# end to end: a plain `inv lock` refuses a rebinding ---------------------------------------------------
PAY = {"pay.py": textwrap.dedent("""
    import invariants as inv

    @inv.provider("limit")
    def limit(account: str) -> int:
        return 100

    @inv.provider("cap")
    def cap(account: str) -> int:
        return 50

    @inv.guard("P-1")
    def pay(account: str, payee: str, amount: int, fee: int) -> None:
        pass
    """), "gateway.yaml": "routes: [{path: /pay, check: P-1}]\n"}
PAY_RULES = """spec: invariant/v0.1
providers:
  limit: {type: int, from: "py:pay.limit", args: {account: args.account}}
  cap: {type: int, from: "py:pay.cap", args: {account: args.account}}
targets:
  "py:pay.pay": {effects: none}
rules:
  - id: P-1
    rev: 1
    status: detect
    subject: {target: "py:pay.pay"}
    bind: {amt: args.amount, lim: provider.limit}
    bad: 'amt > lim'
    enforcers:
      - {point: decorator, timing: pre_effect, mode: detect}
      - {point: gateway, timing: pre_effect, mode: detect,
         artifact: "gateway.yaml#sha256:DIGEST", conformance_test: "t::t"}
"""


@pytest.fixture
def pay(tmp_path):
    root = write_project(tmp_path / "pay", PAY)
    digest = hashlib.sha256((root / "gateway.yaml").read_bytes()).hexdigest()
    (root / "invariants.yaml").write_text(PAY_RULES.replace("DIGEST", digest))
    proc = run_inv("lock", root / "invariants.yaml")
    assert proc.returncode == 0, proc.stderr
    return root


def _edit(path, old, new):
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))


@pytest.mark.parametrize("old,new,expect", [
    ("bind: {amt: args.amount, lim: provider.limit}", "bind: {amt: args.fee, lim: provider.limit}",
     "rule P-1: bind.amt: 'args.amount' -> 'args.fee'  WEAKENED (a binding changes what bad reads)"),
    ("bind: {amt: args.amount, lim: provider.limit}", "bind: {amt: args.amount, lim: provider.cap}",
     "rule P-1: bind.lim: 'provider.limit' -> 'provider.cap'  WEAKENED"),
    ("- {point: decorator, timing: pre_effect, mode: detect}",
     "- {point: decorator, timing: pre_effect, mode: detect, view: {amt: args.fee, lim: provider.limit}}",
     "rule P-1: enforcer decorator view: (none) -> "),
    ('limit: {type: int, from: "py:pay.limit", args: {account: args.account}',
     'limit: {type: int, from: "py:pay.limit", args: {account: args.payee}',
     "provider limit: args.account: 'args.account' -> 'args.payee'  WEAKENED (the provider now reads"),
    ('conformance_test: "t::t"', 'conformance_test: "t::other"', "enforcer gateway conformance_test"),
])
def test_plain_lock_refuses_a_rebinding(pay, old, new, expect):
    rf = pay / "invariants.yaml"
    _edit(rf, old, new)
    before = (pay / "invariants.lock").read_text()
    proc = run_inv("lock", rf)
    assert proc.returncode == 1 and "refused:" in proc.stderr, proc.stdout + proc.stderr
    assert expect in proc.stdout, proc.stdout
    assert (pay / "invariants.lock").read_text() == before
    assert run_inv("lock", rf, "--accept-weakening").returncode == 0


def test_update_artifacts_needs_accept_weakening(pay):
    rf = pay / "invariants.yaml"
    (pay / "gateway.yaml").write_text("# the gateway now checks nothing\n")
    proc = run_inv("lock", rf, "--update-artifacts")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "updated P-1: gateway.yaml sha256" in proc.stdout
    assert "enforcer gateway artifact" in proc.stdout and "WEAKENED" in proc.stdout
    assert run_inv("lock", "--check", rf).returncode == 1              # the rule file moved, the lock did not
    assert run_inv("lock", rf, "--accept-weakening").returncode == 0
    assert run_inv("lock", "--check", rf).returncode == 0


# --- R10-2: canonical comparison of bad ------------------------------------------------------------------
@pytest.mark.parametrize("old,new,want", [
    ("args.size < 1 || args.size > 10000", "args.size < 1  ||  args.size > 10000", "same"),
    ("a || b", "b || a", "same"),
    ("a && b && c", "c && (b && a)", "same"),
    ("x > 4 * y", "x > y * 4", "same"),
    ("x + 1 > y", "1 + x > y", "same"),
    ("a == b", "b == a", "same"),
    ("a != 1", "1 != a", "same"),
    ('s + "x" == t', '"x" + s == t', "unknown"),          # string concatenation does not commute
    ("x == 1", "x == true", "unknown"),                  # type-strict: 1 is not true
    ("a > 1 && b", "a > 1", "stronger"),                 # a conjunct dropped
    ("a > 1", "a > 1 && b", "weaker"),                   # a conjunct added
    ("a > 1 || b", "a > 1", "weaker"),
    ("a > 1", "b || a > 1", "stronger"),
    ("x > 10 || y < 2", "y < 2 || x > 20", "weaker"),    # commuted and loosened
])
def test_compare_bad_on_canonical_trees(old, new, want):
    assert lockmod.compare_bad(old, new, subset.parse) == want


def test_splitting_a_disjunction_into_clauses_is_no_weakening():
    got = _rule({"bad": "a < 1 || a > 10"},
                {"bad": [{"when": "a > 10", "message": "big"}, {"when": "a < 1", "message": "small"}]})
    assert [c.what for c in got] == ["bad clauses regrouped"] and not got[0].weakened
    merged = _rule({"bad": [{"when": "a < 1"}, {"when": "a > 10"}]}, {"bad": "a > 10 || a < 1"})
    assert not any(c.weakened for c in merged)


def test_splitting_with_one_looser_clause_is_weakened():
    [c] = _rule({"bad": "a < 1 || a > 10"},
                {"bad": [{"when": "a < 1", "message": "s"}, {"when": "a > 99", "message": "b"}]})
    assert c.weakened and "fewer cases" in c.note


def test_whitespace_and_commuted_edits_are_noted_not_weakened():
    [c] = _rule({"bad": "a || b"}, {"bad": "b  ||  a"})
    assert not c.weakened and "normalisation" in c.note


# --- R10-3: resolved types ---------------------------------------------------------------------------
def _types_of(tmp_path, monkeypatch, name, src):
    import importlib
    import sys
    pkg = tmp_path / name
    write_project(pkg, {f"{name}.py": textwrap.dedent(src)})
    monkeypatch.syspath_prepend(str(pkg))
    sys.modules.pop(name, None)
    importlib.invalidate_caches()
    mod = importlib.import_module(name)
    pins = lockmod.Schemas()
    return lockmod.signature_types(mod.f, pins), pins.table


ALIAS = """
    from __future__ import annotations
    Caps = list[{t}]
    def f(n: int, capacities: Caps) -> Caps:
        return capacities
    """


def test_an_alias_change_changes_the_pin(tmp_path, monkeypatch):
    a, _ = _types_of(tmp_path / "a", monkeypatch, "alias_a", ALIAS.format(t="int"))
    b, _ = _types_of(tmp_path / "b", monkeypatch, "alias_b", ALIAS.format(t="float"))
    assert a["capacities"] == {"generic": "list", "args": ["int"]}
    assert b["capacities"] == {"generic": "list", "args": ["float"]} and a["return"] != b["return"]


NESTED = """
    from __future__ import annotations
    import dataclasses, typing
    import pydantic

    class Body(pydantic.BaseModel):
        '''Docstring prose: not pinned.'''
        new_size: {size}

    class Meta(typing.TypedDict):
        tag: str

    @dataclasses.dataclass
    class Facts:
        elapsed_s: {elapsed}
        oom_killed: bool
        meta: Meta
        parent: Facts | None = None

    def f(body: Body, facts: Facts) -> Facts | None:
        return None
    """


def test_dataclass_typeddict_and_pydantic_fields_are_pinned(tmp_path, monkeypatch):
    a, ta = _types_of(tmp_path / "a", monkeypatch, "nest_a", NESTED.format(size="int", elapsed="int"))
    b, tb = _types_of(tmp_path / "b", monkeypatch, "nest_b", NESTED.format(size="float", elapsed="int"))
    c, tc = _types_of(tmp_path / "c", monkeypatch, "nest_c", NESTED.format(size="int", elapsed="float"))
    facts = ta[a["facts"]["$ref"]]
    assert facts["fields"]["elapsed_s"] == "int" and facts["fields"]["parent"]["anyOf"]   # a cycle is cut
    assert "nest_a.Meta" in facts["$defs"] and facts["$defs"]["nest_a.Meta"]["fields"] == {"tag": "str"}
    assert tb[b["body"]["$ref"]]["pydantic"]["properties"]["new_size"]["type"] == "number"
    assert tc[c["facts"]["$ref"]]["fields"]["elapsed_s"] == "float"
    assert "description" not in json.dumps(ta[a["body"]["$ref"]])                       # prose stripped


def test_type_alias_statement_is_expanded():
    ns: dict = {}
    try:
        exec("type Caps = list[int]", ns)
    except SyntaxError:                                  # Python < 3.12
        pytest.skip("no type statement")
    assert lockmod.pin_types._type_schema(ns["Caps"], {}, set()) == {"generic": "list", "args": ["int"]}


def test_schema_nesting_is_bounded():
    t: typing.Any = int
    for _ in range(40):
        t = list[t]
    got = json.dumps(lockmod.pin_types._type_schema(t, {}, set()))
    assert "truncated" in got


@dataclasses.dataclass
class Node:
    value: int
    children: list[Node]


def test_a_recursive_dataclass_terminates():
    s = lockmod.resolved_schema(Node)
    assert s["fields"]["children"] == {"generic": "list", "args": [{"$ref": f"{__name__}.Node"}]}


# --- R10-6: lock version 3 ---------------------------------------------------------------------------
@pytest.fixture
def app(tmp_path):
    a = copy_app(CLEAN, tmp_path / "clean")
    assert run_inv("lock", a / "invariants.yaml").returncode == 0
    return a


def test_v3_layout_and_schema_table(app):
    lock = json.loads((app / "invariants.lock").read_text())
    assert lock["lock_version"] == 3 and isinstance(lock["schemas"], dict)
    for rule in lock["rules"].values():
        assert "status" not in rule["semantic"] and "applies_to" not in rule
    for digest, schema in lock["schemas"].items():
        assert digest == lockmod.contracts._sha(lockmod.canonical(schema))          # content-addressed


def test_lock_text_is_deterministic_and_compact():
    lock = {"lock_version": 3, "rules": {"R": {"semantic": {"enforcers": [{"point": "decorator", "mode": "enforce"}]}}}}
    text = lockmod.dumps(lock)
    assert text == lockmod.dumps(json.loads(text)) and json.loads(text) == lock
    assert '{"mode": "enforce", "point": "decorator"}' in text
    assert all(len(line) <= lockmod.DUMP_WIDTH for line in text.splitlines())


def test_strip_prose_keeps_property_names():
    s = {"title": "M", "description": "d", "properties": {"title": {"type": "string", "title": "T"},
                                                          "n": {"type": "integer", "examples": [1]}},
         "enum": [{"title": "kept: data"}]}
    assert lockmod.strip_prose(s) == {"properties": {"title": {"type": "string"}, "n": {"type": "integer"}},
                                       "enum": [{"title": "kept: data"}]}


def test_openapi_shared_schemas_go_to_the_table_once(tmp_path):
    err = {"$ref": "#/components/schemas/Err"}
    spec = {"openapi": "3.1.0", "paths": {
        "/a": {"post": {"operationId": "a", "summary": "prose", "tags": ["x"],
                        "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {"type": "integer"}}}},
                                      "422": {"content": {"application/json": {"schema": err}}}}}},
        "/b": {"post": {"operationId": "b", "parameters": [{"name": "q", "in": "query", "description": "d",
                                                            "schema": {"type": "string"}}],
                        "responses": {"422": {"content": {"application/json": {"schema": err}}}}}}},
        "components": {"schemas": {"Err": {"title": "Err", "type": "object",
                                           "properties": {"detail": {"type": "string", "description": "why"}}}}}}
    (tmp_path / "api.json").write_text(json.dumps(spec))
    pins = lockmod.Schemas()
    pin = lockmod.contract_hash("api", {"type": "openapi", "ref": "api.json"}, tmp_path, ["a", "b"], pins)
    assert len(pins.table) == 1
    [(digest, body)] = pins.table.items()
    assert body == {"name": "api.json/components/schemas/Err",
                    "schema": {"type": "object", "properties": {"detail": {"type": "string"}}}}
    assert pin["operations"]["a"]["responses"]["422"] == {"application/json": {"$ref": digest}}
    assert pin["operations"]["b"]["parameters"] == [{"in": "query", "name": "q", "schema": {"type": "string"}}]
    assert "summary" not in json.dumps(pin)


class Reply(pydantic.BaseModel):
    """Reply prose."""
    cause: typing.Literal["A", "B"]


def test_a_schema_change_is_reported_once_with_its_users():
    def lock(schema):
        pins = lockmod.Schemas()
        ref = pins.add({"name": "m.Reply", **schema})
        return {"targets": {"py:m.f": {"reply_schema": ref}}, "contracts": {"r": {"schema": ref}},
                "schemas": pins.table}
    a = lock(lockmod.resolved_schema(Reply))
    b_schema = lockmod.resolved_schema(Reply)
    b_schema["pydantic"]["properties"]["cause"]["enum"].append("C")
    got = lockmod.changes(a, lock(b_schema))
    assert len(got) == 1 and got[0].where == "schema m.Reply"
    assert "used by target py:m.f reply_schema, contract r schema" in got[0].note


def test_docstring_only_edit_keeps_the_pin():
    class R2(pydantic.BaseModel):
        """Other prose entirely."""
        cause: typing.Literal["A", "B"]
    a, b = lockmod.resolved_schema(Reply), lockmod.resolved_schema(R2)
    assert a == b


# --- R10-7: diffs, strict equality, malformed locks ---------------------------------------------------
def test_dict_changes_are_shown_per_key():
    pol = {"schema": "S", "format_retries": 2, "semantic_resamples": 0, "abstain": {"field": "c", "value": "X"}}
    got = _rule({"model_policy": pol}, {"model_policy": {**pol, "semantic_resamples": 3}})
    assert [str(c) for c in got] == ["rule R: model_policy.semantic_resamples: 0 -> 3  WEAKENED (more resamples "
                                     "give the model more chances to pass)"]


def test_one_and_true_differ():
    assert not lockmod.same(1, True) and not lockmod.same(1, 1.0) and lockmod.same({"a": [1]}, {"a": [1]})
    got = _rule({"model_policy": {"abstain": {"field": "c", "value": 1}}},
                {"model_policy": {"abstain": {"field": "c", "value": True}}})
    assert [c.what for c in got] == ["model_policy.abstain.value"] and got[0].weakened
    assert lockmod.contracts._json_paths({"v": 1}, {"v": True}) == [("v", 1, True)]


@pytest.mark.parametrize("lock,msg", [
    ({"lock_version": True}, "lock_version true is not a valid lock version"),
    ({"lock_version": "3"}, 'lock_version "3" is not a valid lock version'),
    ({"lock_version": 3, "rules": []}, "malformed lock: rules should be an object"),
    ({"lock_version": 3, "rules": {"R": "x"}}, "malformed lock: rules.R should be an object"),
    ({"lock_version": 3, "rules": {"R": {"semantic": [1]}}}, "malformed lock: rules.R.semantic"),
    ({"lock_version": 3, "targets": {"py:t": [1]}}, "malformed lock: targets.py:t"),
    ({"lock_version": 3, "schemas": {"sha256:x": 3}}, "malformed lock: schemas.sha256:x"),
])
def test_malformed_locks_are_a_lock_error_with_the_path(tmp_path, lock, msg):
    f = tmp_path / "invariants.lock"
    f.write_text(json.dumps(lock))
    with pytest.raises(lockmod.LockError, match=msg) as e:
        lockmod.read(f)
    assert str(f) in str(e.value)


def test_malformed_lock_through_the_cli_has_no_traceback(app):
    lock = json.loads((app / "invariants.lock").read_text())
    lock["rules"]["L-1"]["semantic"]["bad"] = 5
    (app / "invariants.lock").write_text(json.dumps(lock))
    proc = run_inv("lock", app / "invariants.yaml")
    assert "Traceback" not in proc.stderr and "TypeError" not in proc.stderr
