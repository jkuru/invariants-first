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
# The package is split by seam:
# - confine.py: tool arguments and their confinement to --root (ToolError, _inside, _rule_file, ...)
# - tools.py: scan, check, status and explain
# - propose.py: propose_rules
# - protocol.py: the tool table, the JSON-RPC Server, serve and main (`python -m invariants.mcp`: __main__.py)
from __future__ import annotations

from .confine import ToolError  # noqa: F401
from .propose import tool_propose_rules  # noqa: F401
from .protocol import TOOLS, Server, main, serve  # noqa: F401
from .tools import tool_check, tool_explain, tool_scan, tool_status  # noqa: F401
