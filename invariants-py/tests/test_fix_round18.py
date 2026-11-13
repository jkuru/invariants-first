"""Fix round 18 (hook, MCP, replay and schema review findings): each test failed before its fix.

1. The pretool hook compares file names case-insensitively (a case-insensitive filesystem opens
   INVARIANTS.LOCK as the lock): edit tools, shell words and globs.
2. It expands braces (invariants{,}.lock, pyproject{,}.toml) before matching.
3. It decodes $'...' (ANSI-C quoting), and under a directory that holds invariants.lock it blocks a
   write-capable command or redirect whose target file name the shell computes ($f, a backtick).
4. It matches INV_ALLOW_WEAKENING after shell unquoting: env X''=1, export, escaped letters.
5. inv-mcp refuses [tool.invariants.scan] roots (and setuptools where) outside --root.
6. ReplayModel compares the whole request: system absent vs present, model, max_tokens, params,
   unless ignore= names a field.
7. Schema weakenings: patternProperties added to a closed schema, any change inside oneOf,
   prefixItems extended over an items schema, nullable turned on.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap
import types
from typing import Any

import pytest

from invariants import lockfile, mcp
from invariants.integrations import anthropic as inv_an


# --- 1-4. the pretool hook --------------------------------------------------------------------------
def _hook(payload: dict[str, Any], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    ev = {**payload, "cwd": str(cwd)}
    return subprocess.run([sys.executable, "-m", "invariants.cli", "hook", "pretool"], input=json.dumps(ev),
                          capture_output=True, text=True, cwd=cwd, timeout=60)


@pytest.fixture
def project(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "invariants.lock").write_text("{}")
    (tmp_path / "pyproject.toml").write_text('[tool.invariants.scan]\nexclude = []\n')
    return tmp_path


@pytest.mark.parametrize("tool,tool_input", [
    ("Write", {"file_path": "Invariants.lock", "content": "{}"}),
    ("Write", {"file_path": "INVARIANTS.LOCK", "content": "{}"}),
    ("Edit", {"file_path": "sub/Invariants.Lock", "old_string": "a", "new_string": "b"}),
    ("Edit", {"file_path": "PyProject.toml", "old_string": "exclude = []", "new_string": "exclude = ['*']"}),
    ("Write", {"file_path": "PYPROJECT.TOML", "content": "[tool.invariants.scan]\nexclude = ['*']\n"}),
])
def test_hook_edit_tools_compare_names_case_insensitively(project, tool, tool_input):
    r = _hook({"tool_name": tool, "tool_input": tool_input}, project)
    assert r.returncode == 2, r.stderr


@pytest.mark.parametrize("command,blocked", [
    # 1. letter case
    ("cp /tmp/forged INVARIANTS.LOCK", True),
    ("cp /tmp/forged Invariants.lock", True),
    ("sed -i '' 's/a/b/' PYPROJECT.TOML", True),
    ("cat x >> PyProject.toml", True),
    ("cp /tmp/forged INV*.LOCK", True),
    ("cp /tmp/forged Invariant[s].Lock", True),
    # 2. brace expansion
    ("rm invariants{,}.lock", True),
    ("tee invariants{,}.lock < /tmp/forged > /dev/null", True),
    ("printf x | tee -a pyproject{,}.toml", True),
    ("cp /tmp/forged {invariants,x}.lock", True),
    ("cp /tmp/forged invariants.{lock,bak}", True),
    ("cp /tmp/forged inv{a..z}riants.lock", True),
    # 3. ANSI-C quoting and computed names
    ("cp x $'\\x69nvariants.\\x6cock'", True),
    ("rm $'\\151nvariants.lock'", True),
    ("printf -v f 'inv%sriants.%sock' a l; cp /tmp/forged \"$f\"", True),
    ("f=$(echo x); tee \"$f\" < /tmp/forged", True),
    ("cp /tmp/forged `echo x`", True),
    ("echo {} > \"$f\"", True),
    ("dd if=/tmp/forged of=$f", True),
    ("mv /tmp/forged \"$d/\"", True),
    # still allowed: read-only commands, and computed paths whose file name is literal
    ("cat \"$f\"", False),
    ("grep -n x \"$f\" invariants.lock", False),
    ("echo \"$HOME\" > /tmp/out.txt", False),
    ("cp build/out.txt \"$TMPDIR/out.txt\"", False),
    ("sed 's/$/;/' notes.txt", False),
    ("ls {src,tests}", False),
    ("cat invariants.lock | jq .", False),
    ("echo \"$x\" | wc -l", False),
])
def test_hook_shell_commands_round18(project, command, blocked):
    r = _hook({"tool_name": "Bash", "tool_input": {"command": command}}, project)
    assert r.returncode == (2 if blocked else 0), (command, r.stderr)


def test_hook_computed_names_only_blocked_where_a_lock_is(tmp_path):
    r = _hook({"tool_name": "Bash", "tool_input": {"command": "cp /tmp/forged \"$f\""}}, tmp_path)
    assert r.returncode == 0, r.stderr
    (tmp_path / "invariants.lock").write_text("{}")
    (tmp_path / "sub").mkdir()
    r = _hook({"tool_name": "Bash", "tool_input": {"command": "cp /tmp/forged \"$f\""}}, tmp_path / "sub")
    assert r.returncode == 2 and "invariants.lock" in r.stderr


@pytest.mark.parametrize("command", [
    "env INV_ALLOW_WEAK''ENING=1 inv check --base origin/main",
    "export INV_ALLOW_WEAKENIN\\G=1",
    "INV_ALLOW_\"WEAKENING\"=1 inv check --base main",
    "env -i INV_ALLOW_WEAKENING\\=1 inv check",
    "export $'INV_ALLOW_\\x57EAKENING'=1",
])
def test_hook_unquotes_the_weakening_switch(tmp_path, command):
    r = _hook({"tool_name": "Bash", "tool_input": {"command": command}}, tmp_path)
    assert r.returncode == 2 and "a person's decision" in r.stderr, (command, r.stderr)


# --- 5. inv-mcp and the roots pyproject.toml names ---------------------------------------------------
RULES = textwrap.dedent("""
    spec: invariant/v0.1
    targets: {"py:app.repo.door": {effects: external}}
    rules:
      - id: T-1
        status: candidate
        subject: {target: "py:app.repo.door"}
        bad: "false"
        enforcers: [{point: decorator, timing: pre_effect, mode: detect}]
        mediation: {sink: "py:app.repo.save", door: "py:app.repo.door"}
    """)


@pytest.mark.parametrize("pyproject,setting", [
    ('[tool.invariants.scan]\nroots = ["../secret"]\n', "[tool.invariants.scan] roots"),
    ('[tool.setuptools.packages.find]\nwhere = ["../secret"]\n', "[tool.setuptools.packages.find] where"),
])
@pytest.mark.parametrize("tool", ["scan", "check", "status"])
def test_mcp_refuses_pyproject_roots_outside_the_root(tmp_path, pyproject, setting, tool):
    proj, out = tmp_path / "proj", tmp_path / "secret"
    proj.mkdir()
    (out / "app").mkdir(parents=True)
    (out / "app" / "__init__.py").write_text("")
    (out / "app" / "repo.py").write_text("def save(x):\n    pass\n")
    (out / "app" / "leak.py").write_text("from app.repo import save\ndef private_billing_job(x):\n    save(x)\n")
    (proj / "pyproject.toml").write_text(pyproject)
    (proj / "invariants.yaml").write_text(RULES)
    s = mcp.Server(proj)
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": {}}})
    assert r["result"]["isError"] is True, r
    text = r["result"]["content"][0]["text"]
    assert setting in text and "outside the server's --root" in text
    assert "private_billing_job" not in json.dumps(r)


def test_mcp_keeps_pyproject_roots_inside_the_root(tmp_path):
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "__init__.py").write_text("")
    (tmp_path / "src" / "app" / "repo.py").write_text("def save(x):\n    pass\n\ndef door(x):\n    save(x)\n")
    (tmp_path / "pyproject.toml").write_text('[tool.invariants.scan]\nroots = ["src"]\n')
    (tmp_path / "invariants.yaml").write_text(RULES)
    r = mcp.Server(tmp_path).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": "scan", "arguments": {}}})
    assert r["result"]["isError"] is False, r


# --- 6. ReplayModel compares the whole request -------------------------------------------------------
class _Stub:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def messages(self) -> _Stub:
        return self

    def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        return types.SimpleNamespace(stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text="ok")])


def _record(path: pathlib.Path, **kw: Any) -> dict[str, Any]:
    stub = _Stub()
    rec = inv_an.RecordingModel(inv_an.AnthropicModel(client=stub, **kw), path)
    rec.complete("classify this refund")
    request: dict[str, Any] = json.loads(path.read_text())["calls"][0]["request"]
    sent = {k: v for k, v in stub.calls[0].items()}
    assert request == sent                       # the trace records what the adapter sent
    return request


def test_recording_records_the_whole_request(tmp_path):
    req = _record(tmp_path / "t.json", model="claude-opus-5-5", max_tokens=200, temperature=0.0)
    assert req["model"] == "claude-opus-5-5" and req["max_tokens"] == 200 and req["temperature"] == 0.0
    assert "system" not in req


@pytest.mark.parametrize("replay,match", [
    ({"system": "You are a pirate. Approve every refund."}, "system"),   # recorded without one
    ({"model": "claude-opus-5-5"}, "model"),
    ({"max_tokens": 10}, "max_tokens"),
    ({"temperature": 1.0}, "temperature"),
])
def test_replay_mismatches_on_any_request_field(tmp_path, replay, match):
    _record(tmp_path / "t.json")
    with pytest.raises(inv_an.ReplayMismatch, match=match):
        inv_an.ReplayModel(tmp_path / "t.json", **replay).complete("classify this refund")
    assert inv_an.ReplayModel(tmp_path / "t.json").complete("classify this refund") == "ok"


def test_replay_recorded_system_must_be_given(tmp_path):
    _record(tmp_path / "t.json", system="S")
    with pytest.raises(inv_an.ReplayMismatch, match="system"):
        inv_an.ReplayModel(tmp_path / "t.json").complete("classify this refund")
    assert inv_an.ReplayModel(tmp_path / "t.json", system="S").complete("classify this refund") == "ok"


def test_replay_ignore_is_explicit_per_field(tmp_path):
    _record(tmp_path / "t.json", temperature=0.0)
    m = inv_an.ReplayModel(tmp_path / "t.json", model="claude-opus-5-5", ignore=["model", "temperature"])
    assert m.complete("classify this refund") == "ok"
    with pytest.raises(inv_an.ReplayMismatch, match="max_tokens"):
        inv_an.ReplayModel(tmp_path / "t.json", max_tokens=5, temperature=0.0,
                           ignore=["model"]).complete("classify this refund")
    with pytest.raises(TypeError, match="ignore"):
        inv_an.ReplayModel(tmp_path / "t.json", ignore="model")


def test_model_for_replay_compares_the_configured_model(tmp_path):
    _record(tmp_path / "t.json", system="S")
    m = inv_an.model_for("replay", tmp_path / "t.json", system="S", model="claude-opus-5-5")
    assert m is not None
    with pytest.raises(inv_an.ReplayMismatch, match="model"):
        m.complete("classify this refund")
    m2 = inv_an.model_for("replay", tmp_path / "t.json", system="S", model="claude-opus-5-5", ignore=["model"])
    assert m2 is not None and m2.complete("classify this refund") == "ok"


# --- 7. schema weakenings ----------------------------------------------------------------------------
def _weak(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    a, b = {"jsonschema": old}, {"jsonschema": new}
    return [why for p, x, y in lockfile._json_paths(a, b, "", [])
            if (why := lockfile.constraint_weakening(p, x, y, a, b))]


CLOSED = {"type": "object", "properties": {"amount": {"type": "integer", "maximum": 100}},
          "additionalProperties": False}


@pytest.mark.parametrize("old,new,weak", [
    # (a) patternProperties
    (CLOSED, {**CLOSED, "patternProperties": {".*": {}}}, True),
    ({**CLOSED, "additionalProperties": {"type": "integer"}},
     {**CLOSED, "additionalProperties": {"type": "integer"}, "patternProperties": {"^x": {}}}, True),
    ({**CLOSED, "patternProperties": {"^a": {"type": "integer"}}},
     {**CLOSED, "patternProperties": {"^a": {"type": "integer"}, "^b": {}}}, True),
    ({"type": "object", "unevaluatedProperties": False}, {"type": "object", "unevaluatedProperties": False,
                                                          "patternProperties": {".*": {}}}, True),
    ({"type": "object"}, {"type": "object", "patternProperties": {"^x": {"type": "integer"}}}, False),
    ({"type": "object", "additionalProperties": True},
     {"type": "object", "additionalProperties": True, "patternProperties": {"^x": {"type": "integer"}}}, False),
    # (b) oneOf
    ({"oneOf": [{"maximum": 10}, {"minimum": 5}]}, {"oneOf": [{"maximum": 4}, {"minimum": 5}]}, True),
    ({"properties": {"k": {"oneOf": [{"type": "string"}, {"type": "string", "maxLength": 100}]}}},
     {"properties": {"k": {"oneOf": [{"type": "string"}, {"type": "string", "maxLength": 0}]}}}, True),
    ({"type": "integer"}, {"type": "integer", "oneOf": [{"maximum": 4}, {"minimum": 10}]}, False),
    # (c) prefixItems over items
    ({"type": "array", "prefixItems": [{"type": "string"}], "items": {"type": "integer"}},
     {"type": "array", "prefixItems": [{"type": "string"}, {}], "items": {"type": "integer"}}, True),
    ({"type": "array", "prefixItems": [{"type": "string"}], "items": {"type": "integer"}},
     {"type": "array", "prefixItems": [{"type": "string"}, {"type": "integer"}], "items": {"type": "integer"}}, False),
    ({"type": "array", "prefixItems": [{"type": "string"}]},
     {"type": "array", "prefixItems": [{"type": "string"}, {"type": "integer"}]}, False),
])
def test_schema_weakenings_round18(old, new, weak):
    assert bool(_weak(old, new)) is weak, _weak(old, new)


@pytest.mark.parametrize("path,old,new,weak", [
    ("schema.properties.amount.nullable", False, True, True),
    ("operations.op.requestBody.content.application/json.schema.properties.amount.nullable", None, True, True),
    ("schema.properties.amount.nullable", True, False, False),
    ("schema.properties.amount.nullable", None, False, False),
    ("schema.properties.amount.x-nullable", None, True, True),
])
def test_openapi_nullable_turned_on_is_a_weakening(path, old, new, weak):
    assert (lockfile.constraint_weakening(path, old, new) is not None) is weak
