"""Round 30, cleanup. Each item with a code change failed before its fix (8 and 9 pin behaviour that held).

1. A clause nested about 100 parentheses deep (or a 3,000-term && chain, or 1,500 `!`) made the
   loader raise a bare RecursionError (`inv validate` printed it). The parser now refuses nesting past
   MAX_NESTING and a tree deeper than MAX_TREE_DEPTH with a CelSyntaxError, a LoadError naming the
   rule, on both backends (cel-expr parses with the same front end). The middle of a ?: is a
   conditionalOr in CEL's grammar, so `a ? b ? c : d : e` is refused as cel-expr refuses it.
2. The subset's regex read an octal escape (\101, \12) as a backreference and refused it; RE2 reads
   \0 plus up to two octal digits, and \1 to \7 followed by at least one more, as a character.
3. `inv init` froze every function on any door-free path, a name-matched one too, in allow, so an
   unresolved/inferred-path site was silently excused. Only extracted-call paths are frozen now; the
   inferred-path site stays unresolved and is drafted in allow_unresolved.
4. A plain `inv lock` wrote a loosened schema when the stored entry was hand-edited ("cannot be
   shown", exit 0). A schema change that cannot be judged is WEAKENED.
5. docs: evaluation.md's current results no longer describe mediation.tables as present; start.md no
   longer says a recursive copy gets past the hook.
6. (tests/test_cel_kept_engines.py) cel-expr's cost message doubled its 'cel-expr:' prefix.
7. A lock stale only by moved allow_unresolved sites said "review the changes, then run `inv lock`";
   it says to run `inv check --relock`.
8. (no code change) The ADK report of two "moved" reflection sites was the formatter changing the old
   token sha; at HEAD the tree sha follows the statement: pinned here.
9. Three surviving mutants of the unwrap checks (an object outside the scan, hasattr of __wrapped__,
   a module named by an ambiguous tail) are caught.
10. A Swagger 2.0 response's schema (not under content) was pinned empty; it is pinned and judged like
    an OpenAPI 3 response's.
"""
from __future__ import annotations

import pathlib

import pytest

import invariants as inv
from cli_support import run_inv, write_project
from invariants_cel.errors import CelSyntaxError
from invariants_cel.subset import evaluate, parse
from invariants_cel.subset.parser import MAX_NESTING, MAX_TREE_DEPTH
from invariants.errors import LoadError

RULE = ('rules:\n  - id: R-1\n    bad: "{src}"\n'
        '    enforcers: [{{point: structure, timing: pre_effect, mode: enforce}}]\n    status: detect\n')


def _rules(tmp_path: pathlib.Path, src: str) -> pathlib.Path:
    p = tmp_path / "invariants.yaml"
    p.write_text(RULE.format(src=src))
    return p


# --- 1. deep nesting is a LoadError naming the rule, never a RecursionError --------------------------
DEEP = {
    "parens": "(" * 100 + "true" + ")" * 100,
    "lists": "[" * 100 + "1" + "]" * 100 + " == []",
    "nots": "!" * 1500 + "true",
    "minus": "-" * 1500 + "1 == 0",
    "and chain": " && ".join(["true"] * 3000),
    "sum": "+".join(["1"] * 3000) + " == 0",
    "index chain": "args" + "[0]" * 2000 + " == 1",
}


@pytest.mark.parametrize("src", DEEP.values(), ids=DEEP.keys())
def test_deep_nesting_is_a_load_error_naming_the_rule(tmp_path, src):
    with pytest.raises(LoadError, match=r"R-1: bad does not parse: the clause is (nested more than|more than)"):
        inv.load(str(_rules(tmp_path, src)))


def test_inv_validate_says_so(tmp_path):
    _rules(tmp_path, DEEP["parens"])
    r = run_inv("validate", cwd=tmp_path)
    assert r.returncode == 1 and "RecursionError" not in r.stdout + r.stderr
    assert f"R-1: bad does not parse: the clause is nested more than {MAX_NESTING} levels deep" in r.stdout + r.stderr


def test_the_limits_themselves_load_and_evaluate():
    assert evaluate(parse("(" * (MAX_NESTING - 1) + "true" + ")" * (MAX_NESTING - 1)), {}) is True
    with pytest.raises(CelSyntaxError, match="nested more than"):
        parse("(" * MAX_NESTING + "true" + ")" * MAX_NESTING)
    long = " && ".join(["true"] * (MAX_TREE_DEPTH - 1))
    assert evaluate(parse(long), {}) is True
    with pytest.raises(CelSyntaxError, match="levels deep as a tree"):
        parse(" && ".join(["true"] * (MAX_TREE_DEPTH + 1)))


