"""Regression tests for fix round 26 (runtime): each test failed before its fix.

H1. The subset reads a str, int, float or bytes subclass as the plain value (as cel-expr does): a
    `class Role(str)` equal to 'guest' is a violation, an int or float subclass does arithmetic, a
    subclass map key is found. Enum members are still their values; bool and uint are kept.
H2. A dataclass or pydantic reply field that a Checked attribute would shadow (value, attempts, rule,
    verdict, _inv_*, an attribute of the Checked subclass) is refused at decoration.
M1. The subset does size(), ordering and + on bytes, as cel-expr (byte length, lexicographic by byte,
    concatenation).
M2. The FastAPI guard passes a Response the handler returns through untouched (a 404 stays a 404); a
    post_effect clause that reads result then is a rule_error decided by on_error, never a 500.
M3. A model call's abstain below effective enforce (detect, a candidate rule) is recorded and the call
    goes on with verdict abstain (no Escalation, no review), sync and async alike.

The CEL tests run on every installed backend; the cel-expr-only ones skip without it.
"""
from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
from typing import Any

import pytest

import invariants as inv
import round26rt as r26

from invariants.cel import available_backends, get_backend
from invariants.cel.errors import CelError

BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]
needs_cel_expr = pytest.mark.skipif("cel-expr" not in BACKENDS, reason="cel-expr not installed")


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def ev(backend: str, expr: str, **env: Any) -> tuple[str, Any]:
    try:
        return "value", get_backend(backend).compile(expr).evaluate(env)
    except CelError:
        return "error", None


class MyStr(str): ...
class MyInt(int): ...
class MyFloat(float): ...
class MyBytes(bytes): ...


class Level(enum.IntEnum):
    LOW = 1


class Name(enum.StrEnum):
    A = "a"


# --- H1: subclasses of str, int, float and bytes are their plain values ----------------------------
def test_role_str_subclass_is_rejected(backend):
    with inv.isolated():
        inv.load(r26.role_doc(), backend=backend)
        with pytest.raises(inv.Rejected):
            r26.delete_account("u1", r26.Role("guest"))
        with pytest.raises(inv.Rejected):
            r26.delete_account("u1", "guest")
        assert r26.delete_account("u1", r26.Role("admin")) == "deleted"


@pytest.mark.parametrize("expr, v", [
    ("x == 'a'", MyStr("a")), ("!(x != 'a')", MyStr("a")), ("x < 'b'", MyStr("a")), ("x in ['a']", MyStr("a")),
    ("m[x] == 1", MyStr("a")), ("[x] == ['a']", MyStr("a")), ("{'k': x} == {'k': 'a'}", MyStr("a")),
    ("x + 1 == 2", MyInt(1)), ("x * 2 == 2 && x / 1 == 1 && x % 2 == 1", MyInt(1)), ("inv.sum([x, 1]) == 2", MyInt(1)),
    ("x + 1.0 == 2.5", MyFloat(1.5)), ("x - 0.5 == 1.0", MyFloat(1.5)),
    ("x == y", MyBytes(b"ab")), ("size(x) == 2", MyBytes(b"ab")),
    ("x + 1 == 2", Level.LOW), ("x == 'a' && x + 'b' == 'ab'", Name.A),
    ("lst.exists(e, e == 'a')", None), ("mk['a'] == 1 && mk[1] == 2", None),
])
def test_subclasses_read_as_plain_values(backend, expr, v):
    env = {"x": v, "y": b"ab", "m": {"a": 1}, "lst": [MyStr("a")], "mk": {MyStr("a"): 1, MyInt(1): 2}}
    assert ev(backend, expr, **env) == ("value", True)


def test_bool_and_out_of_range_int_subclass_unchanged(backend):
    assert ev(backend, "x == true && !(x == 1)", x=True) == ("value", True)
    assert ev(backend, "x + 1 > 0", x=MyInt(2**63))[0] == "error"      # outside int64: still an error
    assert ev(backend, "x > 1.0", x=MyFloat(float("nan")))[0] == "error"   # a NaN subclass too


