"""The tools scan, check, status and explain (propose_rules is in propose.py)."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any, Sequence

import contextlib

from ..confinement import CONFINE_ENV, confining
from .confine import ToolError, _import_paths, _imports, _opt_str, _rel, _root, _rule_file, _source_roots, _strs

CHILD_TIMEOUT = 600


def finding_id(f: dict[str, Any]) -> str:
    """A stable id for one finding: rule, code, file and line (what `explain` takes)."""
    return f"{f.get('rule')}:{f.get('code')}:{f.get('file') or '-'}:{f.get('line') or 0}"


# --- scan ---------------------------------------------------------------------------------------
def run_scan(root: pathlib.Path, rule_file: pathlib.Path, source_roots: Sequence[str] = (),
             exclude: Sequence[str] = (), server_root: pathlib.Path | None = None) -> dict[str, Any]:
    """What `inv scan --format json` computes, with the roots and excludes it would use. For a rule file
    inside server_root, the walk is confined to it (as `check` is): a symlink under the roots that
    resolves outside is not followed, and is an unresolved finding naming only the link."""
    from .. import project, scan as scanner
    from ..errors import LoadError
    try:
        doc = project.read_document(rule_file)
        ns_root = argparse.Namespace(root=[str((root / r).resolve()) for r in source_roots])
        with confined_scan(server_root, rule_file):
            report = scanner.scan(doc, project._roots(ns_root, rule_file),
                                  project._exclude(argparse.Namespace(exclude=list(exclude)), rule_file))
    except (LoadError, scanner.ScanConfigError, project.Usage) as e:
        raise ToolError(str(e)) from e
    data = report.as_dict()
    for f in [*data["findings"], *data.get("accepted", [])]:
        f["root"] = _rel(f.get("root"), root)
        f["id"] = finding_id(f)
    kinds = {f["kind"] for f in data["findings"]}
    if kinds & {"bypass", "forbidden", "missing"}:
        verdict, code = "fail", project.FAILED
    elif report.ablated_enforce:
        verdict, code = "ablated", project.ABLATED
    elif "unresolved" in kinds:
        verdict, code = "unresolved", project.UNRESOLVED
    else:
        verdict, code = "pass", project.OK
    return {"rule_file": _rel(str(rule_file), root), "verdict": verdict, "passed": verdict == "pass",
            "exit_code": code, **data}


def confined_scan(server_root: pathlib.Path | None, rule_file: pathlib.Path) -> contextlib.AbstractContextManager[None]:
    """confining(server_root) for a rule file inside it; nothing for the server's own --rules outside it,
    whose roots are its operator's (the same split as INV_CONFINE_ROOT for `check` and `status`)."""
    if server_root is None or not rule_file.resolve().is_relative_to(server_root):
        return contextlib.nullcontext()
    return confining(server_root)


def tool_scan(server_root: pathlib.Path, default_rules: pathlib.Path | None, args: dict[str, Any]) -> dict[str, Any]:
    root = _root(server_root, args)
    return run_scan(root, _rule_file(root, args, default_rules, server_root), _source_roots(server_root, root, args),
                    _strs(args, "exclude"), server_root)