def test_a_conditional_in_the_middle_of_a_conditional_needs_parentheses():
    with pytest.raises(CelSyntaxError, match="expected ':'"):
        parse("true ? false ? 1 : 2 : 3")
    assert evaluate(parse("true ? (false ? 1 : 2) : 3"), {}) == 2
    assert evaluate(parse("false ? 1 : true ? 2 : 3"), {}) == 2


# --- 2. octal escapes in a pattern read as RE2 reads them ---------------------------------------------
@pytest.mark.parametrize("pattern,text", [
    (r"\101", "A"), (r"\12", "\n"), (r"\123", "S"), (r"\1234", "S4"), (r"\400", "Ā"), (r"\777", "ǿ"),
    (r"[\101-\103]", "B"), (r"\0", "\x00"), (r"\012", "\n"), (r"\08", "\x008")])
def test_an_octal_escape_is_its_character(pattern, text):
    from invariants_cel import _regex
    assert _regex.search(_regex.compile_pattern("^" + pattern + "$"), text, 10_000)[0] is True


@pytest.mark.parametrize("pattern", [r"\1", r"\7", r"\8", r"\9", r"\18", r"[\1]", r"a\1"])
def test_a_lone_digit_escape_is_refused_as_re2_refuses_it(pattern):
    from invariants_cel import _regex
    with pytest.raises(_regex.RegexError, match="backreferences"):
        _regex.compile_pattern(pattern)


def test_an_octal_pattern_loads_and_matches(tmp_path):
    p = tmp_path / "invariants.yaml"
    p.write_text(RULE.format(src="args.s.matches('^" + "\\\\" * 2 + "101$')"))
    rule = inv.load(str(p), install=False).rules["R-1"]
    assert inv.evaluate(rule, {"args": {"s": "A"}}).kind == "violation"
    assert inv.evaluate(rule, {"args": {"s": "B"}}).kind == "ok"
    p.write_text(RULE.format(src="args.s.matches('^" + "\\\\" * 2 + "1$')"))
    with pytest.raises(LoadError, match="backreferences"):
        inv.load(str(p))


# --- 3. inv init freezes extracted back doors, never a function on a name-matched path alone ---------
ORDERS = {
    "app/__init__.py": "", "app/repo.py": "class OrderRepo:\n    def save_order(self, o):\n        pass\n",
    "app/order_service.py": "from app.repo import OrderRepo\ndef place(o):\n    OrderRepo().save_order(o)\n"
                            "def place2(o):\n    OrderRepo().save_order(o)\n",
    "app/api.py": "from app.order_service import place, place2\nfrom app.repo import OrderRepo\n"
                  "def a(o):\n    place(o)\ndef b(o):\n    place2(o)\ndef c(o):\n    OrderRepo().save_order(o)\n"}


def test_init_leaves_an_inferred_path_site_unresolved_for_review(tmp_path):
    from invariants import doors
    root = write_project(tmp_path, {**ORDERS, "app/poke.py": "def poke(r, o):\n    r.save_order(o)\n"})
    d = doors.discover([root]).doors[0]
    assert d.allow == ["app.api.c"], d.allow              # the extracted back door is frozen; poke is not
    assert [(u[0], u[1]) for u in d.unresolved] == [("app/poke.py:2:5", "unresolved/inferred-path")]
    assert not d.active and "allow_unresolved" in d.why_inactive
    text = "\n".join(doors.render(doors.DoorsFound([d], [], {}, 0, None)))
    assert 'allow_unresolved:' in text and '{at: "app/poke.py:2:5", code: unresolved/inferred-path' in text


def test_init_still_freezes_a_function_on_a_longer_extracted_path(tmp_path):
    from invariants import doors
    root = write_project(tmp_path, {**ORDERS, "app/ops.py": (
        "from app.api import c\ndef z(o):\n    c(o)\ndef y(o):\n    z(o)\ndef main(o):\n    c(o)\n    y(o)\n")})
    d = doors.discover([root]).doors[0]
    assert d.allow == ["app.api.c", "app.ops.main", "app.ops.y", "app.ops.z"] and d.active, d


