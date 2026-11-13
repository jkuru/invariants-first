"""Regression tests for fix round 23 (runtime, cel-expr): each test failed before its fix.

H1. cel-expr: a check the backend adds (arithmetic, double(), int(), timestamp(), duration(), long sums)
    never reads a value an earlier comprehension that stopped early left in cel-cpp's slot (the
    cel.bind defect let `xs.exists(q, q > 50) && a + b > 100` pass ship([60, 1], 500, 5)).
H2. A string holding a surrogate code point is a rule_error on every backend, never a crash (cel-expr
    segfaulted converting one).
M1. An int and a double compare exactly on every backend (cel-expr rounded the int past 2^53).
M2. contains(), startsWith(), endsWith() and matches() have a shared string-size limit, are linear on
    cel-expr, and charge the subset's budget by the characters they scan.
M3. Locks held by another thread at a fork are fresh in the child (pydantic reply cache, the backend
    cache, the Anthropic models).
M4. An edge whose model_call target has no schema refuses (LoadError at load, CoverageError at call).
L1. An Enum field in a {field: type} or dataclass schema parses by value.

The CEL tests run on every installed backend; the cel-expr-only ones skip without it.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from typing import Any

import pytest

import invariants as inv
import round23 as r23
from invariants import cel
from invariants.cel import available_backends, get_backend, values
from invariants.cel.errors import CelError, CelSyntaxError
from support import FIXTURES

BACKENDS = [b for b in ("subset", "cel-expr") if b in available_backends()]
HAS_CEL_EXPR = "cel-expr" in BACKENDS
needs_cel_expr = pytest.mark.skipif(not HAS_CEL_EXPR, reason="cel-expr not installed")


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def outcome(backend: str, fn: str, bad: Any, *args: Any) -> tuple[str, str | None]:
    """(kind, error) of one guarded call of round23.<fn>(*args) under a one-rule document."""
    with inv.isolated():
        inv.load(r23.doc(fn, bad), backend=backend)
        try:
            getattr(r23, fn)(*args)
        except inv.InvariantError as e:
            return e.outcome.kind, e.outcome.error
    return "ok", None


def ev(backend: str, expr: str, **args: Any) -> Any:
    return get_backend(backend).compile(expr).evaluate({"args": args})


def kind(backend: str, expr: str, **args: Any) -> tuple[str, Any]:
    try:
        return "value", ev(backend, expr, **args)
    except CelError as e:
        return "error", str(e)


# --- H1: no stale comprehension slot --------------------------------------------------------------
@pytest.mark.parametrize("qtys", [[60], [60, 1], [1, 60, 1], [1, 2, 60, 3]])
def test_a_bulk_line_after_an_early_exit_is_still_rejected(backend, qtys):
    bad = "args.qtys.exists(q, q > 50) && args.total + args.fee > 100"
    assert outcome(backend, "ship", bad, qtys, 500, 5)[0] == "violation"


TRUE_AFTER_EXIT = [   # each is true; cel-expr made each false (a stale slot)
    "args.l.all(x, x > 5) || args.i + 1 == 8",
    "!args.l.all(x, x > 5) && args.i + 1 == 8",
    "args.m.exists(k, true) && args.i + 1 == 8",
    "args.l.exists(x, x == 1) && args.i * 2 == 14",
    "args.l.exists(x, x == 1) && double(args.i) == 7.0",
    "(args.l.exists(x, x == 1) ? 1 : 0) + args.i == 8",
    "args.l.exists_one(x, x == 1) && args.i + 1 == 8",
    "args.l.filter(x, x == 1).size() == 1 && args.i + 1 == 8",
    "inv.sum(args.l) == 6 && args.l.exists(x, x == 1) && args.i + 1 == 8",
    "args.l.exists(x, x == 1) && duration('1s') + duration('1s') == duration('2s')",
    "args.l.exists(x, x == 1) && duration(args.d) + duration('1s') == duration('3s')",
    "args.l.exists(x, args.l.exists(y, y == 1)) && [0].all(k, [0].all(j, args.i * 2 == 14))",
    "args.l.exists(x, x == 1) && " + " + ".join(["args.i"] * 40) + " == 280",
    "args.l.exists(x, x == 1) && int(args.s) + 1 == 8",
]


@pytest.mark.parametrize("expr", TRUE_AFTER_EXIT)
def test_checks_after_an_early_exit_read_their_own_value(backend, expr):
    assert ev(backend, expr, i=7, l=[1, 2, 3], m={"a": 1, "b": 2}, d="2s", s="7") is True


@needs_cel_expr
def test_the_engine_defect_is_detected_and_the_slot_canaries_pass():
    from invariants.cel import expr_python as ep
    be = get_backend("cel-expr")
    assert be._slots_ok is True                         # type: ignore[attr-defined]
    data = {"args": dict(ep.SLOT_DATA)}
    # with cel.bind in a clause with a comprehension, every canary reads a stale slot on this engine
    old = be._FIN_LOOP                                  # type: ignore[attr-defined]
    try:
        be._FIN_LOOP = be._FIN                          # type: ignore[attr-defined]
        stale = []
        for src in ep.SLOT_CANARIES:
            try:
                stale.append(be.compile(src).evaluate_data(data) is not True)
            except CelError:
                stale.append(True)
        assert all(stale)
        assert be._check_slots() is False               # type: ignore[attr-defined]
    finally:
        be._FIN_LOOP = old                              # type: ignore[attr-defined]


@needs_cel_expr
def test_if_the_canaries_fail_a_clause_with_a_comprehension_and_a_check_is_refused(monkeypatch):
    be = get_backend("cel-expr")
    monkeypatch.setattr(be, "_slots_ok", False)
    with pytest.raises(CelSyntaxError, match="stale"):
        be.compile("args.qtys.exists(q, q > 50) && args.total + args.fee > 100")
    with inv.isolated(), pytest.raises(inv.LoadError, match="stale"):
        inv.load(r23.doc("ship", "args.qtys.exists(q, q > 50) && args.total + args.fee > 100"),
                 install=False, backend=be)
    assert be.compile("args.total + args.fee > 100").evaluate({"args": {"total": 99, "fee": 2}}) is True
    assert be.compile("args.qtys.exists(q, q > 50)").evaluate({"args": {"qtys": [60]}}) is True


@needs_cel_expr
def test_a_clause_with_a_comprehension_prints_no_cel_bind():
    from invariants.cel._engine import FrontEnd, has_comprehension
    be = get_backend("cel-expr")
    src = "args.l.exists(x, x > 1) && args.a + " + " + ".join(["args.a"] * 20) + " > double(args.s)"
    front = FrontEnd(src)
    assert has_comprehension(front.ast)
    text = front.text_for(be._NATIVE, be._RENAME, be._FIN_LOOP)   # type: ignore[attr-defined]
    assert "cel.bind" not in text and ".map(__inv_" in text


@needs_cel_expr
def test_arithmetic_in_a_comprehension_body_costs_one_more_iteration():
    # each check costs one iteration in a clause with a comprehension: 2 * 4,999 fits 9,999, 2 * 5,000 not
    assert ev("cel-expr", "args.l.all(x, x * 2 >= 0)", l=list(range(4999))) is True
    with pytest.raises(CelError, match="cost limit"):
        ev("cel-expr", "args.l.all(x, x * 2 >= 0)", l=list(range(5000)))


# --- H2: surrogate code points ---------------------------------------------------------------------
SURR = "\ud800"


@pytest.mark.parametrize("expr,args", [
    ("args.s == 'x'", {"s": SURR}),
    ("args.s == 'x'", {"s": "ok" + SURR + "ok"}),
    ("size(args.s) == 2", {"s": chr(0xD83D) + chr(0xDE00)}),     # a pair as two code points is not text either
    ("args.m == {}", {"m": {SURR: 1}}),
    ("args.l == []", {"l": [{"a": ["x", "\udfff"]}]}),
    ("args.l.exists(x, x.a == 'x')", {"l": [{"a": SURR}]}),
    ("args.m.exists(k, k == 'x')", {"m": {"\udc80": 1}}),
    ("args.s.contains('x')", {"s": SURR}),
])
def test_a_surrogate_string_is_a_rule_error(backend, expr, args):
    k, msg = kind(backend, expr, **args)
    assert k == "error" and "surrogate" in msg and SURR not in msg


def test_a_surrogate_in_a_record_or_str_subclass_is_a_rule_error(backend):
    import dataclasses

    @dataclasses.dataclass
    class Rec:
        name: str

    class S(str):
        pass
    for v in (Rec(SURR), S(SURR)):
        k, msg = kind(backend, "args.v == args.v", v=v)
        assert k == "error" and "surrogate" in msg


def test_a_surrogate_in_a_rule_literal_does_not_load(backend):
    for src in ("args.s == '\\ud800'", "args.s == '" + SURR + "'", "args.s == '\\U00110000'"):
        with pytest.raises(CelSyntaxError):
            get_backend(backend).compile(src)


def test_text_without_surrogates_still_reads(backend):
    assert ev(backend, "args.s == 'héllo 😀' && size(args.s) == 7", s="héllo 😀") is True


CHILD = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, {fixtures!r})
    import invariants as inv
    import round23 as r23
    backend, which = sys.argv[1], sys.argv[2]
    inv.load(r23.doc("text", "args.s == 'x'"), backend=backend)
    s = json.loads('"\\\\ud800"') if which == "json" else {{"k": ["\\ud800"]}}
    try:
        r23.text(s)
    except inv.InvariantError as e:
        print("KIND", e.outcome.kind)
    else:
        print("KIND ok")
""")