# --- check and status (child processes: they import the app) ------------------------------------
def _inv(root: pathlib.Path, server_root: pathlib.Path, rule_file: pathlib.Path, *argv: str) -> tuple[int, str, str]:
    """`inv ARGV` in a child process at root. Contract refs and enforcer artifacts of a rule file inside
    the server's --root are confined to it (INV_CONFINE_ROOT): the child refuses to read, and so to
    echo, a file outside. The server's own --rules outside --root is the operator's, and so are its refs."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for k in ("INV_ALLOW_WEAKENING", "INV_ABLATE", "INV_ABLATE_TESTING", "INV_ABLATE_TRACE",
              "INV_DECISION_TRACE", CONFINE_ENV):
        env.pop(k, None)                            # a reviewer's switch and test-only ablation never leak in
    if rule_file.resolve().is_relative_to(server_root):
        env[CONFINE_ENV] = str(server_root)
    try:
        # -P: root is the cwd, and its own yaml.py or tomllib.py must not shadow the deciding process's
        p = subprocess.run([sys.executable, "-P", "-m", "invariants.cli", *argv], cwd=root, env=env,
                           capture_output=True, text=True, timeout=CHILD_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise ToolError(f"inv {argv[0]} took longer than {CHILD_TIMEOUT}s") from e
    return p.returncode, p.stdout, p.stderr


def _import_options(server_root: pathlib.Path, root: pathlib.Path, args: dict[str, Any]) -> list[str]:
    """--path=DIR and --import=MODULE for the child, from the path and import arguments (each path inside
    the server's --root, each module a dotted name; the = form, so no value is read as an option)."""
    return ([f"--path={p}" for p in _import_paths(server_root, root, args)]
            + [f"--import={m}" for m in _imports(args)])


STEP = re.compile(r"^  (ok|FAIL|skip)\s+([^:]+?)(?::\s*(.*))?$")


def tool_check(server_root: pathlib.Path, default_rules: pathlib.Path | None, args: dict[str, Any]) -> dict[str, Any]:
    root = _root(server_root, args)
    rule_file = _rule_file(root, args, default_rules, server_root)
    argv = ["check", str(rule_file)]
    for r in _source_roots(server_root, root, args):
        argv += ["--root", r]
    for g in _strs(args, "exclude"):
        argv += ["--exclude", g]
    argv += _import_options(server_root, root, args)
    code, out, err = _inv(root, server_root, rule_file, *argv)   # never --relock, --base or --allow-weakening
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
    code, out, err = _inv(root, server_root, rule_file, "status", str(rule_file), "--json",
                          *_import_options(server_root, root, args))
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
                   "An unresolved call is never counted as passed: `inv check` keeps failing until it resolves.",
                   "If a person reads the site and finds it cannot reach the sink, only they may accept it in "
                   "mediation.allow_unresolved ({accept}, with a why). That is a weakening, which `inv lock` "
                   "refuses without --accept-weakening."],
    "missing": ["A sink, door or allow entry the rule names does not exist in the scanned code, or an allow "
                "entry no longer reaches a sink. If the code was renamed, a person updates the rule.",
                "Check the scan roots: the module may be outside them ([tool.invariants.scan] roots).",
                "A stale allow entry (missing/allow-unused) or allow_unresolved entry "
                "(missing/allow-unresolved-unused) is deleted, which is a tightening."],
}
MEANING = {
    "bypass": "a path of call edges from an entrypoint reaches the sink without passing any door",
    "forbidden": "the code uses an idiom that reaches a sink or door around the call graph",
    "unresolved": "the scan cannot follow a call here, so it cannot prove the path passes a door",
    "missing": "something the rule names is not in the scanned code",
}


def _accept_entry(f: dict[str, Any]) -> str:
    """The allow_unresolved entry that would accept an unresolved finding, less the why a person writes."""
    if f.get("kind") != "unresolved" or not f.get("sha"):
        return "an entry naming the site"
    return f'{{at: "{f.get("file")}:{f.get("line")}:{f.get("col")}", code: {f.get("code")}, sha: {f.get("sha")}}}'


def _rule_doc(rule_file: pathlib.Path, rule_id: str) -> dict[str, Any] | None:
    from .. import project
    doc = project.read_document(rule_file)
    return next((r for r in doc.get("rules") or [] if isinstance(r, dict) and r.get("id") == rule_id), None)


def _rule_summary(raw: dict[str, Any]) -> dict[str, Any]:
    med = raw.get("mediation") or {}
    as_list = (lambda v: [v] if isinstance(v, str) else list(v or []))
    door = med.get("door", (raw.get("subject") or {}).get("target") if med.get("sink") else None)
    sink = med.get("sink")
    pairs = {s: as_list(d) for s, d in sink.items()} if isinstance(sink, dict) else None
    if pairs is not None:                              # each sink with its own doors (fix round 30)
        door = list(dict.fromkeys(d for ds in pairs.values() for d in ds))
    allow = med.get("allow")                           # round 32: each back door with the sinks it excuses
    allow_to = {str(a): as_list(s) for a, s in allow.items()} if isinstance(allow, dict) else None
    return {"id": raw.get("id"), "status": raw.get("status"), "statement": raw.get("statement"),
            "subject": raw.get("subject"), "bad": raw.get("bad"), "sinks": as_list(sink),
            **({"pairs": pairs} if pairs is not None else {}),
            "doors": as_list(door), "allow": list(allow_to) if allow_to is not None else as_list(allow),
            **({"allow_to": allow_to} if allow_to is not None else {}),
            "allow_unresolved": list(med.get("allow_unresolved") or raw.get("allow_unresolved") or []),
            "falsified_by": raw.get("falsified_by") or [], "kind": "path law" if med.get("sink") else "value law"}