# --- 4. a changed schema whose stored entry was hand-edited is WEAKENED -------------------------------
ROW = ("import dataclasses\nimport invariants as inv\n\n@dataclasses.dataclass\nclass Row:\n    a: int\n"
       "    b: str\n\n@inv.guard(\"C-2\")\ndef g(r: Row) -> None:\n    pass\n")
ROW_RULES = ('spec: invariant/v0.1\ntargets:\n  "py:mod30.g": {effects: none}\nrules:\n  - id: C-2\n    rev: 1\n'
             '    status: enforce\n    subject: {target: "py:mod30.g"}\n    bad: \'args.r.a < 0\'\n    enforcers:\n'
             '      - {point: decorator, timing: pre_effect, mode: enforce}\n    falsified_by: ["t::t"]\n')


@pytest.mark.parametrize("tamper", [False, True])
def test_plain_lock_refuses_a_loosened_schema_even_with_a_hand_edited_entry(tmp_path, tamper):
    import json
    root = write_project(tmp_path, {"mod30.py": ROW, "invariants.yaml": ROW_RULES})
    assert run_inv("lock", cwd=root).returncode == 0
    p = root / "invariants.lock"
    if tamper:
        stored = json.loads(p.read_text())
        for v in stored["schemas"].values():
            v["x-note"] = "edited by hand"
        p.write_text(json.dumps(stored))
    before = p.read_text()
    (root / "mod30.py").write_text(ROW.replace("a: int", "a: object"))
    r = run_inv("lock", cwd=root)
    assert r.returncode == 1 and "WEAKENED" in r.stdout and "refused" in r.stderr, r.stdout + r.stderr
    assert p.read_text() == before
    if tamper:
        assert "cannot be shown or judged" in r.stdout
    r = run_inv("lock", "--accept-weakening", cwd=root)
    assert r.returncode == 0 and "accepted 1 weakening" in r.stdout, r.stdout + r.stderr


# --- 5. the docs: mediation.tables is gone, a recursive copy is blocked -------------------------------
DOCS = pathlib.Path(__file__).resolve().parents[1] / "docs"


def test_the_evaluation_says_mediation_tables_was_removed():
    text = (DOCS / "evaluation.md").read_text()
    current = text.split("## Current results", 1)[1].split("\n## ", 1)[0]
    assert "`mediation.tables` takes statement kinds" not in current
    assert "until they declare `mediation.tables`" not in current and "Addendum 2" in current


def test_start_no_longer_says_a_recursive_copy_gets_past_the_hook():
    text = " ".join((DOCS / "start.md").read_text().split())
    assert "and `cp -r dir/. .` can each put back" not in text
    assert "A recursive copy, `cp -r dir/. .`, `rsync -a` or `ditto`, from a directory that holds one is blocked" in text


# --- 8. the ADK "moved reflection": a reformat moves the same statement, and its entry follows it -----
PAY = "def send_to_gateway(c, a):\n    pass\n\ndef refund(c, a):\n    send_to_gateway(c, a)\n"
TOOLS = ("import importlib\n\n\ndef resolve(cfg):\n    '''Resolve.\n\n    Returns:\n      tools.\n    '''\n\n"
         "    if '.' not in cfg:\n        module = importlib.import_module('google.adk.tools')\n"
         "    else:\n        module = importlib.import_module(cfg)\n    return module\n")
FORMATTED = ('import importlib\n\n\ndef resolve(cfg):\n    """Resolve.\n    Returns:\n      tools.\n    """\n'
             '    if "." not in cfg:\n        module = importlib.import_module("google.adk.tools")\n'
             '    else:\n        module = importlib.import_module(cfg)\n    return module\n')


def test_a_reformatted_reflection_site_is_the_same_site_moved(tmp_path):
    import invariants_scan as scan
    rule = {"rules": [{"id": "R", "mediation": {"sink": "py:payments.send_to_gateway", "door": ["py:payments.refund"],
                                                 "allow_unresolved": []}}]}
    root = write_project(tmp_path / "a", {"payments.py": PAY, "tools.py": TOOLS})
    first = [f for f in scan.scan(rule, [root]).findings if f.code == "unresolved/reflection"]
    again = [f for f in scan.scan(rule, [root]).findings if f.code == "unresolved/reflection"]
    assert [(f.line, f.col, f.sha) for f in first] == [(f.line, f.col, f.sha) for f in again] and len(first) == 2
    rule["rules"][0]["mediation"]["allow_unresolved"] = [
        {"at": f"tools.py:{f.line}:{f.col}", "code": f.code, "sha": f.sha, "why": "reviewed"} for f in first]
    from invariants.loader import expand_short_forms
    (root / "tools.py").write_text(FORMATTED)
    rep = scan.scan(expand_short_forms(rule), [root])
    assert not [f for f in rep.findings if f.kind != "bypass"], rep.findings
    assert sorted((f.line, f.moved_from) for f in rep.accepted) == [(10, 12), (12, 14)], rep.accepted


