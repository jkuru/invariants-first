# Friction with `invariants` (0.1.0)

These are the places where using the library from outside was awkward, hard or impossible. Each
entry says what I tried and what happened, keeps its original number, and ends with its current
status. The app uses only `import invariants as inv` (and `invariants.testing` in tests).

**Status as of 2026-11-12.** The library removed every feature that reached below the API (storage
enforcers, transactional targets and rollback, provider consistency, `tables:`, table-subject rules,
`.sql` contracts): rules live on API boundaries only. The entries about those features are moot and
are listed at the end, not kept. The schema's own triggers and constraints stay as the store's
defence; their messages say `(db constraint)` (`a fact needs an evidence span (db constraint)`,
`facts is append-only (db constraint)`), not a rule id, so a database refusal is never read as an API
rule's. Entries 5, 6, 9 and 12 stay open, each with its reason; 14's `inv.Dependency` half was
adopted on 2026-11-13.

## 1. `@inv.model_call` is sync only
- **Tried:** an `async def` extractor under `@inv.model_call("K-2", ...)`.
- **Status (2026-11-12): resolved** (fix round 1): `model_call` and `edge` accept `async def`. `extract_async` keeps `asyncio.to_thread`, because one rule has one target and `propose_fact` also serves the sync path and its tests.

## 2. Providers are read before the effect, even for `post_effect` enforcers
- **Tried:** K-15 as a post-effect rule `orphans > 0`, with `orphans` read after the deletion.
- **Status (2026-11-12): resolved** (fix round 1): a provider is read at its enforcer's timing, so K-15's post-effect enforcer reads `orphans` after the call.

## 3. Providers are fetched even when `bad` would not read them
- **Tried:** K-11, `predicate in [functional...] && contradiction != null && ...`.
- **Happened:** the `contradiction` query ran on every ingest, including predicates that are not functional. The benchmark gave about 240 µs for the ingest door guarded against 12 µs bare.
- **Status (2026-11-12): resolved in the library; re-measured.** A provider is now called only when a clause reads the name it is bound to. That laziness is decided by the rule, never by the data: CEL's `&&` evaluates both sides, so K-11 still reads `contradiction` on every ingest, even for a non-functional predicate (docs/concepts.md, "Which providers run"). Profiling showed that query was most of the cost: it sorted every live fact of the subject and predicate. The store's index is now `(subject_id, predicate, id)`, so the query stops at the first live row. `python bench/bench_guards.py --n 2000`, p50/p99 in µs, on the same machine:

  | ingest_door (K-1, 4, 7, 11, 18) | guarded | every rule ablated | bare |
  |---|---|---|---|
  | before the index, subset | 279.0 / 623.8 | 25.4 / 32.8 | 11.1 / 16.5 |
  | before the index, cel-expr | 264.4 / 401.7 | 25.2 / 30.9 | 11.0 / 16.2 |
  | after, subset (3 runs) | 124.7 to 125.7 / about 452 | about 26 / 33 | about 11 / 17 |
  | after, cel-expr (2 runs) | 121.5 to 122.0 / about 138 | 25.5 / 32 | 11.4 / 18 |

  The rest, about 110 µs, is the seven clauses of five rules plus three provider reads
  (`subject_known`, `object_known`, `contradiction`). The subset's p99 stayed near 450 µs across runs.

## 5. Rules held by the type (rung 1) cannot be falsified by ablation
- **Tried:** K-4 (closed `Literal` vocabulary) and K-18 (no id field, `extra="forbid"`) as `point: type`.
- **Happened:** ablating the rule does not change `parse`, so a parse-error falsifier always passes.
- **Workaround:** both rules also have an in-process enforcer at the ingest door (K-4 checks the vocabulary, K-18 checks that the ids were minted by the resolver), and the falsifiers test those.
- **Status (2026-11-12): open, documented.** The library README's Limitations state that ablating a `point: type` rule does not relax `inv.parse`; ablation relaxing a closed schema is not planned. The door enforcers stay.

## 6. One `bad` cannot be both a door precondition and a state invariant
- **Tried:** K-12 with two clauses: "only a CHECKED fact can be promoted" and "needs 2 trusted publishers", with a monitor enforcer over BELIEVED rows.
- **Happened:** the monitor flagged every correctly BELIEVED fact, because the precondition clause is true for them.
- **Workaround:** the precondition is in Python (`Store.promote` raises `ValueError`).
- **Status (2026-11-12): open, declined by the library.** Clauses per enforcer are not added: the lock compares a rule's clauses as one list and could not see a clause dropped from one enforcer (docs/concepts.md). The library's advice is two rules (a precondition rule on the door, a state rule for the monitor); the app keeps the Python check, since "only CHECKED is promoted" is a state-machine step, not a law about data.

## 7. The `monitor` point has no SDK support
- **Status (2026-11-12): resolved** (fix round 1): `inv.monitor(rule_id, rows)` evaluates and records reach with the standard event shape; monitor enforcers may take a `view`.