@pytest.mark.parametrize("which", ["json", "nested"])
def test_a_surrogate_never_crashes_the_process(backend, which):
    code = CHILD.format(fixtures=str(FIXTURES))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    p = subprocess.run([sys.executable, "-c", code, backend, which], capture_output=True, text=True, env=env,
                       timeout=60)
    assert p.returncode == 0, (p.returncode, p.stderr[-500:])     # was -11 (SIGSEGV) on cel-expr
    assert "KIND rule_error" in p.stdout


# --- M1: int and double compared exactly -----------------------------------------------------------
@pytest.mark.parametrize("cents,limit,want", [
    (2**53 + 1, 2.0**53, "violation"),           # 2^53 + 1 > 2^53
    (2**63 - 1, 2.0**63, "ok"),                  # 2^63 - 1 < 2^63
    (2**53, 2.0**53, "ok"),
    (-(2**53) - 1, -(2.0**53), "ok"),
])
def test_int_and_double_compare_exactly(backend, cents, limit, want):
    assert outcome(backend, "pay", "args.cents > args.limit", cents, limit)[0] == want


@pytest.mark.parametrize("expr,want", [
    ("args.a * 1024 + 1 > args.d", True),                  # a computed int past 2^53
    ("int(args.s) > args.d", True),                         # int() of text
    ("inv.sum([args.a * 1024, 1]) > args.d", True),        # inv.sum
    ("args.l.exists(x, x > args.d)", True),                 # an int in a list of the data
    ("double(args.a * 1024) == args.a * 1024 + 1", False),  # == too
    ("args.a * 1024 + 1 in [args.d]", False),               # and in
    ("9007199254740993 > args.d", True),                    # a literal
    ("args.a * 1024 + 1 > 9007199254740992.0 || 1 / 0 == 1", True),   # an error the exact result absorbs
])
def test_computed_and_listed_ints_compare_exactly(backend, expr, want):
    assert ev(backend, expr, a=2**43, s=str(2**53 + 1), l=[1, 2**53 + 1], d=2.0**53) is want


