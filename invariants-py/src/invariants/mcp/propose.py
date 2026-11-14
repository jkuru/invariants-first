"""The tool propose_rules: `inv init`'s discovery as a dry run, checked by the scan."""
from __future__ import annotations

import argparse
import pathlib
import tempfile
from typing import Any

from .confine import (RULE_FILES, ToolError, _inside, _opt_bool, _opt_str, _root, _settings_inside, _source_roots,
                      _strs)
from .tools import finding_id


def tool_propose_rules(server_root: pathlib.Path, default_rules: pathlib.Path | None,
                       args: dict[str, Any]) -> dict[str, Any]:
    """`inv init`'s discovery, rendered into a temporary directory and checked by the scan."""
    from .. import bootstrap, cli, doors, graphify
    from ..confinement import confining
    from ..errors import LoadError
    root = _root(server_root, args)
    limit = args.get("max_doors")
    limit = 5 if limit is None else limit
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ToolError("max_doors must be a non-negative integer")
    # where `inv init` would write; never written here. Resolved inside --root: its pyproject.toml settings
    # (source_dirs, exclude) are read beside it, so a symlink to a rule file outside never leads there.
    target = _inside(server_root, root, RULE_FILES[0], "rule file")
    given = _source_roots(server_root, root, args)
    graph_arg = _opt_str(args, "graphify")
    graph_path = str(_inside(server_root, root, graph_arg, "graphify")) if graph_arg else None
    no_graphify = _opt_bool(args, "no_graphify")
    exclude = _strs(args, "exclude")
    _settings_inside(server_root, target)
    with confining(server_root):     # the walk of the roots stops at a symlink out of --root
        try:
            roots = [pathlib.Path(r) for r in given] or cli.source_dirs(target) or [root]
            for r in roots:
                if not r.exists():
                    raise ToolError(f"source root {r} does not exist")
            scan_roots = cli._roots(argparse.Namespace(root=given), target)
            for r in [*roots, *map(pathlib.Path, scan_roots)]:
                _inside(server_root, root, str(r), "source root")
            found = bootstrap.discover(root, roots, exclude)
            graph = None if no_graphify else graphify.find(root, graph_path)
            if graph is not None:                            # the default graphify-out/graph.json may be a symlink
                _inside(server_root, root, str(graph), "graphify graph")
            scan_exclude = cli._exclude(argparse.Namespace(exclude=exclude), target)
            found.doors = doors.discover(scan_roots, scan_exclude, limit=limit, graph=graph, project=root)
            text = bootstrap.render(found)
            with tempfile.TemporaryDirectory(prefix="inv-propose-") as d:
                draft = pathlib.Path(d) / RULE_FILES[0]
                draft.write_text(text, encoding="utf-8")
                doc = cli.read_document(draft)
                from .. import scan as scanner
                report = scanner.scan(doc, scan_roots, scan_exclude)
        except (LoadError, graphify.GraphifyError, cli.Usage) as e:
            raise ToolError(str(e)) from e
    rows = report.as_dict()["rules"]
    door_rules = []
    for dr in found.doors.doors + found.doors.dropped:
        written = dr in found.doors.doors and dr.active
        door_rules.append({"id": dr.rid, "resource": dr.resource, "doors": dr.doors, "sinks": dr.sinks,
                           **({"pairs": dr.pairs} if dr.pairs is not None else {}),
                           "callers": dr.callers, "allow": dr.allow,
                           **({"allow_to": dr.allow_to} if dr.allow_to else {}), "back_doors": dr.back_doors,
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
