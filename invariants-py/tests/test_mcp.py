"""`inv-mcp`: the read-only MCP server, driven over stdio with real JSON-RPC messages."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
from typing import Any, Iterator

import pytest

from cli_support import run_inv, write_project

STORE = "def write_balance(conn, account, cents):\n    conn.execute('UPDATE balances SET cents = ?', (cents,))\n"
SERVICE = ("from bank.store import write_balance\n\n"
           "def apply_transfer(conn, src, dst, cents, balances):\n"
           "    if cents <= 0:\n        raise ValueError('transfer must be positive')\n"
           "    write_balance(conn, src, balances[src] - cents)\n    write_balance(conn, dst, balances[dst] + cents)\n")
API = ("from bank.service import apply_transfer\n\n"
       "def transfer(conn, body, balances):\n    apply_transfer(conn, body['src'], body['dst'], body['cents'], balances)\n")
PROMO = "from bank.store import write_balance\n\ndef bonus(conn):\n    write_balance(conn, 'a', 100)\n"
DOOR = {"id": "LEDGER", "statement": "Balances change only through a transfer.",
        "door": "py:bank.service.apply_transfer", "sink": "py:bank.store.write_balance"}


def _bank(tmp_path: pathlib.Path, rules: bool = True) -> pathlib.Path:
    root = write_project(tmp_path / "bank", {
        "pyproject.toml": '[project]\nname = "bank"\nversion = "0"\n',
        "bank/__init__.py": "", "bank/store.py": STORE, "bank/service.py": SERVICE, "bank/api.py": API})
    if rules:
        (root / "invariants.yaml").write_text(json.dumps({"rules": [DOOR]}))
        r = run_inv("lock", cwd=root)
        assert r.returncode == 0, r.stdout + r.stderr
    return root


def _tree(root: pathlib.Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


class Client:
    """A JSON-RPC client over the server's stdin and stdout, one message per line."""

    def __init__(self, root: pathlib.Path, *extra: str) -> None:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("INV_ABLATE", None)
        self.proc = subprocess.Popen([sys.executable, "-m", "invariants.mcp", "--root", str(root), *extra],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env=env, cwd=root.parent)
        self.next_id = 0

    def send(self, msg: dict[str, Any] | str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((msg if isinstance(msg, str) else json.dumps(msg)) + "\n")
        self.proc.stdin.flush()

    def read(self) -> dict[str, Any]:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        assert line, f"server closed stdout; stderr: {self.proc.stderr.read() if self.proc.stderr else ''}"
        return json.loads(line)                       # every stdout line is one JSON-RPC message

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.next_id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        reply = self.read()
        assert reply["id"] == self.next_id and reply["jsonrpc"] == "2.0"
        return reply

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        reply = self.request("tools/call", {"name": name, "arguments": arguments})
        assert "result" in reply, reply
        return reply["result"]

    def close(self) -> str:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.close()
        rest = self.proc.stdout.read()
        assert self.proc.wait(timeout=30) == 0
        return rest


@pytest.fixture
def bank(tmp_path: pathlib.Path) -> pathlib.Path:
    return _bank(tmp_path)


@pytest.fixture
def client(bank: pathlib.Path) -> Iterator[Client]:
    c = Client(bank)
    c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "test", "version": "0"}})
    c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    yield c
    if c.proc.poll() is None:
        c.proc.kill()
        c.proc.wait()


# --- the protocol ---------------------------------------------------------------------------------
def test_initialize_ping_and_tools_list(bank):
    c = Client(bank)
    r = c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "test", "version": "0"}})["result"]
    assert r["protocolVersion"] == "2025-06-18"
    assert r["capabilities"] == {"tools": {"listChanged": False}}
    assert r["serverInfo"]["name"] == "invariants" and "never" in r["instructions"].lower()
    c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})     # a notification: no reply
    assert c.request("ping")["result"] == {}                               # so the next line answers the ping
    tools = c.request("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["scan", "check", "status", "explain", "propose_rules"]
    for t in tools:
        assert t["annotations"]["readOnlyHint"] is True and t["annotations"]["destructiveHint"] is False
        assert t["inputSchema"]["type"] == "object" and t["description"]
    assert c.close() == ""


def test_an_older_protocol_version_gets_the_one_the_server_speaks(bank):
    c = Client(bank)
    r = c.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "old", "version": "0"}})["result"]
    assert r["protocolVersion"] == "2025-06-18"
    c.close()