def test_big_ints_without_doubles_stay_on_the_engine(backend):
    assert ev(backend, "args.a * 1024 + 1 > args.b", a=2**43, b=2**53) is True


# --- M2: string functions --------------------------------------------------------------------------
@pytest.fixture
def small_limit():
    old = values.set_string_limit(1000)
    try:
        yield 1000
    finally:
        values.set_string_limit(old)


@pytest.mark.parametrize("expr", ["args.s.contains(args.t)", "args.s.startsWith(args.t)",
                                  "args.s.endsWith(args.t)", "args.s.matches('a')", "args.t.matches(args.s)"])
def test_string_functions_refuse_an_operand_past_the_limit(backend, small_limit, expr):
    assert kind(backend, expr, s="a" * 1000, t="b")[0] == "value"
    k, msg = kind(backend, expr, s="a" * 1001, t="b")
    assert k == "error" and "string-size limit" in msg and "1,001" not in msg


def test_the_limit_is_set_by_the_environment_and_checked():
    code = "from invariants.cel import values; print(values.string_limit())"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path), values.STRING_LIMIT_ENV: "1234"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert out.stdout.strip() == "1234"
    assert values.string_limit() == values.DEFAULT_STRING_LIMIT
    with pytest.raises(ValueError):
        cel.set_string_limit(0)


