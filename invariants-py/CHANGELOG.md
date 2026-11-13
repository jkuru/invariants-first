# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Planned as 0.1.0, the first release. Publishing metadata (the final package name, `project.urls`,
authors, PyPI trusted publishing) is not settled yet: see "Before publishing" in
[RELEASING.md](RELEASING.md). Contributors add their entries here.

### Fix round 19 (agent frameworks)

Tool calls, LangGraph and dependency injection for agents. New page:
[docs/agent-frameworks.md](docs/agent-frameworks.md); [docs/models.md](docs/models.md) updated.
Regression tests in `tests/test_integrations_anthropic.py` and the apps' tests; each failed with the
change reverted. The three example apps' CI passes; their rule files and locks did not change.

- **`AnthropicModel(..., tool=reply_tool(name, schema))`: the reply is a tool call.** The request
  carries `tools=[...]` and `tool_choice` (default `TOOL_CHOICE`, `auto` with one call at most: several
  current models, and requests with thinking on, refuse a forced choice), and `complete()` returns the
  input of the model's `tool_use` block as JSON text, so `@inv.model_call` parses and checks
  tool-call arguments exactly as it does a JSON reply. Before, only text blocks were read and a tool
  call was dropped. `reply_tool()` builds the tool from a pydantic model, dataclass, TypedDict or
  `{field: type}` map (its JSON Schema, closed at the top; `strict=True` for strict tool use).
- **Feedback after a rejected tool call is a `tool_result` turn.** The parser error goes back as a
  user turn of `tool_result` blocks, one per `tool_use` id of the previous reply, with
  `is_error: true` (`TOOL_FEEDBACK_TEMPLATE`), after the assistant turn as the API returned it.
- **Replay and recording handle tool calls.** A trace's `reply` may be a list of text and `tool_use`
  blocks; a request records `tools` and `tool_choice` and a replay compares them like every other
  field. `ReplayModel` and `model_for` take `tool=` and `tool_choice=`. Text traces are unchanged.
- **support-agent: the hand-rolled graph runner is gone.** `graph.py` is plain steps
  (`run_ticket` calls them in order); the planner answers by calling a `submit_plan` tool by default
  (`APP_REPLY=tool|json`), with four new hand-written `tool_*.json` traces. A LangGraph `StateGraph`
  (`support_agent.langgraph_agent`, optional extra `langgraph`, pinned to the tested
  `langgraph==1.2.21`; its tests skip without it) runs the model call in the planner node and the
  edge in the act node, with the agent context as LangGraph's runtime context. Found while testing
  it: a checkpointer restores a `Checked` value as a plain dict, which the edge's type gate refuses
  (`TypeError`); the act node hands such a ticket to a person.
- **fact-pipeline: `inv.Dependency` instead of raw contextvars**, bound per pipeline stage, so two
  pipelines in one thread no longer read each other's store and model client.
- `tests/test_graphify.py` reads a graphify snapshot of the old support-agent; its matched-function
  and call counts were lowered to what the rewritten `graph.py` still has.

### Fix round 18: scan

From a third review of `inv scan`. Regression tests: `tests/test_fix_round18_scan.py`; each failed
before its fix. The three example apps' CI passes and their `inv scan` output is unchanged; the lock
format did not change.

Behaviour changes:

- **Any reference into excluded code is reported, not only a direct call (S-12).** Round 17's
  `unresolved/excluded-callee` saw `write(x)` but not the same function reached any other way: an alias
  (`w = write; w(x)`), a callback (`Thread(target=write)`, `map(write, xs)`), a decorator imported from
  an excluded module, `from app.testing.helpers import *`, `getattr(helpers, "write")(x)`, a scanned
  subclass inheriting a method from an excluded class, or a FastAPI router in `app/admin_test.py`
  mounted with `api.include_router(admin_test.router)`. Now every reference from scanned code into an
  excluded module is reported: a name or attribute read as a value (so a base class, a decorator, a
  callback and a getattr() target too) and a star import. The message says `calls`, `refers to` or
  `star-imports`. References inside a door, a sink or code reached only through them are still not
  reported, and an annotation (`h: Helper = x`) is not a reference.
- **An import of excluded code that runs at import time is reported (S-15).** A bare module-level
  `import app.admin_test` (or `from app import admin_test`, or `from app.admin_test import route`)
  with no later use passed, though the module registers routes when imported. Excluded modules are now
  read lightly for import-time effects: a decorated function or class (`@router.post`, `@app.get`) or a
  top-level call or statement other than constants, dataclass, enum and typing definitions and logging
  setup (`if __name__ == "__main__":` blocks are skipped; a module that cannot be parsed counts as
  running code). A module-level import of such a module (or of an excluded package on its path) is
  `unresolved/excluded-callee`: "imports app.admin_test in app/admin_test.py, which runs code at import
  time that the scan does not read". A module-level import of an excluded module with no such effects
  stays quiet, and imports and references inside functions keep the door/sink exemption.
- **Skipped directories inside the project are excluded modules (S-13).** Files under the directories
  the scan always skips (`build`, `dist`, `venv`, `node_modules`, `site-packages`, `__pycache__`,
  dot-directories) were dropped before the exclude logic, so `from app.build.jobs import route;
  route(x)` passed. They are now noted like excluded files, with the directory's name as the pattern
  ("in a 'build' directory, which the scan always skips"), when their module name is importable under
  one of the project's own package names (`app/build/jobs.py` is `app.build.jobs`). Third-party code
  stays quiet: a virtualenv's `site-packages` or a `build/lib` copy is named from the skipped directory
  down (`venv.lib...`), which no project code imports. `iter_sources` (and `inv init`) still leave
  these files out.
- **`super()` fails closed on bases the scan cannot see (S-14).** Round 17's MRO through the scanned
  classes dropped a base it could not resolve, so `class Leaf(Mixin, pick())` or
  `class Leaf(Mixin, getattr(r, "SinkRepo"))` left `super().save(x)` in `Mixin` pointing at
  `Base.save`. Now, when any class in the MRO of the class holding `super()` or of one of its scanned
  subclasses has a base that is neither a scanned class nor a builtin or standard-library
  (or `typing_extensions`) class (a call, a getattr(), `*bases`, an unresolved name, a third-party or
  excluded class), the call is name-matched (unresolved). A module-level alias (`B = SinkRepo`, bound
  once) now resolves to its class, as a base anywhere, so `class Leaf(Mixin, B)` is a bypass. Excluded
  modules are read for their class statements only: one that subclasses the class holding `super()`
  or a subclass of it (`app/testing/leaf.py: class Leaf(Mixin, SinkRepo)`) also makes the call fail
  closed.

### Fix round 18: ablation

- **`--inv-ablate` counts a falsifier as caught only when its rule fired on the control run.** A
  falsifier that reached the guard without ever making it reject (`halve(4)` on a rule about
  negatives) was reported `caught` when something else failed its ablated run: a test asserting
  `not inv.is_ablated("R-1")`, a module-global counter failing on the third run, or a module-scoped
  token bucket that refilled in time to pass the second control. Now the control re-run must also
  record a decision of the rule: a `violation` at a guard, an edge, a middleware or a monitor; at a
  model call a `violation`, a `parse_error` the enforcer retried or rejected, or an abstain it
  escalated. Otherwise the falsifier is `inconclusive (the rule never fired on the control run: the
  test passes whether or not it holds)`. Decisions in subprocesses count too, through the new
  `INV_DECISION_TRACE` file the plugin sets for the control re-run (`inv-mcp` strips it from its
  children, as it strips `INV_ABLATE_TRACE`). Scan falsifiers (the ablated run reached the rule
  through `inv scan`) keep their own check. Every falsifier of the three apps is still caught.
- **`inv.is_ablated()` and `Registry.is_ablated()` no longer count as reaching the rule.** Only the
  library's enforcement points (guards, edges, model calls, monitors, `inv scan`) note reach, through
  the new `Registry.skips(rule_id)`. A test that only asks is `inconclusive (rule not reached)`. The
  `INV_ABLATE_TRACE` lines now say how the rule was reached (`R-1 enforcer`, `R-23 scan`).
- Not checked, on purpose: that the ablated run's failure *is* the missing rejection. An ablated
  rule is never evaluated, so the absence of a violation there is always true, and tying the
  failing assertion to the skipped call would mean tracing the test's control flow. A test that
  swallows the rejection and fails later for an unrelated reason can still read as caught.

### Fix round 18: hook, MCP, replay, schema

