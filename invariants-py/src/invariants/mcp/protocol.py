"""The JSON-RPC 2.0 session over stdio: the tool table tools/list answers, Server, serve, and main."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import pathlib
import sys
from typing import IO, Any, Callable, Sequence

from .confine import ToolError
from .propose import tool_propose_rules
from .tools import tool_check, tool_explain, tool_scan, tool_status

PROTOCOL_VERSION = "2025-06-18"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603

INSTRUCTIONS = ("Read-only tools for the project's invariants (laws on API boundaries). Use scan, check, status "
                "and explain to understand the laws; use propose_rules to draft new ones. Nothing here approves "
                "or locks a rule: show proposals to a person, who edits invariants.yaml and runs `inv lock`. "
                "Never edit invariants.lock or mediation.allow, and never accept a weakening yourself.")


# --- the tools ----------------------------------------------------------------------------------
_PROJECT = {"root": {"type": "string", "description": "project root, relative to the server's --root (default: it)"},
            "rules": {"type": "string", "description": "rule file, relative to root (default: invariants.yaml|yml|json)"}}
_ROOTS = {"source_roots": {"type": "array", "items": {"type": "string"},
                           "description": "source roots to scan, relative to root (default: as `inv scan`)"},
          "exclude": {"type": "array", "items": {"type": "string"}, "description": "globs to skip"}}
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

Tool = Callable[[pathlib.Path, "pathlib.Path | None", dict[str, Any]], dict[str, Any]]
TOOLS: dict[str, tuple[Tool, str, str, dict[str, Any]]] = {
    "scan": (tool_scan, "Path scan",
             "Run `inv scan`: is every call path to a protected sink passing its door? Returns the verdict, per-rule "
             "results and each finding (kind, code, file, line, path, owner, id).",
             {**_PROJECT, **_ROOTS}),
    "check": (tool_check, "inv check",
              "Run `inv check` read-only (no --relock): rule file, lock freshness, guards and scan, with one verdict "
              "and the result of each step.", {**_PROJECT, **_ROOTS}),
    "status": (tool_status, "Rule standing", "Run `inv status --json`: the standing of each rule (held, "
               "enforce(known-callers), ...) and why.", dict(_PROJECT)),
    "explain": (tool_explain, "Explain a rule or finding",
                "Given a rule id or a finding id from scan, return the rule, its doors and sinks, the path, and the "
                "fix options in plain words.",
                {**_PROJECT, **_ROOTS, "id": {"type": "string", "description": "rule id (R-1) or finding id"}}),
    "propose_rules": (tool_propose_rules, "Propose rules (dry run)",
                      "Run `inv init`'s discovery as a dry run: proposed door rules (each checked by the scan), "
                      "contracts and suggested guards, plus the rule-file text. Writes nothing to the project.",
                      {"root": _PROJECT["root"], **_ROOTS,
                       "max_doors": {"type": "integer", "minimum": 0, "description": "door rules to draft (default 5)"},
                       "graphify": {"type": "string", "description": "a Graphify graph.json for door hints"},
                       "no_graphify": {"type": "boolean", "description": "do not read graphify-out/graph.json"}}),
}


def tools_list() -> list[dict[str, Any]]:
    return [{"name": name, "title": title, "description": desc, "annotations": {"title": title, **READ_ONLY},
             "inputSchema": {"type": "object", "properties": props, "required": ["id"] if name == "explain" else [],
                             "additionalProperties": False}}
            for name, (_, title, desc, props) in TOOLS.items()]


# --- JSON-RPC -----------------------------------------------------------------------------------
class Server:
    """One MCP session: handle() takes one decoded message and returns the response (None for a notification)."""

    def __init__(self, root: pathlib.Path, rules: pathlib.Path | None = None) -> None:
        self.root = root.resolve()
        self.rules = rules.resolve() if rules is not None else None
        self.initialized = False

    def handle(self, msg: Any) -> dict[str, Any] | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            if isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg):
                return None                         # a response to a request we never send: ignore
            mid = msg.get("id") if isinstance(msg, dict) else None
            return _error(mid, INVALID_REQUEST, "not a JSON-RPC 2.0 request (batches are not supported)")
        method, params = msg["method"], msg.get("params") or {}
        notification = "id" not in msg
        if notification:
            if method == "notifications/initialized":
                self.initialized = True
            return None                             # notifications (cancelled, initialized, ...) get no reply
        mid = msg["id"]
        if not isinstance(params, dict):
            return _error(mid, INVALID_PARAMS, "params must be an object")
        try:
            if method == "initialize":
                return _result(mid, self.initialize(params))
            if method == "ping":
                return _result(mid, {})
            if method == "tools/list":
                return _result(mid, {"tools": tools_list()})
            if method == "tools/call":
                return self.call(mid, params)
            return _error(mid, METHOD_NOT_FOUND, f"method not found: {method}")
        except Exception as e:                      # never kill the session
            return _error(mid, INTERNAL_ERROR, f"{type(e).__name__}: {e}")

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        from .. import __version__
        return {"protocolVersion": PROTOCOL_VERSION,        # the one version this server speaks
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "invariants", "title": "invariants (read-only)", "version": __version__},
                "instructions": INSTRUCTIONS}

    def call(self, mid: Any, params: dict[str, Any]) -> dict[str, Any]:
        name, args = params.get("name"), params.get("arguments")
        args = {} if args is None else args
        if name not in TOOLS:
            return _error(mid, INVALID_PARAMS, f"unknown tool: {name}")
        if not isinstance(args, dict):
            return _error(mid, INVALID_PARAMS, "arguments must be an object")
        fn, _, _, props = TOOLS[str(name)]
        extra = sorted(set(args) - set(props))
        if extra:
            return _error(mid, INVALID_PARAMS, f"{name}: unknown argument(s) {', '.join(extra)}")
        noise = io.StringIO()
        try:
            with contextlib.redirect_stdout(noise):        # stdout carries protocol messages only
                data = fn(self.root, self.rules, args)
        except ToolError as e:
            return _result(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:
            return _result(mid, {"content": [{"type": "text", "text": f"{name} failed: {type(e).__name__}: {e}"}],
                                 "isError": True})
        return _result(mid, {"content": [{"type": "text", "text": json.dumps(data, indent=2)}],
                             "structuredContent": data, "isError": False})


def _result(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(server: Server, stdin: IO[str], stdout: IO[str]) -> None:
    """Read newline-delimited JSON-RPC from stdin and answer on stdout until stdin closes."""
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = _error(None, PARSE_ERROR, "parse error: one JSON object per line")
        else:
            reply = server.handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
            stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `inv-mcp` (and `inv mcp`)."""
    p = argparse.ArgumentParser(prog="inv-mcp", description="Read-only MCP server (stdio) over invariants: scan, "
                                "check, status, explain and propose_rules. It never writes the rule file, the "
                                "lock or allow.")
    p.add_argument("--root", default=".", help="project root (default: the current directory)")
    p.add_argument("--rules", default=None, help="rule file (default: invariants.yaml|yml|json in the root)")
    a = p.parse_args(argv)
    root = pathlib.Path(a.root)
    if not root.is_dir():
        print(f"inv-mcp: --root {root} is not a directory", file=sys.stderr)
        return 2
    rules = pathlib.Path(root, a.rules) if a.rules else None
    out = sys.stdout
    sys.stdout = sys.stderr                         # anything else printed goes to stderr, never the protocol
    try:
        serve(Server(root, rules), sys.stdin, out)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout = out
    return 0
