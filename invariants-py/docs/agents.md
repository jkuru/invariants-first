# Agents: understand, propose, verify, approve, lock

A coding agent can help write the laws, but it cannot be the one who accepts them. This page is the
flow that keeps those two jobs apart, and the two read-only MCP servers that give the agent what it
needs to do its half.

```text
understand ──> propose ──> verify ──> human approval ──> lock
 (graphify-mcp,   (propose_rules,   (the scan, in     (a person edits     (`inv lock` by
  inv-mcp)         a dry run)        propose_rules     invariants.yaml)    that person; CI
                                     and `inv check`)                      runs `inv check`)
```

## The rules

- **The agent never approves its own rules.** It proposes; a person reads the proposal and writes
  `invariants.yaml`. A rule an agent wrote and accepted is a rule nobody reviewed.
- **The agent never edits `mediation.allow` or `invariants.lock`.** `allow` is a list of frozen back
  doors, and adding to it is a weakening. The lock is written only by `inv lock` and
  `inv check --relock`, which refuse a weakening unless a person passes `--accept-weakening`.
  `inv hook pretool` blocks the agent's direct edits of the lock, any command that accepts a
  weakening, and an edit of the `pyproject.toml` settings the scan roots come from
  (`[tool.invariants]` and `[tool.setuptools.packages]`, whose `find.where` is a default root)
  ([start.md](start.md#4-make-the-check-unskippable-5-minutes)). A changed root set is a weakening
  in the lock either way.
- **`inv check` never reads the graph or calls a model.** The graph and the agent only suggest. What
  CI enforces is decided by the rule file, the lock and the source, read the same way on every run.

## 1. Understand

Two MCP servers, both read-only, run over stdio:

- **Graphify** (`graphify-mcp`, from `pip install 'graphifyy[mcp]'`) answers questions about the code:
  the path from a new tool to the gateway, what a change touches, which functions sit together.
- **invariants** (`inv-mcp`, installed with this package; `inv mcp [--root DIR] [rulefile]` runs the same server)
  answers questions about the laws: what each rule says, whether every path passes its door, and
  why a finding fails.

A project-level `.mcp.json` with both:

```json
{
  "mcpServers": {
    "graphify": {"command": "graphify-mcp", "args": ["graphify-out/graph.json"]},
    "inv": {"command": "inv-mcp", "args": ["--root", "."]}
  }
}
```

`inv-mcp` speaks MCP protocol version 2025-06-18 (JSON-RPC 2.0, one message per line) and needs
nothing beyond the standard library. Its tools:

| Tool | What it returns |
|---|---|
| `scan` | `inv scan` as data: the verdict (`pass`, `fail`, `unresolved`, `ablated`), each rule's result, and every finding with its kind, code, file, line, call path, owner and an `id` |
| `check` | `inv check` without `--relock`: one verdict and the result of each step (rules, lock, guards, scan) |
| `status` | `inv status --json`: the standing of each rule and why it is not `held` |
| `explain` | for a rule id or a finding id: the rule's statement, its doors and sinks, the path, and the fix options in plain words |
| `propose_rules` | `inv init`'s discovery as a dry run (see below) |

Every tool is marked `readOnlyHint`. None of them writes the rule file, the lock or `allow`; `check`
and `status` import the app in a child process with bytecode writing off, so an edited module is
imported fresh on each call and the project tree is left as it was. The server never passes
`--relock`, `--base`, `--allow-weakening` or `INV_ALLOW_WEAKENING` on.

Every path a tool argument names (`root`, `rules`, `source_roots`, `graphify`) must resolve inside the
server's `--root`, after following symlinks; anything else (an absolute path elsewhere, `..`, a
symlink out) is a tool error (`isError: true`), since `check` and `status` import the code they find.
A filesystem root (`/`) is never a project root. The same holds for the directories the project's
`pyproject.toml` names for scanning or importing, `[tool.invariants.scan] roots` and
`[tool.setuptools.packages.find] where`: one outside `--root` is a tool error naming the setting.
The files a rule file inside `--root` names are confined too: `check` and `status` run `inv` with
`INV_CONFINE_ROOT` set to the server's `--root`, and a contract `ref` or an enforcer `artifact` that
resolves outside it (`ref: ../outside_api.json`, or a symlink out) is refused before it is read, so
its contents never reach a lock diff or an error in the tool's output. Plain `inv check` does not
set it and reads a ref wherever the rule file points.
The server's own `--rules`, chosen by whoever starts it, may sit anywhere (with its settings and refs).

`explain` is the tool an agent should reach for when `inv check` fails. For a bypass it names the
door to call instead, and says that adding the caller to `allow` is a person's decision, not a fix:

```text
LEDGER says: Balances change only through a transfer. At bank/promo.py:4, a path of call edges
from an entrypoint reaches the sink without passing any door: bank.promo.bonus ->
bank.store.write_balance.
fix options:
  Route the call through one of the doors (py:bank.service.apply_transfer) instead of calling the
  sink from bank.promo.bonus.
  If this path is a legitimate, known back door, tell a person: only they may add bank.promo.bonus
  to mediation.allow. That is a weakening, which `inv lock` refuses without --accept-weakening.
  If the doors themselves are wrong, the rule changes, and a person reviews that rule edit.
```

## 2. Propose

`propose_rules` runs what `inv init` runs (pydantic contracts, door discovery with the Graphify
hints when `graphify-out/graph.json` exists, function preconditions) and renders the rule file into
a temporary directory, never into the project. It returns each proposed door rule with its doors,
sinks, the callers that already go through the doors, the back doors it would freeze in `allow`, and
why a rule was left commented out, plus the full rule-file text. The agent's job ends with showing
that proposal to a person, ideally with the reasons in its own words ("these 3 tools reach the
ledger only through `BillingService`; `admin.goodwill_refund` is a back door that would be frozen").

## 3. Verify

Each proposed rule comes back already checked by the real scan (`scan` in the result: `pass`, or the
findings that would make it fail). A suggestion from the graph or the agent that the scan cannot
confirm is not a rule. After a person writes the file, `check` (or `inv check` itself) shows whether
the whole set holds.

## 4. Human approval

A person reads the proposal, decides which laws are worth keeping, edits `invariants.yaml`, and owns
the result. The agent may draft the edit for them to review, but the decision, and any
`--accept-weakening`, is theirs. For value laws, the falsifier tests that `pytest --inv-ablate`
proves load-bearing are part of what they review.

## 5. Lock

The person runs `inv lock` and commits `invariants.yaml` and `invariants.lock` together. From then on
CI runs `inv check --base origin/main`, pre-commit runs `inv check --relock`, and the agent's hooks run
`inv hook pretool` and `inv hook stop`. A later change that weakens a law fails until a person
accepts it, whoever wrote the change.
