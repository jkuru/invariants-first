# Friction with `invariants` (0.1.0)

> **Since this was written:** the library removed every feature that reached below the API
> (storage enforcers, transactional targets and rollback, provider consistency, `tables:`, `.sql`
> contracts). Entries about those features no longer apply; see the library's CHANGELOG.

These are the places where using the library from outside was awkward, hard or impossible.
Each entry says what I tried, what happened, and what the library should do. The library was
not patched. The app uses only `import invariants as inv`. It reads `Registry` attributes
(`rules`, `sink`, `record`, `is_ablated`, `counts`), which exist on the public `inv.Registry` class.

## 1. `@inv.model_call` is sync only
- **Tried:** an `async def` extractor under `@inv.model_call("K-2", ...)`.
- **Happened:** the wrapper calls `fn(...)` without awaiting it. The coroutine reaches `parse`, which raises "reply is not an object". The format retries are spent on unawaited coroutines.
- **Workaround:** `Pipeline.extract_async` runs the sync model call in worker threads (`asyncio.to_thread`). The registry is context-local, so it carries across.
- **Library should:** detect a coroutine function in `model_call` and `edge` and await it, the same way `guard` already does.

## 2. Providers are read before the effect, even for `post_effect` enforcers
- **Tried:** K-15 as a post-effect rule `orphans > 0`, with `orphans` read from a `same_txn` provider after the deletion.
- **Happened:** `guard.begin()` fetches every provider before the call. The post-effect check therefore saw the count from before the deletion, which is always 0. The guard was vacuous.
- **Workaround:** the door returns `RetractionReport.live_orphans`, which it re-counts in the same transaction. The decorator enforcer binds that value with `view: {orphans: result.live_orphans}`, and the monitor keeps the provider. The value is now reported by the code the rule is checking.
- **Library should:** fetch providers for post-effect enforcers after the call, inside the transaction, or offer a consistency value such as `after_effect`.

## 3. Providers are fetched even when `bad` would not read them
- **Tried:** K-11, `predicate in [functional...] && contradiction != null && ...`.
- **Happened:** the `contradiction` query runs on every ingest, including predicates that are not functional. In the benchmark the ingest door costs about 240 µs guarded against 12 µs bare. About five SQLite provider reads per call account for most of that.
- **Library should:** load providers lazily, when CEL first selects the name.

## 4. Ablation cannot reach storage enforcers
- **Tried:** K-8 (append-only trigger) and K-9 (UNIQUE) with `status: enforce`, which needs `falsified_by`.
- **Happened:** `--inv-ablate` turns off only in-process guards. A falsifier for a trigger still passes, so the session fails with "guard not load-bearing". Without `falsified_by`, the loader caps the rule at `detect`.
- **Workaround:** `store.ddl()` leaves out each storage enforcer while `inv.current().is_ablated(rule_id)` is true. Production code now carries a test hook.
- **Library should:** give storage enforcers an ablation signal, such as a documented `inv.storage_enforcer(rule_id)` context or a callback that DDL builders call, or let a storage enforcer declare its own falsifier mechanism.

## 5. Rules held by the type (rung 1) cannot be falsified by ablation
- **Tried:** K-4 (closed `Literal` vocabulary) and K-18 (no id field, `extra="forbid"`) as `point: type`.
- **Happened:** ablating the rule does not change `parse`, so a parse-error falsifier always passes.
- **Workaround:** both rules also got an in-process enforcer at the ingest door:
  - K-4 checks the vocabulary.
  - K-18 checks that the ids were minted by the resolver, through `same_txn` providers.
  The falsifiers test those enforcers.
- **Library should:** let ablating a `type` rule relax the closed schema for that field, or exempt rung-1 enforcers from ablation and report them as "held by the schema".

