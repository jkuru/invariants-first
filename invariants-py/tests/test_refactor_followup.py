"""After the layered refactor: the MCP server confines the project it imports from and the roots
propose_rules reads (1-3), and an OpenAPI contract file that is not UTF-8 is a LoadError, not a crash (4)."""
from __future__ import annotations

import json
import pathlib

import pytest

from cli_support import write_project
from invariants.errors import LoadError
from invariants.lock import pinning
from test_mcp import Client, _bank


def _text(r):
    return r["content"][0]["text"]


def test_check_refuses_a_project_root_above_the_server_root(tmp_path):
    # the nearest pyproject.toml sits above --root: check would import from there
    outer = write_project(tmp_path / "outer", {"pyproject.toml": '[project]\nname = "o"\nversion = "0"\n'})
    inner = outer / "inner"
    inner.mkdir()
    (inner / "invariants.yaml").write_text(json.dumps({"rules": []}))
    c = Client(inner)
    for tool in ("check", "status"):
        r = c.call(tool)
        assert r["isError"] is True and "outside the server's --root" in _text(r), (tool, r)
    c.close()


def test_a_src_symlink_outside_the_root_is_refused(tmp_path):
    root = _bank(tmp_path)
    away = tmp_path / "away"
    away.mkdir()
    (root / "src").symlink_to(away, target_is_directory=True)
    c = Client(root)
    r = c.call("check")
    assert r["isError"] is True and "default src/" in _text(r)
    c.close()


def test_propose_rules_confines_pyproject_roots(tmp_path):
    root = _bank(tmp_path, rules=False)
    (tmp_path / "elsewhere").mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "bank"\nversion = "0"\n'
                                         '[tool.invariants.scan]\nroots = ["../elsewhere"]\n')
    c = Client(root)
    r = c.call("propose_rules")
    assert r["isError"] is True and "outside the server's --root" in _text(r)
    c.close()


def test_propose_rules_refuses_a_graph_symlinked_outside(tmp_path):
    root = _bank(tmp_path, rules=False)
    graph = tmp_path / "graph.json"
    graph.write_text('{"nodes": [], "links": []}')
    (root / "graphify-out").mkdir()
    (root / "graphify-out" / "graph.json").symlink_to(graph)
    c = Client(root)
    r = c.call("propose_rules")
    assert r["isError"] is True and "graphify graph" in _text(r)
    assert c.call("propose_rules", no_graphify=True)["isError"] is False
    c.close()


def test_a_non_utf8_openapi_file_is_a_load_error(tmp_path):
    f = tmp_path / "api.yaml"
    f.write_bytes(b"openapi: 3.0.0\ninfo: {title: \xff\xfe}\n")
    with pytest.raises(LoadError, match="UTF-8"):
        pinning._openapi_file("api", f)


# --- the split keeps module names, so an upgrade alone never re-pins a lock or breaks a pickle --------
import importlib  # noqa: E402
import inspect  # noqa: E402
import pickle  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import invariants as inv  # noqa: E402
from invariants.errors import CoverageError  # noqa: E402
from invariants.integrations.exporters import BufferedExporter  # noqa: E402

OLD_HOMES = {
    "invariants.guard": ["Checked", "FrozenDict", "FrozenList", "ModelCallDecorator"],
    "invariants.registry": ["Handoff", "JsonlReviews", "MemoryReviews", "ReviewStore", "Registry", "EventSink"],
    "invariants.scan": ["Finding", "Report", "Mediation", "SqlWrite", "ScanConfigError"],
    "invariants.cli": ["BaseUnavailable", "Usage"],
    "invariants.lockfile": ["Change", "LockError", "Schemas"],
}


@pytest.mark.parametrize("home", sorted(OLD_HOMES))
def test_moved_classes_keep_their_module_name(home):
    mod = importlib.import_module(home)
    for name in OLD_HOMES[home]:
        assert getattr(mod, name).__module__ == home, name


def test_a_signature_annotated_with_a_library_class_pins_as_before():
    ns: dict = {"inv": inv}                     # evaluated annotations, as in a module without future import
    exec(compile("def withdraw(note: inv.Checked = None, h: inv.Handoff = None) -> None: ...", "<t>", "exec",
                 dont_inherit=True), ns)
    assert str(inspect.signature(ns["withdraw"])) == \
        "(note: invariants.guard.Checked = None, h: invariants.registry.Handoff = None) -> None"


def test_pickles_name_the_old_modules():
    lock = importlib.import_module("invariants.lock")
    data = pickle.dumps(lock.Change("w", "x"))
    assert b"invariants.lockfile" in data and pickle.loads(data) == lock.Change("w", "x")
    g = importlib.import_module("invariants.guard")
    assert g._rebuild_checked.__module__ == g._freeze.__module__ == "invariants.guard"


def test_a_registry_sink_pickles_and_deep_copies():
    import copy
    r = inv.Registry()
    r.sink.append({"rule": "R"})
    assert list(pickle.loads(pickle.dumps(r.sink))) == [{"rule": "R"}]
    assert list(copy.deepcopy(r.sink)) == [{"rule": "R"}]


def test_standing_honours_a_verify_override(tmp_path, monkeypatch):
    fx = pathlib.Path(__file__).parent / "fixtures" / "cli" / "clean"
    monkeypatch.syspath_prepend(str(fx))

    class Strict(inv.Registry):
        def verify(self, modules=None):
            raise CoverageError("custom coverage policy failed")

    r = Strict()
    with inv.using(r):
        inv.load(str(fx / "invariants.yaml"))
        importlib.import_module("ledger.service")
        state, why = r.standing(r.rules and next(iter(r.rules)), scan=True)
    assert state != "held" and any("custom coverage policy failed" in w for w in why)


def test_an_exporter_whose_close_timed_out_recovers():
    def slow(r, k, e):
        time.sleep(0.3)
    b = BufferedExporter(slow, maxsize=1, on_full="block")
    b.start()
    b("R", "pass", {})
    time.sleep(0.05)
    assert b.close(timeout=0.01) is False
    time.sleep(0.6)                                    # the flusher finishes its batch and exits
    assert b.running is False
    b.start()
    b("R", "pass", {})
    assert b.drain(timeout=2.0) is True
    assert b.close(timeout=2.0) is True


def test_a_base_exception_in_the_target_leaves_no_phantom_pending():
    def boom(r, k, e):
        raise SystemExit(3)
    b = BufferedExporter(boom)
    seen = []
    old = threading.excepthook
    threading.excepthook = lambda a: seen.append(a.exc_type)
    try:
        b.start()
        b("R", "pass", {})
        deadline = time.monotonic() + 2
        while b.running and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        threading.excepthook = old
    assert b.running is False and b.pending == 0 and b.failed == 1