@needs_cel_expr
def test_contains_on_cel_expr_is_linear():
    old = values.set_string_limit(4_000_000)
    try:
        p = get_backend("cel-expr").compile("args.s.contains(args.t)")
        n = 2_000_000
        t0 = time.perf_counter()
        assert p.evaluate({"args": {"s": "a" * n, "t": "a" * (n // 2) + "b"}}) is False
        assert time.perf_counter() - t0 < 2.0          # 17 s before (cel-cpp's quadratic find)
    finally:
        values.set_string_limit(old)


def test_the_subset_charges_string_scans_to_its_budget():
    from invariants.cel.subset import SubsetBackend
    p = SubsetBackend().compile("args.l.all(x, args.s.contains(x))")
    s = "a" * 900_000
    with pytest.raises(CelError, match="cost limit"):
        p.evaluate({"args": {"s": s, "l": ["b"] * 50}})          # 50 scans of 900,000 characters
    assert p.evaluate({"args": {"s": "ab", "l": ["b"] * 50}}) is True


# --- M3: fork safety -------------------------------------------------------------------------------
def _fork_and_call(fn: Any) -> int:
    """Fork; the child calls fn() under a 5 s alarm. The child's exit status (0: fn returned)."""
    pid = os.fork()
    if pid == 0:                                              # pragma: no cover - the child
        signal.alarm(5)
        try:
            fn()
            os._exit(0)
        except BaseException:
            os._exit(3)
    _, status = os.waitpid(pid, 0)
    return -os.WTERMSIG(status) if os.WIFSIGNALED(status) else os.WEXITSTATUS(status)


def _held(lock_of: Any, fn: Any) -> int:
    """Another thread holds the lock (lock_of() reads it now) while the process forks."""
    taken, release = threading.Event(), threading.Event()

    def holder() -> None:
        with lock_of():
            taken.set()
            release.wait(10)
    t = threading.Thread(target=holder)
    t.start()
    taken.wait(5)
    try:
        return _fork_and_call(fn)
    finally:
        release.set()
        t.join()


fork_only = pytest.mark.skipif(not hasattr(os, "fork") or sys.platform == "win32", reason="needs os.fork")


@fork_only
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_forked_child_parses_while_the_parent_held_the_pydantic_cache_lock():
    pydantic = pytest.importorskip("pydantic")
    from invariants.integrations import pydantic as ip

    class M(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="forbid")
        a: int
    assert _held(lambda: ip._WALKED_LOCK, lambda: inv.parse('{"a": 1}', M)) == 0


@fork_only
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_forked_child_gets_a_backend_while_the_parent_held_the_backend_lock():
    assert _held(lambda: cel._LOCK, lambda: cel.register_backend("x-r23", cel.SubsetBackend)) == 0


@fork_only
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_forked_child_uses_a_model_whose_lock_the_parent_held():
    from invariants.integrations.anthropic import AnthropicModel
    m = AnthropicModel(client=object())
    assert _held(lambda: m._lock, m.client) == 0


# --- M4: an edge with nothing to re-check against --------------------------------------------------
def test_an_edge_rule_whose_target_has_no_schema_does_not_load():
    with inv.isolated(), pytest.raises(inv.LoadError, match="functools.wraps"):
        inv.load(r23.edge_doc())


def test_an_edge_whose_target_lost_its_schema_refuses_the_call(monkeypatch):
    from invariants.testing import make_checked
    with inv.isolated():
        inv.load(r23.edge_doc("plain_plan"))
        forged = make_checked(r23.CPlan, {"action": "launch", "n": 1, "extra": "x"}, verdict="ok")
        monkeypatch.delattr(r23.plain_plan, "__inv_schema__")
        r23.SEEN.clear()
        with pytest.raises(inv.CoverageError, match="functools.wraps"):
            r23.act(forged)                # on_error fail_open_and_page: refused anyway
        assert r23.SEEN == []


def test_an_edge_with_its_schema_still_rechecks():
    from invariants.testing import make_checked
    with inv.isolated():
        inv.load(r23.edge_doc("plain_plan"))
        r23.SEEN.clear()
        good = make_checked(r23.CPlan, {"action": "go", "n": 1}, verdict="ok")
        assert r23.act(good) == "acted" and r23.SEEN[0] is not good and r23.SEEN[0].value == {"action": "go", "n": 1}


# --- L1: Enum fields parse by value ----------------------------------------------------------------
def _mix(**fields: Any) -> tuple[str, Any]:
    r23.MIX[:] = [r23.reply(**fields)]
    try:
        c = r23.mix()
    except inv.InvariantError as e:
        return e.outcome.kind, None
    return "ok", c


def test_enum_fields_parse_by_value_and_rules_read_the_value():
    with inv.isolated():
        inv.load(r23.enum_doc())
        k, c = _mix(color="red", level=2)
        assert k == "ok" and c.value == {"color": r23.Color.RED, "level": r23.Level.HIGH}
        assert type(c.value["color"]) is r23.Color
        assert _mix(color="blue", level=2)[0] == "violation"
        assert r23.use_mix(c) == {"color": r23.Color.RED, "level": r23.Level.HIGH}   # the edge re-checks it


@pytest.mark.parametrize("fields", [{"color": "green", "level": 1}, {"color": "red", "level": True},
                                    {"color": "red", "level": 1.0}, {"color": "RED", "level": 1},
                                    {"color": None, "level": 1}])
def test_a_value_outside_the_enum_is_a_parse_error(fields):
    with inv.isolated():
        inv.load(r23.enum_doc())
        assert _mix(**fields)[0] == "parse_error"


def test_an_enum_field_in_a_dataclass_schema_parses_by_value():
    with inv.isolated():
        inv.load(r23.enum_doc())
        r23.MIX[:] = [r23.reply(color="red", level=1)]
        p = r23.paint()
        assert p.value == r23.Paint(r23.Color.RED, r23.Level.LOW)
        r23.MIX[:] = [r23.reply(color="blue", level=1)]
        with pytest.raises(inv.InvariantError) as ei:
            r23.paint()
        assert ei.value.outcome.kind == "violation"


def test_an_edge_refuses_a_member_of_another_enum():
    import enum

    from invariants.testing import make_checked

    class Other(enum.Enum):
        RED = "red"
    with inv.isolated():
        inv.load(r23.enum_doc())
        forged = make_checked(r23.CMix, {"color": Other.RED, "level": r23.Level.LOW}, verdict="ok")
        with pytest.raises(inv.InvariantError) as ei:
            r23.use_mix(forged)
        assert ei.value.outcome.kind == "parse_error"


# --- L2: the duration range and constant timezones -------------------------------------------------
@needs_cel_expr
def test_a_duration_past_int64_nanoseconds_is_refused_on_cel_expr_not_misread():
    import datetime as dt
    long = dt.timedelta(seconds=315576000000)            # CEL's bound; cel-cpp held it saturated
    assert ev("subset", "args.a + args.b == args.a", a=long, b=dt.timedelta(0)) is True
    k, msg = kind("cel-expr", "args.a + args.b == args.a", a=long, b=dt.timedelta(0))
    assert k == "error" and "out of range" in msg     # was False (a saturated value), never an error
    assert kind("cel-expr", "duration(args.s) > duration('1s')", s="3000000h")[0] == "error"
    k, msg = kind("cel-expr", "duration('3000000h') > duration('1s')")
    assert k == "error" and "out of range" in msg     # a literal too, at run time
    assert ev("subset", "duration('3000000h') > duration('1s')") is True
    ok = dt.timedelta(days=365 * 290)
    assert ev("cel-expr", "args.a + args.b == args.a", a=ok, b=dt.timedelta(0)) is True


def test_a_constant_timezone_the_subset_refuses_is_a_rule_error_everywhere(backend):
    import datetime as dt
    t = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    k, msg = kind(backend, "args.t.getHours('+25:00') == 1", t=t)
    assert k == "error" and "timezone" in msg           # cel-expr read a 25-hour offset (True)
    assert ev(backend, "args.t.getHours('+05:00') == 5", t=t) is True