def test_protocol_errors(client):
    assert client.request("resources/list")["error"]["code"] == -32601
    assert client.request("tools/call", {"name": "lock", "arguments": {}})["error"]["code"] == -32602
    assert client.request("tools/call", {"name": "scan", "arguments": {"write": True}})["error"]["code"] == -32602
    assert client.request("tools/call", {"name": "scan", "arguments": []})["error"]["code"] == -32602
    client.send("{not json")
    assert client.read() == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700,
                                                                       "message": "parse error: one JSON object per line"}}
    client.send([{"jsonrpc": "2.0", "id": 9, "method": "ping"}].__repr__().replace("'", '"'))   # a batch
    assert client.read()["error"]["code"] == -32600
    client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
    assert client.request("ping")["result"] == {}                  # the cancel notification got no reply


# --- the tools ------------------------------------------------------------------------------------
def test_scan_passes_then_finds_a_back_door(client, bank):
    r = client.call("scan")
    assert r["isError"] is False
    data = r["structuredContent"]
    assert json.loads(r["content"][0]["text"]) == data
    assert data["passed"] is True and data["verdict"] == "pass" and data["exit_code"] == 0
    assert data["rules"]["LEDGER"]["result"] == "pass" and data["rule_file"] == "invariants.yaml"
    (bank / "bank/promo.py").write_text(PROMO)
    data = client.call("scan")["structuredContent"]
    assert data["verdict"] == "fail" and data["exit_code"] == 1
    (f,) = data["findings"]
    assert f["kind"] == "bypass" and f["path"] == ["bank.promo.bonus", "bank.store.write_balance"]
    assert f["id"] == "LEDGER:bypass:bank/promo.py:4" and not pathlib.PurePath(f["root"] or ".").is_absolute()


def test_explain_a_finding_and_a_rule(client, bank):
    (bank / "bank/promo.py").write_text(PROMO)
    fid = client.call("scan")["structuredContent"]["findings"][0]["id"]
    data = client.call("explain", id=fid)["structuredContent"]
    assert data["rule"]["doors"] == ["py:bank.service.apply_transfer"]
    assert data["rule"]["sinks"] == ["py:bank.store.write_balance"] and data["rule"]["kind"] == "path law"
    assert data["path"] == ["bank.promo.bonus", "bank.store.write_balance"]
    assert "bank.promo.bonus -> bank.store.write_balance" in data["explanation"]
    assert any("py:bank.service.apply_transfer" in fix for fix in data["fix_options"])
    assert any("only they may add" in fix and "mediation.allow" in fix for fix in data["fix_options"])
    rule = client.call("explain", id="LEDGER")["structuredContent"]
    assert rule["scan_result"] == "fail" and [f["id"] for f in rule["findings"]] == [fid]
    assert "Balances change only through a transfer." in rule["rule"]["statement"]
    assert client.call("explain", id="NOPE")["isError"] is True
    assert client.call("explain", id="LEDGER:bypass:bank/gone.py:1")["isError"] is True
    assert client.request("tools/call", {"name": "explain", "arguments": {}})["result"]["isError"] is True


def test_check_and_status(client, bank):
    data = client.call("check")["structuredContent"]
    assert data["passed"] is True and data["exit_code"] == 0, data
    steps = {s["step"]: s["result"] for s in data["steps"]}
    assert steps["rules"] == "ok" and steps["lock"] == "ok" and steps["scan"] == "ok"
    status = client.call("status")["structuredContent"]["status"]
    assert "LEDGER" in json.dumps(status)
    (bank / "bank/promo.py").write_text(PROMO)
    data = client.call("check")["structuredContent"]
    assert data["passed"] is False and data["exit_code"] == 1
    scan = next(s for s in data["steps"] if s["step"] == "scan")
    assert scan["result"] == "fail" and any("bank.promo.bonus" in d for d in scan["detail"])


def test_check_never_relocks_a_stale_lock(client, bank):
    (bank / "invariants.yaml").write_text(json.dumps({"rules": [dict(DOOR, rev=2)]}))   # not prose: stale
    before = (bank / "invariants.lock").read_text()
    data = client.call("check")["structuredContent"]
    assert data["passed"] is False and {s["step"]: s["result"] for s in data["steps"]}["lock"] == "fail"
    assert (bank / "invariants.lock").read_text() == before


