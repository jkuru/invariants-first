"""`inv-mcp` (and `inv mcp`): a read-only Model Context Protocol server over stdio.

JSON-RPC 2.0, one message per line, MCP protocol version 2025-06-18: initialize, ping, tools/list and
tools/call. Standard library only. The tools let a coding agent understand a project's laws and
propose new ones; none of them writes the rule file, invariants.lock or `allow`:

  scan           the path scan on a project root (findings, each with an id for `explain`)
  check          `inv check` without --relock: validate, lock --check, verify and scan
  status         the standing of each rule (`inv status --json`)
  explain        a rule or a finding in plain words: the rule, its doors and sinks, the path, the fixes
  propose_rules  `inv init`'s discovery in dry-run mode: the proposed rules, each checked by the scan,
                 rendered into a temporary directory and never into the project

`check` and `status` import the app, so they run `inv` in a child process (with bytecode writing off):
a module the agent edited between two calls is imported fresh each time. `scan`, `explain` and
`propose_rules` only parse source, so they run in this process. A person approves and locks rules:
the server has no tool that does.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from typing import IO, Any, Callable, Sequence

PROTOCOL_VERSION = "2025-06-18"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
RULE_FILES = ("invariants.yaml", "invariants.yml", "invariants.json")
CHILD_TIMEOUT = 600

INSTRUCTIONS = ("Read-only tools for the project's invariants (laws on API boundaries). Use scan, check, status "
                "and explain to understand the laws; use propose_rules to draft new ones. Nothing here approves "
                "or locks a rule: show proposals to a person, who edits invariants.yaml and runs `inv lock`. "
                "Never edit invariants.lock or mediation.allow, and never accept a weakening yourself.")


class ToolError(Exception):
    """A tool failed for a reason the agent should read (reported with isError: true)."""


# --- the project --------------------------------------------------------------------------------
# Every path a tool argument names (root, rules, source_roots, graphify) resolves inside the server's
# --root, after following symlinks: `check` and `status` import the code they find, so a path outside
# would run code the person who started the server never pointed it at.
def _inside(server_root: pathlib.Path, base: pathlib.Path, given: str, what: str) -> pathlib.Path:
    """base / given, resolved (symlinks followed); ToolError unless it is inside server_root."""
    path = (base / given).resolve()
    if not path.is_relative_to(server_root):
        raise ToolError(f"{what} {given!r} resolves to {path}, outside the server's --root {server_root}; "
                        f"a tool reads only inside it (start inv-mcp with a wider --root to reach it)")
    return path


def _root(server_root: pathlib.Path, args: dict[str, Any]) -> pathlib.Path:
    given = args.get("root")
    root = _inside(server_root, server_root, given, "root") if isinstance(given, str) and given else server_root
    if root == pathlib.Path(root.anchor):
        raise ToolError(f"project root {root} is the filesystem root; start inv-mcp with --root at a project")
    if not root.is_dir():
        raise ToolError(f"project root {root} is not a directory")
    return root


def _rule_file(root: pathlib.Path, args: dict[str, Any], default: pathlib.Path | None,
               server_root: pathlib.Path) -> pathlib.Path:
    given = args.get("rules")
    if isinstance(given, str) and given:
        path = _inside(server_root, root, given, "rules")
    elif default is not None and not args.get("root"):
        path = default                                  # the server's own --rules: its operator chose it
    else:
        found = next((root / n for n in RULE_FILES if (root / n).is_file()), root / RULE_FILES[0])
        path = _inside(server_root, root, found.name, "rule file")
    if not path.is_file():
        raise ToolError(f"no rule file at {path}; run propose_rules to draft one (a person writes it)")
    return path


def _strs(args: dict[str, Any], key: str) -> list[str]:
    v = args.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ToolError(f"{key} must be a list of strings")
    return list(v)


def _source_roots(server_root: pathlib.Path, root: pathlib.Path, args: dict[str, Any]) -> list[str]:
    """The source_roots argument, each resolved inside the server's --root."""
    return [str(_inside(server_root, root, r, "source root")) for r in _strs(args, "source_roots")]


def _rel(path: str | None, root: pathlib.Path) -> str | None:
    if not path:
        return path
    try:
        return pathlib.Path(path).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return path


def finding_id(f: dict[str, Any]) -> str:
    """A stable id for one finding: rule, code, file and line (what `explain` takes)."""
    return f"{f.get('rule')}:{f.get('code')}:{f.get('file') or '-'}:{f.get('line') or 0}"