## 8. `inv scan` has no exclude option
- **Status (2026-11-12): resolved:** the clean roots live in `[tool.invariants.scan]`, which `inv scan` reads (and the lock pins); ci.sh passes no roots.

## 9. Rules that share a door each repeat its mediation
- **Happened:** K-4, K-7, K-11 and K-18 share K-1's door. Each needs its own copy of `mediation.sink` and `mediation.door` to be scanned and counted.
- **Status (2026-11-12): open.** The library has no mediation shared by target. The copies are small and the lock pins each one, so a dropped copy is visible. (The rest of this entry, credit for storage constraints as mediation and `reach 0` for storage-only rules, is moot: those rules are gone.)

## 10. `route` does not reach the app's records
- **Status (2026-11-12): resolved** (fix round 1): route and escalate hand a `Handoff` with the bound values to a configurable review store (`inv.configure(review_store=...)`), and `Routed.bound` carries them. The app still writes its own Conflict row, which is its domain record, not a review.

## 12. The CEL subset has no `substring`
- **Happened:** K-2 ("the quote is at its offsets") cannot be written as `text.substring(start, end) != quote`.
- **Workaround:** a `span(chunk_id, start, end)` provider slices the text in Python.
- **Status (2026-11-12): open, no backend has it.** The library's two backends are the subset and cel-expr (celpy was removed), and neither supports the CEL strings extension (`substring`, `charAt`, `indexOf`; docs/cel.md lists string extensions as unsupported on both). The `span` provider stays; it is typed, read-only and pinned in the lock.

## 13. The vocabulary is duplicated between the schema and the rule
- **Happened:** K-4's list and K-11's functional list repeated `models.PREDICATES` and `models.FUNCTIONAL`, because a rule cannot reference a contract's `Literal`.
- **Status (2026-11-12): resolved in the app.** Python holds the vocabulary once, in `models.Predicate` (the `Literal` the pinned `fact_reply` contract carries); `models.PREDICATES` is derived from it. `models.FUNCTIONAL` is gone: no code read it, so K-11's functional list lives only in the rule file. K-4's list is the rule's own copy (still needed: a rule cannot read a contract's enum), and `tests/test_rules.py` asserts it equals the `Literal`, entry for entry and in order (the old test only checked containment), and that K-11's list is a subset of the vocabulary. Both lists are in the rule file, so the lock shows any edit of them.

## 14. Providers and dependencies
- **Happened:** `@inv.provider` registered one name per function, so `subject_known` and `object_known` were two identical functions; and a module-level model call has no `self`, so the K-2 providers read the store from a contextvar (`fact_pipeline.context`).
- **Status (2026-11-12): one name per function resolved; dependency injection available, not yet adopted.** `providers.entity_known` is registered as `@inv.provider("subject_known", "object_known")`. The lock reported the new `from:` of both names as WEAKENED (a different function supplies the value); the function body is the same one-line read as both old ones, so it was accepted. `inv.Dependency` now covers the contextvar, but the binding lives in `context.py` and `Pipeline.__init__` (the model-client wiring), which a parallel change is rewriting; the K-2 providers move to an `inv.Dependency` when that lands.
- **Status (2026-11-13): adopted.** `fact_pipeline.context` holds two `inv.Dependency` values (the store and the model client) instead of raw contextvars. Each `Pipeline` stage that makes a model call binds its own with `with self.bound():`, so a second `Pipeline` built in the same thread no longer reroutes the first one's model calls and K-2 reads (`tests/test_async.py::test_two_pipelines_in_one_context_read_their_own_store_and_client`, which failed before). The constructor still binds them for the rest of the caller's context, for a model call made directly after building one; the rule file and the lock did not change.

## 15. There is no public way to observe detect-mode outcomes
- **Status (2026-11-12): resolved** (fix round 1): `inv.observe` and `inv.capture` are public and `Registry.sink` is bounded; the tests use `inv.capture()`.

## 17. Parse cost
- **Status (2026-11-12): resolved** (fix round 1): schema field types are cached per schema.

## Moot: features removed from the library

These entries were about features that reached below the API. The features are gone, so the entries
no longer apply; their text is in the git history.

- **4. Ablation cannot reach storage enforcers.** Storage enforcers are removed, and so are K-8 (append-only rows) and K-9 (one canonical id per external record): the schema keeps them as its own constraints, and no rule names them.
- **9 (storage half). `inv status` gives no credit for storage mediation.** There is no storage mediation; see 9 above for what is left.
- **11. `fail_closed` turns a programming error into the rule's outcome.** It was about a transactional door called outside its transaction. Transactional targets (and `inv.WiringError`) are removed; the store's transaction is the implementation's business.
- **16. Rules without a target are barely checked.** Table-subject rules are removed; every rule has an API target.