# --- 9. three surviving mutants of the unwrap checks ----------------------------------------------------
# payments.refund is a decorated door: an unwrap of an object the scan cannot name may lift its guard
DECORATED = ("def deco(f):\n    return f\n\ndef send_to_gateway(c, a):\n    pass\n\n@deco\ndef refund(c, a):\n"
             "    send_to_gateway(c, a)\n")
DOOR_RULE = {"rules": [{"id": "R", "mediation": {"sink": "py:payments.send_to_gateway",
                                                  "door": ["py:payments.refund"]}}]}


def _unwraps(tmp_path, files):
    import invariants_scan as scan
    rep = scan.scan(DOOR_RULE, [write_project(tmp_path, {"payments.py": DECORATED, **files})])
    return [(f.code, f.file, f.line) for f in rep.findings if "unwrap" in f.code]


def test_an_unwrap_of_an_unnamed_object_beside_a_decorated_door_is_forbidden(tmp_path):
    # the baseline the three below differ from
    got = _unwraps(tmp_path, {"ops.py": "def go(x):\n    return x.__wrapped__(1, 2)\n"})
    assert got == [("forbidden/unwrap", "ops.py", 2)], got


@pytest.mark.parametrize("call", ["inspect.unwrap(json.loads)", "json.loads.__wrapped__", "json.__wrapped__",
                                  "getattr(json.loads, '__wrapped__')"])
def test_an_unwrap_of_an_object_outside_the_scan_is_not_reported(tmp_path, call):
    got = _unwraps(tmp_path, {"ops.py": f"import inspect\nimport json\n\ndef go():\n    return {call}\n"})
    assert got == [], got


def test_hasattr_of_wrapped_only_asks(tmp_path):
    got = _unwraps(tmp_path, {"ops.py": "import payments\n\ndef go(x):\n    return hasattr(x, '__wrapped__') "
                                        "or hasattr(payments.refund, '__wrapped__')\n"})
    assert got == [], got


def test_a_module_named_by_a_tail_of_several_under_the_roots_is_not_outside_the_scan(tmp_path):
    # `import fleet.store` may be a/fleet/store.py or b/fleet/store.py (which one depends on sys.path): an
    # object under it is scanned code, never "outside", so its __wrapped__ is an unwrap the scan reports
    store = "def save(x):\n    pass\n"
    got = _unwraps(tmp_path, {"a/__init__.py": "", "a/fleet/__init__.py": "", "a/fleet/store.py": store,
                              "b/__init__.py": "", "b/fleet/__init__.py": "", "b/fleet/store.py": store,
                              "ops.py": "import fleet.store\n\ndef go():\n    return fleet.store.save.__wrapped__(1)\n"})
    assert got == [("forbidden/unwrap", "ops.py", 4)], got


# --- 10. a Swagger 2.0 response's schema is pinned and judged as an OpenAPI 3 one is -----------------------
def _pin_spec(tmp_path: pathlib.Path, spec: dict) -> dict:
    import json
    from invariants import lock as lockmod
    d = tmp_path / f"spec{len(list(tmp_path.iterdir()))}"
    d.mkdir()
    (d / "api.json").write_text(json.dumps(spec))
    pins = lockmod.Schemas()
    pin = lockmod.contract_hash("api", {"type": "openapi", "ref": "api.json"}, d, ["a"], pins)
    return {"contracts": {"api": pin}, "schemas": pins.table}


def _sw_reply(schema: dict, **op) -> dict:
    return {"swagger": "2.0", "paths": {"/a": {"get": {"operationId": "a", **op,
                                                        "responses": {"200": {"description": "ok", "schema": schema}}}}}}