def tool_explain(server_root: pathlib.Path, default_rules: pathlib.Path | None, args: dict[str, Any]) -> dict[str, Any]:
    root = _root(server_root, args)
    rule_file = _rule_file(root, args, default_rules, server_root)
    ident = _opt_str(args, "id")
    if not ident:
        raise ToolError("explain needs an id: a rule id (R-1) or a finding id from scan (R-1:bypass:app/x.py:12)")
    rule_id = ident.split(":", 1)[0]
    from ..errors import LoadError
    try:
        raw = _rule_doc(rule_file, rule_id)
    except LoadError as e:
        raise ToolError(str(e)) from e
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
    scan = run_scan(root, rule_file, _source_roots(server_root, root, args), _strs(args, "exclude"), server_root)
    findings = [f for f in scan["findings"] if f["rule"] == rule_id]
    accepted = [f for f in scan.get("accepted", []) if f["rule"] == rule_id]
    if accepted:
        out["accepted"] = accepted                     # reviewed unresolved sites: shown, not failing
    out["scan_result"] = scan["rules"].get(rule_id, {}).get("result", "not_scanned")
    out["not_scanned"] = scan["not_scanned"].get(rule_id)
    if ":" in ident:
        hit = next((f for f in [*findings, *accepted] if f["id"] == ident), None)
        if hit is None:
            raise ToolError(f"no finding {ident!r} in a fresh scan (fixed already, or the code moved: run scan)")
        out["finding"] = hit
        out["path"] = hit.get("path") or []
        fill = {"doors": ", ".join(rule["doors"]) or "none declared", "owner": hit.get("owner") or "this function",
                "code": hit.get("code"), "accept": _accept_entry(hit)}
        where = f"{hit.get('file')}:{hit.get('line')}" if hit.get("file") else "the scanned code"
        chain = " -> ".join(out["path"]) if out["path"] else None
        said = str(rule["statement"] or "every call to its sinks passes a door").strip().rstrip(".")
        detail = f": {chain}." if chain else f". {str(hit.get('message') or '').rstrip('.')}."
        out["explanation"] = f"{rule_id} says: {said}. At {where}, {MEANING.get(hit['kind'], hit['kind'])}{detail}"
        if hit.get("why") is not None:
            out["explanation"] += (f" A person accepted this site after review (mediation.allow_unresolved): "
                                   f"{hit['why']}. It is shown, and does not fail the rule.")
        out["fix_options"] = [s.format(**fill) for s in FIXES.get(hit["kind"], [])]
    else:
        out["findings"] = findings
        out["explanation"] = (f"{rule_id} is a path law: every call path from an entrypoint to "
                              f"{', '.join(rule['sinks'])} must pass a door ({', '.join(rule['doors']) or 'none'})"
                              + ("; each sink through its own doors only: " + "; ".join(
                                  f"{s} through {' or '.join(d)}" for s, d in rule["pairs"].items())
                                 if "pairs" in rule else "")
                              + ("; frozen back doors, each for the sinks it lists only: " + "; ".join(
                                  f"{a} to {', '.join(s)}" for a, s in rule["allow_to"].items())
                                 if rule.get("allow_to") else
                                 f"; frozen back doors: {', '.join(rule['allow'])}" if rule["allow"] else "")
                              + f". The scan says: {out['scan_result']}"
                              + (f" with {len(findings)} finding(s); explain each by its id." if findings else "."))
        if accepted:
            out["explanation"] += (f" {len(accepted)} unresolved site(s) are accepted after review in "
                                   f"mediation.allow_unresolved (shown in accepted, not failing).")
        out["fix_options"] = sorted({s.format(doors=", ".join(rule["doors"]), owner=f.get("owner") or "the caller",
                                              code=f.get("code"), accept=_accept_entry(f))
                                     for f in findings for s in FIXES.get(f["kind"], [])})
    return out
