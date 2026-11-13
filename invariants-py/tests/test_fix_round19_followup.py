"""Round 19 follow-up, from the combined review: a frozen Checked value parses like the plain data it
was made from (1), and an annotation renamed in a pinned signature is stale even when the resolved
type is the same, since an alias can carry Annotated metadata no pin holds (2)."""
from __future__ import annotations

import dataclasses
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

import invariants as inv
from invariants import lock

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import round19 as r19  # noqa: E402


@pytest.fixture
def checked():
    with inv.isolated():
        inv.load(r19.model_doc())
        r19.REPLIES[:] = [r19.reply()]
        yield r19.plan()


def test_a_frozen_checked_value_reparses(checked):
    assert type(checked.value).__name__ == "FrozenDict"
    again = inv.parse(checked.value, r19.Plan)
    assert again == inv.parse(json.loads(json.dumps(checked.value)), r19.Plan)


def test_a_model_call_may_return_a_previous_checked_value(checked):
    r19.REPLIES[:] = [checked.value]
    assert r19.plan().value == checked.value


@dataclasses.dataclass
class Inner:
    tags: list[str]


def test_frozen_parts_nest_in_a_new_reply(checked):
    assert inv.parse({"x": {"tags": checked.value["tags"]}}, {"x": Inner})["x"].tags == list(checked.value["tags"])
    assert inv.parse({"x": checked.value}, {"x": dict[str, Any]})["x"] == checked.value


SRC = '''
from __future__ import annotations
import typing
class Dep:
    def __init__(self, f): self.f = f
AdminUser = typing.Annotated[str, Dep("get_admin")]
AnyUser = typing.Annotated[str, Dep("get_any")]
def f(user: %s) -> None: pass
'''


def _pin(tmp_path, monkeypatch, name, ann):
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(SRC % ann))
    monkeypatch.syspath_prepend(str(tmp_path))
    doc = {"contracts": {}, "targets": {f"py:{name}.f": {"effects": "external"}}, "providers": {}, "rules": []}
    return json.loads(json.dumps(lock.build(doc, tmp_path)).replace(name, "MOD"))


def test_an_aliased_annotated_swap_is_stale(tmp_path, monkeypatch):
    old = _pin(tmp_path, monkeypatch, "sig_a", "AdminUser")
    new = _pin(tmp_path, monkeypatch, "sig_b", "AnyUser")
    got = lock.changes(old, new)
    assert got and not all(c.info for c in got)


def test_a_quoted_annotation_is_still_the_same_signature(tmp_path, monkeypatch):
    old = _pin(tmp_path, monkeypatch, "sig_c", "AdminUser")
    new = _pin(tmp_path, monkeypatch, "sig_d", "'AdminUser'")
    assert all(c.info for c in lock.changes(old, new))
