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
- **The agent never edits `mediation.allow`, `allow_unresolved` or `invariants.lock`.** `allow` is a
  list of frozen back doors and `allow_unresolved` a list of unresolved sites a person has read;
  adding to either is a weakening. The lock is written only by `inv lock`, which refuses a weakening
  unless a person passes `--accept-weakening`, and by `inv check --relock`, which never re-pins a
  weakening (it fails and names `inv lock --accept-weakening`). On `inv check`,
  `--accept-weakening` only lets `--base` pass a weakening already in the lock.
  `inv hook pretool` blocks the agent's direct edits of the lock, any command that accepts a
  weakening, and an edit of the `pyproject.toml` settings the scan roots come from
  (`[tool.invariants]` and `[tool.setuptools.packages]`, whose `find.where` is a default root),
  and `inv init` with `--root` or `--exclude` in a tree that holds a lock (init writes those to
  `[tool.invariants.scan]`; beside a lock it only prints what to add there by hand)
  ([start.md](start.md#4-make-the-check-unskippable-5-minutes)). A changed root set is a weakening
  in the lock either way. It also blocks the cheap ways around those checks: running a Python
  script inside the lock's directory tree whose text names the lock (or `LOCK_NAME`) or
  `pyproject`, or one that imports such a module (below), `git apply`/`git am` of a patch that
  names either, `git stash pop|apply` of an entry
  that changes either, `git checkout REF -- PATHS` or `git restore --source REF PATHS` whose
  pathspec may match either (`.`, a directory holding it, a glob), `patch` (`gpatch`, `busybox
  patch`) of a patch that names either (read from `-i`, its second operand or a `<` redirect; one
  piped in is blocked, `--dry-run` is not), a git command that moves the tree to a commit whose copy
  of either differs (`git merge REF`, `git cherry-pick REF|A..B`, `git reset --hard|--keep|--merge
  REF`, `git checkout REF` or `git switch REF` without paths, or the start point of a new branch),
  a recursive copy (`cp -r`/`-a`, `rsync -r`/`-a`, `ditto`) into the lock's tree of a directory
  that holds either, and any edit of
  `.claude/settings.json` or `.claude/settings.local.json`, where the hooks themselves are
  configured. The hook is a text check on one command and an early warning, not the gate: a
  determined agent can still get past it. `inv check --base origin/main` in CI is the gate.

  Behind `python FILE`, `python -m MOD`, `uv run ...` and a pytest run (`pytest`, `py.test`,
  `python -m pytest`, `uv run pytest`) the hook also reads the project's own modules: those the
  script imports, transitively (`import a.b`, `from . import x`, each package's `__init__.py`, a
  constant `importlib.import_module("m")`), and for pytest every `conftest.py` in the working
  directory and above it up to the lock's directory, below it and below the directories the
  arguments name (dot-directories, `venv`, `node_modules`, `build`, `dist` and the like left
  out), each `-p` plugin and each `pytest_plugins` entry, and what those import. A module is the
  project's when it is found inside the lock's directory tree (from the script's directory, the
  working directory, the tree or its `src/`); a symlinked one is read where it points. The module
  `python -m MOD` runs (`MOD.py` or `MOD/__main__.py`, under the working directory, its `src/`, the
  tree or its `src/`) is the script itself, read as `python FILE` is. Such an imported
  module is blocked when its text names the lock (or `LOCK_NAME`), or names `pyproject` together
  with `invariants` or `setuptools` (a module that only reads pyproject.toml is ordinary). What it
  does not see: test modules (`test_*.py`; a test that writes the lock in a temporary copy is
  ordinary, and so is one that reads it), a name computed at run time (`import_module(name)`,
  `open("invariants." + ext)`, a path read from a file or the environment), an import it cannot
  resolve to a file in the tree (an installed package, `PYTHONPATH`, a `.pth` file, an editable
  install from elsewhere, a relative import climbing above the tree), plugins named in
  `pytest.ini`, `tox.ini`, `setup.cfg` or `[tool.pytest.ini_options]` (`addopts = -p`,
  `pythonpath`), entry-point plugins, a script run by other tools (`make`, `tox`, `nox`, a shell
  script), and anything past 400 files. Among shell commands it does not see an archive unpacked
  over the tree (`tar -x`, `unzip`, `bsdtar`, `7z x`), `ed`/`ex`/`vim -c` scripts fed a file name
  the shell computes, `git pull` and `git rebase` (what they bring in is fetched or replayed as they
  run), and a write by a program it does not know; the
  clearly-writing ones it can check cheaply (patch, the git commands above, a recursive copy) it
  blocks, the rest are left to `inv check --base` in CI.
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
| `check` | `inv check` without `--relock`: one verdict and the result of each step (rules, lock, guards, scan); `path` and `import` are its `--path` and `--import` |
| `status` | `inv status --json`: the standing of each rule and why it is not `held`; takes `path` and `import` too |
| `explain` | for a rule id or a finding id: the rule's statement, its doors and sinks, the path, and the fix options in plain words |
| `propose_rules` | `inv init`'s discovery as a dry run (see below) |

Every tool is marked `readOnlyHint`. None of them writes the rule file, the lock or `allow`; `check`
and `status` import the app in a child process with bytecode writing off, so an edited module is
imported fresh on each call and the project tree is left as it was. The server never passes
`--relock`, `--base`, `--allow-weakening` or `INV_ALLOW_WEAKENING` on.

Every path a tool argument names (`root`, `rules`, `source_roots`, `path`, `graphify`) must resolve
inside the server's `--root`, after following symlinks; anything else (an absolute path elsewhere,
`..`, a symlink out) is a tool error (`isError: true`), since `check` and `status` import the code
they find. An `import` entry must be a dotted module name (`app.api`): a path, an option (`-x`) or a
relative name is a tool error, and the module it names is confined like any other (below).
A filesystem root (`/`) is never a project root. The same holds for the directories the project's
`pyproject.toml` names for scanning or importing, `[tool.invariants.scan] roots` and
`[tool.setuptools.packages.find] where`: one outside `--root` is a tool error naming the setting.
The files a rule file inside `--root` names are confined too: `check` and `status` run `inv` with
`INV_CONFINE_ROOT` set to the server's `--root`, and a contract `ref` or an enforcer `artifact` that
resolves outside it (`ref: ../outside_api.json`, or a symlink out) is refused before it is read, so
its contents never reach a lock diff or an error in the tool's output. The lock file is read the
same way: an `invariants.lock` that is a symlink resolving outside `--root` is a stale lock naming
only its path, and nothing behind it is read. The modules it names are
confined the same way (a `py:` target, a provider's `from:`, and every `module:attr` contract ref,
pydantic, dataclass, TypedDict or Python, all import through one function): a target, contract or provider module whose file is not inside `--root` (the
standard library and installed packages included: `py:os.environ`, `py:this`) is refused before it
is imported, so its import-time code never runs and none of its objects reach a pin or an error.
The attribute path after the module stays inside `--root` too: it goes only through objects
defined there (`app.mod:os.environ._data` or `py:app.mod.sys.modules` is refused), and a contract
ref never goes through a module object at all (name the module before the colon).
That holds below the top-level package too: while `INV_CONFINE_ROOT` is set, a finder first on
`sys.meta_path` checks every module the child imports before its code runs, and refuses one of the
project's (reached through a directory inside `--root`, such as a symlink `app/leak ->
../../vault`, or a submodule of a package whose `__init__.py` is inside `--root`, whose `__path__`
may have been extended, or of a namespace package (PEP 420, no `__init__.py`) with a directory inside
`--root`, whose portions follow `sys.path`) that resolves, symlinks followed, outside it. So a
namespace package the project shares with an installed distribution cannot load that
distribution's modules under `inv mcp`; run `inv check` yourself for such a project. The standard library and
installed packages the project's code imports are reached from outside `--root` and load as before.
The tools that scan in the server's own process (`scan`, `explain`, `propose_rules`) are confined
the same way for a rule file inside `--root`: the walk of the source roots does not follow a
symlink (to a directory or a file) that resolves outside it. It reads nothing behind the link and
reports it as an unresolved finding, `unresolved/outside-root`, that names only the link's path, so
a rule never passes on code that was skipped. A symlink to a directory elsewhere inside `--root` is
followed as before, and so is everything under plain `inv scan`.
Plain `inv check` does not set it: it reads a ref wherever the rule file points and imports any
module the rule file names. Neither ever writes an object's value into an error: a target or
contract that has no signature or source is named with its type (`a _Environ object`), never its
repr.
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
CI runs `inv check --base origin/main`, pre-commit runs `inv check --relock --fail-on-repin`, and
the agent's hooks run `inv hook pretool` and `inv hook stop`. A later change that weakens a law fails until a person
accepts it, whoever wrote the change.

The code the agent writes cannot switch that off from the inside: `inv check` (and `inv lock` and
the stop hook) imports the app only in a child process, and the process that decides on a weakening
never imports the project, so a module that sets `INV_ALLOW_WEAKENING` or patches `invariants.lock`
at import changes nothing. It trusts the code it imports to describe itself: a
signature or model schema is pinned as the imported code reports it, so a module that lies about
its own schema is believed. That is why the guarded and contract modules, like the rule file, the
lock and the CI workflow, need a code owner (start.md, "Make the check unskippable"; lockfile.md,
"What the lock trusts").
