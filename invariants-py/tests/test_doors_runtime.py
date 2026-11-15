"""The run-time door check (inv.doors.install, pytest --inv-doors): a path law's sink reached only through
its doors, checked as the code runs, so dynamic dispatch the scan cannot follow is caught too."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import textwrap
import pathlib
from typing import Any, Iterator

import pytest

import invariants as inv
from invariants import doors_runtime

pytest_plugins = ["pytester"]

SHOP = textwrap.dedent('''\
    import invariants as inv

    class Gateway:
        def refund(self, amount):
            return ("refunded", amount)

        @staticmethod
        def s_charge(amount):
            return amount

        @classmethod
        def c_charge(cls, amount):
            return (cls.__name__, amount)

        async def arefund(self, amount):
            await asyncio_sleep()
            return amount

    async def asyncio_sleep():
        import asyncio
        await asyncio.sleep(0)

    class SubGateway(Gateway):
        pass

    class Ledger:
        def write_a(self):
            return "a"

        def write_b(self):
            return "b"

    class Service:
        def __init__(self):
            self.gw = Gateway()
            self.ledger = Ledger()

        def refund(self, amount):
            return self.gw.refund(amount)

        def audit(self, amount):
            return self.refund(amount)

        @inv.guard("CAP")
        def guarded_refund(self, amount):
            return self.gw.refund(amount)

        def s_door(self, amount):
            return Gateway.s_charge(amount), self.gw.c_charge(amount)

        async def arefund(self, amount):
            return await self.gw.arefund(amount)

        def door_a(self):
            return self.ledger.write_a()

        def door_b(self, also_a=False):
            return self.ledger.write_a() if also_a else self.ledger.write_b()

        def gen_door(self, n):
            for i in range(n):
                yield self.gw.refund(i)

        async def agen_door(self, n):
            for i in range(n):
                yield self.gw.refund(i)

        def thread_door(self, amount):
            import threading
            out = []
            t = threading.Thread(target=lambda: out.append(self.gw.refund(amount)))
            t.start()
            t.join()
            return out

        async def to_thread_door(self, amount):
            import asyncio
            return await asyncio.to_thread(self.gw.refund, amount)

    def legacy(gw, amount):
        return gw.refund(amount)

    def legacy_a(ledger, which="a"):
        return ledger.write_a() if which == "a" else ledger.write_b()

    class Slotted:
        __slots__ = ()

        def write(self):
            return "w"

    SLOTTED = Slotted()

    def tool_getattr(gw, amount):
        return getattr(gw, "re" + "fund")(amount)

    DISPATCH = {"refund": lambda gw, amount: gw.refund(amount)}

    def tool_dispatch(gw, amount):
        return DISPATCH["refund"](gw, amount)

    def door_fn(gw, amount):
        return gw.refund(amount)
    ''')

USER = "from shop import door_fn, Gateway\n\ndef call(amount):\n    return door_fn(Gateway(), amount)\n"


def _doc(*rules: dict[str, Any]) -> dict[str, Any]:
    cap = {"id": "CAP", "statement": "small", "guard": "py:shop.Service.guarded_refund", "bad": "args.amount > 100",
           "tests": ["t.py::t"]}
    return {"spec": "invariant/v0.1", "targets": {"py:shop.Service.guarded_refund": {"effects": "external"}},
            "rules": [cap, *rules]}


GW = {"id": "GW-DOOR", "door": ["py:shop.Service.refund", "py:shop.Service.guarded_refund",
                                "py:shop.Service.gen_door", "py:shop.door_fn",
                                "py:shop.Service.thread_door", "py:shop.Service.agen_door", "py:shop.Service.to_thread_door"],
      "sink": "py:shop.Gateway.refund", "allow": ["py:shop.legacy"]}


@pytest.fixture
def shop(tmp_path: pathlib.Path) -> Iterator[Any]:
    (tmp_path / "shop.py").write_text(SHOP)
    (tmp_path / "shop_user.py").write_text(USER)
    sys.path.insert(0, str(tmp_path))
    try:
        import shop as mod
        yield mod
    finally:
        for h in doors_runtime.installed():
            while h.installed:
                h.uninstall()
        sys.path.remove(str(tmp_path))
        for m in ("shop", "shop_user"):
            sys.modules.pop(m, None)


def _load(*rules: dict[str, Any]) -> inv.Registry:
    return inv.load(_doc(*rules), install=False)


# --- the check ---------------------------------------------------------------------------------------------
def test_a_door_less_sink_call_raises_in_enforce(shop):
    reg = _load(GW)
    with inv.using(reg), inv.doors.install(mode="enforce") as h:
        with pytest.raises(inv.BypassError) as e:
            shop.Gateway().refund(987654)
        msg = str(e.value)
        assert "GW-DOOR" in msg and "py:shop.Gateway.refund" in msg and "py:shop.Service.refund" in msg
        assert "987654" not in msg                                  # never an argument value
        assert e.value.rule == "GW-DOOR" and e.value.point == "door"
        assert isinstance(e.value, inv.InvariantError)
        assert h.violations == [{"rule": "GW-DOOR", "sink": "py:shop.Gateway.refund", "mode": "enforce"}]
        assert reg.counts("GW-DOOR")["violation"] == 1


def test_a_door_less_sink_call_records_in_detect(shop, caplog):
    reg = _load(GW)
    with inv.using(reg), inv.capture(reg) as events, inv.doors.install(mode="detect") as h:
        with caplog.at_level(logging.WARNING, logger="invariants"):
            assert shop.Gateway().refund(5) == ("refunded", 5)      # the call goes on
    assert [(e["rule"], e["outcome"], e["point"], e["mode"], e["sink"]) for e in events] == [
        ("GW-DOOR", "violation", "door", "detect", "py:shop.Gateway.refund")]
    assert "5" not in events[0]["messages"][0].replace("GW-DOOR", "")
    assert h.violations[0]["mode"] == "detect"
    assert any("GW-DOOR" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def test_through_the_door_passes_and_nothing_is_recorded(shop):
    reg = _load(GW)
    with inv.using(reg), inv.capture(reg) as events, inv.doors.install(mode="enforce") as h:
        assert shop.Service().refund(5) == ("refunded", 5)
        assert shop.Service().audit(6) == ("refunded", 6)           # a door deeper down the stack
        assert inv.doors.active() == frozenset()                    # nothing left active after it
    assert events == [] and h.violations == []


def test_getattr_and_dict_dispatch_from_a_non_door_fail(shop):
    with inv.using(_load(GW)), inv.doors.install(mode="enforce"):
        with pytest.raises(inv.BypassError):
            shop.tool_getattr(shop.Gateway(), 5)
        with pytest.raises(inv.BypassError):
            shop.tool_dispatch(shop.Gateway(), 5)
        with pytest.raises(inv.BypassError):                         # an inherited sink, through a subclass
            shop.SubGateway().refund(5)


def test_an_allow_entry_passes(shop):
    with inv.using(_load(GW)), inv.doors.install(mode="enforce") as h:
        assert shop.legacy(shop.Gateway(), 5) == ("refunded", 5)
    assert h.violations == []


def test_the_mapping_form_checks_each_sinks_own_doors(shop):
    rule = {"id": "LEDGER", "sink": {"py:shop.Ledger.write_a": ["py:shop.Service.door_a"],
                                     "py:shop.Ledger.write_b": "py:shop.Service.door_b"},
            "allow": {"py:shop.legacy_a": ["py:shop.Ledger.write_a"]}}
    with inv.using(_load(rule)), inv.doors.install(mode="enforce"):
        svc = shop.Service()
        assert svc.door_a() == "a" and svc.door_b() == "b"
        assert shop.legacy_a(svc.ledger) == "a"                     # allowed for write_a
        with pytest.raises(inv.BypassError) as e:                    # a door of write_b is no door for write_a
            svc.door_b(also_a=True)
        assert "py:shop.Ledger.write_a" in str(e.value) and "py:shop.Service.door_a" in str(e.value)
        assert "door_b" not in str(e.value)
        with pytest.raises(inv.BypassError):                         # allowed for write_a only
            shop.legacy_a(svc.ledger, "b")


def test_two_rules_on_one_sink_must_each_be_satisfied(shop):
    one = {"id": "ONE", "door": "py:shop.Service.refund", "sink": "py:shop.Gateway.refund"}
    two = {"id": "TWO", "door": "py:shop.Service.audit", "sink": "py:shop.Gateway.refund"}
    with inv.using(_load(one, two)), inv.doors.install(mode="enforce") as h:
        with pytest.raises(inv.BypassError) as e:
            shop.Service().refund(5)                                 # ONE's door, not TWO's
        assert e.value.rule == "TWO"
        assert shop.Service().audit(5) == ("refunded", 5)           # audit -> refund: both doors running
        with pytest.raises(inv.BypassError) as e:
            shop.Gateway().refund(5)
        assert e.value.rule == "ONE"
    assert [v["rule"] for v in h.violations] == ["TWO", "ONE"]


def test_two_rules_both_missed_in_detect_record_both(shop):
    one = {"id": "ONE", "door": "py:shop.Service.refund", "sink": "py:shop.Gateway.refund"}
    two = {"id": "TWO", "door": "py:shop.Service.audit", "sink": "py:shop.Gateway.refund"}
    with inv.using(_load(one, two)), inv.doors.install(mode="detect") as h:
        shop.Gateway().refund(5)
    assert sorted(v["rule"] for v in h.violations) == ["ONE", "TWO"]


def test_an_ablated_rule_is_skipped(shop, monkeypatch):
    reg = _load(GW)
    with inv.using(reg), inv.doors.install(mode="enforce") as h:
        with inv.ablated("GW-DOOR"):
            assert shop.Gateway().refund(5) == ("refunded", 5)
        monkeypatch.setenv("INV_ABLATE", "GW-DOOR")
        monkeypatch.setenv("INV_ABLATE_TESTING", "1")
        assert shop.Gateway().refund(5) == ("refunded", 5)
        monkeypatch.delenv("INV_ABLATE")
        with pytest.raises(inv.BypassError):
            shop.Gateway().refund(5)
    assert len(h.violations) == 1


def test_a_retired_rule_is_not_installed(shop):
    with inv.using(_load({**GW, "status": "retired"})), inv.doors.install(mode="enforce") as h:
        assert shop.Gateway().refund(5) == ("refunded", 5)
        assert h.rules == []


def test_the_mode_is_capped_by_the_rules_status(shop):
    with inv.using(_load({**GW, "status": "detect"})), inv.doors.install(mode="enforce") as h:
        assert shop.Gateway().refund(5) == ("refunded", 5)          # a detect rule records, never raises
    assert h.violations == [{"rule": "GW-DOOR", "sink": "py:shop.Gateway.refund", "mode": "detect"}]


def test_async_door_to_sink_passes_and_door_less_async_fails(shop):
    rule = {"id": "ASYNC", "door": "py:shop.Service.arefund", "sink": "py:shop.Gateway.arefund"}
    with inv.using(_load(rule)), inv.doors.install(mode="enforce"):
        assert inspect_coro(shop.Gateway.arefund) and inspect_coro(shop.Service.arefund)
        assert asyncio.run(shop.Service().arefund(3)) == 3
        with pytest.raises(inv.BypassError):
            asyncio.run(shop.Gateway().arefund(3))


def inspect_coro(fn: Any) -> bool:
    import inspect
    return inspect.iscoroutinefunction(fn)


def test_contexts_are_isolated_across_concurrent_tasks(shop):
    rule = {"id": "ASYNC", "door": "py:shop.Service.arefund", "sink": "py:shop.Gateway.arefund"}

    async def main() -> list[Any]:
        gate = asyncio.Event()
        orig = shop.Gateway.arefund

        async def slow(self: Any, amount: int) -> int:               # the sink waits inside the door
            await gate.wait()
            return await orig(self, amount)

        async def through_door() -> Any:
            return await shop.Service().arefund(1)

        async def bypass() -> Any:
            await asyncio.sleep(0)                                   # while the door task is inside its door
            try:
                return await shop.Gateway().arefund(2)
            except inv.BypassError as e:
                return e
            finally:
                gate.set()
        a = asyncio.create_task(through_door())
        b = asyncio.create_task(bypass())
        return list(await asyncio.gather(a, b))
    with inv.using(_load(rule)), inv.doors.install(mode="enforce"):
        got = asyncio.run(main())
    assert got[0] == 1 and isinstance(got[1], inv.BypassError)


def test_threads_a_plain_thread_loses_the_door_to_thread_keeps_it(shop):
    with inv.using(_load(GW)), inv.doors.install(mode="detect") as h:
        assert shop.Service().thread_door(1) == [("refunded", 1)]
        assert [v["rule"] for v in h.violations] == ["GW-DOOR"]     # the documented limit
        assert asyncio.run(shop.Service().to_thread_door(7)) == ("refunded", 7)
        assert len(h.violations) == 1


def test_uninstall_restores_the_exact_originals(shop):
    import shop_user
    rule = {"id": "S", "door": "py:shop.Service.s_door",
            "sink": ["py:shop.Gateway.s_charge", "py:shop.Gateway.c_charge", "py:shop.SubGateway.arefund"]}
    before = {k: shop.Gateway.__dict__[k] for k in ("refund", "s_charge", "c_charge")}
    svc_before = dict(shop.Service.__dict__)
    door_fn, sub = shop.door_fn, dict(shop.SubGateway.__dict__)
    h = inv.doors.install(_load(GW, rule), mode="enforce")
    assert shop.Gateway.__dict__["refund"] is not before["refund"]
    assert shop.door_fn is not door_fn and shop_user.door_fn is shop.door_fn      # rebound where imported
    assert "arefund" in shop.SubGateway.__dict__                                  # inherited: put on the subclass
    h.uninstall()
    assert {k: shop.Gateway.__dict__[k] for k in before} == before
    assert all(shop.Gateway.__dict__[k] is v for k, v in before.items())
    assert dict(shop.Service.__dict__) == svc_before
    assert shop.door_fn is door_fn and shop_user.door_fn is door_fn
    assert dict(shop.SubGateway.__dict__) == sub
    assert not h.installed
    h.uninstall()                                                   # twice: nothing more


def test_a_door_imported_by_name_elsewhere_counts(shop):
    import shop_user
    with inv.using(_load(GW)), inv.doors.install(mode="enforce") as h:
        assert shop_user.call(4) == ("refunded", 4)
    assert h.violations == []


def test_install_twice_is_idempotent(shop):
    reg = _load(GW)
    orig = shop.Gateway.__dict__["refund"]
    h1 = inv.doors.install(reg, mode="detect")
    placed = shop.Gateway.__dict__["refund"]
    h2 = inv.doors.install(reg, mode="detect")
    assert h1 is h2 and shop.Gateway.__dict__["refund"] is placed
    with inv.using(reg):
        shop.Gateway().refund(1)
    assert len(h1.violations) == 1 and reg.counts("GW-DOOR")["violation"] == 1
    h2.uninstall()
    assert shop.Gateway.__dict__["refund"] is placed                # still installed once
    h1.uninstall()
    assert shop.Gateway.__dict__["refund"] is orig


def test_other_rules_stack_and_uninstall_in_any_order(shop):
    one = _load({"id": "ONE", "door": "py:shop.Service.refund", "sink": "py:shop.Gateway.refund"})
    two = _load({"id": "TWO", "door": "py:shop.Service.audit", "sink": "py:shop.Gateway.refund"})
    orig = shop.Gateway.__dict__["refund"]
    h1 = inv.doors.install(one, mode="detect", registry=one)
    h2 = inv.doors.install(two, mode="detect", registry=two)
    shop.Gateway().refund(1)
    assert [v["rule"] for v in h1.violations] == ["ONE"] and [v["rule"] for v in h2.violations] == ["TWO"]
    h1.uninstall()                                                  # the older first: it passes through now
    shop.Gateway().refund(1)
    assert len(h1.violations) == 1 and len(h2.violations) == 2
    h2.uninstall()
    assert shop.Gateway.__dict__["refund"] is orig


def test_staticmethod_and_classmethod_sinks(shop):
    rule = {"id": "S", "door": "py:shop.Service.s_door", "sink": ["py:shop.Gateway.s_charge", "py:shop.Gateway.c_charge"]}
    with inv.using(_load(rule)), inv.doors.install(mode="enforce"):
        assert isinstance(shop.Gateway.__dict__["s_charge"], staticmethod)
        assert isinstance(shop.Gateway.__dict__["c_charge"], classmethod)
        assert shop.Service().s_door(3) == (3, ("Gateway", 3))
        with pytest.raises(inv.BypassError):
            shop.Gateway.s_charge(3)
        with pytest.raises(inv.BypassError):
            shop.Gateway().s_charge(3)
        with pytest.raises(inv.BypassError):
            shop.SubGateway.c_charge(3)


def test_outermost_over_a_guard_which_still_runs(shop):
    reg = _load(GW)
    guard = shop.Service.__dict__["guarded_refund"]
    with inv.using(reg), inv.doors.install(mode="enforce"):
        placed = shop.Service.__dict__["guarded_refund"]
        assert placed is not guard and placed.__wrapped__ is guard
        assert shop.Service().guarded_refund(5) == ("refunded", 5)
        with pytest.raises(inv.Rejected):
            shop.Service().guarded_refund(500)
        from invariants.registry.coverage import _target_problems
        assert _target_problems(reg.rules["CAP"]) == []
        reloaded = _load(GW)                                         # loaded while installed: the target is ours
        assert _target_problems(reloaded.rules["CAP"]) == []


def test_a_generator_door_counts_only_while_it_runs(shop):
    with inv.using(_load(GW)), inv.doors.install(mode="enforce"):
        g = shop.Service().gen_door(3)
        assert next(g) == ("refunded", 0)
        assert inv.doors.active() == frozenset()                    # between steps: not running
        with pytest.raises(inv.BypassError):
            shop.Gateway().refund(1)
        assert list(g) == [("refunded", 1), ("refunded", 2)]


def test_an_async_generator_door_counts_while_each_step_is_awaited(shop):
    async def main() -> Any:
        got = []
        async for x in shop.Service().agen_door(2):
            got.append(x)
            assert inv.doors.active() == frozenset()
        return got
    with inv.using(_load(GW)), inv.doors.install(mode="enforce") as h:
        assert asyncio.run(main()) == [("refunded", 0), ("refunded", 1)]
    assert h.violations == []


def test_rules_none_reads_the_current_registry(shop):
    with inv.using(_load(GW)), inv.doors.install(mode="enforce") as h:
        assert h.rules == ["GW-DOOR"]


def test_an_unresolved_path_is_a_load_error_and_nothing_stays_wrapped(shop):
    orig = shop.Gateway.__dict__["refund"]
    reg = _load(GW, {"id": "BAD", "door": "py:shop.Service.refund", "sink": "py:shop.Gateway.nope"})
    with pytest.raises(inv.LoadError, match="nope"):
        inv.doors.install(reg, mode="enforce")
    assert shop.Gateway.__dict__["refund"] is orig and doors_runtime.installed() == []


def test_a_failed_wrap_puts_back_what_it_already_wrapped(shop):
    orig = shop.Gateway.__dict__["refund"]
    reg = _load(GW, {"id": "SLOT", "door": "py:shop.Service.refund", "sink": "py:shop.SLOTTED.write"})
    with pytest.raises(inv.LoadError, match="cannot wrap write"):
        inv.doors.install(reg, mode="enforce")
    assert shop.Gateway.__dict__["refund"] is orig and doors_runtime.installed() == []


def test_a_bad_mode_is_refused(shop):
    with pytest.raises(ValueError, match="mode"):
        inv.doors.install(_load(GW), mode="block")  # type: ignore[arg-type]


# --- pytest --inv-doors ------------------------------------------------------------------------------------
PLUGIN_TESTS = textwrap.dedent('''\
    import pytest, invariants as inv, shop

    def test_through_the_door():
        assert shop.Service().refund(5) == ("refunded", 5)

    def test_swallowed():
        try:
            shop.tool_getattr(shop.Gateway(), 5)
        except inv.BypassError:
            pass
    ''')


def _plugin_project(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(shop=SHOP)
    (pytester.path / "invariants.json").write_text(json.dumps(_doc(GW)))
    pytester.makeini("[pytest]\ninv_rules = invariants.json\n")
    pytester.makepyfile(test_app=PLUGIN_TESTS)


def test_inv_doors_fails_the_session_on_a_swallowed_violation(pytester):
    _plugin_project(pytester)
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--inv-doors", "--inv-report-json", "rep.json")
    assert r.ret == 1, r.stdout.str()
    r.stdout.fnmatch_lines(["*invariants doors*1 violation*",
                            "*GW-DOOR*py:shop.Gateway.refund*test_app.py::test_swallowed*", "*2 passed*"])
    data = json.loads((pytester.path / "rep.json").read_text())
    assert data["doors"] == {"installed": ["GW-DOOR"], "violations": [
        {"rule": "GW-DOOR", "sink": "py:shop.Gateway.refund", "test": "test_app.py::test_swallowed", "count": 1}],
        "expected": []}
    assert data["rules"]["GW-DOOR"]["violation"] == 1


def test_without_inv_doors_nothing_is_checked(pytester):
    _plugin_project(pytester)
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--inv-report-json", "rep.json")
    assert r.ret == 0, r.stdout.str()
    assert "doors" not in json.loads((pytester.path / "rep.json").read_text())


def test_inv_doors_finds_the_rule_file_in_the_rootdir(pytester):
    _plugin_project(pytester)
    (pytester.path / "tox.ini").unlink(missing_ok=True)
    pytester.makeini("[pytest]\n")
    (pytester.path / "invariants.json").rename(pytester.path / "invariants.yaml")   # JSON is YAML
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--inv-doors")
    assert r.ret == 1, r.stdout.str()
    r.stdout.fnmatch_lines(["*invariants doors*1 violation*"])


def test_inv_doors_under_xdist_merges_the_workers(pytester):
    pytest.importorskip("xdist")
    _plugin_project(pytester)
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-n", "2", "--inv-doors", "--inv-report-json", "rep.json")
    assert r.ret == 1, r.stdout.str()
    data = json.loads((pytester.path / "rep.json").read_text())
    assert data["doors"]["installed"] == ["GW-DOOR"]
    assert data["doors"]["violations"] == [
        {"rule": "GW-DOOR", "sink": "py:shop.Gateway.refund", "test": "test_app.py::test_swallowed", "count": 1}]


def test_inv_doors_with_a_sink_that_does_not_resolve_is_a_usage_error(pytester):
    _plugin_project(pytester)
    doc = _doc({**GW, "sink": "py:shop.Gateway.nope"})
    (pytester.path / "invariants.json").write_text(json.dumps(doc))
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--inv-doors")
    assert r.ret == 4, r.stdout.str() + r.stderr.str()
    assert "--inv-doors" in r.stderr.str() and "nope" in r.stderr.str()


# --- end to end: apps/support-agent ------------------------------------------------------------------------
APP = pathlib.Path(__file__).resolve().parents[2] / "apps" / "support-agent"
GETATTR_TOOL = textwrap.dedent('''\
    """A tool that reaches the payment gateway through getattr, skipping BillingService: the scan cannot
    follow the name, so it is the run-time check's to catch."""
    from support_agent import context


    def sneaky_refund_tool(ctx, order_id, amount_cents):
        gateway = getattr(ctx.billing, "str" + "ipe")
        return getattr(gateway, "ref" + "und")(order_id, amount_cents)


    def test_sneaky_tool_refunds(ctx):
        try:
            sneaky_refund_tool(ctx, "o-1", 100)
        except Exception:
            pass                                   # whatever the tool's caller does with the error
    ''')


@pytest.mark.skipif(not (APP / "invariants.yaml").is_file(), reason="apps/support-agent is not in this checkout")
def test_support_agent_getattr_tool_fails_under_inv_doors(tmp_path):
    pytest.importorskip("pydantic")
    import subprocess
    app = tmp_path / "support-agent"
    shutil.copytree(APP, app, ignore=shutil.ignore_patterns(".venv", "build", "__pycache__", "*.egg-info"))
    (app / "tests" / "test_sneaky.py").write_text(GETATTR_TOOL)
    env = {**__import__("os").environ, "PYTHONPATH": str(app / "src")}
    env.pop("INV_ABLATE", None)
    base = [sys.executable, "-m", "pytest", "-p", "no:randomly", "tests/test_sneaky.py", "tests/test_flows.py"]
    clean = subprocess.run(base, cwd=app, env=env, capture_output=True, text=True)
    assert clean.returncode == 0, clean.stdout + clean.stderr                # the scan-blind tool passes
    got = subprocess.run([*base, "--inv-doors", "--inv-report-json", "rep.json"], cwd=app, env=env,
                         capture_output=True, text=True)
    assert got.returncode == 1, got.stdout + got.stderr
    data = json.loads((app / "rep.json").read_text())
    assert data["doors"]["violations"] == [
        {"rule": "STRIPE-GATEWAY-DOOR", "sink": "py:support_agent.repo.StripeGateway.refund",
         "test": "tests/test_sneaky.py::test_sneaky_tool_refunds", "count": 1}]
    assert "STRIPE-GATEWAY-DOOR" in data["doors"]["installed"]


# --- inv_bypass: a test that skips a door on purpose -------------------------------------------------------
MARKED = textwrap.dedent('''
    import pytest, shop

    @pytest.mark.inv_bypass("GW-DOOR", reason="runs the planted back door")
    def test_planted_back_door():
        assert shop.tool_getattr(shop.Gateway(), 5) == ("refunded", 5)
    ''')


def test_an_inv_bypass_test_runs_its_bypass_and_passes_the_session(pytester):
    _plugin_project(pytester)
    pytester.makepyfile(test_app=MARKED)
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--inv-doors", "--inv-report-json", "rep.json")
    assert r.ret == 0, r.stdout.str()
    r.stdout.fnmatch_lines(["*invariants doors*0 violation(s), 1 expected (inv_bypass)*", "*1 passed*"])
    data = json.loads((pytester.path / "rep.json").read_text())
    assert data["doors"]["violations"] == []
    assert data["doors"]["expected"] == [{"rule": "GW-DOOR", "sink": "py:shop.Gateway.refund",
                                          "test": "test_app.py::test_planted_back_door", "count": 1}]
    assert data["rules"]["GW-DOOR"]["violation"] == 0


def test_inv_bypass_excuses_only_the_test_it_marks(pytester):
    _plugin_project(pytester)
    pytester.makepyfile(test_app=MARKED + textwrap.dedent('''
        def test_unmarked():
            try:
                shop.tool_getattr(shop.Gateway(), 5)
            except Exception:
                pass
        '''))
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--inv-doors")
    assert r.ret == 1, r.stdout.str()
    r.stdout.fnmatch_lines(["*1 violation(s), 1 expected*", "*GW-DOOR*test_app.py::test_unmarked*"])


@pytest.mark.parametrize("marker, why", [('@pytest.mark.inv_bypass("NOPE")', "names NOPE"),
                                         ("@pytest.mark.inv_bypass()", "needs one or more rule ids")])
def test_an_inv_bypass_marker_must_name_a_checked_rule(pytester, marker, why):
    _plugin_project(pytester)
    pytester.makepyfile(test_app=f"import pytest\n\n{marker}\ndef test_x():\n    pass\n")
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--inv-doors")
    assert r.ret == 4, r.stdout.str()
    assert why in r.stderr.str(), r.stderr.str()


def test_without_inv_doors_the_marker_is_inert(pytester):
    _plugin_project(pytester)
    pytester.makepyfile(test_app=f'import pytest\n\n@pytest.mark.inv_bypass("NOPE")\ndef test_x():\n    pass\n')
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--strict-markers")
    assert r.ret == 0, r.stdout.str()


def test_expected_blocks_nest_and_close():
    with doors_runtime.expected("A", "B"):
        with doors_runtime.expected("A"):
            assert doors_runtime._EXPECTED == {"A": 2, "B": 1}
        assert doors_runtime._EXPECTED == {"A": 1, "B": 1}
    assert doors_runtime._EXPECTED == {}
    with pytest.raises(TypeError):
        with doors_runtime.expected():
            pass
