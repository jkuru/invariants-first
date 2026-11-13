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
# The module is a package split by seam; every name the single mcp.py had is re-exported here:
# - confine.py: tool arguments and their confinement to --root (ToolError, _inside, _rule_file, ...)
# - tools.py: scan, check, status and explain
# - propose.py: propose_rules
# - protocol.py: the tool table, the JSON-RPC Server, serve and main (`python -m invariants.mcp`: __main__.py)
from __future__ import annotations

from .confine import (RULE_FILES, ToolError, _inside, _opt_bool, _opt_str, _rel, _root, _rule_file,  # noqa: F401
                      _settings_inside, _source_roots, _strs)
from .propose import tool_propose_rules  # noqa: F401
from .protocol import (_PROJECT, _ROOTS, INSTRUCTIONS, INTERNAL_ERROR, INVALID_PARAMS,  # noqa: F401
                       INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR, PROTOCOL_VERSION, READ_ONLY, TOOLS, Server,
                       Tool, _error, _result, main, serve, tools_list)
from .tools import (CHILD_TIMEOUT, FIXES, MEANING, STEP, _inv, _rule_doc, _rule_summary, finding_id,  # noqa: F401
                    run_scan, tool_check, tool_explain, tool_scan, tool_status)

# Classes keep the module name they had before the split (pickles and reprs name it).
for _obj in list(globals().values()):
    if isinstance(_obj, type) and (_obj.__module__ == __name__ or _obj.__module__.startswith(__name__ + ".")):
        _obj.__module__ = 'invariants.mcp'
del _obj