def test_uint_kept_in_the_exact_evaluation():
    from invariants.cel.subset.data import Uint, _data
    assert type(_data(Uint(5))) is Uint
    assert type(_data(MyInt(5))) is int and type(_data(MyStr("a"))) is str
    assert type(_data(MyFloat(1.0))) is float and type(_data(MyBytes(b"a"))) is bytes
    assert _data(Level.LOW) == 1 and type(_data(Name.A)) is str
    with pytest.raises(CelError):
        _data(MyFloat(float("nan")))            # a non-finite float subclass is refused where it is read


@needs_cel_expr
def test_exact_path_with_subclasses_agrees():
    # an int past 2^53 meeting a double: cel-expr evaluates the clause exactly on the subset
    for e in ("x + 9007199254740993 > 1.0 && s == 'a'", "size(b) + 9007199254740993 > 1.0 && b + b == bb"):
        env = {"x": MyInt(1), "s": MyStr("a"), "b": MyBytes(b"ab"), "bb": b"abab"}
        assert ev("cel-expr", e, **env) == ev("subset", e, **env) == ("value", True)


# --- H2: reply fields a Checked attribute would shadow are refused ---------------------------------
@pytest.mark.parametrize("field", ["verdict", "rule", "attempts", "value", "_inv_state", "_inv_later"])
def test_shadowed_dataclass_field_refused(field):
    Ruling = dataclasses.make_dataclass("Ruling", [(field, str), ("note", str)], frozen=True)

    class CheckedRuling(inv.Checked):
        __slots__ = ()

    with pytest.raises(TypeError, match=f"reply field '{field}'.*shadowed.*reply_{field}"):
        @inv.model_call("J-1", schema=Ruling, returns=CheckedRuling)
        def judge(claim: str) -> str: ...


def test_shadowed_subclass_attribute_refused():
    @dataclasses.dataclass(frozen=True)
    class Plan:
        total: int

    class CheckedPlan(inv.Checked):
        __slots__ = ()

        def total(self) -> int:
            return 0

    with pytest.raises(TypeError, match="reply field 'total'.*CheckedPlan attribute"):
        @inv.model_call("J-1", schema=Plan, returns=CheckedPlan)
        def judge(claim: str) -> str: ...


def test_shadowed_pydantic_field_refused():
    pydantic = pytest.importorskip("pydantic")

    class Ruling(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="forbid")
        verdict: str

    class CheckedRuling(inv.Checked):
        __slots__ = ()

    with pytest.raises(TypeError, match="reply field 'verdict'"):
        @inv.model_call("J-1", schema=Ruling, returns=CheckedRuling)
        def judge(claim: str) -> str: ...


def test_dict_schema_with_verdict_key_is_allowed():
    # a {field: type} reply is a dict, read by key (checked.value['verdict']): nothing is shadowed
    class CheckedRuling(inv.Checked):
        __slots__ = ()

    @inv.model_call("J-1", schema={"verdict": str}, returns=CheckedRuling)
    def judge(claim: str) -> str: ...
    assert judge is not None


def test_unshadowed_fields_still_forward():
    @dataclasses.dataclass(frozen=True)
    class Ruling:
        decision: str

    class CheckedRuling(inv.Checked):
        __slots__ = ()

    @inv.model_call("J-2", schema=Ruling, returns=CheckedRuling)
    def judge(claim: str) -> str: ...
    assert judge is not None


# --- M1: bytes on the subset -----------------------------------------------------------------------
@pytest.mark.parametrize("expr", [
    "size(b) == 2", "b.size() == 2", "size(n) == 2", "b < c", "b <= c", "!(b > c)", "c >= b", "z < b",
    "b + c == d", "b + z == b", "hi > b", "!(b < z)",
])
def test_bytes_size_order_concat(backend, expr):
    env = {"b": b"ab", "c": b"b", "d": b"abb", "z": b"", "hi": b"\xff", "n": "é".encode()}
    assert ev(backend, expr, **env) == ("value", True)


def test_bytes_do_not_mix_with_strings(backend):
    assert ev(backend, "b + s == s", b=b"a", s="a")[0] == "error"
    assert ev(backend, "b < s", b=b"a", s="a")[0] == "error"