# --- scan ---------------------------------------------------------------------------------------
def run_scan(root: pathlib.Path, rule_file: pathlib.Path, source_roots: Sequence[str] = (),
             exclude: Sequence[str] = ()) -> dict[str, Any]:
    """What `inv scan --format json` computes, with the roots and excludes it would use."""
    from . import cli, scan as scanner
    from .errors import LoadError
    try:
        doc = cli.read_document(rule_file)
        ns_root = argparse.Namespace(root=[str((root / r).resolve()) for r in source_roots])
        report = scanner.scan(doc, cli._roots(ns_root, rule_file), cli._exclude(argparse.Namespace(exclude=list(exclude)),
                                                                                  rule_file))
    except (LoadError, scanner.ScanConfigError, cli.Usage) as e:
        raise ToolError(str(e)) from e
    data = report.as_dict()
    for f in data["findings"]:
        f["root"] = _rel(f.get("root"), root)
        f["id"] = finding_id(f)
    kinds = {f["kind"] for f in data["findings"]}
    if kinds & {"bypass", "forbidden", "missing"}:
        verdict, code = "fail", cli.FAILED
    elif report.ablated_enforce:
        verdict, code = "ablated", cli.ABLATED
    elif "unresolved" in kinds:
        verdict, code = "unresolved", cli.UNRESOLVED
    else:
        verdict, code = "pass", cli.OK
    return {"rule_file": _rel(str(rule_file), root), "verdict": verdict, "passed": verdict == "pass",
            "exit_code": code, **data}


def tool_scan(server_root: pathlib.Path, default_rules: pathlib.Path | None, args: dict[str, Any]) -> dict[str, Any]:
    root = _root(server_root, args)
    return run_scan(root, _rule_file(root, args, default_rules, server_root), _source_roots(server_root, root, args),
                    _strs(args, "exclude"))