def test_propose_rules_is_a_dry_run(tmp_path):
    root = _bank(tmp_path, rules=False)
    write_project(root, {"bank/batch.py": "from bank.store import write_balance\n\n"
                                          "def refund_all(conn, rows):\n    for a, c in rows:\n"
                                          "        write_balance(conn, a, c)\n"})
    before = _tree(root)
    c = Client(root)
    data = c.call("propose_rules")["structuredContent"]
    assert data["written"] is False and "never approves its own rules" in data["note"]
    (door,) = data["door_rules"]
    assert door["doors"] == ["bank.service.apply_transfer"] and door["sinks"] == ["bank.store.write_balance"]
    assert door["allow"] == ["bank.batch.refund_all"] and door["drafted"] == "rule"
    assert door["scan"]["result"] == "pass"                           # the drafted rule, checked by the scan
    assert data["scan"]["verdict"] == "pass"
    assert f"  - id: {door['id']}" in data["rule_file"]
    assert data["suggested_guards"][0]["function"] == "bank.service.apply_transfer"
    assert _tree(root) == before                                      # nothing written: no rule file, no lock
    assert not (root / "invariants.yaml").exists() and not (root / "invariants.lock").exists()
    c.close()


def test_no_tool_writes_the_project(client, bank):
    (bank / "bank/promo.py").write_text(PROMO)
    before = _tree(bank)
    fid = client.call("scan")["structuredContent"]["findings"][0]["id"]
    for name, args in [("check", {}), ("status", {}), ("explain", {"id": fid}), ("explain", {"id": "LEDGER"}),
                       ("propose_rules", {})]:
        r = client.call(name, **args)
        assert r["isError"] is False, (name, r)
    assert _tree(bank) == before                                      # lock, rules, allow and no __pycache__
    data = client.call("propose_rules")["structuredContent"]
    assert "already has invariants.yaml" in data["note"]


def test_missing_rule_file_is_a_tool_error(tmp_path):
    root = _bank(tmp_path, rules=False)
    c = Client(root)
    for name in ("scan", "check", "status"):
        r = c.call(name)
        assert r["isError"] is True and "no rule file" in r["content"][0]["text"]
    c.close()


def test_rules_and_root_arguments(tmp_path, bank):
    other = tmp_path / "elsewhere"
    other.mkdir()
    c = Client(other, "--rules", str(bank / "invariants.yaml"))
    assert c.call("scan")["structuredContent"]["rules"]["LEDGER"]["result"] == "pass"
    outside = c.call("scan", root=str(bank))             # round 17: a root outside --root is refused
    assert outside["isError"] is True and "outside the server's --root" in outside["content"][0]["text"]
    assert c.call("scan", root="missing")["isError"] is True
    c.close()
    c = Client(tmp_path)                                  # a --root above the project reaches it
    assert c.call("scan", root="bank")["structuredContent"]["passed"] is True
    c.close()


def test_console_script_and_no_new_dependency():
    import tomllib
    project = tomllib.loads((pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]
    assert project["scripts"]["inv-mcp"] == "invariants.mcp:main"
    # the server is standard library only: the core's one third-party dependency is PyYAML, the rest are
    # the workspace's own libraries (packages/), standard library only themselves
    assert project["dependencies"] == ["pyyaml>=6", "invariants-cel==0.1.0", "invariants-scan==0.1.0",
                                       "invariants-schema-diff==0.1.0"]


def test_main_rejects_a_missing_root(tmp_path):
    p = subprocess.run([sys.executable, "-m", "invariants.mcp", "--root", str(tmp_path / "nope")],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert p.returncode == 2 and "not a directory" in p.stderr and p.stdout == ""


def test_stdout_carries_only_protocol_messages(client, bank, monkeypatch):
    """Library code that prints during a tool call never corrupts the stream (in-process check)."""
    from invariants import mcp
    server = mcp.Server(bank)

    def noisy(root, rules, args):
        print("hello from the library")
        return {"ok": True}
    monkeypatch.setitem(mcp.TOOLS, "scan", (noisy, *mcp.TOOLS["scan"][1:]))
    import io
    out = io.StringIO()
    mcp.serve(server, io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": "scan", "arguments": {}}}) + "\n"), out)
    (line,) = out.getvalue().splitlines()
    assert json.loads(line)["result"]["structuredContent"] == {"ok": True}
