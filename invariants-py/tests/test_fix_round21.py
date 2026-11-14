"""Fix round 21: propose_rules judges settings from inside --root (1); the FastAPI verifier registers
under the registry's lock and reads one loaded generation (2); BufferedExporter's drain keeps its
timeout without a flusher and waits for another caller's export, and close reports that drain (3);
the event sink's inherited repr/copy/== walk the locked snapshot (4, a pin: they did already); inv
verify imports schema-class contract modules, and validate reports a malformed document (5); an edge
with several Checked parameters needs at= (6)."""
from __future__ import annotations

import collections
import copy
import json
import pathlib
import sys
import threading
import time
from typing import Any

import pytest

import invariants as inv
from cli_support import run_inv, write_project
from invariants import mcp
from invariants import project
from invariants.cli import commands
from invariants.integrations.exporters import BufferedExporter
from invariants.registry import Registry


def wait_until(cond: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


class Spy:
    """Stands for a registry lock: counts how often it was taken."""

    def __init__(self, lock: Any) -> None:
        self.lock, self.entered = lock, 0

    def __enter__(self) -> Any:
        self.entered += 1
        return self.lock.__enter__()

    def __exit__(self, *exc: Any) -> Any:
        return self.lock.__exit__(*exc)


def _call(s: mcp.Server, name: str, **args: Any) -> dict[str, Any]:
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    assert r is not None
    return r["result"]  # type: ignore[no-any-return]


# --- 1. propose_rules: the rule file it would write resolves inside --root -----------------------------
def test_propose_rules_refuses_a_rule_file_symlinked_out_of_the_root(tmp_path):   # failed before
    server_root, outside = tmp_path / "project", tmp_path / "elsewhere"
    (outside / "secret").mkdir(parents=True)
    (outside / "secret" / "s.py").write_text("def door():\n    pass\n")
    (outside / "invariants.yaml").write_text("rules: []\n")
    server_root.mkdir()
    # settings that name only directories inside --root, so no later check trips on them: before the fix
    # they were read from outside --root and applied (the scan roots and excludes of another project)
    (outside / "pyproject.toml").write_text(f'[tool.invariants.scan]\nroots = [{json.dumps(str(server_root))}]\n'
                                            'exclude = ["*.py"]\n')
    (server_root / "app.py").write_text("def d():\n    pass\n")
    (server_root / "invariants.yaml").symlink_to(outside / "invariants.yaml")
    res = _call(mcp.Server(server_root), "propose_rules")
    assert res["isError"] is True, res
    assert "rule file 'invariants.yaml' resolves to" in res["content"][0]["text"], res


def test_propose_rules_still_works_without_a_rule_file(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("def d():\n    pass\n")
    res = _call(mcp.Server(root), "propose_rules")
    assert res["isError"] is False, res


# --- 2. FastAPI: the verifier is added under the lock and reads one generation ---------------------------
def test_register_adds_its_verifier_under_the_registry_lock():   # failed before
    from fastapi import FastAPI
    from invariants.integrations import fastapi as inv_fastapi
    reg, app = Registry(), FastAPI()
    spy = Spy(reg._lock)
    reg._lock = spy                              # type: ignore[assignment]
    inv_fastapi.register(app, reg)
    inv_fastapi.register(app, reg)               # the same app once
    assert spy.entered == 2 and len(reg.verifiers) == 1
    inv_fastapi.register(FastAPI(), reg)
    assert len(reg.verifiers) == 2


def test_the_route_check_reads_rules_and_contracts_of_one_generation():   # failed before
    from fastapi import FastAPI
    from invariants.integrations import fastapi as inv_fastapi
    reg, app = Registry(), FastAPI()
    inv_fastapi.register(app, reg)
    seen = []

    class Swapping(Registry):
        """A reload lands between reading rules and contracts: the attributes now hold the new file."""

        def _view(self) -> Any:
            v = super()._view()
            seen.append(v)
            self.contracts = {"c": {"type": "openapi", "operation": "nope"}}
            return v

    swap = Swapping()
    swap.rules = {}
    reg.verifiers[0](swap)
    assert len(seen) == 1                       # read through one view, not the attributes


def test_add_verifier_dedupes_by_key_only():
    reg = Registry()
    assert reg.add_verifier(lambda r: None, key="k") is True
    assert reg.add_verifier(lambda r: None, key="k") is False
    assert reg.add_verifier(lambda r: None) is True and reg.add_verifier(lambda r: None) is True
    assert len(reg.verifiers) == 3


# --- 3. BufferedExporter: drain's timeout, waiting for another export, close's result ------------------
def test_a_synchronous_drain_stops_at_its_deadline_with_the_rest_queued():   # failed before
    fast = threading.Event()

    def slow(rule_id: str, kind: str, event: dict[str, Any]) -> None:
        if not fast.is_set():
            time.sleep(0.05)

    exp = BufferedExporter(slow)
    for i in range(40):
        exp("R-1", "ok", {"i": i})
    t0 = time.monotonic()
    assert exp.drain(timeout=0.2) is False
    took = time.monotonic() - t0
    assert took < 1.0, f"drain(timeout=0.2) took {took:.2f}s"
    assert 0 < exp.exported < 40 and exp.pending == 40 - exp.exported
    fast.set()
    assert exp.drain() is True and exp.exported == 40 and exp.pending == 0


def test_a_drain_waits_for_another_callers_export():   # failed before
    gate, started = threading.Event(), threading.Event()

    def target(rule_id: str, kind: str, event: dict[str, Any]) -> None:
        started.set()
        gate.wait(5)

    exp = BufferedExporter(target)
    exp("R-1", "ok", {})
    other = threading.Thread(target=exp.drain)
    other.start()
    assert started.wait(5)
    threading.Timer(0.2, gate.set).start()
    try:
        assert exp.drain(timeout=3) is True and exp.exported == 1
    finally:
        gate.set()
        other.join(5)


def test_a_drain_waiting_for_another_export_still_keeps_its_timeout():
    gate, started = threading.Event(), threading.Event()

    def target(rule_id: str, kind: str, event: dict[str, Any]) -> None:
        started.set()
        gate.wait(5)

    exp = BufferedExporter(target)
    exp("R-1", "ok", {})
    other = threading.Thread(target=exp.drain)
    other.start()
    try:
        assert started.wait(5)
        t0 = time.monotonic()
        assert exp.drain(timeout=0.1) is False
        assert time.monotonic() - t0 < 1.0
    finally:
        gate.set()
        other.join(5)


def test_close_reports_a_final_drain_that_ran_out_of_time():   # failed before
    fast = threading.Event()

    def slow(rule_id: str, kind: str, event: dict[str, Any]) -> None:
        if not fast.is_set():
            time.sleep(0.05)

    exp = BufferedExporter(slow)
    for i in range(40):
        exp("R-1", "ok", {"i": i})
    assert exp.close(timeout=0.2) is False and exp.pending > 0
    fast.set()
    assert exp.close() is True and exp.pending == 0 and exp.exported == 40


def test_close_with_the_flusher_running_and_time_to_spare_is_true():
    exp = BufferedExporter(lambda rule_id, kind, event: None).start()
    for i in range(100):
        exp("R-1", "ok", {"i": i})
    assert exp.close(timeout=5) is True and exp.exported == 100 and not exp.running


def test_concurrent_synchronous_drains_export_each_event_once():
    seen: list[int] = []
    lock = threading.Lock()

    def target(rule_id: str, kind: str, event: dict[str, Any]) -> None:
        with lock:
            seen.append(event["i"])

    exp = BufferedExporter(target, maxsize=5000)
    for i in range(2000):
        exp("R-1", "ok", {"i": i})
    results: list[bool] = []
    ts = [threading.Thread(target=lambda: results.append(exp.drain(5))) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert results == [True] * 4 and sorted(seen) == list(range(2000)) and exp.pending == 0


# --- 4. the event sink's inherited deque operations walk the locked snapshot ---------------------------
class Slow:
    """A value whose repr and comparison run Python code, so the interpreter may switch threads inside."""

    def __repr__(self) -> str:
        return "".join(str(i) for i in range(20))

    def __eq__(self, other: object) -> bool:
        sum(range(20))
        return isinstance(other, Slow)

    __hash__ = object.__hash__


SINK_OPS = {
    "repr": lambda s: repr(s), "copy": lambda s: s.copy(), "copy.copy": lambda s: copy.copy(s),
    "eq_deque": lambda s: s == collections.deque([{"v": Slow()}] * len(s)),
    "deque_eq": lambda s: collections.deque([{"v": Slow()}] * len(s)) == s,
    "add": lambda s: s + collections.deque(), "deque()": lambda s: collections.deque(s),
}


@pytest.mark.parametrize("op", sorted(SINK_OPS))
def test_inherited_sink_operations_take_the_registry_lock(op):
    reg = Registry(sink_size=10)
    for _ in range(3):
        reg.record("R-1", "ok", {"v": Slow()})
    spy = Spy(reg._lock)
    reg.sink._lock = spy
    SINK_OPS[op](reg.sink)
    assert spy.entered >= 1


@pytest.mark.parametrize("op", sorted(SINK_OPS))
def test_inherited_sink_operations_survive_concurrent_recording(op):
    reg = Registry(sink_size=1000)
    for _ in range(1000):
        reg.record("R-1", "ok", {"v": Slow()})
    stop = threading.Event()

    def writer() -> None:
        while not stop.is_set():
            reg.record("R-1", "ok", {"v": Slow()})

    t = threading.Thread(target=writer)
    t.start()
    errors = []
    try:
        for _ in range(15):
            try:
                SINK_OPS[op](reg.sink)
            except RuntimeError as e:
                errors.append(e)
    finally:
        stop.set()
        t.join()
    assert not errors, errors[0]


# --- 5. inv verify imports schema-class contract modules; validate reports a malformed document --------
def test_contract_modules_include_schema_class_refs():   # failed before
    doc = {"contracts": {"a": {"type": "dataclass", "ref": "app.models:Order"},
                         "b": {"type": "schema", "ref": "app.schemas:Thing"},
                         "c": {"type": "schema", "ref": "schemas/thing.json"},
                         "d": {"type": "pydantic", "ref": "app.pmodels:P"},
                         "e": {"type": "python", "ref": "app.fns:f"},
                         "f": {"type": "jsonschema", "ref": "x.json"}, "g": "not a mapping"}}
    assert project._contract_modules(doc) == ["app.models", "app.schemas", "app.pmodels", "app.fns"]


def test_verify_imports_a_dataclass_contract_module(tmp_path):   # failed before
    marker = tmp_path / "imported"
    root = write_project(tmp_path / "proj", {
        "app/__init__.py": "",
        "app/models.py": (f"import dataclasses, pathlib\npathlib.Path({str(marker)!r}).write_text('yes')\n\n"
                          "@dataclasses.dataclass\nclass Order:\n    n: int\n")})
    (root / "invariants.json").write_text(json.dumps(
        {"contracts": {"order": {"type": "dataclass", "ref": "app.models:Order"}},
         "rules": [{"id": "R-1", "status": "candidate", "bad": "false"}]}))
    r = run_inv("verify", root / "invariants.json", cwd=root)
    assert marker.exists(), r.stdout + r.stderr


def test_validate_reports_a_malformed_document_without_jsonschema(tmp_path, monkeypatch, capsys):  # failed before
    monkeypatch.setattr(commands, "_schema_errors", lambda doc: None)
    path = tmp_path / "invariants.json"
    path.write_text(json.dumps({"targets": ["py:a.b"], "rules": [{"id": "R-1", "applies_to": "py:*", "bad": "false"}]}))
    import argparse
    code = commands.cmd_validate(argparse.Namespace(rulefile=str(path), path=[]))
    err = capsys.readouterr().err
    assert code == commands.FAILED and "lockfile:" in err, err


# --- 6. an edge with several Checked parameters ---------------------------------------------------------
class CA(inv.Checked): ...


class CB(inv.Checked): ...


def test_several_checked_parameters_need_at():   # failed before
    with pytest.raises(TypeError, match=r"more than one parameter annotated with an inv.Checked subclass \(a, b\)"):
        @inv.edge("E-1")
        def both(a: CA, b: CB) -> None: ...


def test_at_names_one_checked_parameter():
    @inv.edge("E-1", at="b")
    def pick(a: CA, b: CB | None) -> None: ...
    assert pick.__inv_at__ == "b"               # type: ignore[attr-defined]

    @inv.edge("E-1")
    def one(a: CA, n: int) -> None: ...
    assert one.__inv_at__ == "a"                # type: ignore[attr-defined]
    with pytest.raises(TypeError, match="not an inv.Checked subclass"):
        @inv.edge("E-1", at="n")
        def wrong(a: CA, n: int) -> None: ...
    with pytest.raises(TypeError, match="naming one of its parameters"):
        @inv.edge("E-1", at="zz")
        def missing(a: CA, b: CB) -> None: ...


# --- after the registry/mcp split: small protocol and standing fixes ------------------------------
def test_mcp_unhashable_tool_name_and_non_object_params_are_invalid_params():
    from invariants.mcp.protocol import Server, INVALID_PARAMS
    import pathlib as _p
    s = Server(_p.Path.cwd().resolve(), None)
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": ["scan"]}})
    assert r["error"]["code"] == INVALID_PARAMS
    for bad in ([], 0, ""):
        r = s.handle({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": bad})
        assert r["error"]["code"] == INVALID_PARAMS, bad
    assert "result" in s.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"})


def test_standing_honours_a_patch_of_registry_verify_on_the_class(monkeypatch):
    import importlib
    import invariants as inv
    from invariants.errors import CoverageError
    reg_mod = importlib.import_module("invariants.registry")
    fx = pathlib.Path(__file__).parent / "fixtures" / "cli" / "clean"
    monkeypatch.syspath_prepend(str(fx))

    def strict(self, modules=None):
        raise CoverageError("class-level policy")
    monkeypatch.setattr(reg_mod.Registry, "verify", strict)
    r = reg_mod.Registry()
    with inv.using(r):
        inv.load(str(fx / "invariants.yaml"))
        importlib.import_module("ledger.service")
        state, why = r.standing(next(iter(r.rules)), scan=True)
    assert state != "held" and any("class-level policy" in w for w in why)


def test_propose_rules_treats_max_doors_null_as_absent(tmp_path):
    from invariants.mcp.propose import tool_propose_rules
    from invariants.mcp.confine import ToolError
    try:
        tool_propose_rules(tmp_path.resolve(), None, {"max_doors": None})
    except ToolError as e:
        assert "max_doors" not in str(e), e


# --- after the loader split: malformed input is a LoadError, never a raw Python error -------------
@pytest.mark.parametrize("target", ["py:", "py:.x", "py:a..b", "py:a.b.", "py:1x"])
def test_a_malformed_py_target_is_a_load_error(target):
    from invariants.errors import LoadError
    from invariants.resolve import resolve
    with pytest.raises(LoadError, match="dotted Python name"):
        resolve(target)


def test_a_non_string_provider_argument_is_a_load_error(monkeypatch):
    import sys
    import invariants as inv
    from invariants.errors import LoadError
    monkeypatch.syspath_prepend(str(pathlib.Path(__file__).parent / "fixtures"))
    r19 = __import__("round19")
    prov = {"count": {"type": "int", "from": "py:round19.count", "args": {"v": 1}}}
    bind = {"prev": "old_self.size", "c": "provider.count"}
    doc = r19.guard_doc("G-OLD", "Box.grow", "c > 100", timing="post_effect", bind=bind, providers=prov)
    with inv.isolated():
        with pytest.raises(LoadError, match="must be a path string"):
            inv.load(doc, install=False)