## 6. One `bad` cannot be both a door precondition and a state invariant
- **Tried:** K-12 with two clauses: "only a CHECKED fact can be promoted" and "needs 2 trusted publishers". The same rule has a monitor enforcer over BELIEVED rows.
- **Happened:** the monitor flagged every correctly BELIEVED fact, because the precondition clause is true for them.
- **Workaround:** the precondition moved into Python (`Store.promote` raises `ValueError`).
- **Library should:** allow clauses per enforcer (for example `bad: [{when, message, points: [decorator]}]`), or separate `pre:` from state.

## 7. The `monitor` point has no SDK support
- **Tried:** a nightly job for K-12, K-15 and K-16.
- **Happened:**
  - The SDK does not evaluate `monitor` enforcers.
  - A `view` on a monitor enforcer is a load error ("view must be a name map or 'rows'").
  - There is no helper that evaluates over rows and records reach.
- **Workaround:** `monitor.run` calls `inv.evaluate(rule, base, "rule", reg, None, in_txn=True)`. That needs to know the internal `"rule"` key of `names_for`. It also asserts `in_txn` on trust. It then builds the event dict by hand for `Registry.record` so the pytest report counts it.
- **Library should:** provide `inv.monitor(rule_id, rows, txn=...)`, allow views on monitor enforcers, and record events with the standard shape.

## 8. `inv scan` has no exclude option
- **Tried:** keeping `bypass/` in the repository but out of the clean scan "by config".
- **Happened:** the only control is `--root`. The default root is the rule file's directory, which would scan `tests/`, `bench/` and `bypass/`.
- **Workaround:** the roots are listed in `[tool.fact-pipeline.scan]` in `pyproject.toml`, and `ci.sh` turns them into `--root` flags.
- **Library should:** support `--exclude GLOB`, or a `[tool.invariants.scan]` table with roots and excludes.

## 9. `inv status` gives no credit for storage mediation
- **Happened:** K-7, K-8 and K-9 are backed by a CHECK, a trigger or UNIQUE. They still show `enforce(known-callers)`, with the reason "inv scan cannot check it: mediation declares no sink".
- **Happened:** K-4, K-7, K-11 and K-18 share K-1's door. Each would need its own copy of `mediation.sink` to be counted.
- **Happened:** `--inv-report` shows `reach 0` for the storage-only rules, which reads as "never exercised".
- **Library should:**
  - accept a storage chokepoint (`mediation.storage: {table, constraint}`) as mediation;
  - let rules share one mediation by target;
  - mark storage-only rules as "not counted in process" instead of showing 0.

## 10. `route` does not reach the app's records
- **Happened:** `on_violation: route` opens a review in `Registry.reviews`. That review is in memory and is lost when the worker exits. `Routed` carries only `review_id` and the outcome, not the bound values.
- **Workaround:** `Pipeline.ingest` catches `Routed`, re-queries the rival fact, writes a Conflict row, and retries.
- **Library should:** offer a route handler hook, for example `inv.on_route(rule_id, fn(env) -> review_id)`, that runs with the evaluated environment so the app can persist the record.

## 11. `fail_closed` turns a programming error into the rule's outcome
- **Tried:** calling `Store.ingest_door` outside `Store.txn`. This is a bug in the caller.
- **Happened:** the `same_txn` providers give `rule_error`. Under `fail_closed` that applies K-11's `on_violation: route`, so the bug opens a "conflict review" (`test_door_outside_its_transaction_fails_closed`).
- **Library should:** raise a distinct error, or use reject or halt, when the transaction boundary is missing. That is a wiring defect, not a data outcome.

## 12. The CEL subset has no `substring`
- **Happened:** K-2 ("the quote is at its offsets") cannot be written as `text.substring(start, end) != quote`.
- **Workaround:** a `span(chunk_id, start, end)` provider slices the text in Python.
- **Library should:** support the CEL strings extension (`substring`, `charAt`, `indexOf`) on every backend.

## 13. The vocabulary is duplicated between the schema and the rule
- **Happened:** K-4's list and K-11's functional list repeat `models.PREDICATES` and `models.FUNCTIONAL`, because a rule cannot reference a contract's `Literal`.
- **Workaround:** `tests/test_rules.py` checks that they agree.
- **Library should:** let `bad` reference contract enums, for example `contract.fact_reply.predicate.values`.