- **The pretool hook compares file names in any letter case.** On a case-insensitive filesystem
  (macOS's default) `Invariants.lock`, `INVARIANTS.LOCK` and `PyProject.toml` open the protected
  files, and an edit tool on them, or a shell command naming them (also as a glob), passed. Tool
  paths, shell words and globs are now compared casefolded.
- **It expands braces** (`invariants{,}.lock`, `pyproject{,}.toml`, `inv{a..z}riants.lock`; comma
  and range forms) before matching; an expansion too large to list matches when it holds a stem of
  a protected name.
- **It decodes `$'...'` quoting** (`$'\x69nvariants.\x6cock'`), and in a directory that holds
  `invariants.lock` (or below one) it blocks a write-capable command (`cp`, `mv`, `tee`, `dd`, `rm`,
  `ln`, `install`, `rsync`, `sed -i`, ...) or a redirect whose target file name the shell computes
  (`"$f"`, a backtick), as `printf -v f ...; cp x "$f"` did. A read-only command with `$`, and a
  target whose file name is literal (`"$TMPDIR/out.txt"`), stay allowed.
- **It matches `INV_ALLOW_WEAKENING` after shell unquoting**, so `env INV_ALLOW_WEAK''ENING=1`,
  `export INV_ALLOW_WEAKENIN\G=1` and quoted `X=1 cmd` forms are blocked. The hook stays an early
  warning; `inv check --base` in CI is the gate.
- **`inv-mcp` keeps the roots pyproject.toml names inside `--root`.** `[tool.invariants.scan]
  roots = ["../secret"]` made `scan` read outside the server's root and `check` import from it.
  Those roots and `[tool.setuptools.packages.find] where` are now resolved and refused with a tool
  error naming the setting when they lie outside `--root`.
- **Behaviour change: `ReplayModel` compares the whole request.** It matched only the keys the trace
  recorded, so a trace with no system prompt replayed under any system prompt, and the model and
  params were never compared. A `RecordingModel` now records `model`, `max_tokens`, the params,
  `system` (when set) and the messages, and a `ReplayModel` takes the same configuration as the
  `AnthropicModel` it stands in for (`model_for` passes it) and compares every field; a system
  prompt on one side only is a mismatch. `ignore=["model", ...]` leaves named fields out. Traces
  written before this record no `model` or `max_tokens` and now mismatch: the three apps' traces
  have them added (the requests otherwise unchanged and still produced by the apps' prompt code;
  the replies kept; still marked hand-written).
- **Schema weakenings the lock missed**: `patternProperties` added (or a pattern added to it) to a
  schema whose `additionalProperties` or `unevaluatedProperties` is `false` or a schema; any change
  inside `oneOf` (a tightened branch can accept more); `prefixItems` extended past its old length
  over an `items` schema that checked those positions; and OpenAPI `nullable` (or `x-nullable`)
  turned on.
- **A property added to a closed schema is a weakening**: with `additionalProperties` (or
  `unevaluatedProperties`) `false` or a schema, an input carrying the new property was rejected and
  now passes, so an optional field added to a pydantic `extra="forbid"` model is **WEAKENED**. A new
  property that is also `required`, with a schema that constrains its value, is not. Existing locks
  are unaffected: only new edits are judged.
- **`RecordingModel` never loses a paid call to the trace.** A request it could not serialise (bytes
  in an image block) made the API call and then crashed writing the trace. The request is now
  serialised before the call: bytes are recorded as `{"$bytes_base64": "..."}`, and any other value
  JSON cannot hold raises `TypeError` before anything is sent.
- **docs/start.md names the whole-tree commands the hook does not catch** (`git checkout -- .`,
  `git restore`, `git stash`, `tar x`, `cp -r dir/. .`); CI is the gate for them.

### Fix round 17: ablation, MCP, providers

- **`--inv-ablate` no longer counts state carried between re-runs as caught.** The re-runs keep
  session, package, module and class fixtures, so a falsifier that passes with or without the guard
  but spends a module-scoped budget failed on its third run (the ablated one) and was reported
  `caught`. A failing ablated re-run is now followed by a second control re-run with the rule on:
  if that fails too, the falsifier is `inconclusive (state carried between runs: ...)`. A failing
  first control is `inconclusive (failed on a control re-run without ablation; ...)`. Both name the
  fixtures broader than the function the test used (`budget (module)`), and neither counts as caught,
  so an `enforce` rule with no other falsifier fails the session. A caught falsifier now costs one
  more re-run per rule.
- **`inv-mcp` path arguments stay inside `--root`.** `root`, `rules`, `source_roots` and `graphify`
  are resolved (symlinks followed) and must lie inside the server's `--root`; an absolute path
  elsewhere, `..` or a symlink out is a tool error (`isError: true`). Before, `check` and `status`
  imported, and so ran, code from any directory an argument named. A filesystem root (`/`) is never a
  project root. The server's own `--rules` may still sit anywhere.
- **Behaviour change: async providers run one at a time by default**, in the order the rule file
  declares them, as before round 16. Round 16 awaited them all together with `asyncio.gather`, which
  breaks providers that share a resource that is not safe for concurrent use (one SQLAlchemy
  `AsyncSession`). Concurrency is opt-in: `concurrent: true` in the provider record, or
  `@inv.provider("name", concurrent=True)` (the record's value wins when it has one); consecutive
  calls of providers that all opted in are gathered, and one that did not is never overlapped. The
  JSON Schema and the loader take the new boolean field. `inv lock` pins it when set, as a neutral
  change (never a weakening), so existing locks stay fresh.
- **The evaluation README's B1 counts are current**: B1 drops 36 of 73 pool-service tests and 48 of
  90 fact-pipeline tests (each app's `tests/test_replay.py` added two since the 34 and 46 recorded
  before it).

### A real-model path for model_call, with replay

- **`invariants.integrations.anthropic`** (new `anthropic` extra, imported lazily; no core
  dependency). `AnthropicModel(model="claude-haiku-5-5", system=..., max_tokens=...)` gives a
  `model_call` body `complete(prompt, feedback) -> str` (and `acomplete` for an async body). One
  invocation with its format retries is one conversation: the parser error comes back as a follow-up
  user turn after the model's previous reply, with the API's content blocks (thinking included) sent
  back unchanged, while the first attempt and a semantic resample (`inv_feedback` None) open a fresh
  one. The conversation is context-local, so threads and tasks keep their own. `ReplayModel(path)`
  answers from a JSON trace (`{"fixture": "recorded" | "hand-written", "model", "calls"}`), matching
  each request and failing with a unified diff (`ReplayMismatch`) on a mismatch; `RecordingModel`
  wraps an `AnthropicModel` and writes `"fixture": "recorded"` traces; `model_for(mode, trace)` maps
  `replay`/`record`/`anthropic`/`fake`. See [docs/models.md](docs/models.md).
- **The demo apps take `APP_MODEL=replay|anthropic|record|fake`.** Their happy-path flows run on
  replay in tests and CI (`tests/test_replay.py`, traces in `tests/fixtures/traces/`, one per app
  exercising a format failure fixed on the second turn); the seeded-defect tests stay on each app's
  fake model. `pytest --record` with `ANTHROPIC_API_KEY` set records real traces. The committed traces
  are hand-written fixtures in the API's response shape, not recordings, and say so
  (`"fixture": "hand-written"`). support-agent's CI-defect tests now keep CI's `PYTHONPATH` after the
  app copy's `src` in the `inv check` subprocess.

### Two CEL backends: subset and cel-expr

- **Removed: the `celpy` backend** (cel-python). `cel-python` is no longer in the `cel` extra, and
  `auto` now picks `cel-expr`, else `subset`. `INV_CEL_BACKEND=celpy` (or `backend="celpy"`) is a
  `ValueError` that says to use `subset` or `cel-expr`. The backend's process-wide
  `sys.setrecursionlimit` change and its `__inv_*` call renames went with it.
- **`has()` on a value with no fields is a `rule_error` on the subset**, as on cel-expr:
  `has(x.f)` with `x` null (or a number, a string or a list) gave `false`. On a map it still tests
  the key, and a null field is present.
- **A spent cost budget is never absorbed on the subset.** `false && <costly>`, `true || <costly>`
  and an `exists`/`all`/`exists_one` that had already decided returned a result once the budget ran
  out inside the other branch; they are now a `rule_error` "cost limit exceeded".
- **`(?i)` in the subset's `matches()` folds case as RE2 does**, one character at a time. It used
  `upper()`/`lower()`, so `'ß'.matches('(?i)[A-Z]')` and `'ﬁ'.matches('(?i)[A-Z]')` were true and
  `'ſ'.matches('(?i)s')` and the Kelvin sign against `k` were false; all now agree with cel-expr.
- **Engine error text never carries request data unless debug.** The redaction of cel-expr's
  messages matched some kinds ("cost limit exceeded", "non-finite number", `timestamp():`) anywhere
  in the text and before the specific kinds, and returned such a message raw, so a missing map key
  or a bad regex from the request that held one of those phrases reached the outcome error. Each
  kind is now matched at the start of the message, the specific kinds first, and reported as a
  fixed text; the raw message stays in `CelError.detail` (shown with `inv.configure(debug=True)`).
  `inv.zip()`'s length mismatch no longer names the two lengths outside `detail`, on either
  backend. A modulus by zero on cel-expr is now reported as "division by zero".

### Ablation in the normal protocol, pytest-xdist, `inv-mcp` and the evaluation harness

- **`--inv-ablate` keeps one fixture lifetime.** A falsifier's control and ablated re-runs now run
  inside the normal test protocol, right after its own run, each torn down only to the test
  function. Session, package, module and class fixtures are set up once, as without the plugin
  (before, every re-run ran with `nextitem=None` after the main loop, so they were torn down and set
  up again per re-run). A fixture teardown error after the re-runs is reported as the test's
  teardown error. A falsifier the session never ran (`-x`, `--maxfail`) is reported `not run`.
- **pytest-xdist.** Under `-n N` each worker ablates the falsifiers it runs; the controller merges the
  ablation results, the `--inv-verify` result and the `--inv-report` counts from the workers' output.
- **Scan falsifiers.** `inv scan` exits 4 while its rule is ablated, which is neither a pass nor a
  fail, so a falsifier asserting `returncode != 0` passed for the wrong reason. The new `inv_scan`
  fixture reads the scan honestly: `failed(rule)` and `findings()` answer what it reported, and
  `code` or `passed(rule)` of an ablated rule skip the ablated re-run, which the plugin reports as
  `inconclusive (read an ablated scan's exit code or pass; assert its findings)`. The pattern for a
  raw subprocess (exit code 1 exactly, plus the rule's finding) is documented in concepts.md.
- **`inv-mcp`.** A read-only MCP server over stdio (protocol 2025-06-18, standard library only):
  `scan`, `check` (never `--relock`), `status`, `explain` (a rule or finding in plain words, with the
  fix options) and `propose_rules` (`inv init`'s discovery as a dry run in a temporary directory,
  each proposed rule checked by the scan). No tool writes the rule file, the lock or `allow`. The
  console script is `inv-mcp`; see [docs/agents.md](docs/agents.md) for the agentic flow
  (understand, propose, verify, human approval, lock) and a `.mcp.json` with Graphify.
- **Evaluation harness shipped.** [evaluation/](evaluation) holds the 24-change experiment: the
  ground truth, the edits and patches, the runner (relative paths; it copies `apps/` into a working
  directory) and the published results it compares against. docs/evaluation.md says how to
  reproduce the numbers.
- **CI** runs the suite under `python -O` and under pytest-xdist (`-n 2`); `pytest-xdist` joins the
  `dev` extra.

### Nothing below the API

Every layer is an API, and the rules stay on API boundaries: value laws on an API's inputs and
outputs, path laws on which API may call which. The features that reached below the API are
removed.

- **Removed.** Storage enforcers (`point: storage`, `view: rows`, `inv.storage_rejection`,
  `inv.storage_errors`, `StatusMap.storage`); transactional targets (`effects: transactional`, a
  target's `transaction`, `@inv.transaction` and its savepoints, `inv.RolledBack`, `inv.WiringError`,
  `StatusMap.rolled_back`); provider `consistency` (a provider is a plain typed read at its enforcer's
  timing, and `inv.monitor` no longer takes `in_txn`); `tables:` and `mediation.tables` with the
  scan's raw-SQL findings (`forbidden/raw-sql`, `unresolved/computed-sql`, DROP TRIGGER) and its
  warnings (`Report.warnings`, `sink_writes`, `uncovered_tables`, `scan_warning` in `inv status`);
  `.sql` contracts and the module `invariants.sql_constraints`; `inv init --sql`. `effects` is now
  `none` or `external`, so a post-effect check enforces only on a pure function and reports on
  anything else.
- **Migration.** A rule file that still uses a removed field fails to load with a message saying
  what to do (for example `consistency: provider consistency levels were removed: ... delete the
  field`). A lock taken before this release shows each removed pin once as WEAKENED, with the note
  "removed in this version: it reached below the API"; a person accepts it once with
  `inv lock --accept-weakening`. The store's own constraints, triggers and transactions keep working
  as the implementation's business; no rule names them.
- **Example apps.** pool-service's R-22 is a pre-effect check of the requested size (it was a
  rolled-back transition rule), and its store's trigger refusal is a plain 409 the app answers
  itself. fact-pipeline drops K-8 (append-only rows) and K-9 (one canonical id per record), which
  had no API: the schema keeps them as constraints. Its K-15, a post-condition of an external
  effect, is now `detect`, with the monitor reconciling.

### Security: an allowed back door is not a door

- **`allow` no longer lets new code through a frozen back door.** The scan used to treat an `allow`
  entry like a door, so a new caller of an allowed function reached the sink unseen, and anything
  inside an allowed function (unwrapping the door, monkeypatching it, `eval`) was not reported. Now
  a path to the sink is let through only when every function on it is allowed (the call chains
  frozen when `allow` was written): a new caller of an allowed back door, or new code one calls, is
  a `bypass`. Inside an allowed function, the ways it reaches the sink are still excused, but
  `forbidden/unwrap`, `forbidden/monkeypatch` and `unresolved/reflection` are reported. `inv init`
  freezes each back door with its call chain (re-scanning until the rule passes, at most 5 rounds).
- **`inv hook pretool` blocks direct edits of `invariants.lock`.** An edit tool on the lock (Edit,
  Write, MultiEdit, NotebookEdit) or a shell command that writes it (a redirect, `tee`, `sed -i`,
  `perl -i`, `cp`, `mv`, `rm`, `truncate`, `dd`) is refused: only `inv lock` and
  `inv check --relock` write the lock, and both refuse a weakening. Reading it is fine. The hook's
  `PreToolUse` matcher in docs/start.md is now `Bash|Edit|Write|MultiEdit|NotebookEdit`, so an edit
  tool reaches it.

### Security: the weakening gate fails closed

- **`inv check --base` fails closed.** A base ref that does not exist, a checkout that is not a git
  repository, and a lock at the base that cannot be read or compared (damaged, or another lock
  version) now fail the step; before, each passed with a note. A reviewer may accept an unreadable
  or old base lock with `--allow-weakening` or `INV_ALLOW_WEAKENING=1`; nothing accepts a missing
  ref. Only a base with no `invariants.lock` passes, reported as a first adoption. The GitHub Action
  no longer ignores a failed `git fetch` (`|| true` is gone) and checks that the base ref exists
  after it, with a clear error.
- **The scan configuration is pinned.** When a rule has a sink, the lock has a `scan` section: the
  roots and exclude globs `inv check` scans with (`[tool.invariants.scan]`, the defaults, and any
  `inv check --root/--exclude`). An added exclude or a removed root is WEAKENED; the reverse is a
  change only. `inv lock` takes `--root` and `--exclude` to pin extra ones. A lock taken before is
  compared with the default excludes, so its first `scan` pin is a non-weakening re-pin unless it
  adds an exclude (`inv check --relock` takes it). `lockfile.DEFAULT_SCAN_EXCLUDE` holds the
  defaults (`cli.DEFAULT_SCAN_EXCLUDE` is the same tuple).
- **Contract removals and schema references are judged.** Removing a contract is WEAKENED (a rule
  may name it, and an unreferenced contract such as an `inv init` pydantic pin is itself the pin of
  its constraints). In a pinned schema, a `$ref` removed (the value becomes `{}`, Any) or pointed
  at a schema with a different body, a `$defs` entry removed while referenced, and
  `additionalProperties` going from `false` to absent or `true`, or from absent to `true`, are
  WEAKENED.
- **Dataclass, TypedDict and live OpenAPI contracts are examined.** A dataclass or TypedDict contract
  is pinned as a JSON Schema (`{name, jsonschema}`: pydantic's `TypeAdapter` when pydantic is
  installed, else `lockfile.stdlib_json_schema`) and judged like a pydantic model; an existing lock
  shows its old `fields` pin replaced once, as a change only. A new contract form
  `{type: openapi, app: "module:app"}` pins the operations of a live app's `app.openapi()` (a
  FastAPI app), and the schemas of an OpenAPI contract's operations are now judged too.
- **Fewer false weakenings.** These are tightenings now: `ge=1` to `gt=0` on an integer (and the
  maximum side; `le=k` to `lt=k` on any number), an `enum` replaced by a `const` it held, `type`
  `number` to `integer` or a type dropped from a list, and `^`/`$` anchors added to an otherwise
  identical `pattern`. A type added to a type list is now WEAKENED (it was missed).
- **`inv hook pretool` is harder to talk past.** File paths are resolved with `os.path.realpath`
  first, so a symlink to the lock is the lock. A shell command that names `invariants.lock` (or a
  glob that could match it) is blocked unless every segment naming it is clearly read-only (`cat`,
  `less`, `head`, `grep` and the like, `git diff|show|log|status|add`, `inv check|lock
  --check|status|scan|validate`, no redirect into a file). An edit that changes `[tool.invariants]`
  in `pyproject.toml`, and a shell command that writes `pyproject.toml`, are blocked too. The docs
  now say plainly that the hooks are an early warning and `inv check --base` in CI is the gate.
- **pre-commit stages the re-pinned lock.** `inv check --relock --fail-on-repin` (the `inv-check`
  hook's entry now) re-pins a lock that no change weakens and then exits 1 with "invariants.lock
  re-pinned; review and `git add invariants.lock`", so a commit never leaves the new lock unstaged.

### Added

- **Receiver types follow attribute chains.** `ctx.billing.refund()` with `ctx: AgentContext`, where
  `AgentContext` sets `self.billing = BillingService(...)` or declares `billing: BillingService`
  (a dataclass), is an extracted call. Attributes resolve in the module that defines the class. An
  attribute that any code sets from outside its object (`obj.x = ...` outside the object's own
  methods, or `setattr(obj, "x", ...)`) is never typed, so a swapped dependency stays unresolved.
- **Graphify hints for `inv init`.** With `graphify-out/graph.json` in the project (or
  `--graphify PATH`; `--no-graphify` turns it off), `inv init` reads a
  [Graphify](https://github.com/Graphify-Labs/graphify) graph (`--graph` is the same option) as plain
  JSON: nodes are matched to
  functions by file and line, communities propose doors (a caller of a write in another community,
  itself called, is a door even when nothing is named Service; it must not be an entry point, and all
  its callers must sit in a third community), and the call links Graphify's parser
  extracted (`EXTRACTED` only, never `INFERRED` or `AMBIGUOUS`) count as callers. Every proposed rule is still checked by
  the scan before it is written; `inv check` never reads the graph, and Graphify is never imported
  or run (`invariants.graphify`). Run for real (graphify 0.9.107) on five codebases, including a
  three-layer fixture with unconventional names, it added one door and no rule the heuristics missed:
  its communities follow call chains, not layers. Real graphs are pinned in `tests/fixtures/graphify/`.
- **Module constants in `inv init`.** A precondition that compares with a module-level literal
  bound once (`MAX_REFUND = 10_000`; `if amount > MAX_REFUND: raise`) is translated with the value
  inlined (`args.amount > 10000`). An int, float, str, bool or None counts, and so does a tuple, list
  or set of them (`if currency not in CURRENCIES: raise`). A name bound twice, rebound with `global`,
  a loop variable or an import is left untranslated and says so.
- **Factory return types in the scan.** `get_repo().add(...)` and `r = get_repo(); r.add(...)` are
  extracted calls when `def get_repo() -> Repo` (or `Optional[Repo]`, `Repo | None`): factories and
  FastAPI dependency helpers. The annotation resolves where the function is written, and subclass
  overrides are reached.
- **Typed sessions and route layers in `inv init`.** A receiver annotated with a store type
  (`s: Session`, `db: AsyncSession`, an alias such as `SessionDep = Annotated[Session, ...]`, also
  imported) is a store client whatever its name, so its writes mark a sink. A function called only
  by HTTP route handlers (`@router.post`, `@app.get` ...) that calls a write is that write's door,
  even when one route calls it. On the full-stack FastAPI template this still writes no door rule:
  its routes call `crud` and the session directly, so there is no such layer.
- **apps/support-agent.** A tool-calling support agent: a graph of nodes registered as values, an
  `@inv.model_call` planner (2 format retries, 1 resample, `ESCALATE_TO_HUMAN` hands off), an
  `@inv.edge` executor, tools that call `BillingService` doors, and `StripeGateway` / `LedgerRepo`
  sinks. `inv init` on its code found the tool and plan schemas, both door rules and the one real
  back door. Twelve seeded defects (a tool that skips the door, the sink in the tool registry, a
  raised cap, more resamples, a new caller of the frozen back door, an over-refund, bad JSON, an
  abstention, a forged plan, a plan changed after the check, a deleted guard, a deleted rule) are
  each caught, and safe changes pass. It runs in the monorepo's CI with the other two apps.

### API laws at every layer: doors without decorators

The rules now stay on API boundaries: value laws on an API's inputs and outputs, and path laws on
which API may call which. Nothing in this section looks below a repository's API.

- **Door rules.** `door:` and `sink:` (each a `py:` path or a list) make a path law in four lines: no
  call path may reach the sink without passing a door. It expands to `mediation`, `bad: 'false'` and
  one `structure` enforcer, is `enforce` by default, needs no decorator and no test, and is checked
  by `inv scan` inside `inv check`. `allow:` freezes known back doors: the scan lets them (and
  everything inside them) through, a new back door fails, an entry that is no longer a back door
  fails until deleted (`missing/allow-unused`), and an emptied `allow` means none. The lock reports a
  new `allow` entry or a new door as a weakening and a removed one as a tightening. With `guard:`,
  the guarded function is the door. (How `allow` lets paths through changed: see the security entry
  above.)
- **`inv init` finds the doors.** For each repository (a Repo, Store, Gateway or DAO class, or a
  `repo`/`store`/`db` module, or the functions that write through an ORM session, a Django manager,
  a Mongo collection, a DynamoDB table, a client's `create` call or raw SQL), it proposes one door
  rule: the repository's write methods are the sinks, its service-layer callers are the doors (or,
  with no service layer, the caller most functions go through). It runs the real scan on the
  proposal and freezes what it finds in `allow`, so the drafted file passes `inv check` and a new
  back door fails it. At most five rules are written, money, quota and approval names first
  (`--max-doors`); `--no-doors` skips it.
- **`inv init` is API-only.** It never reads `.sql` files (`--sql` was removed with the other
  below-the-API features, above).
- **The scan types method receivers.** A call on a receiver of a known class is an extracted edge: a
  constructor call, a local variable or `self.x` bound only to one, or a parameter annotated with a
  scanned class (`Optional[...]`, `X | None` and string annotations too; tuple assignment pairs
  element-wise). Through an annotation or `self.x`, the call also reaches subclass overrides. Anything
  less certain stays name-matched and is reported as unresolved.
- **The scan skips test code and migrations by default**, at any depth (`tests/`, `test_*.py`,
  `*_test.py`, `conftest.py`, `migrations/`, `alembic/`), so a unit test calling a repository directly
  is not a back door. `[tool.invariants.scan] exclude` replaces the list.
- Each scan finding carries the function it is in (`owner` in `--format json`).

### Fixed

- **celpy on Python 3.11.** A long clause (a 200-term sum) evaluated deep in the caller's stack hit
  Python 3.11's recursion limit and came back as an opaque `celpy: evaluation error` (fail-closed,
  so the call was rejected). The evaluation is now retried once with a higher limit, which is
  restored afterwards, and a clause too deep even then is reported as nested too deeply.

### Adoption: a 10-minute start

- **`inv init`** drafts `invariants.yaml` from what the code already has. It reads the source and
  imports nothing. Pydantic models with constraints become `pydantic` contracts. Leading `if ...: raise` and `assert` preconditions over the
  parameters become suggested short-form rules, translated to CEL and left commented out. Anything
  it cannot translate faithfully is listed, not guessed. It runs `inv lock` and never overwrites a
  rule file. Models are found through `BaseModel`, `RootModel` and `SQLModel` bases, subscripted
  bases such as `RootModel[list[str]]` and `Generic[T]`, and their subclasses.
- **Short rule form.** `guard:` and `tests:` expand to `subject.target`, one decorator enforcer and
  `falsified_by`. A rule without a status is `enforce` when it has tests and `detect` otherwise.
  `rules:` may be empty.
- **Constraint weakening.** A pinned pydantic schema that now accepts more input is WEAKENED: a
  raised or removed bound, a new enum value, a field no longer required, and so on. It is no longer
  just a pin change. The new `sql` contract type pins CHECK, NOT NULL, UNIQUE and PRIMARY KEY
  constraints; removing or editing one is WEAKENED.
- **`inv check`** runs `validate`, `lock --check`, `verify` and `scan` with one exit code, skipping
  the steps with nothing to check.
  - `--relock` re-pins a stale lock when nothing weakens, for pre-commit.
  - `--base REF` fails on a weakening since the lock committed at REF, even one the change
    re-pinned itself. A reviewer overrides it with `--allow-weakening` or `INV_ALLOW_WEAKENING=1`.
- **Agent and CI hooks.**
  - `inv hook pretool` is a Claude Code PreToolUse hook. It blocks commands that accept a weakening.
  - `inv hook stop` is a Stop hook. It sends the agent back once while `inv check` fails.
  - `.pre-commit-hooks.yaml` defines the `inv-check` hook.
  - `action.yml` is a GitHub Action that runs `inv check --base` with a label-based override.
  - [docs/start.md](docs/start.md) is the 10-minute guide.
- The CLI prints the lock path as given, not resolved to an absolute path.

### 0.1.0 (unreleased): summary

The first release of `invariants`: rule-file-driven invariant guards for Python, with the evidence
to show that each one holds.

- **Rule files.** JSON or YAML (`spec: invariant/v0.1`) with targets and their effects, typed
  read-only providers, and rules whose `bad` is CEL. `inv.load` checks
  everything at load time: targets, providers, the timing of every enforcer, the status the evidence
  allows, every CEL function against the chosen backend, and a type check of every clause.
- **Enforcers.** `@inv.guard` (sync, async, methods), `@inv.model_call` (closed schemas, format
  retries with parser feedback, semantic resamples, abstain) and `@inv.edge`, plus monitor, FastAPI
  middleware and structure (scan) points. Four
  outcomes (`ok`, `violation`, `rule_error`, `parse_error`) and four actions (reject, halt,
  escalate, route) with review stores and a durable fallback.
- **CEL.** Three backends (`subset`, `cel-expr`, `celpy`) behind one front end, one Python-to-CEL
  value mapping, cost budgets on every backend, a non-finite policy that makes NaN a `rule_error`,
  and an 18-suite conformance corpus that every backend passes with no expected failures.
- **CLI.** `inv validate`, `inv lock` (lock version 3: contract-level pins, a semantic diff, and a
  weakening gate behind `--accept-weakening`), `inv verify`, `inv scan` (a static path-law check,
  text, JSON and SARIF output), `inv status` (computed standing), and `inv init`, `inv check` and
  `inv hook` for adoption.
- **pytest plugin.** `--inv-verify`, `--inv-ablate` (each rule switched off; its falsifiers must
  fail because of it), `--inv-report`, and the `inv_registry`, `inv_ablated` and `inv_events`
  fixtures.
- **Integrations.** Pydantic (strict closed schemas, contracts), FastAPI and ASGI
  (`application/problem+json` with no values echoed), and OpenTelemetry spans.
- **Documentation and examples.** A front-page README with a runnable quickstart, reference pages
  under `docs/`, a measured evaluation (`docs/evaluation.md`), and three standalone example apps
  under `apps/` (pool-service, fact-pipeline, support-agent).

Known limitations are listed in the README and in each reference page.

### Development history

The entries below record each review and certification round before the first release, newest
first. They are kept for reviewers and for anyone tracing why a behaviour is the way it is.

### Fix round 17: scan

From a second review of `inv scan`. Regression tests: `tests/test_fix_round17_scan.py`; each failed
before its fix. The three example apps' CI passes; the lock format did not change.

Behaviour changes:

- **`super()` follows the subclasses' MRO (S-9).** Round 16 resolved `super().m()` in class `C`
  against `C`'s own method resolution order, so in cooperative multiple inheritance
  (`class Mixin(Base)` calling `super().save(x)`, `class Leaf(Mixin, SinkRepo)`, `Leaf().save(x)`) the
  edge went to `Base.save` and the run-time path to `SinkRepo.save` was missed: a regression from the
  name-matched edge before it. The edge now goes to the next `m` after `C` in the MRO of `C` and of
  every scanned subclass of `C` (their union). When a subclass's MRO has no consistent C3 order, the
  call is name-matched again (fail closed).
- **Calls into excluded code are reported (S-10).** A production module whose path matches an
  exclude (`app/testing/helpers.py`, `app/test_fastpath.py` under the defaults) was not read, so a
  back door through it from `app/api.py` was invisible. Excluded files are now noted by module name
  (not parsed), and a call from scanned code into an excluded module, or a function or class in one,
  is `unresolved/excluded-callee`, naming the file and the exclude glob. Calls inside a door, a sink or
  code reached only through them are not reported. Excluded code calling scanned code (a test suite
  importing the app) stays excluded and quiet.
- **An unwrap goes to the rules it unwraps (S-11; pool-service FRICTION item 12).** A
  `forbidden/unwrap` (`f.__wrapped__`, `getattr(f, "__wrapped__")`) was listed under every scanned
  rule. When the scan knows which scanned function is unwrapped, it is listed only under the rules
  where that function is a sink, door or allowed back door, or reaches a sink without a door. An
  unwrap of something the scan cannot name still goes to every rule. In pool-service
  (`inv scan --root src --root bypass`), `ResizeService.resize_door.__wrapped__` at
  `bypass/pool_service/rogue.py:15` is now under R-21, R-22 and R-23, no longer R-7 and R-20.

### Fix round 17: the lock and hook review

From a review of `lockfile.py` and the `inv check --base` and `inv hook pretool` paths of `cli.py`.
Regression tests: `tests/test_fix_round17_lock.py` (each failed before its fix). The three example apps'
CI passes unchanged; the lock format did not change.

- **`--base` no longer takes a symlinked or moved lock for a first adoption (HIGH).** Replacing
  `invariants.lock` with a symlink to a re-pinned file made `--base` resolve it, look for the
  target at the base, find nothing and pass as "first adoption". The lock is now looked up at its
  own repository path (`git ls-tree` at the base, which also tells a symlink or a directory from a
  file); a lock that is a symlink, or sits under a symlinked directory, fails and cannot be
  accepted; and when the base has an `invariants.lock` that the change moved or removed, the step
  fails (a reviewer may accept it with `--allow-weakening`).
- **`additionalProperties: false` to `{}` or a schema is WEAKENED again.** It fell through to "no
  weakening". `false` to anything else is a weakening; a schema to absent or `true` too; a schema to
  another schema is judged inside it.
- **A renamed OpenAPI operation is compared with its old self.** Renaming an operation (FastAPI
  derives the operationId from the function name) showed one operation removed and one added and
  judged neither, so a looser body went through. Operations are now matched by method and path as
  well as operationId: a rename is reported and compared field by field. An operation removed with
  no counterpart is WEAKENED when a rule names it (`subject.operation`) and reported otherwise.
- **The pretool hook reads shell words, not text.** `inv` takes no abbreviated options
  (`allow_abbrev=False` on every parser), and the hook blocks an abbreviation of `--accept-weakening`
  or `--allow-weakening` of five characters or more. `--output`, `--output=` and `-o` count as
  writing on any command (grep's `-o` excepted), as do `git -c`, a `GIT_*` variable before `git`, and
  `rg --pre`. Names are matched after shell unquoting and as globs (`"invariants".lock`,
  `invariants\.lock`, `inv*.lock`, `*.lock`, `pyproject''.toml`). A command that does not parse, or
  uses `$(`, backticks, `${`, `eval`, `sh -c`, `xargs` or `python -c`, is blocked when it mentions
  `lock` or `pyproject`; a writing command with an expanded name is blocked when it mentions the
  lock, `invariants`, `pyproject` or `toml`. `git add invariants.lock` stays allowed. The docs say
  what the hook catches and that `inv check --base` in CI is the gate.
- **The first scan pin is compared with the base's default roots.** A lock pinned before the scan
  was pinned was compared as if its roots equalled the new ones, so narrowing the roots in the same
  change passed. `inv check --base` now compares with the roots and excludes `[tool.invariants.scan]`
  (else src/ and the rule file's directory) gave at the base, and `inv lock` with those of the
  working tree.
- **The integer slack of a bound and its twin needs integral bounds.** `minimum: 1.5` to
  `exclusiveMinimum: 0.6` on an integer lets 1 through and is now WEAKENED; `ge=1` to `gt=0` is
  still a tightening.
- **Removing a structural keyword is WEAKENED.** `items`, `prefixItems`, `contains`, `allOf`, `not`,
  `propertyNames`, `dependentRequired` (or one of its entries), `dependentSchemas`, `if`, `then`,
  `else`, `properties` and `unevaluatedProperties` removed were not judged; adding one is a
  tightening. Any change inside `not` or `if` is a possible weakening, since there a tighter
  subschema can let more input through.

### Fix round 16: the scan review

From a review of `inv scan` and `inv init`. Regression tests: `tests/test_fix_round16_scan.py`. The
three example apps' CI passes unchanged; the lock format did not change.

Behaviour changes:

- **A sink taken as a value through a typed receiver escapes (S-1).** `cb = repo.save`,
  `add_task(self.repo.save)` and `functools.partial(repo.save, x)`, where the receiver's class is
  known (an annotated parameter, a constructor, `self.x`), recorded nothing, so a call through the
  value was invisible. They are now `unresolved/sink-escapes`; calls (`repo.save(x)`) stay edges.
- **Generic bases and `super()` (S-2).** `class ConcreteRepo(BaseRepo[T])` was not seen as a
  subclass of `BaseRepo`, so a call through a `BaseRepo` receiver skipped the `ConcreteRepo.save`
  override. A subscripted base is now its class (`Generic[T]` and `Protocol` are ignored), and a
  `BaseRepo[int]` annotation types a receiver. `super().m()` (and `super(Cls, self).m()`) is an edge
  to the next `m` in the method resolution order through the scanned classes; it was a name-matched
  call.
- **Receiver typing on FastAPI-style code (S-3).** These were unresolved and are now typed:
  `await get_repo()` on an `async def ... -> Repo`; `with X() as r` and `async with` by the scanned
  class's `__enter__`/`__aenter__` return annotation (`-> Self` too) or a `@contextmanager` /
  `@asynccontextmanager` factory's `-> Iterator[Repo]` / `AsyncIterator[Repo]`;
  `Annotated[Repo, Depends(...)]` parameters and module-level aliases of it; and `@property` getters
  annotated `-> Repo` (`self.repo.save()`). A call through a property or method attribute of a scanned
  class (`self.repo.save()` with an untyped property) was dropped silently; it is now unresolved.
- **Builtin containers are not unknown receivers (S-4).** With a sink named `update` or `add`,
  `d.update()` and `s.add()` on a dict, set or list literal, comprehension, `dict()`/`set()`/`list()`
  or a parameter annotated `dict[...]`/`set[...]` were `unresolved/unknown-receiver`. They are the
  container's own methods now, unless a scanned subclass of that builtin overrides the method.
- **Reflection gaps (S-5).** Reflective builtins taken as a value (`g = getattr`, `setattr`, `eval`,
  `vars`, `importlib.import_module`, `operator.methodcaller` ...) and any use of `sys.modules` other
  than `name in sys.modules` are `unresolved/reflection`; `type(obj).__dict__` and
  `obj.__class__.__dict__` are class-namespace lookups (`forbidden/getattr-sink` for a literal sink
  name). Rebinding a scanned module or class that holds a sink, door or allowed function from outside
  (`pkg.repo.Repo = Fake`, `pkg.repo = fake`, `setattr(pkg.repo, "Repo", Fake)`) is
  `forbidden/monkeypatch`.
- **One finding per call site (S-6).** The scan reported one path per entrypoint, so a function with
  two direct sink calls showed one finding, and two entrypoints through one helper call showed two at
  the same place. Each call site that reaches the sink without a door is now one finding (shortest
  path from the first entrypoint that reaches it), in the same output format; rule results are
  unchanged. `Report.on_paths` keeps every function on any such path, which `inv init` freezes and
  `missing/allow-unused` reads, so a back door reached from several entrypoints is frozen whole.
- **`inv init` no longer writes a rule it cannot freeze (S-7).** When every finding was inside a door
  or sink, `allow` stayed empty and the rule was written active, then failed `inv check`. It is now
  written commented out with the reason.
- **Truth tests translate by type (S-8).** `if not name: raise` became `!args.name`, invalid CEL for
  a string or list. A parameter tested for truth is translated by its annotation: str, bytes, list,
  dict, set to `size(args.x) == 0`, bool to `!args.x`, and with `Optional[...]` (or a `None` default)
  `args.x == null || ...` first. Anything else tested for truth is listed as not translated.
  `bootstrap.to_cel` takes the parameter kinds as an optional argument; without it, it behaves as
  before.

### Fix round 16: the runtime review

Regression tests: `tests/test_fix_round16.py` (fixture `tests/fixtures/round16.py`); each failed
before its fix. mypy is clean. The lock format did not change.

Behaviour changes:

- **Off, retired and ablated model calls are skipped.** A model call whose rule is retired, whose
  `model_call` enforcer is in effective mode `off` (it used to act as `detect`), or whose rule is
  ablated calls the model once and parses the reply: no format retries, no abstain escalation (both
  used to run before the ablation check), no `bad`, no event. `act()` is a no-op for an `off` enforcer.
- **`old_self` from `bind` and views.** The snapshot covers every `old_self` path read by `bad`, by a
  `bind` entry it reads, by any enforcer `view` or by a provider argument (`bind: {prev:
  old_self.balance}` was an empty snapshot and a rule_error). It takes bytes, datetimes, durations,
  UUIDs and Enum members (read through cel/values.py as arguments are), and a field absent on
  `self` is absent from the snapshot, so `has(old_self.a.b)` is false instead of a rule_error.
- **`self` in model calls and edges.** On a method, `self` is a root (as in guards); `args` no longer
  holds it.
- **Roots an enforcer never sets are load errors**, with the fix: `result` before the call or at a
  model_call or edge, `reply` at a guard, `old_self` at a model_call, edge or monitor, and
  `args.self` or `args.inv_feedback` anywhere.
- **Each edge uses its own enforcer**: the edge enforcer whose `target` is the consumer, not the
  rule's first edge enforcer.
- **One generation per call** in model calls and edges too (rules and providers read together;
  `judged` passes the call's providers). `standing()` reads effective modes, so a spike-demoted rule
  is `monitored`. A reload clears the spike demotion of a rule whose record changed, and keeps it for
  an unchanged one.
- **Standing is stricter.** `held` is withheld while `mediation.allow` names a back door that still
  resolves and is not in `residual_paths`, while the chokepoint names none of the rule's functions
  or a `py:` path that does not resolve, and, given the pytest plugin's ablation results
  (`inv.standing(id, ablation=...)`, `status_report.build(reg, report, ablation=...)`,
  `status_report.load_ablation(path)`), while no falsifier was caught. `inv verify` reports a
  `py:`-keyed guard on another function than its key names, and runs `verify_routes` for every app
  given to `integrations.fastapi.install` (or `register`) through the new `Registry.verifiers`.
- **Typing.** A `TypedDict` argument is typed by its keys (it was an open map). A `{field: type}`
  reply schema is closed: `reply.x` for an undeclared `x` is a LoadError. The JSON Schema allows
  `view` on middleware and monitor enforcers and declares `subject.operation`.
- **Providers.** Cache keys keep the argument types (`1`, `True` and `1.0` no longer share a
  value). Async providers are awaited together with `asyncio.gather`, each distinct call once. A
  provider bound to a name no clause reads is not called. `@inv.provider("a", "b")` registers one
  function under several names. New `inv.Dependency`: a context-local value a provider reads.
  Clauses per enforcer (FRICTION fact-pipeline 6) are not added: the lock compares a rule's clauses
  as one list and could not see a clause dropped from one enforcer; see concepts.md.
- **Pydantic replies are model instances** (`isinstance(c.value, Model)`): Enum members,
  datetimes and nested models, not the JSON-form dataclass. `Field(exclude=True)` fields no longer
  make every reply a parse_error (KeyError). An Enum with list or tuple values is validated by
  pydantic instead of always failing. A `datetime` without a timezone is a parse_error (so
  format_retries apply) instead of a rule_error when a rule reads it. `model_of()` returns a fresh
  validated copy. Migration: `dataclasses.asdict(c.value)` becomes `c.value.model_dump(mode="json")`;
  a model that must be immutable sets `frozen=True`; a forged model instance is no longer refused as
  "not built by inv.parse" (it is re-validated and re-checked, as a dataclass instance is).
- **`Any` fields are copied** at parse and at an edge, so the sender mutating an `Any` value after
  the check cannot change what the consumer reads.
- **`model_call(..., returns=CheckedT)`.** The body is annotated with what it returns (`-> str`),
  and the decorated function is `CheckedT` (awaitable for `async def`) under mypy strict, with no
  `type: ignore`. The older form (body annotated `-> CheckedT`) still works.
- **`JsonlReviews.aopen`** runs the write and fsync in a worker thread; async guards, model calls and
  edges await it (`Registry.ahand_off`, `evaluate.aact`). `open()` is unchanged.
- **OpenTelemetry spans** start when the check began and end when the outcome was recorded (events
  carry `start_ns` and `end_ns`), and a violation, rule_error or parse_error sets status ERROR.

### Fix round 15 (certification round 12 findings)

From certification round 12 (no blockers or majors; four minors and a group of nits). Regression
tests: `tests/test_fix_round15.py`. The suite passes on every backend and under `python -O`, and
mypy is clean. The lock format did not change, so the example apps were not re-locked.

Behaviour changes:

- **An edit that can newly raise is not a strengthening (R12-1).** The polarity analysis judged a
  `bad` edit by the values its nodes take and ignored that the new condition could error where the
  old one could not. Under `on_error: fail_open_and_page` such an error lets the call through, so
  `args.region in ["xx", "yy"]` to `[..., args.cfg.extra]`, `size(s) / 2 > 1` to `size(s) / 0 > 1`
  and `args.n + 1 > 5` to `args.n + 2 > 5` were re-pinned as "matches more cases" while the new rule
  let through calls the old one rejected. An edit is now "stronger" only if it adds no
  sub-expression that can raise: no new field or variable read, index or function call, no new or
  changed divisor or modulus (a non-zero literal other than -1 is fine), and no new or changed
  arithmetic that may overflow int64 (arithmetic over int literals and `size()` with a bound below
  2^63 is fine). Otherwise it is WEAKENED with "the new condition can raise an error the old one
  could not (a read of args.cfg.extra)". The rule applies whatever the rule's `on_error`. An
  operand added to an existing `||` or `&&`, and a new `bad` clause, are exempt, because wherever the
  old condition held its own operands decide the result. So a lower threshold, a literal `in`-list
  member, and a disjunct or clause over any fields are still accepted. Two round-14 tests changed
  accordingly: `uint(a) * 2` to `* 3` and `a + 1` to `a + 2` are now "unknown". Documented in
  docs/lockfile.md.
- **Namespace reflection in the scan (R12-2).** `store.__dict__["sink"](c)`,
  `store.__dict__.get("sink")(c)`, `store.__getattribute__("sink")(c)` and
  `object.__getattribute__(store, "sink")(c)` passed with no findings. A lookup in a scanned
  module's or class's namespace is now a `getattr` with that name: a constant key is
  `forbidden/getattr-sink` for the sink (or door) name and a call edge for any other function, and
  anything else (a computed key, `.items()`, the dict as a value, an assignment into it,
  `inspect.getmembers(store)`) is `unresolved/reflection`, like `vars(store)`. A function's
  `__dict__` is its metadata and stays harmless.
- **`inv scan` finds a src layout (R12-5d).** With no `--root` and no `[tool.invariants.scan]`
  roots, `inv scan` and `inv status` scanned only the rule file's directory, so a src-layout project
  got "missing sink/door" while `inv lock` found `src/` by itself. When a `pyproject.toml` sits
  beside the rule file, its `src/` and setuptools `packages.find.where` directories are now roots
  of their own, next to the rule file's directory. A file under two roots is named from the
  deepest one, once.

Packaging and documentation:

- **The `[fastapi]` extra brings httpx (R12-3)**, which `fastapi.testclient` needs, so cookbook
  recipe 6 runs in a fresh venv. The cookbook intro says so, and recipe 10 runs `bash ci.sh`.
- **docs/evaluation.md leads with the current results (R12-4):** the short answer, tables and
  verdict after the lock redesign (2 of 24 lock failures, 1 of 8 benign changes red, 8/8 covered and
  3/8 uncovered defects caught, 6/16 by an invariants mechanism). The original run follows in full
  as the "before". The page no longer cites unpublished paths or development process; the harness
  is described as not shipped. The limitations and bias caveats are unchanged.
- **Nits (R12-5).** The fix-round-14 entry no longer says a field declared `int` qualifies for the
  integer rewrites, and neither does the `_Canon.integral` docstring. The README CLI section
  documents `--path DIR`. RELEASING.md says the static CI badge must be swapped for a live one.

### Fix round 14 (certification round 11 findings)

From certification round 11 (one major, four minors, two nits). Regression tests:
`tests/test_fix_round14.py`. The suite passes on every backend and under `python -O`, and mypy is
clean. The lock format did not change, so the two example apps were not re-locked; their CI passes.

Behaviour changes:

- **Literals in `bad` keep their CEL type (R11-1, major).** The canonical tree tagged nothing, and
  Python says `true == 1 == 1.0` and `false == 0`, so `args.m.approved == true` to `== 1` on a dyn
  field was re-pinned with exit 0 as an "equivalent rewrite", though the new rule never fires. Each
  literal is now `("lit", type, value)` (bool, int, double, string, null, bytes), and `-0.0` is
  kept apart from `0.0`. Every equality and membership test in the comparison (clause matching,
  regrouping, operands common to both sides, `in`-list members, ternaries, the `false` shortcut)
  uses that form. A threshold edit across types (`> 5` to `> 6.0`, `/ 2` to `/ 2.0`) has no
  direction and is a possible weakening. The other comparisons in `lockfile.py` were already
  type-strict (`same()` compares canonical JSON).
- **Scaling by a factor of unknown sign has no direction (R11-2).** `args.n * 2 > -10` to
  `args.n * 3 > -10` on an `int` n was "matches more cases"; for n = -4 it matches fewer. `*` and
  `/` now keep a direction only when the other operand is provably non-negative: a non-negative
  literal (so a literal times a literal), `size()`, `uint()`, a uint-typed field, or sums, products
  and quotients of those. Otherwise the edit is a possible weakening. The README caveat that the
  analysis assumed non-negative operands is gone.
- **Integer-only rewrites need provable integers, not annotations.** `> n` to `>= n+1` and
  `!(a >= b)` to `a < b` count as the same condition only for int literals, `size()`, `int()`,
  `uint()` and arithmetic on those. A field declared `int` no longer qualifies, because Python does
  not enforce annotations and a caller can pass 10000.5. Such an edit is a possible weakening;
  write `int(args.n) > 10000` or use `--accept-weakening`.
- **More equivalent rewrites (R11-3).** These are now the same condition:
  - a mirrored comparison (`10000 < args.size` for `args.size > 10000`);
  - `!` pushed inward: De Morgan, `!(a == b)` as `a != b`, and `!!e` as `e` when `e` is provably a
    bool (a comparison, `&&`, `||`, `!`, `has()`, a comprehension, or a bool-typed field);
  - a renamed comprehension variable (variables are named by nesting depth);
  - a repeated disjunct or conjunct, a repeated `in`-list member, or a repeated clause, dropped
    (the clause case is "bad repeated clause removed", not "matches fewer cases").

  With the rule's declared types (the loader's static type environments, one per enforcer, now
  passed to the comparison by `inv lock`), two rewrites that hold only for integers also apply
  when both operands are provably int or uint: `> 10000` as `>= 10001` (and `< 1` as `<= 0`), and
  `!(a >= 1)` as `a < 1` (NaN breaks it for doubles). Provably int means an int literal,
  `size()`, `int()`, `uint()` or arithmetic on those; a field declared `int` does not qualify (see
  above: Python does not enforce the annotation).
  A double, a dyn value or a comprehension variable is not assumed to be an integer.
- **Numeric `+` and `*` are commuted but no longer regrouped.** `x + 1.0 + 1.0` and
  `x + (1.0 + 1.0)` differ by rounding near 2^53, and int chains differ by overflow, so a
  regrouping was re-pinned as "equivalent" while changing what the rule flags. `||` and `&&`
  chains are still flattened.
- **The scan resolves `.__call__` (R11-4).** `store.sink.__call__(c)` (and
  `getattr(store.sink, "__call__")(c)`) is a call of the sink, so a bypass is reported, and
  `helper.__call__(c)` makes a door-only helper's SQL count as reachable from outside the door.
  Another dunder read on a known function (`sink.__get__(o)`, `helper.__func__`,
  `getattr(f, "__get__")`) is a use of it as a value (`unresolved/sink-escapes` for a sink; a
  helper used as a value is not door-only). `__name__`, `__doc__` and other metadata reads are
  not. `.__wrapped__` and `getattr`/`operator.methodcaller` with `"__wrapped__"` stay forbidden.
- **`inv` imports this checkout's source (R11-5).** `validate`, `lock`, `verify` and `status` put
  every `--path`, then the project's `src/`, its setuptools `packages.find` `where` and
  `[tool.invariants.scan]` roots, the rule file's directory and the project root (the nearest
  `pyproject.toml`) ahead of site-packages. With a src layout and no `--path`, `inv lock --check`
  used to import whatever copy the venv had installed (an editable install of another checkout or
  worktree) and report "fresh" for an edit to this one. When a pinned module is still imported
  from outside the project, `inv lock` and `inv verify` print a warning naming its file. The
  README CLI table documents `--path`.
- **`--check` validates the schemas table (R11-6).** Each key must be the sha256 of its body,
  every `$ref` must name an entry, and no entry may be unreferenced. A damaged table is reported
  as stale with each problem. A plain `inv lock` says the table is damaged, drops entries whose
  key does not match their body (so a hand-edited body is never shown as the old side of a diff)
  and rewrites it.
- **Diff and docs (R11-7).** A list whose members were replaced prints as one line,
  `types.return.args: ["int"] -> ["float"]`. Removing a `mediation.residual_paths` entry is a
  possible weakening (`inv status` withholds `held` while one is listed, so removing it can raise
  standing); adding one is not. The README no longer mentions fix rounds.

### Fix round 13 (certification round 10 findings)

From certification round 10 (one major, five minors, two nits). Regression tests:
`tests/test_fix_round13.py`. The suite passes on every backend and under `python -O`, and mypy is
clean. The two example apps changed only their rule files and locks (below), and their CI passes.

Behaviour changes:

- **Every field that changes what `bad` reads is a possible weakening (R10-1, major).** A plain
  `inv lock` used to re-pin these with exit 0: a rebound `bind`, an enforcer `view`, provider
  `args`, `from` or `type`, `model_policy` (more resamples or retries, a new schema, abstain or
  feedback), a removed `compares_to`, and an enforcer `artifact`. That includes an artifact digest
  rewritten by `--update-artifacts`. All of these are now WEAKENED and refused without
  `--accept-weakening`. So are `subject` (contract, operation, target), `scope`,
  `conformance_test`, `generated`, a target's `transaction`, `timing` moved to `post_effect`, and
  `compensable` set to true. Every field of `schema/invariants.schema.json` is classified in
  `lockfile.FIELD_POLICY` (weakening, ranked, more-is-weaker, presence, analysed, neutral, prose,
  pin, computed), and a field that is not listed is a possible weakening. A test walks the schema
  (and the loader's field sets), so CI fails on a new field without a classification. The
  loader's `model_policy` defaults are filled in before comparing, so writing a default out is no
  change.
- **`bad` is compared as a canonical tree (R10-2).** These count as the same condition, recorded
  but not WEAKENED:
  - whitespace;
  - commuted operands of `||`, `&&`, `==`, `!=`, and of `+` and `*` when an operand is plainly
    numeric (`+` may be string or list concatenation, so it is not reordered otherwise);
  - regrouped chains;
  - a disjunction split into clauses or merged from them.

  The polarity analysis now also decides an added or dropped disjunct or conjunct
  (`a > 1` to `a > 1 && b` is weaker). An edit it cannot decide is still a possible weakening.
- **Resolved types are pinned (R10-3).** Target, provider and function-contract signatures keep
  their text and gain `types`: each parameter and the return type, resolved with
  `typing.get_type_hints`. Aliases (`Caps = list[int]`, PEP 695 `type`), `Literal`, unions and
  `NewType` are expanded. A dataclass, TypedDict or pydantic model a signature names is pinned by
  its fields, recursively. Cycles are cut by name, and nesting is bounded at 64 nested classes or
  24 levels. These now make the lock stale: `Caps` from `list[int]` to `list[float]`,
  `JobFacts.elapsed_s` from int to float, `JobFacts.oom_killed` from bool to `bool | None`,
  `ResizeRequest.new_size` from int to float, `Approval.state` from str to `str | None`, and the
  python-side `LeaseIn.slots`.
- **Raw-SQL detection (R10-4).**
  - New forms are caught: `MERGE INTO` (insert, update and delete), comments between keywords
    (`INSERT /* hint */ INTO`, `DELETE -- c` then `FROM`), `UPDATE t AS x SET`, `UPDATE t x SET`,
    `UPDATE ONLY`, `DELETE FROM ONLY`, `INSERT` without `INTO` (and MySQL's `IGNORE` and the
    priority modifiers), and `ALTER TABLE IF EXISTS`.
  - Named, indexed and partial placeholders in the table position (`{table}`, `{0}`, `%(t)s`,
    `lea{t}`, `:name`) are reported `unresolved/computed-sql`.
  - What counts as door code is now decided by call-graph reachability, not lexical location. A
    helper reached only through a door or sink (and never used as a value) is door code. So is a
    module-level string constant used only by door or sink code. The two false positives are
    gone, and the same constant used anywhere else is still flagged. `scan.sql_writes(text)` is
    the new parser.
- **The sink's own SQL counts for held (R10-5).** `inv scan` collects the tables and statement
  kinds a rule's sink code writes: the sinks, what they call short of a door, and the constants
  they use. When `mediation.tables` does not cover them, it warns, whatever the sink's `effects`
  are, `external` included. `inv status` then does not report the rule held. The JSON report gains
  `sink_writes` and `uncovered_tables` per rule. Pool-service R-7 (an external sink running
  `INSERT INTO remediations`) is no longer reported held while a raw insert can bypass its door.
  With the new `tables` entry, that insert is a `forbidden/raw-sql` finding.
- **Lock version 3 (R10-6, breaking for lockfiles).**
  - A content-addressed top-level `schemas: {sha256: schema}` table holds every pinned schema.
    Targets, providers and contracts reference it as `{"$ref": "sha256:..."}`, and a schema change
    is reported once, with its users.
  - Prose (`title`, `description`, `summary`, `tags`, `examples`, `$comment`) is stripped at
    every level, so a docstring edit no longer re-pins. Property names are kept.
  - An OpenAPI operation is pinned as its method, path, `parameters`, `requestBody` and
    `responses` schemas. Shared `#/components/schemas` refs go into the table once, so the 422
    error body is no longer inlined per operation.
  - Rule status is stored once, and the duplicate `applies_to` expansion is dropped (`targets`
    holds it).
  - The text is written with sorted keys, two-space indent, and any value that fits in 128
    columns on one line. It is byte-identical across hash seeds and Python 3.12 and 3.14.
  - `inv lock --check` refuses a version-2 or version-1 lock with a hint. `inv lock` migrates
    version 2 by comparing rule semantics, effects, transactions, signature text, provider fields
    and contract references, so the weakening gate holds across the migration.
  - App locks shrink from 931 to 645 lines (30,622 to 27,253 bytes, pool-service) and from 1,092
    to 681 lines (34,426 to 30,886 bytes, fact-pipeline), while pinning more: resolved types and
    the fields of every model a signature names.
- **Diffs and robustness (R10-7).**
  - A dict-valued change prints one line per changed key (`model_policy.semantic_resamples: 0 ->
    3`) instead of two truncated dicts.
  - Comparisons are type-strict (canonical JSON), so `1` to `true` is a change, in the diff and in
    the freshness check.
  - A malformed lock (a non-integer or boolean `lock_version`, or a section or entry of the wrong
    shape) is a `lockfile.LockError` that names the file and the place. A deeper mismatch found
    while comparing is reported the same way, never as a raw TypeError.
- **Docs (R10-8).** The README's lockfile section, its scan section and the Limitations scan
  bullet are rewritten: tables are matched per table and statement kind. docs/rule-file.md gains
  the per-field classification table. The README perf table is re-measured with the r5 probe.

Example apps (rule files and locks only):

- **pool-service.** R-7 declares `tables: [{remediations: [insert, update]}]`.
- **fact-pipeline.** K-4, K-7, K-11 and K-18 add `{evidence: [insert]}`, because their sink
  `insert_fact_row` also writes evidence.
- **Locks.** Both locks were migrated to version 3. The rule edits showed up in the migration as
  reviewed, non-weakening `mediation.tables` changes.

### Fix round 12 (evaluation findings)

From the agent-defect experiment in [docs/evaluation.md](docs/evaluation.md). Regression tests:
`tests/test_fix_round12.py`. The suite passes on every backend and under `python -O`; mypy is
clean. The two example apps changed only their rule files and locks (below), and their CI passes.

Behaviour changes:

- **Lock pins at contract granularity (lock version 2, breaking for lockfiles).** `inv lock --check`
  failed on 18 of the experiment's 24 changes, 16 times only because a whole-module contract hash
  moved, which trains people to re-pin by reflex. The lock now pins each target's signature,
  effects, transaction and model-call reply schema; each provider's declaration and signature
  (return type included); each rule's semantic content, with prose fields as digests; and each
  contract as its resolved schema (pydantic, dataclass, TypedDict, `{field: type}`), its named
  OpenAPI operations (local `$ref`s resolved), a function's signature, or a file digest. A
  `python` module contract no longer hashes the module. A body-only refactor, a new function, or a
  new unreferenced endpoint leaves the lock fresh. In the re-run, lock failures fell from 18 of 24
  to 2 (P04, F12), and benign changes needing a re-pin from 6 of 8 to 1 of 8.
- **Semantic lock diff, WEAKENED, and `--accept-weakening`.** Every change is listed as
  `<where>: <what>: old -> new`. A change that relaxes a rule is marked WEAKENED: lower status or
  mode, removed enforcer, clause or rule, `on_violation` below reject, `on_error:
  fail_open_and_page`, staler provider consistency, removed sink or table or statement kind, an
  added door on a scanned sink, a target declared pure, a target no longer covered, and a `bad`
  that matches fewer cases. The direction of a `bad` edit is decided by a polarity analysis over
  the CEL parse tree when the edit changes only numeric literals, list-literal elements or
  comparison strictness; any other `bad` edit is a possible weakening. `inv lock` prints the diff
  and refuses to write a weakened change without `--accept-weakening` (P04's 4x to 10x cap is now
  refused on a reflex re-pin).
- **Lock versions.** `inv lock --check` refuses a version-1 lock with a migration hint; `inv lock`
  migrates it, requiring `--accept-weakening` when a rule changed since its version-1 hash. A lock
  newer than the library is refused.
- **Pydantic schema pin (F12).** A pydantic contract was hashed as `inspect.getsource(Model)`, so a
  `Literal` in a module-level alias changed the schema without moving the pin. It is now
  `model_json_schema()`, canonicalised; dataclass and dict schemas pin their resolved annotations.
- **`mediation.tables` per statement kind.** An item may be `{table: [insert, update, delete, ddl]}`
  (schema and loader updated). The raw-SQL check reads concatenated literals and schema-qualified
  names, reports which statement it found, and reports a write to a computed table name as
  `unresolved/computed-sql`.
- **Warning for rules that protect stored data without `mediation.tables`.** A rule whose door
  guards a `transactional` effect or that has a storage enforcer, and that declares no tables, gets
  a scan warning (text output, JSON `warnings`, stderr for json/sarif) and `inv status` no longer
  reports it `held`: raw SQL around the door is invisible to the scan. The evaluation's raw
  `INSERT INTO leases` probe now fails `inv scan` for pool-service.
- `inv status` rows carry `scan_warning`. `inv lock` output now also counts providers.

Example apps (rule files and locks only): pool-service declares `tables: [{leases: [insert,
update]}]` on R-20 and `tables: [{pools: [update]}]` on R-21 and R-22; fact-pipeline gives K-4, K-7,
K-11 and K-18 the ingest door's sink and `tables: [{facts: [insert, update]}]`, and K-16 the
`fact_status` sink, doors and tables of K-12. Both locks were migrated to version 2. K-15 still
warns: its effect (`UPDATE sources`) is also written by `add_source`, so no table spec fits
without a code change.

### Fix round 11 (certification round 9 findings)

Regression tests: `tests/test_fix_round11.py` (fixture `tests/fixtures/round11.py`), the new
conformance suite `tests/conformance/cel/keys.yaml`, and new cases in `nonfinite.yaml` (a NaN in a
value compared whole) and `time.yaml` (ASCII digits, zone names with a NUL or control character).
Each test encodes a behaviour the round-9 probes showed wrong before this round, on every backend.
The suite passes on every backend and under `python -O`; both example apps' CI passes unchanged.

Behaviour changes:

- **Whole-value equality with a nested NaN (minor, R9-1).** On the subset, a list, map or record
  compared whole (`==`, `!=`, `in`) used Python's `==`, so a NaN inside it was not noticed (and one
  shared NaN object compared equal to itself): `args.items != args.items` was `ok`, where cel-expr
  and celpy gave a `rule_error`. The subset now reads such a value whole through the engines'
  conversion (`_engine.to_plain`): a NaN or ±Infinity anywhere in it is a `rule_error`, a record is
  the map of its fields (so it equals a map with the same fields, as on the engines), and nested
  values compare by CEL's rules, never by identity. With `allow_non_finite=True` a list holding a
  NaN is not equal to itself on every backend.
- **`model_of()` and edges (minor, R9-2).** `model_of(c)` returned the instance stored on the reply,
  a mutable pydantic model the edge never re-checked, so a consumer could read a value mutated or
  swapped after the model call. Now `model_of(c)` validates the reply's JSON attributes again on
  every call and returns a new instance (a tampered reply raises `ParseError`), and an `@inv.edge`
  calls its consumer with a new `Checked` over the re-validated copy the rule judged, not the
  object it received. `Checked` now has `==` (same class, rule, verdict, attempts and an equal
  value) and a matching hash, so the copy equals the original.
- **pydantic's API is not a field (nit, R9-3).** `BaseModel` attributes (`model_fields`,
  `model_config`, `copy`, `json`, ...) counted as fields: `reply.model_fields == 1` loaded (and was
  `ok` on the subset, a `rule_error` on the engines), and a "no field" error listed about 30 of them.
  Now only model fields, computed fields and your own properties are fields, at load and at run
  time, for replies and arguments; the error lists just those.
- **ASCII digits and zone names (nit, R9-4).** Duration, timestamp, offset and ISO 8601 parsing, and
  the subset tokenizer, use ASCII digits only (`re.ASCII`): `"PT٥M"` was 5 minutes and `"+٠٥:٣٠"` a
  valid zone on two backends. A zone name with a NUL, space, control or non-ASCII character is
  refused on every backend (a `LoadError` as a constant, a `rule_error` from data). cel-expr cut
  every string or bytes value at its first NUL (`"ab\x00cd" == "ab"` was true there); a clause that
  reads a value holding a NUL is now a `rule_error` on cel-expr.
- **Map keys and indexes (nit, R9-5).** `{1: 'a'}[1.0]` was `'a'` on the subset and cel-expr but a
  `rule_error` on celpy. Map lookup and `in` now follow CEL on every backend: numeric keys match
  across int and double, a bool never matches a number (the subset and celpy took `{true: 'a'}[1]`
  and `1 in {true: 'a'}`), a list index may be an integral double (`[7, 8][1.0] == 8`; celpy took
  `[7, 8][true]`), and a map literal key must be an int, bool or string (`{1.0: 'a'}` and
  `{null: 1}` evaluated on the subset and celpy; `{[1]: 1}` raised a raw `TypeError` on the subset).
  A repeated map literal key is a `rule_error` on every backend.
- **sdist tests (nit, R9-6).** The sdist ships `.gitignore` (`MANIFEST.in`), the `.gitignore` test
  skips when the file is absent, and the mypy return-type test puts only a source checkout on
  `MYPYPATH` (mypy refuses a site-packages directory there), so the sdist's own tests pass against
  an installed package.

### Fix round 10 (certification round 8 findings)

Regression tests: `tests/test_fix_round10.py` (fixture `tests/fixtures/round10.py`), the new
conformance suite `tests/conformance/cel/numeric.yaml`, and new cases in `time.yaml` (timezones
from data) and `pyvalues.yaml` (only read element fields are converted). Each test encodes a
behaviour the round-8 probes showed broken before this round, on every backend. The suite passes on
every backend and under `python -O`; both example apps' CI passes unchanged (neither app mixes int
and double arithmetic in a rule).

Behaviour changes:

- **Typed model replies (major).** A pydantic reply's `datetime` and `timedelta` fields reached
  rules as their JSON text, so `reply.at < timestamp(...)` or `reply.wait > duration('1h')` loaded
  and then made every model call a `rule_error`; reply fields were not type-checked (a
  `NaiveDatetime` reply field loaded). Now rules read the validated model instance through the same
  Python-to-CEL mapping as arguments (`cel/values.py`), at the top level and in nested models,
  lists and dicts: a `datetime` is a timestamp, a `timedelta` a duration, a `UUID` a string, an
  `Enum` its value. This holds for `model_call`, the edge re-check, and a `Checked` that was
  deep-copied or unpickled (each validates again from the reply's attributes, so a tampered
  attribute is still caught). `reply` is type-checked against the model's annotations exactly like
  `args`: `reply.at == 'x'` is a `LoadError`, and so is a clause that reads a `NaiveDatetime`,
  `Decimal` or `date` reply field. The reply's own attributes keep their JSON form, as documented
  (`c.cause` is the enum's value); `model_of(c)` is the typed instance. Dataclass and
  `{field: type}` schemas could not hold a datetime at all; now a `datetime` field parses RFC 3339
  text with a timezone (no timezone: `parse_error`), a `timedelta` field CEL duration text or ISO
  8601 (`"PT5M"`, as pydantic writes it), and a `UUID` field its text, with value-free error text.
- **Timezone arguments (minor).** A constant offset of 24 hours or more (`getHours('+25:00')`) made
  `inv.load` raise a raw `ValueError`; it is now a `LoadError` ("unknown timezone"), and minutes
  must be under 60. A timezone read from data is checked by the shared rules on cel-expr too
  (routed through `inv.__zone`), so `""` and `"+99:00"` are a `rule_error` on every backend
  (cel-expr read `""` as UTC and wrapped large offsets).
- **No int-double arithmetic (minor).** The type checker treated int and double as one family, so
  `args.price * args.qty` (float times int) loaded and was a `rule_error` on every call on the
  subset and cel-expr while celpy computed it. CEL has no implicit numeric conversion: int, uint and
  double are now separate kinds for arithmetic, and the mixed form is a `LoadError` that suggests
  `double(args.qty)`; `%` on doubles is a `LoadError` too. Comparisons across int and double stay
  allowed (CEL's heterogeneous numeric comparison). At run time celpy now refuses mixed arithmetic
  and the subset refuses `double % double`, as cel-expr and CEL do.
- **Only read element fields are converted (improvement).** A list read only through a
  comprehension's variable (`args.items.exists(i, i.qty < 0)`) or through `size()` now converts just
  the fields the body reads (or nothing but the length), on the engines as in the type checker
  (`cel.subset.reads`, `_engine.project`), so an `Item` with a `Decimal` price loads when the rule
  never reads the price. `filter()`, indexing, `in` and any use of the whole element still read it
  whole and are refused as before. A cyclic or non-finite value in an unread field is no longer
  noticed by the engines, as on the subset.

### Fix round 9 (certification round 7 findings)

Regression tests: `tests/test_fix_round9.py` (fixture `tests/fixtures/round9.py`) and the new
conformance suites `tests/conformance/cel/time.yaml`, `pyvalues.yaml` and `nesting.yaml`, which run
on every backend (the harness now decodes Python values in a case's env, `{$timestamp: ...}`,
`{$duration: ...}`, `{$uuid: ...}`, `{$naive: ...}`, `{$date: ...}`, `{$decimal: ...}`, and a case may
name the `backends` it runs on, for `type()`). Each regression test encodes a behaviour the round-7
probes showed broken before this round. The suite passes on every backend and under `python -O`.

Behaviour changes:

- **datetime, timedelta and UUID values work in rules; Decimal, date, time and naive datetimes are
  refused clearly (major).** These values loaded fine and then made every call a `rule_error`
  ("unsupported value type datetime"; a UUID read as "integer value out of int64 range"), even
  `x == null` and `has(x)` on cel-expr and celpy, so an expiry, deadline, lease or money rule was
  an outage under `fail_closed`. Now one documented mapping (`invariants/cel/values.py`, README
  "Python to CEL types", docs/rule-file.md) is used by every backend and by the load-time type
  checker: an aware `datetime` is a timestamp (in UTC), a `timedelta` a duration, a `UUID` its
  string, an `Enum` its value, `None` is null (`== null` and `has()` work for every `Optional`
  field). A naive `datetime` is refused (the library never assumes a timezone): a `rule_error` at
  run time, a `LoadError` for pydantic's `NaiveDatetime`. `datetime.date`, `datetime.time`,
  `Decimal` (CEL has no decimal, and a double loses money precision: pass int minor units or a str),
  `Fraction`, `complex` and sets are refused with the conversion to make: a `LoadError` when the
  clause reads a path annotated with one, or a record, list or map whose annotation holds one (the
  engines convert what a clause reads whole), else a `rule_error` naming the type. The type checker
  knows timestamp and duration as families of their own (CEL's ts ± dur, dur ± dur, ts − ts; no
  `-dur` or `dur * n`), UUID as string, refuses a field read on a timestamp, and checks a constant
  `timestamp('...')`/`duration('...')` and a getter's constant timezone at load. On cel-expr,
  timestamps and durations reach the engine as protobuf `Timestamp`/`Duration` messages (cel-expr-
  python converts a Python datetime only into a variable declared as one), so the `cel` extra now
  includes `protobuf`; without it a clause that reads an annotated datetime is a `LoadError` saying
  so. The subset now has `timestamp()`, `duration()`, timestamp and duration values (comparison,
  arithmetic, `string()`, `int(timestamp)`) and the ten getters, with an optional IANA or offset
  timezone. `timestamp()`/`duration()` of text, `string()` of a timestamp or duration and the
  getters follow one set of rules on every backend (RFC 3339; `1h30m`-style durations;
  sub-microsecond values refused; `"5400s"`, `"-1.500s"`, `"...T09:30:00.500Z"`). A value of a
  scalar type (datetime, UUID, ...) has no fields on any backend (`args.deadline.year` used to read
  the Python attribute). On the subset, list elements compared with `==` or `in` go through the
  mapping too (`uuid in list_of_uuids`, `[Enum.A] == ["a"]`), and a set is refused as on the engines.
  **Upgrade note:** a rule that reads a `Decimal`, `date` or set, or a record holding one, used to
  load and now does not; and `timestamp()`, `duration()` and the getters now load on the subset.
- **cel-expr loads long arithmetic and deep nesting again (minor).** Fix round 8's non-finite
  rewrite wrapped every `+`, `-`, `*`, `/` in a `cel.bind` check, so a 16-term sum or 15 nested
  multiplications passed cel-cpp's fixed 32-level recursion limit and failed to load with an engine
  message naming `__inv_v`. Now the root of each arithmetic tree is checked once, and each divisor
  that is itself arithmetic (a NaN or ±Infinity operand of `+`, `-`, `*` or a numerator always makes
  a non-finite result; only a divisor can hide one, `x / Infinity == 0`), plus each `double()`. A
  `+`/`-` chain longer than 8 terms is printed in nested `cel.bind` chunks, which keep its left-to-right
  order (and so its rounding and int overflow), and `&&`/`||` chains are printed flat so the parser
  balances them: a 200-term sum, 29 nested operations and a 100-clause `||` chain with arithmetic
  load. A rule nested past the limit anyway is a `LoadError` that names the rule and cel-cpp's 32-level
  limit, without the adapter's identifiers. The engine text for a 50-term sum is 842 characters
  (was 5,798) and the check costs about 0.1 to 0.2 µs per arithmetic expression.
- **celpy computed values are celtypes (minor).** celpy 0.5 returns plain `float`, `int`,
  `timedelta` and `datetime` from arithmetic, so `type(args.a * 2.0) == double` was false,
  `string(duration('2s') - duration('1s'))` was `'0:00:01'` and `.getSeconds()` of a computed
  duration had no overload. The adapter wraps results back into `DoubleType`, `IntType`,
  `DurationType` and `TimestampType`. celpy's `-duration`, `duration * n` and `duration / n` (no
  overload in CEL) are errors, `duration.getMilliseconds()` is the millisecond part (was the total),
  `string(timestamp)` is in UTC (kept the offset), `int(timestamp)` rounds down before 1970 and
  `timestamp()`/`duration()` text follow the shared rules (celpy took `'2024-03-10'` and `'1d'`, and
  ignored an offset written `+0530`).

### Fix round 8 (certification round 6 findings)

Regression tests: `tests/test_fix_round8.py` (fixture `tests/fixtures/round8.py`) and the
conformance suite `tests/conformance/cel/nonfinite.yaml` plus new cases in `types.yaml`. Each
regression test encodes a behaviour the round-6 probes (or this round's own probing) showed broken
on the code before this round; the exceptions are the tests that check unchanged behaviour still
holds (plain JSON still parses, finite arithmetic, CEL's global forms of `contains`/`matches`,
`type(x) == int` on celpy). Most run on every installed CEL backend
whatever `INV_CEL_BACKEND` says, and the suite also passes under `python -O`.

Behaviour changes:

- **Pydantic replies are validated strictly (major).** A pydantic schema validated in pydantic's
  lax mode, after the core's non-finite check. So the strings `"NaN"`, `"inf"` and `"-Infinity"`
  became float NaN and Infinity, and `@inv.model_call` returned verdict ok holding a NaN (a rule
  that did not read the field passed; the edge then refused the same value). `"0.5"` and `true`
  became floats too, where a dataclass schema refuses them. Now a pydantic reply is validated with
  `model_validate_json(strict=True)` over its JSON form: a string or a boolean is not a float, on
  every field, nested models, `list[float]` and `dict[str, float]` included. Then the validated
  instance is walked, and a NaN or ±Infinity anywhere in it is a `parse_error`. A model whose
  `model_config` sets `strict=False` explicitly keeps pydantic's lax coercion, and the walk still
  refuses non-finite values. `inv.parse`, `@inv.model_call` and `@inv.edge` agree. **Upgrade
  note:** a model that relied on lax coercion (`"0.5"` for a float, `1` for a bool, `"1"` for an
  int) now gets a `parse_error`; set `strict=False` in its `model_config` to keep the old behaviour.
- **A NaN or ±Infinity made inside a clause is a rule_error on every backend.** `0.0 / 0.0`,
  `x / 0.0`, an overflow (`1e200 * 1e200`, and `inf - inf` after it), `double("NaN")` and an
  `inv.sum` that overflows gave three different answers (subset: rule_error; cel-expr: IEEE, so a
  reply `{"a": 0.0, "b": 0.0}` passed `reply.a / reply.b < 0.0 || reply.a / reply.b > 1.0`; celpy:
  true for every comparison with NaN). Now every `+`, `-`, `*`, `/`, `double()` and `inv.sum`
  result is checked on every backend: a non-finite double is a `rule_error` (`fail_closed`
  rejects). The subset checks in its evaluator; cel-expr gets the rule text rewritten so each such
  operation's result passes through a check (`cel.bind` evaluates it once, and the Python check
  runs only on a non-finite double; it costs about 0.25 µs per arithmetic operation); celpy has
  its operators and `double()` overridden. `inv.configure(allow_non_finite=True)` restores IEEE
  semantics, now on the subset too (`1.0 / 0.0` is Infinity there, not "division by zero"). Integer
  division by zero is still a `rule_error` everywhere. With this, NaN and Infinity never get past a
  range check, whether they come from a reply, an argument or the clause's own arithmetic.
- **Call arity and the global macro form are checked at load.** `size(x, 1)`, `x.size(1)`,
  `size()`, `'a'.startsWith()`, `matches('a')`, `'a'.matches('a', 'b')`, `x.has(y)` and the like
  loaded and then were a `rule_error` on every call. The shared front end now has an arity table
  per standard function, global and receiver form (`cel.subset.ARITY`), and the loader refuses any
  other shape with a `LoadError` that says what to write. `exists(xs, c, p)` (a macro written in
  global form) is a `CelSyntaxError` in the parser, so a `LoadError` on every backend; on the
  subset it was an internal `AssertionError` (a `TypeError` under `python -O`). The library no
  longer has any `assert` statement; a test checks this, and one runs the checks under `-O`.
- **Overlapping `inv.ablated(scope="registry")` blocks are counted.** When two threads ablated the
  same rule and the first left its block, the rule was enforced again for the second. The registry
  scope is now a counter under the registry's lock (like `ablated_everywhere`): the rule stays
  ablated until the last block exits.
- **parse_error text in exceptions, events and logs carries no reply values.** A reply value
  (`'SECRET-PII-1' is not one of [...]`), an extra key the reply made up or a `dict[str, T]` key
  went into the exception message, `Outcome.error`, the attempt record, the event and the WARNING
  log line. Now `str(ParseError)` names the field and the kind of error only ("field cause: string
  is not one of the allowed values", "extra fields forbidden (1 not in the schema)", reply-made
  keys as `*`; pydantic errors as `loc: error_type`). The full text is in `ParseError.feedback`,
  which is still what the model gets as `inv_feedback`; outcomes, events and logs use it only with
  `debug` on.
- **Conversions agree across engines.** celpy accepted Unicode digits (`int('٣')`), underscores
  (`int('1_0')`), hex text (`int('0x10')`), `int(-9223372036854775808.0)` and `bool(1)`, and
  printed `string(true)` as `True` and `string(null)` as `None`; cel-expr accepted hex text in
  `double('0x10')`. With `allow_non_finite=True`, celpy gave +Infinity for every `x / 0.0`
  (`0.0 / 0.0` and `-1.0 / 0.0` included); it is IEEE now, as on the other backends. The adapter now
  routes these through the subset's CEL-spec conversions on both engines, so all three give the
  same answer (conformance cases in `types.yaml`; `expected_failures.yaml` stays empty). The
  README's "Known engine differences" lists what still differs (`string()` of a double, the
  subset's missing types, the cost budgets).
- **celpy compares computed durations and timestamps correctly** (found during this round).
  celpy's arithmetic returns a plain `timedelta`/`datetime` where a literal is its own type, and
  the adapter's equality saw two types: `duration('2s') - duration('1s') == duration('1s')` was
  false, so a clause using it never fired. Values are now normalised to one type each.
- **Changelog.** This section is now `## [Unreleased]` (Keep a Changelog), the heading
  `CONTRIBUTING.md` points to; `RELEASING.md` says how to turn it into the 0.1.0 entry.

### Fix round 7 (certification round 5 findings)

Regression tests: `tests/test_fix_round7.py` (fixture `tests/fixtures/round7.py`); every
regression test in it failed on the code before this round (the parity tests that check the
backends agree on conversions passed on the engines before, and fail on the subset). Most run on
every installed CEL backend whatever `INV_CEL_BACKEND` says.

Behaviour changes:

- **NaN and Infinity in replies and rule inputs never get past a range check (blocker).** (Values
  made by a clause's own arithmetic were caught on the subset only until fix round 8.) `inv.parse` used Python's
  `json.loads`, which accepts the non-JSON tokens `NaN`, `Infinity` and `-Infinity`, so a reply
  `{"confidence": NaN}` passed `reply.confidence < 0.0 || reply.confidence > 1.0` (every IEEE
  comparison with NaN is false) with verdict ok, on every backend, and the edge accepted it. Now
  those tokens are a `parse_error`, and so is any non-finite float in a decoded value (a number
  like `1e999`, an already-decoded reply, an `Any` field, a nested list), for dict, dataclass and
  pydantic schemas, in `inv.parse` and in the edge's re-check. For values that do not come from a
  reply (guard arguments, results, providers, a FastAPI body, since pydantic accepts NaN by
  default), a clause that reads a NaN or ±Infinity is now a `rule_error` on every backend
  (`fail_closed` rejects; the FastAPI integration answers 503). `inv.configure(allow_non_finite=True)`
  restores IEEE semantics; the README documents the NaN-safe clause form `!(x >= lo && x <= hi)`.
- **Function names are checked at load (major).** A clause calling a function the chosen backend
  does not have (`nosuchfn(x)`, `x.foo()`, `xs.exist(...)`, or `timestamp()`, `duration()`,
  `uint()`, `bytes()`, `type()` on the subset) loaded and then was a `rule_error` on every call.
  Now it is a `LoadError` naming the function and the backend. Each built-in backend declares its
  names (`functions`); cel-expr and celpy share the CEL standard list, which a test checks against
  both engines. The subset now implements the conversions `int()`, `double()`, `string()`,
  `bool()` and `dyn()` per the CEL spec (`string()` of a double is a `rule_error`, since the
  engines print doubles differently); the load-time type check knows their result types and
  refuses `x.int()` and wrong arities. The README lists the standard functions per backend.
- **`--inv-ablate` no longer counts a rule-error pass as evidence.** A falsifier whose control
  run gave the rule only `rule_error` outcomes (never `violation` or `parse_error`) is reported
  `inconclusive (rule_error, not violation)` rather than `caught`: it passed because the rule
  could not be evaluated. An `enforce` rule with no caught falsifier still fails the session.
- **An edge enforcer needs the model call's rule.** An `edge` enforcer on a rule without
  `model_policy` loaded, and its edge did no schema re-check. It is now a `LoadError`.
- **rule_error text carries no request values.** Missing map keys, bad dynamic regex patterns and
  values quoted in engine errors went into `Outcome.error`, events, exception messages and logs.
  Now only the error kind is reported ("no such key", "invalid regular expression", "index out of
  range", "conversion failed", ...; a field or key named in the rule text is still named), on
  every backend; the engine's own text is added only with `debug`. cel-expr's RE2 still writes a
  bad pattern to stderr itself, which the library cannot stop: documented in README and SECURITY.
- **`inv.ablated()` is context-local.** It turned the rule off for every thread and task using the
  registry; now only for the current thread or task (and tasks started in the block).
  `inv.ablated(rule_id, scope="registry")` keeps the registry-wide behaviour for falsifiers with
  worker threads; `--inv-ablate` still ablates in every registry, thread and subprocess.
- **`inv.load()` copies a dict source.** It wrote defaults into the caller's dict and kept it as
  `Rule.raw`, so editing the dict after load changed live guards. The source is now deep-copied
  (an uncopyable document is a `LoadError`).
- **Performance table re-measured** with p50 and p99 per backend, the machine, the Python
  version and the rule shape (an 8-element list for the `exists` clause).
- **Publishing checklist.** `RELEASING.md` lists what to settle before the first upload; the
  CHANGELOG's TODO comment is gone; the note at the top of this release points there.

### Fix round 6 (certification round 4 findings)

Regression tests: `tests/test_fix_round6.py` (fixture `tests/fixtures/round6.py`); every test in it
failed on the code before this round.

Behaviour changes:

- **No exception escapes a model call from a hostile reply.** `inv.parse` raises only `ParseError`,
  whatever the reply holds: invalid UTF-8, non-object JSON, non-string keys, nesting deep enough to
  exhaust the stack, a too-long reply (`max_reply_size`, default 1,000,000 characters, also
  `INV_MAX_REPLY_SIZE` and `inv.configure(max_reply_size=...)`), objects whose `__eq__`, `__hash__`
  or `__repr__` raise, a dict subclass that raises, a schema `__post_init__` that raises. Each is a
  `parse_error`, retried per `format_retries`, then `on_violation`, with its event. Before, these
  escaped as `UnicodeDecodeError`, `TypeError`, `RecursionError` or the hostile object's own error,
  skipping retry, escalation, review and the event. Enum membership compares by exact plain type
  (no user `__eq__` runs; `True` is not `1`), and error text shows values through a short, safe
  repr. A hypothesis fuzz test feeds arbitrary JSON, text and bytes to dict, dataclass and pydantic
  schemas.
- **Typed fields are checked, not crashed on.** `list[T]`, `dict[str, T]`, `Optional`/unions and
  nested dataclasses are validated field by field (before: `TypeError` from `isinstance` on a
  subscripted generic). A field type parse cannot check is a `TypeError` when `@inv.model_call`
  decorates. An `int` is accepted for a `float` field.
- **Abstain works for every schema kind.** A `{field: type}` reply whose abstain field holds the
  abstain value now escalates, as dataclass and pydantic replies did; before it came back with
  verdict ok.
- **The edge re-validates the closed schema.** An `@inv.edge` re-parses the received value against
  the model call's closed schema before it re-evaluates `bad`, for all schema kinds: forged extra or
  missing fields, out-of-enum values, wrong types, a subclass of the schema dataclass, a cyclic value
  and a verdict that is not a plain string are refused (`parse_error`, or `rule_error` for the
  verdict). `bad` runs on the validated copy. Before, a dict-schema value was not re-checked at all,
  and a forged cyclic dataclass raised `RecursionError` out of the edge.
- **A failing review store is explicit.** When the review store raises or returns no id, the new
  `review_fallback` store is tried (`inv.configure(review_fallback=inv.JsonlReviews(path))`;
  `JsonlReviews` appends each hand-off as an fsynced JSON line). When no review can be opened,
  route, escalate and abstain raise the new `inv.ReviewNotOpened` (an `InvariantError`) carrying
  `.handoff`, `.action` and the store error as `__cause__`; the ASGI/FastAPI mapping answers 503.
  Before, `Routed` was raised with `review_id=None` and the work survived only in a log line.
- **Redaction.** A property, mapping lookup or provider that raises while a rule reads it appears in
  `rule_error` text, events and logs as its exception type only, on every backend. The message is
  kept on `CelError.detail` and added only with `inv.configure(debug=True)` or `INV_DEBUG=1`. Before,
  the subset and engine field readers put the message (which may hold secrets) into the error.
- **`Checked` supports copy, deepcopy and pickle.** `copy.copy` returns the same immutable object;
  `copy.deepcopy` and pickle rebuild an equal `Checked` over a copy of the value (closed pydantic
  replies pickle by their model and are re-validated on load). Copies carry no extra trust: the edge
  re-validates them. `Checked` no longer lends the wrapped value's dunder protocols.
- **`@inv.model_call` keeps the declared return type** (also `invariants.integrations.pydantic.model_call`):
  the decorated function is typed `Callable[P, CheckedX]`, not `Callable[P, Any]`.
- **Abstain declarations are checked.** An abstain `field` that is not a string or not in a
  `{field: type}` schema, and a `value` that is not a scalar, are `LoadError` (before: `AttributeError`
  or `TypeError` at load for a dict schema).
- **cel-expr cost limit, enforced and named.** The engine's fixed comprehension iteration budget
  (9,999 iterations per clause evaluation, nested comprehensions multiplying) now surfaces as
  `rule_error` "cost limit exceeded (cel-expr: ...)"; `ExprPythonBackend.iteration_limit` states it.
  README and SECURITY say exactly what is and is not budgeted.
- **Reports carry no local absolute paths.** The pytest plugin writes `rules_file` relative to the
  pytest rootdir. The apps gained a `.gitignore` (build outputs, caches, environments), and their
  stale `build/` reports, which held absolute local paths, are removed; the library's stale
  `build/` copy is removed too.

Not changed:

- `project.urls` stays out of `pyproject.toml` until a real URL exists (see "Before publishing" in RELEASING.md).

### Fix round 5 (certification round 3 findings)

Behaviour changes:

- **A guarded call binds to one rule generation.** A call reads its rules and the provider table
  together when it starts (`Registry.generation`) and binds providers from that table through its
  post-effect check, so an `inv.load` (hot reload) during the effect no longer raises `KeyError`
  from the async path or gives a spurious `rule_error` on the sync path.
- **Non-CEL exceptions no longer escape provider binding.** A mapping argument whose lookup raises
  (a dict subclass with a hostile `__getitem__`) is a `rule_error` on the sync, async and
  snapshot-provider paths; before, it escaped the async pre-effect and the snapshot-provider paths.
  `prefetch_async` never raises (it returns why it failed) and the guard turns that into a
  `rule_error` for that rule.
- **More load-time type checks.** `has()` of a field a closed type does not have, `in` with an
  element of another family than the list's elements or a map's keys, and a constant `matches()`
  pattern that is not valid RE2 are `LoadError`. Subset-only refusals (unsupported `\p{..}`
  classes, oversized programs, escapes full RE2 may take) still load.
- **Env ablation is quiet only inside a real pytest session.** `under_pytest()` is true only while
  the plugin's `pytest_configure` has run in this process, not whenever `pytest` is importable or
  imported. A production process that imports pytest through a dependency now logs (and writes to
  stderr) that ablated guards are off.

Documented, not changed:

- The subset backend's `matches()` step bound is now spelled out in the README next to the
  per-backend limits: a long input (tens of thousands of characters for a small pattern) is a
  `rule_error` on the subset and a result on cel-expr and celpy. Scaling the bound with input
  length was tried and rejected: it lets a caller buy CPU time with input size, once per
  `matches()` in a loop.

### Fix round 4 (review findings)

Behaviour changes:

- **Unknown fields on closed types are `LoadError`.** `rec.sizee` on a dataclass, pydantic model
  (not `extra='allow'`) or TypedDict without that field, and any field on an int, string, bool,
  bytes or list (`args.n.foo`), are refused at load. Before, only the first step of a path was
  checked and these gave `rule_error` on every call. Properties and class attributes count as
  fields; `Any`, plain `dict` and other classes stay open. `T | None` now keeps `T`'s fields.
- **Sync enforcer reading an async provider is `LoadError`.** A decorator, middleware, edge or
  model_call enforcer whose target is a sync function cannot await an `async def` provider. Before,
  the rule loaded and gave `rule_error` on every call (so `fail_open_and_page` never enforced).
- **Equality across type families is `LoadError`.** `==`/`!=` between disjoint families
  (`args.n == "x"` with `n: int`) is refused, as standard CEL checkers do. `null` and dyn compare
  with anything; int and double are one family.
- **Env ablation outside pytest is loud without logging config.** When the `invariants` logger has
  no real handler (the library's NullHandler suppresses Python's last-resort handler), the ERROR
  line is also written to stderr. `PYTEST_CURRENT_TEST` alone no longer counts as a pytest run.
- **An enforcing edge refuses any received verdict but ok.** A `Checked` whose verdict is
  `rule_error` (a model call that failed open) or that has no verdict is refused by the edge's
  `on_violation`, whatever its `on_error`; `on_error` covers only the edge's own re-evaluation
  errors (guide 2.5, 2.6). Before, an edge with `fail_open_and_page` paged and let it through even
  when `bad` held on the value.
- **`--inv-ablate` counts `caught` only when the ablation caused the failure.** Each falsifier is
  re-run once without ablation (it must pass) and once ablated, and the ablated run must reach the
  rule: a guard, `inv.is_ablated` or `inv scan` acting on the ablation, in this process or a
  subprocess (they note it in the file the new `INV_ABLATE_TRACE` names). Otherwise the row is
  `inconclusive (failed on a re-run without ablation)` or `inconclusive (rule not reached)`.
  Before, any failed re-run was `caught`, so a stateful or flaky test that never touched the guard
  made the rule "load-bearing". A subprocess falsifier must act on the ablation through the
  library, not by reading `INV_ABLATE` itself.

### Fix round 3 (review findings)

Behaviour changes:

- **No ReDoS in `matches()` on the subset backend.** It now runs on a linear-time RE2-syntax matcher
  (`invariants.cel._regex`, a Pike VM) instead of Python `re`, and its steps count against the cost
  budget. `^([a-z0-9]+-?)+$` on `"a"*27 + "!"` took 4.7 s and now takes well under a millisecond; a
  huge input ends in `rule_error`. RE2 semantics now match cel-expr and celpy: `\d \w \s` are ASCII,
  `$` matches only at the end of text. `\p{Script}` classes are refused (general categories work).
  New conformance cases in suite `regex`.
- **Nested transactions.** A failed guard dooms every open block of the same `@inv.transaction`
  boundary and owner, so a re-entrant `txn()` raises `RolledBack` at the outermost exit even when
  an inner failure was swallowed (before, the outer block committed the rejected write).
  `@inv.transaction(savepoint=True)` stops the doom at that block, for real savepoints.
- **Type check at load.** Clauses are type-checked against target annotations, provider types and
  the model-call schema. Ordering or arithmetic across type families, logic on a non-bool, `in` on
  a non-container, string functions on a non-string and a non-bool clause are `LoadError` (and
  fail `inv validate`). Before, such a rule loaded and gave `rule_error` on every call.
- **`py:` guard keys fail closed.** A guard keyed by `py:...` raises `LoadError` when no rule file
  is loaded in the current registry, or when the key matches no loaded rule and no `targets:` entry.
  Before, it returned unchecked.
- **classmethod/staticmethod.** `@inv.guard`, `@inv.model_call` and `@inv.edge` work above
  `@classmethod`/`@staticmethod` too (before: a raw `inspect` TypeError at import).
- **Edge parse_error text** names the closed schema and the received type, not "Reply, not Reply".
- **Env ablation outside pytest** (`INV_ABLATE` with `INV_ABLATE_TESTING=1`) logs at ERROR.
- **Typing and packaging.** mypy is clean and pinned in the `dev` extra (`mypy==2.4.1`); CI installs
  it from there. `invariants.integrations.fastapi` re-exports `PROBLEM_JSON`, `StatusMap` and
  `problem`, and the integrations declare `__all__`. A `.gitignore` is added; `build/` and `dist/`
  are removed from the tree.
- **Docs.** README: exit code 4, nesting, Performance section, type-check scope, the schema-held
  ablation gap, environment ablation, and why spike demotion stays off by default.

### Fix round 1 (from the two outside apps and the review)

Behaviour changes:

- **Bounded sink.** `Registry.sink` is a ring buffer (default 1000 events, `INV_SINK_SIZE`,
  `inv.configure(sink_size=...)`; 0 keeps none). Reach counters stay exact. Use `inv.capture()` or
  the `inv_events` fixture to collect events in tests. Memory stays flat over 1e5 calls.
- **Transactional boundary checked before the effect.** A transactional target called outside its
  own `@inv.transaction` (or under another instance's) gives `rule_error` before the function runs,
  for every live enforcer of a rule that needs the boundary (a post-effect enforcer or a `same_txn`
  provider). Before, the effect ran and committed first.
- **Provider timing.** A `post_effect` enforcer reads its providers after the effect (inside the
  transaction); `snapshot` providers are still read before it. Before, every provider was read
  before the call, so post-effect provider checks were vacuous.
- **No exception escapes a guard.** A property that raises on a provider argument or snapshot path,
  a backend or conversion error, celpy's `ValueError`/`OverflowError` on big ints, and a `Checked`
  with no verdict are all `rule_error`. Observers, exporters, review stores and pagers that raise
  are logged. Detect mode always proceeds.
- **int64 everywhere.** An input int outside int64 is a `rule_error` on every backend (new
  conformance suite `int_range`). The subset gains `matches()` (suite `regex`).
- **Escalation opens a review.** `escalate` and abstain hand off to the review store, so
  `Escalation.review_id` is set (202 bodies now include it).
- **Provider error text.** Outcome errors say only `provider X raised TypeName`; the message (which
  may hold secrets) needs `inv.configure(debug=True)` or `INV_DEBUG=1`. `ok` events log at DEBUG,
  violations at `violation_log_level` (default WARNING).
- **Strict ablation.** `--inv-ablate` fails the session when an `enforce` rule has no caught
  falsifier (missing, failing with the guard on, or inconclusive). `--inv-ablate-lenient` restores
  the old check. Ablation re-runs also set `INV_ABLATE`, so subprocess falsifiers (an `inv scan` run,
  for example) see the rule off; `inv scan` skips ablated rules.
- **verify** reports a guard that names a rule whose target is a different function.
- **Generators** are refused by `@inv.guard`, `@inv.model_call` and `@inv.edge` (TypeError).
  `@inv.transaction` refuses anything but a generator or async generator.
- **Standing.** A rule on an `effects: none` target needs no `mediation.sink` to be held.

Added:

- Async: `@inv.model_call` and `@inv.edge` on `async def`; `@inv.transaction` on async
  generators (an async context manager); `async def` providers, awaited by async guards, model
  calls and edges (a sync call that needs one gets `rule_error`).
- Operations: `inv.configure(review_store=, pager=, exporter=, error_spike=, violation_log_level=,
  sink_size=, debug=)`, `inv.Handoff` (with the bound values), `inv.ReviewStore`,
  `inv.MemoryReviews`, `inv.ErrorSpike` (rule-error spike demotes to detect and pages;
  `Registry.restore`), `inv.observe` and `inv.capture` (now public). Exceptions carry `.bound`
  and `.point`.
- `invariants.integrations.otel.exporter()`: one span per evaluation, `invariant.<point>`, with
  `invariant.rule_id`, `rule_rev`, `outcome`, `mode`, `attempts` and more (extra `otel`).
- `inv.monitor(rule_id, rows)`; `monitor` enforcers may take a `view`.
- `inv.storage_rejection` and `inv.storage_errors`, answered with `StatusMap.storage` (409) and
  `point: storage` in the problem body; `inv.is_ablated` for storage DDL.
- `inv.load_resource(package, name)` for rule files shipped inside a package.
- `invariants.testing.make_checked` / `make_unbuilt`.
- `inv scan --exclude GLOB` and `[tool.invariants.scan] roots/exclude` in pyproject.toml;
  `inv lock --update-artifacts`.
- Docs: Concepts, a transactional example, "from enforce(known-callers) to held", Operations and a
  longer Limitations list in the README; `docs/rule-file.md` (every field) and
  `docs/design-guide.md`.

Internal:

- The env is projected once per evaluation for all clauses on the engine backends; dataclass
  schema field types are cached. R-1 (4 clauses): about 23 us guarded on subset, 20 us on
  cel-expr (was 29 and 26.5).
- `mypy` passes (`[tool.mypy]`, a CI job); decorators are ParamSpec-typed; `__version__` comes from
  the installed metadata; engine tests skip without the `cel` extra (a CI job checks that).

The first release: the rule-file loader, the decorators, three CEL backends, the `inv` CLI,
the pytest plugin, and the pydantic, ASGI and FastAPI integrations.

### Core

- `inv.load` reads JSON or YAML rule files, checks them, and resolves every target, provider
  and transaction boundary. Any defect raises `inv.LoadError`.
- Decorators: `@inv.provider`, `@inv.guard`, `@inv.transaction`, `@inv.model_call` and `@inv.edge`.
  `inv.Checked` carries `.verdict` and `.attempts`.
- Every evaluation ends in one of four outcomes: `ok`, `violation`, `rule_error` or `parse_error`.
  Clause lists follow CEL's commutative error rules.
- Each `on_violation` raises its own exception: `Rejected`, `Halted`, `Escalation` or `Routed`.
  `RolledBack` reports a swallowed failure inside `@inv.transaction`.
- `inv.verify` and `inv.standing`. Registries are context-local, through `inv.isolated` and
  `inv.using`. `inv.ablated` and `INV_ABLATE` turn a rule off.

### Changes made while integrating the feature work

- **Errors.** Every `InvariantError` has a `rule` attribute. The abstain `Escalation` and
  `RolledBack` now name their rule too.
- **Guard points.**
  - `@inv.guard(..., points=("decorator", "middleware"))` sets which enforcer points a guard
    evaluates. `middleware` is now an in-process point.
  - `inv.verify` reports a guard that does not evaluate a point its rule uses.
  - The FastAPI guard evaluates both points.
- **Pydantic schemas.** `inv.model_call`, `inv.parse` and the loader accept a pydantic v2 model
  (with `extra='forbid'`) directly as a closed schema.
- **Verify scope.** `Registry.verify(modules=None)` and `inv.verify(reg, modules)` check only the
  guards in the packages of the loaded targets by default. That default comes from
  `Registry.scope()`: targets, edge targets, transaction boundaries and `python` contracts. A guard
  in an unrelated module, such as another app's guard in the same process, no longer breaks verify.
  Pass `modules=` to widen or narrow the check.
- **Contracts.** The registry keeps the rule file's `contracts` (`Registry.contracts`), and
  `install` copies them.
- **Registry hooks.**
  - `invariants.registry.observe(fn)` sees every evaluation in every registry.
  - `invariants.registry.ablated_everywhere(rule_id)` turns a rule off in every registry, including
    ones that already exist.
  - The guards check `Registry.is_ablated`.
- **Rule file schema.**
  - `mediation` now declares the fields `sink`, `door` and `tables`, which the scan uses, and it
    is closed.
  - `applies_to` is declared as a pattern or a list of patterns.
  - The loader checks the same shapes.
- **Standing.** The standing reason now reads "inv scan not run".

### CEL

- Backends:
  - `subset`, the stdlib evaluator and the default.
  - `cel-expr`, which uses cel-expr-python.
  - `celpy`, which uses cel-python.
- `INV_CEL_BACKEND=subset|cel-expr|celpy|auto` picks the backend.
- The `cel` extra installs both engines.
- The helpers that both engine backends use now live in `invariants/cel/_engine.py`.
- Subset fixes. The subset now has zero expected conformance failures:
  - String literals keep non-ASCII text.
  - `all`, `exists` and `exists_one` absorb element errors the way `&&` and `||` do.
  - `has()` tests presence only.
  - 64-bit integer overflow is an error.
  - A property that raises becomes a `rule_error`.
  - `inv.*` arity and the `inv.unique_by` variable are checked when a rule loads.
  - Enum members compare as their values, as they do on the engine backends.
- The conformance suite has 167 YAML cases in 9 suites. It runs against every installed backend,
  and no backend has an expected-failure entry.

### CLI

- `inv validate`, `inv lock [--check]`, `inv verify [--import MOD]`, `inv scan` and `inv status`.
- `inv scan` reads source with `ast` only and never imports it. It writes text, JSON or SARIF 2.1.0.
- Exit codes:
  - 0: ok
  - 1: a check failed
  - 2: usage error
  - 3: the scan found unresolved calls (not a pass)

### pytest plugin

- Options: `--inv-rules`, `--inv-verify`, `--inv-ablate`, `--inv-report` and `--inv-report-json`.
- Fixtures: `inv_registry` and `inv_ablated`.
- The report counts evaluations in every registry, through `observe`.
- Ablation reaches every registry, through `ablated_everywhere`. It no longer changes
  `os.environ`.

### Integrations

- `invariants.integrations.pydantic`: `closed()`, `model_of()`, `check_contracts()`.
- `invariants.integrations.asgi`: `problem()`, `StatusMap` and `InvariantMiddleware`, which turn
  guard exceptions into problem+json responses.
- `invariants.integrations.fastapi`: `guard()`, `install()`, `operations()`, `find_route()`
  and `verify_routes()`.