# --- check and status (child processes: they import the app) ------------------------------------
def _inv(root: pathlib.Path, *argv: str) -> tuple[int, str, str]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for k in ("INV_ALLOW_WEAKENING", "INV_ABLATE", "INV_ABLATE_TESTING", "INV_ABLATE_TRACE"):
        env.pop(k, None)                            # a reviewer's switch and test-only ablation never leak in
    try:
        p = subprocess.run([sys.executable, "-m", "invariants.cli", *argv], cwd=root, env=env,
                           capture_output=True, text=True, timeout=CHILD_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise ToolError(f"inv {argv[0]} took longer than {CHILD_TIMEOUT}s") from e
    return p.returncode, p.stdout, p.stderr


STEP = re.compile(r"^  (ok|FAIL|skip)\s+([^:]+?)(?::\s*(.*))?$")


def tool_check(server_root: pathlib.Path, default_rules: pathlib.Path | None, args: dict[str, Any]) -> dict[str, Any]:
    root = _root(server_root, args)
    rule_file = _rule_file(root, args, default_rules, server_root)
    argv = ["check", str(rule_file)]
    for r in _source_roots(server_root, root, args):
        argv += ["--root", r]
    for g in _strs(args, "exclude"):
        argv += ["--exclude", g]
    code, out, err = _inv(root, *argv)                 # never --relock, --base or --allow-weakening
    steps: list[dict[str, Any]] = []
    for line in out.splitlines():
        m = STEP.match(line)
        if m:
            steps.append({"step": m.group(2).strip(), "result": m.group(1).lower(), "detail": [m.group(3)] if m.group(3) else []})
        elif line.startswith("        ") and steps:
            steps[-1]["detail"].append(line.strip())
    return {"rule_file": _rel(str(rule_file), root), "passed": code == 0, "exit_code": code, "steps": steps,
            "output": out.strip(), "stderr": err.strip()}


def tool_status(server_root: pathlib.Path, default_rules: pathlib.Path | None, args: dict[str, Any]) -> dict[str, Any]:
    root = _root(server_root, args)
    rule_file = _rule_file(root, args, default_rules, server_root)
    code, out, err = _inv(root, "status", str(rule_file), "--json")
    if code != 0:
        raise ToolError(f"inv status exited {code}: {(err or out).strip()}")
    try:
        data = json.loads(out)
    except ValueError as e:
        raise ToolError(f"inv status gave no JSON: {out.strip()}") from e
    return {"rule_file": _rel(str(rule_file), root), "status": data}


# --- explain ------------------------------------------------------------------------------------
FIXES = {
    "bypass": ["Route the call through one of the doors ({doors}) instead of calling the sink from {owner}.",
               "If this path is a legitimate, known back door, tell a person: only they may add {owner} to "
               "mediation.allow. That is a weakening, which `inv lock` refuses without --accept-weakening.",
               "If the doors themselves are wrong, the rule changes, and a person reviews that rule edit."],
    "forbidden": ["Remove the idiom ({code}): call a door ({doors}) the ordinary way, so the scan can follow it.",
                  "A getattr, .__wrapped__ or monkeypatch on a sink or door hides a path; there is no safe "
                  "variant of it to allow."],
    "unresolved": ["Make the call statically resolvable: a direct call, a constructor call, or a receiver "
                   "annotated with its class (see docs/scan.md, Method calls).",
                   "An unresolved call is never counted as passed: `inv check` keeps failing until it resolves."],
    "missing": ["A sink, door or allow entry the rule names does not exist in the scanned code, or an allow "
                "entry no longer reaches a sink. If the code was renamed, a person updates the rule.",
                "Check the scan roots: the module may be outside them ([tool.invariants.scan] roots).",
                "A stale allow entry (missing/allow-unused) is deleted, which is a tightening."],
}
MEANING = {
    "bypass": "a path of call edges from an entrypoint reaches the sink without passing any door",
    "forbidden": "the code uses an idiom that reaches a sink or door around the call graph",
    "unresolved": "the scan cannot follow a call here, so it cannot prove the path passes a door",
    "missing": "something the rule names is not in the scanned code",
}


def _rule_doc(rule_file: pathlib.Path, rule_id: str) -> dict[str, Any] | None:
    from . import cli
    doc = cli.read_document(rule_file)
    return next((r for r in doc.get("rules") or [] if isinstance(r, dict) and r.get("id") == rule_id), None)


def _rule_summary(raw: dict[str, Any]) -> dict[str, Any]:
    med = raw.get("mediation") or {}
    as_list = (lambda v: [v] if isinstance(v, str) else list(v or []))
    door = med.get("door", (raw.get("subject") or {}).get("target") if med.get("sink") else None)
    return {"id": raw.get("id"), "status": raw.get("status"), "statement": raw.get("statement"),
            "subject": raw.get("subject"), "bad": raw.get("bad"), "sinks": as_list(med.get("sink")),
            "doors": as_list(door), "allow": as_list(med.get("allow")),
            "falsified_by": raw.get("falsified_by") or [], "kind": "path law" if med.get("sink") else "value law"}


def tool_explain(server_root: pathlib.Path, default_rules: pathlib.Path | None, args: dict[str, Any]) -> dict[str, Any]:
    root = _root(server_root, args)
    rule_file = _rule_file(root, args, default_rules, server_root)
    ident = args.get("id")
    if not isinstance(ident, str) or not ident:
        raise ToolError("explain needs an id: a rule id (R-1) or a finding id from scan (R-1:bypass:app/x.py:12)")
    rule_id = ident.split(":", 1)[0]
    raw = _rule_doc(rule_file, rule_id)
    if raw is None:
        raise ToolError(f"no rule {rule_id!r} in {_rel(str(rule_file), root)}")
    rule = _rule_summary(raw)
    out: dict[str, Any] = {"rule": rule}
    if rule["kind"] == "value law":
        out["explanation"] = (f"{rule_id} is a value law: {rule['statement'] or 'its bad condition'} is checked on "
                              f"the arguments and result of {(raw.get('subject') or {}).get('target') or 'its target'} "
                              f"at run time by its guard; the path scan does not check it.")
        out["fix_options"] = ["Keep the guard on the target and make the code satisfy the condition.",
                              "If the rule itself is wrong, a person edits it; a relaxed condition is a weakening "
                              "the lock refuses without --accept-weakening."]
        return out
    scan = run_scan(root, rule_file, _source_roots(server_root, root, args), _strs(args, "exclude"))
    findings = [f for f in scan["findings"] if f["rule"] == rule_id]
    out["scan_result"] = scan["rules"].get(rule_id, {}).get("result", "not_scanned")
    out["not_scanned"] = scan["not_scanned"].get(rule_id)
    if ":" in ident:
        hit = next((f for f in findings if f["id"] == ident), None)
        if hit is None:
            raise ToolError(f"no finding {ident!r} in a fresh scan (fixed already, or the code moved: run scan)")
        out["finding"] = hit
        out["path"] = hit.get("path") or []
        fill = {"doors": ", ".join(rule["doors"]) or "none declared", "owner": hit.get("owner") or "this function",
                "code": hit.get("code")}
        where = f"{hit.get('file')}:{hit.get('line')}" if hit.get("file") else "the scanned code"
        chain = " -> ".join(out["path"]) if out["path"] else None
        said = str(rule["statement"] or "every call to its sinks passes a door").strip().rstrip(".")
        detail = f": {chain}." if chain else f". {str(hit.get('message') or '').rstrip('.')}."
        out["explanation"] = f"{rule_id} says: {said}. At {where}, {MEANING.get(hit['kind'], hit['kind'])}{detail}"
        out["fix_options"] = [s.format(**fill) for s in FIXES.get(hit["kind"], [])]
    else:
        out["findings"] = findings
        out["explanation"] = (f"{rule_id} is a path law: every call path from an entrypoint to "
                              f"{', '.join(rule['sinks'])} must pass a door ({', '.join(rule['doors']) or 'none'})"
                              + (f"; frozen back doors: {', '.join(rule['allow'])}" if rule["allow"] else "")
                              + f". The scan says: {out['scan_result']}"
                              + (f" with {len(findings)} finding(s); explain each by its id." if findings else "."))
        out["fix_options"] = sorted({s.format(doors=", ".join(rule["doors"]), owner=f.get("owner") or "the caller",
                                              code=f.get("code")) for f in findings for s in FIXES.get(f["kind"], [])})
    return out


# --- propose_rules ------------------------------------------------------------------------------
def tool_propose_rules(server_root: pathlib.Path, default_rules: pathlib.Path | None,
                       args: dict[str, Any]) -> dict[str, Any]:
    """`inv init`'s discovery, rendered into a temporary directory and checked by the scan."""
    from . import bootstrap, cli, doors, graphify
    from .errors import LoadError
    root = _root(server_root, args)
    limit = args.get("max_doors", 5)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ToolError("max_doors must be a non-negative integer")
    target = root / RULE_FILES[0]                        # where `inv init` would write; never written here
    given = _source_roots(server_root, root, args)
    graph_arg = args.get("graphify")
    graph_path = (str(_inside(server_root, root, graph_arg, "graphify")) if isinstance(graph_arg, str) and graph_arg
                  else None)
    exclude = _strs(args, "exclude")
    try:
        roots = [pathlib.Path(r) for r in given] or cli.source_dirs(target) or [root]
        for r in roots:
            if not r.exists():
                raise ToolError(f"source root {r} does not exist")
        found = bootstrap.discover(root, roots, exclude)
        graph = None if args.get("no_graphify") else graphify.find(root, graph_path)
        scan_roots = cli._roots(argparse.Namespace(root=given), target)
        scan_exclude = cli._exclude(argparse.Namespace(exclude=exclude), target)
        found.doors = doors.discover(scan_roots, scan_exclude, limit=limit, graph=graph, project=root)
        text = bootstrap.render(found)
        with tempfile.TemporaryDirectory(prefix="inv-propose-") as d:
            draft = pathlib.Path(d) / RULE_FILES[0]
            draft.write_text(text)
            doc = cli.read_document(draft)
            from . import scan as scanner
            report = scanner.scan(doc, scan_roots, scan_exclude)
    except (LoadError, graphify.GraphifyError, cli.Usage) as e:
        raise ToolError(str(e)) from e
    rows = report.as_dict()["rules"]
    door_rules = []
    for dr in found.doors.doors + found.doors.dropped:
        written = dr in found.doors.doors and dr.active
        door_rules.append({"id": dr.rid, "resource": dr.resource, "doors": dr.doors, "sinks": dr.sinks,
                           "callers": dr.callers, "allow": dr.allow, "back_doors": dr.back_doors,
                           "drafted": "rule" if written else ("commented out" if dr in found.doors.doors else
                                                              "beyond max_doors"),
                           "why_not_drafted": dr.why_inactive or ("" if dr in found.doors.doors else
                                                                  f"only the first {limit} are drafted"),
                           "scan": rows.get(dr.rid, {"result": "not drafted"})})
    contracts = [{"model": f"{m.module}:{m.name}", "constraints": m.constraints} for m in found.models if m.constraints]
    guards = [{"function": f"{g.module}.{g.qualname}", "clauses": [{"bad": c, "message": msg} for c, msg in g.clauses],
               "not_translated": g.skipped} for g in found.guards if g.clauses]
    existing = next((n for n in RULE_FILES if (root / n).exists()), None)
    return {"written": False,
            "note": ("A draft only: nothing was written to the project. A person reviews it, writes "
                     "invariants.yaml, and runs `inv lock`; the agent never approves its own rules."
                     + (f" The project already has {existing}; `inv init` would refuse to overwrite it." if existing else "")),
            "door_rules": door_rules, "no_door": found.doors.no_door, "contracts": contracts,
            "suggested_guards": guards, "summary": bootstrap.summary(found),
            "scan": {"verdict": "fail" if any(r["result"] == "fail" for r in rows.values()) else "pass",
                     "rules": rows, "findings": [dict(f.as_dict(), id=finding_id(f.as_dict())) for f in report.findings]},
            "rule_file": text}


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
        from . import __version__
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


if __name__ == "__main__":
    raise SystemExit(main())