## 14. Providers and dependencies
- **Happened:** `@inv.provider` registers one name per function, so `subject_known` and `object_known` had to be two identical functions.
- **Happened:** a provider gets only binding paths. Store methods pass `self`. A module-level model call has no `self`, so the K-2 providers read the store from a contextvar (`fact_pipeline.context`).
- **Library should:** allow one function under several provider names, and allow a provider `context` argument or dependency injection.

## 15. There is no public way to observe detect-mode outcomes
- **Happened:** `observe()` lives in `invariants.registry`, which is outside the public API this app may import.
- **Workaround:** tests read `inv.current().sink`. That list grows without bound in a long-running worker.
- **Library should:** make `observe` public (`inv.observe`), and cap or rotate `Registry.sink`.

## 16. Rules without a target are barely checked
- **Happened:** K-8 (`subject: {table: facts}`, `bad: old_self.id != ""`) and K-9 (`bind: {rows: args.rows}`) load without any check of their paths. For storage-only rules, `bad` is documentation that nothing evaluates or differentially tests.
- **Library should:** type-check table-subject rules against a table contract, and generate the differential harness that section 2.7 describes.

## 17. Parse cost
- **Happened:** profiling shows `parse()` calls `typing.get_type_hints` (through `schema_fields`) on every reply. That is about a third of the `model_call` overhead (70 µs guarded against 30 µs bare).
- **Library should:** cache the schema fields per schema.


## Library fix round 1: what changed for this app

- 2: post-effect enforcers read providers after the effect. K-15's decorator enforcer reads the
  `orphans` provider again; the `result.live_orphans` view is gone.
- 4: `inv.is_ablated(rule_id)` is the public signal `store.ddl()` uses (the DDL hook stays: it is how
  a storage enforcer is ablated).
- 7: `inv.monitor(rule_id, rows, in_txn=True)` replaces the hand-built evaluation and event;
  monitor enforcers may take a `view`.
- 10: route and escalate hand a `Handoff` (with the bound values) to a configurable review store
  (`inv.configure(review_store=...)`); `Routed.bound` carries the values. The app still writes its
  own Conflict row.
- 11: calling a transactional door outside its transaction is now a `rule_error` before anything
  is written. (Superseded in round 3 notes below: it no longer goes through `on_violation`.)
- 15: `inv.observe` and `inv.capture` are public and `Registry.sink` is bounded; the tests use
  `inv.capture()`.
- 17: dataclass schema field types are cached.
- 1: `model_call` and `edge` accept `async def`. `extract_async` keeps `asyncio.to_thread`, because
  one rule has one target and `propose_fact` also serves the sync path and its tests.

- 8: the clean roots live in `[tool.invariants.scan]`, which `inv scan` reads; ci.sh passes no roots.

Still open: 3 (lazy providers), 5, 6 (per-enforcer clauses), 9, 12 (`substring`), 13, 14, 16.


## Library fix round 3: what changed for this app

- 11 (closed): a missing transaction boundary is a wiring defect. Under enforce + `fail_closed` the
  door raises `inv.WiringError` (an `InvariantError`) before anything is written, and opens no
  review; `on_violation: route` is not applied. `test_door_outside_its_transaction_fails_closed`
  still holds (it expects an `InvariantError` and no facts).
- 5 (documented): the library README's Limitations now state that ablating a `point: type` rule
  does not relax `inv.parse`. K-4 and K-18 keep their in-process enforcer at the ingest door.
- 3 (documented, still open): the README's Performance section gives per-backend and per-clause
  costs and says every provider an enforcer reads is called on every call. Providers stay eager:
  every clause is evaluated (no short-circuit across clauses), so laziness would only help a
  provider read under `&&`/`||` inside one clause.
- New load-time type check: every K-rule loads unchanged (no clause mixes type families).

Still open: 3 (lazy providers), 6, 9, 12, 13, 14, 16.
