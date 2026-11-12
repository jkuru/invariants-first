"""Regression tests for fix round 6 (certification round 4): each test failed before its fix."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import logging
import os
import pathlib
import pickle
import shutil
import subprocess
import sys
import textwrap
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import invariants as inv
import round6 as r6
from invariants.cel import get_backend

ROOT = pathlib.Path(__file__).resolve().parent.parent
KINDS = list(r6.MODELS)
GOOD = {"cause": "OK", "v": 1, "note": "n"}


def good(kind: str, **over: Any) -> dict[str, Any]:
    return {**GOOD, **({"tags": []} if kind == "pydantic" else {}), **over}


@pytest.fixture
def iso():
    with inv.isolated() as r:
        r6.REPLIES.clear()
        r6.CONSUMED.clear()
        inv.load(r6.doc())
        yield r


def call(kind: str, *replies: Any) -> tuple[Any, Any]:
    """(Checked, None) or (None, InvariantError). Anything else propagates and fails the test."""
    r6.REPLIES[:] = list(replies)
    try:
        return r6.MODELS[kind][0]("p"), None
    except inv.InvariantError as e:
        return None, e


# --- 1. MAJOR: hostile replies never raise anything but the documented InvariantErrors -------------
class BadHash(str):
    """Hashes once (to go into the dict), then raises on every later hash."""
    hashed = False

    def __hash__(self) -> int:
        if self.hashed:
            raise RuntimeError("hash")
        self.hashed = True
        return str.__hash__(self)


class BadEq(str):
    def __eq__(self, other: object) -> bool:
        raise RuntimeError("eq")

    __hash__ = str.__hash__


class BadRepr:
    def __repr__(self) -> str:
        raise RuntimeError("repr")


class BadDict(dict):  # type: ignore[type-arg]
    def keys(self):  # type: ignore[override]
        raise RuntimeError("keys")

    def __iter__(self):  # type: ignore[override]
        raise RuntimeError("iter")


def _deep(n: int) -> str:
    return '{"cause": "OK", "v": 1, "note": ' + "[" * n + "]" * n + "}"


HOSTILE = {
    "invalid-utf8-bytes": b"\xff\xfe\xff",
    "utf16-garbage": b"\xff\xfe\x00\xd8",
    "non-string-keys": {1: 2, "a": 3},
    "hash-raises-key": {BadHash("cause"): "OK", "v": 1, "note": "n"},
    "eq-raises-enum-value": {"cause": BadEq("OK"), "v": 1, "note": "n"},
    "repr-raises-value": {"cause": "OK", "v": BadRepr(), "note": "n"},
    "dict-subclass-raises": BadDict(cause="OK", v=1, note="n"),
    "deeply-nested-json": _deep(200_000),
    "huge-int": '{"cause": "OK", "v": ' + "9" * 5000 + ', "note": "n"}',
    "too-large": json.dumps({**GOOD, "note": "x" * 1_100_000}),
    "self-recursive-dict": None,             # filled below
    "a-list": "[1, 2]",
    "a-number": "3",
    "null": "null",
    "not-json": "{oops",
}
_rec: dict[str, Any] = {"cause": "OK", "v": 1}
_rec["note"] = _rec
HOSTILE["self-recursive-dict"] = _rec


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("name", list(HOSTILE))
def test_hostile_reply_is_a_parse_error_then_on_violation(iso, kind, name):
    hostile = HOSTILE[name]
    with inv.capture(iso) as events:
        c, e = call(kind, hostile, hostile)          # format_retries 1: two replies, then on_violation
    assert c is None and isinstance(e, inv.Rejected), (c, e)
    assert e.outcome.kind == "parse_error" and len(e.attempts) == 2
    assert [ev["action"] for ev in events if ev["rule"] == r6.MODELS[kind][3]] == ["retry", "reject"]
    assert r6.REPLIES == []


@pytest.mark.parametrize("kind", KINDS)
def test_hostile_reply_then_good_reply_retries_to_ok(iso, kind):
    c, e = call(kind, HOSTILE["hash-raises-key"], json.dumps(good(kind)))
    assert e is None and c.verdict == "ok" and c.attempts == 2


def test_hostile_reply_on_the_async_path(iso):
    r6.REPLIES[:] = [b"\xff\xfe\xff", {1: 2}]
    with pytest.raises(inv.Rejected, match="parse_error"):
        asyncio.run(r6.admodel("p"))


def test_dataclass_post_init_that_raises_is_a_parse_error(iso):
    @dataclasses.dataclass(frozen=True)
    class Strict:
        n: int

        def __post_init__(self) -> None:
            raise ValueError("no")
    with pytest.raises(inv.ParseError, match="ValueError"):
        inv.parse('{"n": 1}', Strict)


def test_parse_checks_nested_types_instead_of_raising_typeerror():
    schema = {"tags": list[str], "meta": dict[str, int], "opt": int | None}
    assert inv.parse('{"tags": ["a"], "meta": {"k": 1}, "opt": null}', schema) == {
        "tags": ["a"], "meta": {"k": 1}, "opt": None}
    for bad in ('{"tags": [1], "meta": {}, "opt": 1}', '{"tags": [], "meta": {"k": "x"}, "opt": 1}',
                '{"tags": [], "meta": {}, "opt": "x"}'):
        with pytest.raises(inv.ParseError):          # was: TypeError (subscripted generics in isinstance)
            inv.parse(bad, schema)


def test_unsupported_schema_field_type_is_refused_when_decorating():
    with pytest.raises(TypeError, match="unsupported field type"):
        @inv.model_call("X-1", schema={"s": set[int]})
        def f() -> r6.CheckedD: ...


JSON = st.recursive(st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
                    lambda c: st.lists(c, max_size=4) | st.dictionaries(st.text(max_size=6), c, max_size=4),
                    max_leaves=20)
NEAR = st.fixed_dictionaries({"cause": st.sampled_from(["OK", "BAD", "CANNOT_COMPLY", "NOPE", 1, None]),
                              "v": JSON, "note": JSON}, optional={"tags": JSON, "extra": JSON})
REPLY = (JSON.map(json.dumps) | st.text(max_size=40) | st.binary(max_size=40) | NEAR.map(json.dumps) | NEAR
         | JSON)


@pytest.mark.parametrize("kind", KINDS)
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture,
                                                                   HealthCheck.too_slow])
@given(replies=st.lists(REPLY, min_size=2, max_size=2))
def test_fuzz_model_call_only_documented_outcomes(kind, replies):
    with inv.isolated() as reg:
        inv.load(r6.doc())
        c, e = call(kind, *replies)
    if e is None:
        assert c.verdict == "ok" and isinstance(c, r6.MODELS[kind][2])   # enforce: anything else raised
    else:
        assert type(e) in (inv.Rejected, inv.Escalation), e
        if isinstance(e, inv.Escalation):
            assert e.attempts[-1]["outcome"] == "abstain" and e.review_id
        else:
            assert e.outcome.kind in ("parse_error", "violation", "rule_error")
    assert sum(1 for ev in reg.sink if ev["rule"] == r6.MODELS[kind][3]) >= 1 or isinstance(e, inv.Escalation)


@settings(max_examples=150, deadline=None)
@given(raw=st.text(max_size=60) | st.binary(max_size=60) | JSON | NEAR)
def test_fuzz_parse_raises_only_parse_error(raw):
    for schema in (r6.DictReply, r6.DcReply, r6.PdReply):
        try:
            inv.parse(raw, schema)
        except inv.ParseError:
            pass


# --- 2. MAJOR: abstain fires for every schema kind -------------------------------------------------
@pytest.mark.parametrize("kind", KINDS)
def test_abstain_escalates_for_every_schema_kind(iso, kind):
    rid = r6.MODELS[kind][3]
    runs = iso.counts(rid).get("runs", 0)
    c, e = call(kind, json.dumps(good(kind, cause="CANNOT_COMPLY")))
    assert c is None and isinstance(e, inv.Escalation), c          # dict was: verdict ok
    assert e.attempts[0]["outcome"] == "abstain" and e.review_id and e.rule == rid
    assert iso.counts(rid).get("runs", 0) == runs                   # bad never ran


# --- 3. MAJOR: the edge re-validates the closed schema ---------------------------------------------
def _forge(cls: type, value: Any, verdict: Any = "ok") -> Any:
    obj = object.__new__(cls)
    for k, v in (("value", value), ("attempts", 1), ("rule", "x"), ("verdict", verdict)):
        object.__setattr__(obj, k, v)
    return obj


def _forged_dc(cls: type, **fields: Any) -> Any:
    obj = object.__new__(cls)
    for k, v in fields.items():
        object.__setattr__(obj, k, v)
    return obj


@pytest.mark.parametrize("value", [
    {**GOOD, "evil": 1},                         # forged extra field
    {**GOOD, "cause": "ROOT"},                   # out of enum
    {**GOOD, "v": "1"},                          # wrong type
    {"cause": "OK", "v": 1},                     # missing field
], ids=["extra", "enum", "type", "missing"])
def test_edge_rechecks_a_dict_schema(iso, value):
    with pytest.raises(inv.Rejected, match="parse_error"):
        r6.dconsume(_forge(r6.CheckedD, value))
    assert r6.CONSUMED == []


def test_edge_rechecks_a_dataclass_schema(iso):
    with pytest.raises(inv.Rejected, match="not one of"):
        r6.cconsume(_forge(r6.CheckedC, _forged_dc(r6.DcReply, cause="ROOT", v=1, note="n")))

    @dataclasses.dataclass(frozen=True)
    class Sub(r6.DcReply):
        pass
    with pytest.raises(inv.Rejected, match="was not built by inv.parse"):
        r6.cconsume(_forge(r6.CheckedC, Sub("OK", 1, "n")))
    assert r6.CONSUMED == []


def test_edge_rechecks_a_pydantic_schema(iso):
    closed = r6.pmodel.__inv_schema__
    with pytest.raises(inv.Rejected, match="parse_error"):
        r6.pconsume(_forge(r6.CheckedP, _forged_dc(closed, cause="ROOT", v=1, note="n", tags=[])))
    with pytest.raises(inv.Rejected, match="parse_error"):
        r6.pconsume(_forge(r6.CheckedP, _forged_dc(closed, cause="OK", v=1, note="n", tags=[3])))


@pytest.mark.parametrize("kind", KINDS)
def test_edge_accepts_an_honest_value_of_every_schema_kind(iso, kind):
    c, e = call(kind, json.dumps(good(kind)))
    assert e is None
    got = r6.MODELS[kind][1](c)
    assert got == c and got is not c        # round 11: the consumer gets the re-validated copy


def test_edge_refuses_a_verdict_that_is_not_a_string(iso):
    class Sly:
        def __eq__(self, o: object) -> bool:
            return True

        def __ne__(self, o: object) -> bool:
            return False
    with pytest.raises(inv.Rejected, match="no verdict"):
        r6.dconsume(_forge(r6.CheckedD, dict(GOOD), verdict=Sly()))


def test_edge_bad_still_rechecked(iso):
    with pytest.raises(inv.Rejected, match="violation"):
        r6.dconsume(_forge(r6.CheckedD, {**GOOD, "v": 1000}))


# --- 4. minor: a bad abstain field on a dict schema is a LoadError ----------------------------------
@pytest.mark.parametrize("abstain,match", [
    ({"field": "nope", "value": "CANNOT_COMPLY"}, "abstain field 'nope' is not in the {field: type} schema"),
    ({"field": ["cause"], "value": "CANNOT_COMPLY"}, "abstain field must be a field name"),
    ({"field": "cause", "value": "UNKNOWN"}, "not a member of the closed enum"),
    ({"field": "cause", "value": {"x": 1}}, "abstain value must be"),
])
def test_dict_schema_bad_abstain_is_a_load_error(abstain, match):
    d = r6.doc(**{"D-1": {"model_policy": {"format_retries": 1, "semantic_resamples": 0,
                                            "feedback": {"format": "none"}, "abstain": abstain}}})
    with inv.isolated(), pytest.raises(inv.LoadError, match=match.replace("{", r"\{").replace("}", r"\}")
                                       .replace("[", r"\[")):
        inv.load(d)


# --- 5. minor: a forged recursive value is refused cleanly -------------------------------------------
def test_forged_recursive_dataclass_is_refused_cleanly(iso):
    val = _forged_dc(r6.DcReply, cause="OK", v=1)
    object.__setattr__(val, "note", val)
    with pytest.raises(inv.Rejected, match="parse_error"):            # was: RecursionError
        r6.cconsume(_forge(r6.CheckedC, val))


def test_forged_recursive_dict_is_refused_cleanly(iso):
    val: dict[str, Any] = {"cause": "OK", "v": 1}
    val["note"] = val
    with pytest.raises(inv.Rejected, match="parse_error"):
        r6.dconsume(_forge(r6.CheckedD, val))


# --- 6. minor: a failing review store is explicit, and a durable fallback keeps the work -----------
class Down:
    def open(self, h: inv.Handoff) -> str:
        raise OSError("store down")


class NoId:
    def open(self, h: inv.Handoff) -> Any:
        return None


@pytest.mark.parametrize("store", [Down(), NoId()], ids=["raises", "no-id"])
def test_failing_review_store_raises_review_not_opened(iso, store, caplog):
    inv.configure(iso, review_store=store)
    c, e = call("dict", json.dumps(good("dict", cause="CANNOT_COMPLY")))
    assert isinstance(e, inv.ReviewNotOpened) and e.review_id is None and e.action == "escalate"
    assert e.handoff.reason == "abstain" and e.handoff.bound["reply"]["cause"] == "CANNOT_COMPLY"
    assert "no review was opened" in str(e)
    assert "store down" not in caplog.text          # the store's message is logged only with debug


def test_review_fallback_keeps_the_work_durably(iso, tmp_path):
    spool = tmp_path / "fallback.jsonl"
    inv.configure(iso, review_store=Down(), review_fallback=inv.JsonlReviews(spool))
    inv.load(r6.doc(on_violation="route"))
    c, e = call("dict", json.dumps(good("dict", v=1000)))
    assert isinstance(e, inv.Routed) and e.review_id.startswith("jsonl-")
    rows = [json.loads(line) for line in spool.read_text().splitlines()]
    assert rows[0]["id"] == e.review_id and rows[0]["action"] == "route" and rows[0]["bound"]["reply"]["v"] == 1000


def test_review_store_and_fallback_both_failing(iso):
    inv.configure(iso, review_store=Down(), review_fallback=NoId())
    inv.load(r6.doc(on_violation="route"))
    _, e = call("dict", json.dumps(good("dict", v=1000)))
    assert isinstance(e, inv.ReviewNotOpened) and e.action == "route" and e.outcome.kind == "violation"


def test_review_not_opened_maps_to_503():
    from invariants.integrations.asgi import problem
    h = inv.Handoff("R", "route")
    status, body = problem(inv.ReviewNotOpened("x", h, rule="R"))
    assert status == 503 and body["type"] == "urn:invariants:review-unavailable"


# --- 7a. a property's exception message is redacted unless debug ----------------------------------
def test_property_exception_message_is_redacted(caplog):
    caplog.set_level(logging.DEBUG, logger="invariants")
    with inv.isolated() as reg:
        inv.load(r6.leaky_doc())
        with inv.capture(reg) as events, pytest.raises(inv.Rejected) as ei:
            r6.leaky(r6.Leaky())
        assert "raised RuntimeError" in str(ei.value)
        assert r6.SECRET not in str(ei.value) and r6.SECRET not in ei.value.outcome.error
        assert all(r6.SECRET not in json.dumps(ev, default=str) for ev in events)
        assert r6.SECRET not in caplog.text
        inv.configure(reg, debug=True)
        with pytest.raises(inv.Rejected, match=r6.SECRET):
            r6.leaky(r6.Leaky())


def test_provider_path_property_message_is_redacted():
    from invariants.providers import resolve_path
    with pytest.raises(inv.cel.CelError) as ei:
        resolve_path({"args": {"o": r6.Leaky()}}, "args.o.boom")
    assert r6.SECRET not in str(ei.value) and ei.value.detail and r6.SECRET in ei.value.detail


# --- 7b. Checked supports copy, deepcopy and pickle; the edge still re-checks ----------------------
@pytest.mark.parametrize("kind", KINDS)
def test_checked_copy_deepcopy_pickle(iso, kind):
    c, _ = call(kind, json.dumps(good(kind)))
    assert copy.copy(c) is c
    d = copy.deepcopy(c)
    assert type(d) is type(c) and d.verdict == "ok" and d.attempts == c.attempts and d.value == c.value
    p = pickle.loads(pickle.dumps(c))
    assert type(p) is type(c) and p.verdict == "ok" and p.value == c.value
    consume = r6.MODELS[kind][1]
    assert consume(d) == d and consume(p) == p     # round 11: an equal re-validated copy
    with pytest.raises(AttributeError):
        d.verdict = "x"


def test_deepcopy_then_mutate_is_still_refused(iso):
    c, _ = call("dict", json.dumps(good("dict")))
    d = copy.deepcopy(c)
    d.value["evil"] = 1                     # the dict is a copy: the original is untouched
    assert "evil" not in c.value
    with pytest.raises(inv.Rejected, match="extra fields"):
        r6.dconsume(d)


def test_unpickle_refuses_a_non_checked_class():
    from invariants.guard import _rebuild_checked
    with pytest.raises(TypeError):
        _rebuild_checked(dict, {}, 1, "r", "ok")


# --- 7c. model_call keeps the declared Checked return type ----------------------------------------
@pytest.mark.skipif(shutil.which("mypy") is None and not (pathlib.Path(sys.executable).parent / "mypy").exists(),
                    reason="mypy not installed")
def test_model_call_keeps_the_checked_return_type(tmp_path):
    snippet = tmp_path / "typed_use.py"
    snippet.write_text(textwrap.dedent('''
        import dataclasses
        from typing import Literal, assert_type
        import invariants as inv
        from invariants.integrations import pydantic as invp

        @dataclasses.dataclass(frozen=True)
        class Reply:
            cause: Literal["A", "B"]

        class CheckedReply(inv.Checked):
            __slots__ = ()

        @inv.model_call("R-1", schema=Reply)
        def propose(prompt: str) -> CheckedReply:
            raise NotImplementedError

        @inv.model_call("R-2", schema=Reply)
        async def apropose(prompt: str) -> CheckedReply:
            raise NotImplementedError

        @invp.model_call("R-3", schema=Reply)
        def ppropose(prompt: str) -> CheckedReply:
            raise NotImplementedError

        async def use() -> None:
            assert_type(propose("x"), CheckedReply)
            assert_type(await apropose("x"), CheckedReply)
            assert_type(ppropose("x"), CheckedReply)
    '''))
    src = pathlib.Path(inv.__file__).resolve().parent.parent         # the invariants under test
    # A source checkout goes on MYPYPATH; an installed package is found as is (mypy refuses a
    # site-packages directory in MYPYPATH, so the sdist's tests run against an install too).
    env = {k: v for k, v in os.environ.items() if k != "MYPYPATH"}
    if src.name not in ("site-packages", "dist-packages"):
        env["MYPYPATH"] = str(src)
    r = subprocess.run([sys.executable, "-m", "mypy", "--no-incremental", "--strict", "--follow-imports=silent",
                        str(snippet)], cwd=tmp_path, capture_output=True, text=True, timeout=600, env=env)
    assert r.returncode == 0, r.stdout + r.stderr


# --- 7d. cel-expr's per-clause cost limit is enforced and named ------------------------------------
def test_cel_expr_iteration_budget_is_a_cost_limit_rule_error():
    try:
        b = get_backend("cel-expr")
    except Exception:
        pytest.skip("cel-expr not installed")
    p = b.compile("args.xs.exists(x, x < 0)")
    assert p.evaluate({"args": {"xs": list(range(9_999))}}) is False
    with pytest.raises(inv.cel.CelError, match="cost limit exceeded"):
        p.evaluate({"args": {"xs": list(range(10_000))}})
    nested = b.compile("args.xs.exists(x, args.xs.exists(y, x + y < 0))")
    with pytest.raises(inv.cel.CelError, match="cost limit exceeded"):
        nested.evaluate({"args": {"xs": list(range(101))}})
    assert b.iteration_limit == 10_000


# --- 7e/f. packaging and app hygiene ---------------------------------------------------------------
def test_publishing_checklist_names_project_urls_and_no_url_is_invented():
    """Round 7: the CHANGELOG TODO became the "Before publishing" checklist in RELEASING.md."""
    releasing = (ROOT / "RELEASING.md").read_text()
    assert "## Before publishing" in releasing and "project.urls" in releasing
    assert "RELEASING.md" in (ROOT / "CHANGELOG.md").read_text()
    assert "[project.urls]" not in (ROOT / "pyproject.toml").read_text()


def test_report_rules_file_is_relative_to_the_rootdir() -> None:
    from invariants.pytest_plugin import InvSession

    class Cfg:
        rootpath = ROOT
    plug = object.__new__(InvSession)
    plug.config = Cfg()  # type: ignore[attr-defined]
    assert plug._shown(ROOT / "tests" / "fixtures" / "invariants.yaml") == "tests/fixtures/invariants.yaml"
    assert plug._shown(pathlib.Path("/elsewhere/x.yaml")) == "/elsewhere/x.yaml"