# --- M2: a Response from a FastAPI handler passes through -------------------------------------------
def _shop() -> Any:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    import warnings
    warnings.simplefilter("ignore")
    import round26rt_shop as shop
    from fastapi.testclient import TestClient
    from invariants.integrations import fastapi as inv_fastapi
    inv_fastapi.install(shop.app)
    return shop, TestClient(shop.app, raise_server_exceptions=False)


def test_response_passes_through_pre_effect():
    shop, c = _shop()
    with inv.isolated():
        inv.load(shop.doc("args.oid == 'bad'"))
        for base in ("/orders", "/aorders"):
            r = c.get(f"{base}/missing")
            assert (r.status_code, r.json()) == (404, {"detail": "not found"})
            assert c.get(f"{base}/1").json() == {"total": 5}
            assert c.get(f"{base}/bad").status_code == 422


@pytest.mark.parametrize("on_error, status", [("fail_closed", 503), ("fail_open_and_page", 404)])
def test_post_effect_rule_on_a_response_is_a_rule_error(on_error, status):
    shop, c = _shop()
    with inv.isolated() as reg:
        pages: list[Any] = []
        reg.pager = pages.append
        inv.load(shop.doc("result.total > 100", on_error=on_error, timing="post_effect"))
        for base in ("/orders", "/aorders"):
            assert c.get(f"{base}/missing").status_code == status
            assert c.get(f"{base}/1").status_code == 200
            assert c.get(f"{base}/big").status_code == 422
        kinds = [e["outcome"] for e in reg.sink]
        assert kinds.count("rule_error") == 2 and kinds.count("violation") == 2
        assert len(pages) == (2 if on_error == "fail_open_and_page" else 0)


# --- M3: an abstain below enforce never blocks ------------------------------------------------------
def _run(fn: Any) -> Any:
    r = fn("o1")
    return asyncio.run(r) if asyncio.iscoroutine(r) else r


@pytest.mark.parametrize("status", ["candidate", "detect"])
@pytest.mark.parametrize("fn", ["plan", "aplan"])
def test_abstain_below_enforce_proceeds(status, fn):
    with inv.isolated() as reg:
        inv.load(r26.abstain_doc(status))
        r26.REPLIES[:] = [json.dumps({"action": "ESCALATE_TO_HUMAN", "amount": 0})]
        c = _run(getattr(r26, fn))
        assert c.verdict == "abstain" and c.attempts == 1 and c.value.action == "ESCALATE_TO_HUMAN"
        assert reg.review_store.items == ()     # no review opened
        ev_ = [e for e in reg.sink if e["outcome"] == "abstain"]
        assert len(ev_) == 1 and ev_[0]["action"] == "proceed" and ev_[0]["mode"] == "detect"
        assert r26.execute(c, "o1") == 0         # the edge in detect records and proceeds


@pytest.mark.parametrize("fn", ["plan", "aplan"])
def test_abstain_enforce_escalates_and_detect_model_call_with_enforcing_edge(fn):
    with inv.isolated():
        inv.load(r26.abstain_doc("enforce"))
        r26.REPLIES[:] = [json.dumps({"action": "ESCALATE_TO_HUMAN", "amount": 0})]
        with pytest.raises(inv.Escalation, match="opened"):
            _run(getattr(r26, fn))
    with inv.isolated():
        inv.load(r26.abstain_doc("enforce", model_mode="detect"))
        r26.REPLIES[:] = [json.dumps({"action": "ESCALATE_TO_HUMAN", "amount": 0})]
        c = _run(getattr(r26, fn))
        assert c.verdict == "abstain"
        with pytest.raises(inv.InvariantError):  # the enforcing edge refuses a verdict that is not ok
            r26.execute(c, "o1")


def test_abstain_ablated_is_passive():
    with inv.isolated():
        inv.load(r26.abstain_doc("detect"))
        r26.REPLIES[:] = [json.dumps({"action": "ESCALATE_TO_HUMAN", "amount": 0})]
        with inv.ablated("M-1"):
            assert r26.plan("o1").verdict == "ok"