def _oa_reply(schema: dict) -> dict:
    return {"openapi": "3.0.3", "paths": {"/a": {"get": {"operationId": "a", "responses": {
        "200": {"description": "ok", "content": {"application/json": {"schema": schema}}}}}}}}


def test_a_swagger_2_response_schema_is_pinned(tmp_path):
    reply = {"type": "object", "properties": {"n": {"type": "integer", "maximum": 5}}}
    pin = _pin_spec(tmp_path, _sw_reply(reply))
    assert pin["contracts"]["api"]["operations"]["a"]["responses"] == {"200": {"application/json": reply}}
    pin = _pin_spec(tmp_path, _sw_reply(reply, produces=["application/xml", "text/plain"]))
    assert pin["contracts"]["api"]["operations"]["a"]["responses"]["200"] == {"application/xml": reply,
                                                                              "text/plain": reply}
    no_body = {"swagger": "2.0", "paths": {"/a": {"get": {"operationId": "a", "responses": {"204": {"description": "x"}}}}}}
    assert _pin_spec(tmp_path, no_body)["contracts"]["api"]["operations"]["a"]["responses"] == {"204": {}}


@pytest.mark.parametrize("old,new", [
    ({"type": "integer", "maximum": 5}, {"type": "integer", "maximum": 9}),
    ({"type": "string", "enum": ["x"]}, {"type": "string", "enum": ["x", "y"]}),
    ({"type": "object", "required": ["a"]}, {"type": "object"}),
])
def test_a_swagger_2_response_is_judged_as_an_openapi_3_one(tmp_path, old, new):
    from invariants import lock as lockmod

    def judged(make) -> list:
        return sorted((c.what, c.weakened) for c in lockmod.changes(_pin_spec(tmp_path, make(old)),
                                                                     _pin_spec(tmp_path, make(new))))
    sw, oa = judged(_sw_reply), judged(_oa_reply)
    assert sw and sw == oa, (sw, oa)
    assert any(w for _, w in sw), sw                     # each loosens what the API may return

R29C = {
    "r29c/__init__.py": "",
    "r29c/store.py": "def set_size(conn, n):\n    conn.execute(n)\n",
    "r29c/service.py": ("from r29c import store\n\n\ndef resize(conn, n: int) -> None:\n"
                        "    store.set_size(conn, n)\n"),
    "r29c/retry.py": "from r29c import store\n\n\ndef retry(conn, n, name):\n    getattr(store, name)(conn, n)\n",
}
R29C_RULE = 'rules:\n  - id: F-1\n    door: "py:r29c.service.resize"\n    sink: "py:r29c.store.set_size"\n'


def test_a_lock_stale_only_by_moved_sites_says_relock(tmp_path):
    import json
    root = write_project(tmp_path, {**R29C, "invariants.yaml": R29C_RULE})
    sha = next(f["sha"] for f in json.loads(run_inv("scan", "--format", "json", cwd=root).stdout)["findings"]
               if f["file"] == "r29c/retry.py")

    def entry(line: int, why: str = "only reads") -> str:
        return (f'    allow_unresolved: [{{at: "r29c/retry.py:{line}:5", code: unresolved/computed-attribute, '
                f'sha: "{sha}", why: "{why}"}}]\n')
    (root / "invariants.yaml").write_text(R29C_RULE + entry(5))
    assert run_inv("lock", cwd=root).returncode == 0
    (root / "r29c/retry.py").write_text("from r29c import store\n\n\n\n" + R29C["r29c/retry.py"].split("\n\n\n", 1)[1])
    (root / "invariants.yaml").write_text(R29C_RULE + entry(6))
    c = run_inv("check", cwd=root)
    assert c.returncode == 1 and "moved r29c/retry.py:5:5 -> r29c/retry.py:6:5" in c.stdout, c.stdout
    assert "run `inv check --relock` to re-pin them" in c.stdout and "then run `inv lock`" not in c.stdout
    assert run_inv("check", "--relock", cwd=root).returncode == 0
    # a moved site beside another change: the general advice
    (root / "invariants.yaml").write_text(R29C_RULE.replace("rules:\n", "rules:\n" + R29C_RULE[7:].replace(
        "F-1", "F-2")) + entry(5))
    (root / "r29c/retry.py").write_text(R29C["r29c/retry.py"])
    c = run_inv("check", cwd=root)
    assert c.returncode == 1 and "then run `inv lock`" in c.stdout and "--relock` to re-pin" not in c.stdout, c.stdout
