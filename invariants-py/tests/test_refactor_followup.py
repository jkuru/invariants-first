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
