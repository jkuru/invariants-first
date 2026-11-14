"""Regression tests for fix round 5 (certification round 3): each test failed before its fix."""
from __future__ import annotations

import asyncio
import contextvars
import os
import pathlib
import subprocess
import sys
import threading
from typing import Any

import pytest

import invariants as inv
import round4 as r4
import round5 as r5
from invariants import registry
from invariants_cel import get_backend

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture
def iso():
    with inv.isolated() as r:
        r5.HOOK.clear()
        yield r
        r5.HOOK.clear()


# --- major: non-CEL exceptions never escape the async prefetch or snapshot-provider paths -------------
@pytest.mark.parametrize("call", [
    lambda: r5.door(r5.Evil()),                         # sync pre (was already a rule_error)
    lambda: asyncio.run(r5.adoor(r5.Evil())),           # async pre: escaped RuntimeError
    lambda: r5.sdoor(r5.Evil()),                        # sync post, snapshot provider: escaped
    lambda: asyncio.run(r5.asdoor(r5.Evil())),          # async post, snapshot provider: escaped
], ids=["sync-pre", "async-pre", "sync-snapshot", "async-snapshot"])
def test_hostile_mapping_argument_is_a_rule_error(iso, call):
    inv.load(r5.hostile_doc())
    with pytest.raises(inv.Rejected, match="rule_error") as ei:
        call()
    assert "RuntimeError" in str(ei.value)


def test_hostile_mapping_in_a_plain_path_is_a_cel_error():
    from invariants.providers import resolve_path
    with pytest.raises(inv.cel.CelError, match="raised RuntimeError"):
        resolve_path({"args": {"d": r5.Evil()}}, "args.d.k")


def _outcomes(reg: inv.Registry, rule_id: str) -> list[str]:
    return [e["outcome"] for e in reg.sink if e.get("rule") == rule_id]


def test_reload_during_an_async_call_keeps_the_call_on_its_generation(iso):
    inv.load(r5.reload_doc("lim"))
    r5.HOOK.append(lambda: inv.load(r5.reload_doc("lim2")))
    assert asyncio.run(r5.areload(3)) == 3          # was: KeyError 'lim' escaped after the effect
    assert _outcomes(iso, "S-2") == ["ok"]


def test_reload_during_a_sync_call_keeps_the_call_on_its_generation(iso):
    inv.load(r5.reload_doc("lim"))
    r5.HOOK.append(lambda: inv.load(r5.reload_doc("lim2")))
    assert r5.reload(3) == 3
    assert _outcomes(iso, "S-1") == ["ok"]          # was: rule_error (binding raised KeyError), paged


def test_reload_from_another_thread_mid_call(iso):
    inv.load(r5.reload_doc("lim"))
    entered, go = threading.Event(), threading.Event()

    def hold() -> None:
        entered.set()
        assert go.wait(10)
    r5.HOOK.append(hold)
    out: list[Any] = []
    ctx = contextvars.copy_context()           # the thread guards against the isolated registry
    t = threading.Thread(target=lambda: ctx.run(lambda: out.append(r5.reload(3))))
    t.start()
    assert entered.wait(10)
    inv.load(r5.reload_doc("lim2"))
    go.set()
    t.join(10)
    assert out == [3]
    assert _outcomes(iso, "S-1") == ["ok"]


# --- minor: has() of an unknown field, and `in` across families, are load errors ---------------------
@pytest.mark.parametrize("bad", ["has(rec.sizee)", 'args.n in ["a"]', "args.n in [true]",
                                 'rec.name in [1, 2]', "has(maybe_rec.sizee)"])
def test_has_and_in_are_type_checked_at_load(bad):
    with inv.isolated(), pytest.raises(inv.LoadError, match="ill-typed"):
        inv.load(r4.doc(r4.rule("G-1", "door", bad)))


@pytest.mark.parametrize("bad", ["has(rec.size) && rec.size > 100", "has(bag.anything)", "args.n in [1, 2]",
                                 'rec.name in ["a", "b"]', "args.anything in [1]", 'rec.name in bag'])
def test_well_typed_has_and_in_still_load(bad):
    with inv.isolated():
        inv.load(r4.doc(r4.rule("G-1", "door", bad)))


# --- minor: a constant bad pattern in matches() is a load error --------------------------------------
@pytest.mark.parametrize("pat", ["(", "a{2,1}", "[z-a]", "(?=x)"])
def test_constant_invalid_regex_is_a_load_error(pat):
    with inv.isolated(), pytest.raises(inv.LoadError, match="bad pattern"):
        inv.load(r4.doc(r4.rule("G-1", "door", f'rec.name.matches("{pat}")')))


def test_valid_constant_regex_loads_and_runs():
    with inv.isolated():
        inv.load(r4.doc(r4.rule("G-1", "door", 'rec.name.matches("^r$")')))
        with pytest.raises(inv.Rejected, match="violation"):
            r4.door(5)


# --- minor: armed env ablation is loud in a non-test process that imports pytest ----------------------
def test_ablation_in_a_process_that_imports_pytest_is_still_loud():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST", "INV_"))}
    env.update({"INV_ABLATE": "G-1", "INV_ABLATE_TESTING": "1",
                "PYTHONPATH": os.pathsep.join([str(FIXTURES), str(pathlib.Path(inv.__file__).parent.parent)])})
    code = ("import pytest, invariants as inv, round4 as r4\n"
            "inv.load(r4.doc(r4.rule('G-1', 'door', 'args.n < 0')))\n"
            "print('passed', r4.door(-1))\n")
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "passed -1" in r.stdout
    assert "OFF in this process" in r.stderr


def test_a_configured_pytest_session_counts_as_under_pytest():
    assert registry.under_pytest() is True


# --- minor (documented, not changed): subset matches() on a long input is a bounded rule_error -------
LONG = "abcde" * 20000            # 100,000 characters


def test_long_input_matches_on_the_full_re2_backends_gives_a_result():
    if type(get_backend()).__name__ == "SubsetBackend":
        pytest.skip("the subset's bound is pinned below")
    assert get_backend().compile('s.matches("(a|b|c|d|e)*f")').evaluate({"s": LONG}) is False


def test_long_input_matches_on_the_subset_is_a_bounded_cost_error():
    import time
    from invariants_cel import subset
    prog = subset.SubsetBackend().compile('s.matches("(a|b|c|d|e)*f")')
    start = time.perf_counter()
    with pytest.raises(inv.cel.CelError, match="cost limit exceeded"):
        prog.evaluate({"s": LONG})
    assert time.perf_counter() - start < 1.0


def test_readme_documents_the_subset_regex_bound():
    text = "\n".join(p.read_text(encoding="utf-8") for p in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))])
    assert "matches() step bound" in text
