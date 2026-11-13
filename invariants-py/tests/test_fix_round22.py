"""Small edges after round 21: EventSink != a list (1); provider args and edge targets checked even
when no live rule uses them (2); NewType pinned as its supertype, an unreadable contract directory a
LoadError (3); explain on a rule file that does not load is a tool error (4)."""
from __future__ import annotations

import os
import pathlib
import sys
import typing

import pytest

import invariants as inv
from invariants.errors import LoadError


def test_event_sink_not_equal_to_a_list_agrees_with_equal():
    r = inv.Registry()
    r.sink.append({"a": 1})
    assert r.sink == [{"a": 1}] and not (r.sink != [{"a": 1}])
    assert r.sink != [{"a": 2}]


def test_a_non_string_argument_of_an_unused_provider_is_a_load_error(monkeypatch):
    monkeypatch.syspath_prepend(str(pathlib.Path(__file__).parent / "fixtures"))
    doc = {"providers": {"count": {"type": "int", "from": "py:round19.count", "args": {"v": 1}}}, "rules": []}
    with inv.isolated(), pytest.raises(LoadError, match="must be a path string"):
        inv.load(doc, install=False)


def test_a_retired_rules_edge_target_must_still_be_a_py_name(monkeypatch):
    monkeypatch.syspath_prepend(str(pathlib.Path(__file__).parent / "fixtures"))
    import round19 as r19
    doc = r19.model_doc()
    rule = doc["rules"][0]
    rule["status"] = "retired"
    rule["enforcers"].append({"point": "edge", "timing": "pre_effect", "mode": "enforce", "target": "consume"})
    with inv.isolated(), pytest.raises(LoadError, match="py: name"):
        inv.load(doc, install=False)


UserId = typing.NewType("UserId", int)


def test_newtype_is_pinned_as_its_supertype():
    from invariants.lock.pin_jsonschema import _json_type
    from invariants.lock.pin_types import _unalias
    assert _unalias(UserId) is int
    assert _json_type(UserId, {}, 0) == {"type": "integer"}


def test_explain_on_a_rule_file_that_does_not_load_is_a_tool_error(tmp_path):
    from invariants.mcp.confine import ToolError
    from invariants.mcp.tools import tool_explain
    (tmp_path / "invariants.yaml").write_text("rules: [\n")
    with pytest.raises(ToolError):
        tool_explain(tmp_path.resolve(), None, {"id": "R-1"})
