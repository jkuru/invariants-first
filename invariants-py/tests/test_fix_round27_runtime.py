"""Regression tests for fix round 27 (runtime): each test failed before its fix.

1. A model call's abstain at effective enforce records its event (outcome abstain, action escalate)
   through Registry.record, so observers (the --inv-report column), exporters (OTel) and the
   decision trace (--inv-ablate) all see it, sync and async; is_decision counts it.
2. An enforcing @inv.edge given a Checked with verdict abstain refuses it as outcome abstain (by
   on_violation), not as a rule_error: HTTP 422 urn:invariants:abstained for reject (202 for an
   escalate edge), never the rule-error 503, and no rule_error toward an error spike.
3. BufferedExporter: a start() racing the flusher's exit (after it decided to stop, before it
   cleared itself) no longer strands the events queued next with no flusher.
4. INV_ABLATE (armed by INV_ABLATE_TESTING=1) is read when a guard asks, not when the registry was
   built: the default registry, built at import, honours it as the loader's log says.
5. inv.parse refuses a NaN or Infinity under Any inside a tuple, set, frozenset or a list or dict
   subclass (and as a dict key), as it does inside a plain list or dict.
6. `inv hook stop` finds the rule file in the event's cwd or above it; a lock with no rule file
   sends the agent back; its relock re-pins a tightening and never a weakening.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

import invariants as inv
import round26rt as r26
from invariants.integrations import asgi
from invariants.integrations.exporters import BufferedExporter
from invariants.registry import is_decision, observe
from invariants.testing import make_checked

from test_adoption import _edit, _hook, _project
from cli_support import run_inv, write_project

ABSTAIN = json.dumps({"action": "ESCALATE_TO_HUMAN", "amount": 0})


def _run(fn: Any) -> Any:
    r = fn("o1")
    return asyncio.run(r) if asyncio.iscoroutine(r) else r


# --- 1: an enforced abstain is a recorded decision -------------------------------------------------
@pytest.mark.parametrize("fn", ["plan", "aplan"])
def test_enforced_abstain_records_an_escalate_event(fn, tmp_path, monkeypatch):
    trace = tmp_path / "decided"
    monkeypatch.setenv("INV_DECISION_TRACE", str(trace))
    seen: list[tuple[str, str, Any]] = []
    exported: list[dict[str, Any]] = []
    unobserve = observe(lambda rid, kind, ev: seen.append((rid, kind, ev.get("action"))))
    try:
        with inv.isolated() as reg:
            inv.configure(reg, exporter=lambda rid, kind, ev: exported.append(ev))
            inv.load(r26.abstain_doc("enforce"))
            r26.REPLIES[:] = [ABSTAIN]
            with pytest.raises(inv.Escalation, match="opened") as ei:
                _run(getattr(r26, fn))
            assert ei.value.outcome is not None and ei.value.outcome.kind == "abstain"
            evs = [e for e in reg.sink if e["rule"] == "M-1"]
            assert [(e["outcome"], e["action"], e["mode"]) for e in evs] == [("abstain", "escalate", "enforce")]
            assert evs[0]["attempts"] == 1 and evs[0]["start_ns"] <= evs[0]["end_ns"]
            assert reg.counts("M-1")["abstain"] == 1 and reg.evidence["M-1"]["runs"] == 1
            review = reg.review_store.items[0]
            assert review["rule"] == "M-1" and review["id"] == ei.value.review_id
    finally:
        unobserve()
    assert ("M-1", "abstain", "escalate") in seen                 # the --inv-report counter
    assert [e["outcome"] for e in exported] == ["abstain"]        # an exporter (OTel) sees it too
    assert trace.read_text().split() == ["M-1"]                   # --inv-ablate's decision ledger


def test_is_decision_counts_an_escalated_abstain_only():
    assert is_decision("abstain", {"action": "escalate"})
    assert not is_decision("abstain", {"action": "proceed"})     # below enforce: nothing was decided
    assert is_decision("violation", {"action": "proceed"}) and not is_decision("ok", {"action": "proceed"})


# --- 2: an edge refusing an abstain verdict -----------------------------------------------------------
def _abstained() -> Any:
    return make_checked(r26.CheckedReply, r26.Reply(action="ESCALATE_TO_HUMAN", amount=0), verdict="abstain",
                        rule="M-1")


def test_enforcing_edge_refuses_an_abstain_as_abstain_not_rule_error():
    with inv.isolated() as reg:
        inv.load(r26.abstain_doc("enforce"))
        with pytest.raises(inv.Rejected) as ei:
            r26.execute(_abstained(), "o1")
        assert ei.value.outcome.kind == "abstain"
        assert [(e["outcome"], e["action"]) for e in reg.sink] == [("abstain", "reject")]
        assert reg.counts("M-1").get("rule_error", 0) == 0
        status, body = asgi.problem(ei.value)
        assert status == 422 and body["type"] == "urn:invariants:abstained" and body["outcome"] == "abstain"


def test_escalating_edge_answers_an_abstain_202():
    doc = r26.abstain_doc("enforce")
    doc["rules"][0]["enforcers"][1]["on_violation"] = "escalate"
    with inv.isolated():
        inv.load(doc)
        with pytest.raises(inv.Escalation) as ei:
            r26.execute(_abstained(), "o1")
        status, body = asgi.problem(ei.value)
        assert status == 202 and body["outcome"] == "abstain" and body["review_id"]


def test_detect_edge_records_an_abstain_and_proceeds():
    with inv.isolated() as reg:
        inv.load(r26.abstain_doc("enforce", edge_mode="detect"))
        assert r26.execute(_abstained(), "o1") == 0
        assert [(e["outcome"], e["action"]) for e in reg.sink] == [("abstain", "proceed")]


# --- 3: BufferedExporter start() racing the flusher's exit ---------------------------------------
class _HookLock:
    """A lock that runs on_release after each release (the Condition built on it uses it too)."""

    def __init__(self) -> None:
        self._l = threading.Lock()
        self.on_release: Any = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        return self._l.acquire(blocking, timeout)

    def release(self) -> None:
        self._l.release()
        if self.on_release is not None:
            self.on_release()

    def locked(self) -> bool:
        return self._l.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()


def test_start_between_the_flushers_stop_and_its_exit_keeps_a_flusher():
    got: list[Any] = []
    exp = BufferedExporter(lambda rid, kind, ev: got.append(ev["i"]))
    lock = _HookLock()
    exp._lock, exp._changed = lock, threading.Condition(lock)  # type: ignore[assignment]
    exiting, resume = threading.Event(), threading.Event()

    def on_release() -> None:
        # the flusher let go of the lock having decided to stop, while it still names itself the flusher
        if (threading.current_thread() is exp._thread and exp._stopping and not exp._queue
                and not exiting.is_set()):
            exiting.set()
            resume.wait(5)
    lock.on_release = on_release
    exp.start()
    flusher = exp._thread
    assert flusher is not None
    exp.close(timeout=0)              # ask it to stop, without waiting
    exiting.wait(1)                   # (before the fix: the flusher is paused in the window here)
    exp.start()                       # a host restarts it
    exp("R-1", "ok", {"i": 1})
    resume.set()
    flusher.join(5)
    deadline = time.monotonic() + 5
    while not got and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert exp.running, "start() returned with no flusher running"
        assert got == [1], "the event queued after start() was stranded"
    finally:
        lock.on_release = None
        assert exp.close(5)


# --- 4: INV_ABLATE read when asked ----------------------------------------------------------------
def test_inv_ablate_set_after_the_registry_was_built_is_honoured(monkeypatch):
    with inv.isolated() as reg:       # built before the environment names the rule, as the default one
        monkeypatch.setenv("INV_ABLATE", "ROLE-1")
        monkeypatch.setenv("INV_ABLATE_TESTING", "1")
        inv.load(r26.role_doc())
        assert inv.is_ablated("ROLE-1") and reg.ablated == {"ROLE-1"}
        assert r26.delete_account("u", "guest") == "deleted"     # the loader said its guards are off: they are
        monkeypatch.delenv("INV_ABLATE")
        assert not inv.is_ablated("ROLE-1")
        with pytest.raises(inv.Rejected):
            r26.delete_account("u", "guest")


def test_inv_ablate_unarmed_is_still_ignored(monkeypatch):
    with inv.isolated():
        monkeypatch.setenv("INV_ABLATE", "ROLE-1")
        monkeypatch.delenv("INV_ABLATE_TESTING", raising=False)
        inv.load(r26.role_doc())
        assert not inv.is_ablated("ROLE-1")
        with pytest.raises(inv.Rejected):
            r26.delete_account("u", "guest")


# --- 5: non-finite numbers in any container under Any ----------------------------------------------
class _L(list): ...
class _D(dict): ...


NAN, INF = float("nan"), float("inf")


@pytest.mark.parametrize("v", [(NAN,), {NAN}, frozenset([INF]), _L([NAN]), _D(a=INF), [(1.0, NAN)],
                               {"a": (NAN,)}, collections.OrderedDict(a=NAN), {NAN: 1}, ((((-INF,),),),)],
                         ids=["tuple", "set", "frozenset", "list-subclass", "dict-subclass", "tuple-in-list",
                              "tuple-in-dict", "ordereddict", "key", "nested-tuple"])
def test_parse_refuses_non_finite_inside_any_container(v):
    with pytest.raises(inv.ParseError, match="non-finite"):
        inv.parse({"x": v}, {"x": Any})


def test_parse_keeps_finite_containers_and_strings_under_any():
    got = inv.parse({"x": [(1.0, "nan"), {2.5}, frozenset({b"inf"}), {"k": (3,)}]}, {"x": Any})
    assert got["x"] == [(1.0, "nan"), {2.5}, frozenset({b"inf"}), {"k": (3,)}]


# --- 6: the stop hook ---------------------------------------------------------------------------
def test_hook_stop_relock_repins_a_tightening_and_never_a_weakening(tmp_path):
    root = _project(tmp_path, "r27_stop")
    assert run_inv("init", cwd=root).returncode == 0
    lock_file = root / "invariants.lock"
    models = root / "src/r27_stop/models.py"
    _edit(models, "le=10_000", "le=5_000")                         # tighter: the hook re-pins it
    before = lock_file.read_bytes()
    assert _hook("stop", {"cwd": str(root)}, root).returncode == 0
    tightened = lock_file.read_bytes()
    assert tightened != before
    _edit(models, "le=5_000", "le=100_000")                        # looser: sent back, the lock untouched
    r = _hook("stop", {"cwd": str(root)}, root)
    assert r.returncode == 2 and "maximum raised" in r.stderr
    assert lock_file.read_bytes() == tightened


def test_hook_stop_finds_the_rule_file_above_the_cwd(tmp_path):
    root = _project(tmp_path, "r27_up")
    assert run_inv("init", cwd=root).returncode == 0
    _edit(root / "src/r27_up/models.py", "le=10_000", "le=100_000")
    sub = root / "src/r27_up"
    r = _hook("stop", {"cwd": str(sub)}, sub)
    assert r.returncode == 2 and "maximum raised" in r.stderr, r.stderr


def test_hook_stop_refuses_a_lock_without_its_rule_file(tmp_path):
    root = _project(tmp_path, "r27_gone")
    assert run_inv("init", cwd=root).returncode == 0
    (root / "invariants.yaml").unlink()
    r = _hook("stop", {"cwd": str(root)}, root)
    assert r.returncode == 2 and "no rule file" in r.stderr and "invariants.lock" in r.stderr
    assert _hook("stop", {"cwd": str(root), "stop_hook_active": True}, root).returncode == 0
    (root / ".git").mkdir()                                       # the repository root bounds the search
    nowhere = root / "empty"
    nowhere.mkdir()
    (root / "invariants.lock").unlink()
    assert _hook("stop", {"cwd": str(nowhere)}, nowhere).returncode == 0
    assert os.listdir(nowhere) == []


# --- 7: `python -m MOD` reads MOD itself with the script rule ----------------------------------------
EDITS_PYPROJECT = "import pathlib\npathlib.Path('pyproject.toml').write_text('')\n"   # names no 'invariants'


def _pretool(root: Any, command: str) -> Any:
    payload = {"tool_name": "Bash", "cwd": str(root), "tool_input": {"command": command}}
    return subprocess.run([sys.executable, "-m", "invariants.cli", "hook", "pretool"], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=root, timeout=60)


@pytest.mark.parametrize("files,command", [
    ({"tools/__init__.py": "", "tools/edit.py": EDITS_PYPROJECT}, "python -m tools.edit"),
    ({"tools/__init__.py": "", "tools/edit/__init__.py": "", "tools/edit/__main__.py": EDITS_PYPROJECT},
     "python -m tools.edit"),
    ({"src/pkg/__init__.py": "", "src/pkg/edit.py": EDITS_PYPROJECT}, "python -m pkg.edit"),
    ({"src/pkg/__init__.py": "", "src/pkg/edit/__init__.py": "", "src/pkg/edit/__main__.py": EDITS_PYPROJECT},
     "python3 -m pkg.edit"),
], ids=["module", "package-main", "src-module", "src-package-main"])
def test_hook_blocks_python_m_of_a_module_that_writes_pyproject(tmp_path, files, command):
    """As `python tools/edit.py` is blocked (a script's own text: pyproject alone counts), so is
    `python -m` of it, wherever the module lives (under cwd or in src/)."""
    root = write_project(tmp_path / "p", {"invariants.lock": "{}\n", "pyproject.toml": "[project]\n", **files})
    r = _pretool(root, command)
    assert r.returncode == 2 and "pyproject.toml" in r.stderr, (command, r.stderr)
    write_project(root, {k: ("print(1)\n" if v == EDITS_PYPROJECT else v) for k, v in files.items()})
    r = _pretool(root, command)
    assert r.returncode == 0, (command, r.stderr)
