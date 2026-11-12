# invariants prototype

This is a teaching prototype of the Python SDK designed in `../Invariant_first.md` (section 4.4). It is not the library. It exists to check that the API sketch works as written, using only the standard library. It uses explicit decorators built on `inspect.signature(...).bind` and `functools.wraps`. It does no AST rewriting and never runs `eval` or `exec` on rule text or source code.

It implements the nine design decisions D1-D9 (providers, status and mode, effects on targets, model policy, two views of one rule, `old_self`, outcome edges, verify, the edge re-check), with the amendments to D4 (no `on_exhaust`) and D6 (snapshot paths are inferred).

## Run

```
python3 demo.py                  # 65 numbered checks in sections; exits 0 when all pass
INV_ABLATE=R-1 python3 demo.py   # mutation sanity: disables R-1's guard, and checks 19 and 20 FAIL
```

You need Python 3.10 or later. It was tested on 3.14 and needs no third-party packages.

Ablation results (`INV_ABLATE=<id>`; each run exits 1):

| Rule off | Checks that fail |
|---|---|
| R-1 | 19, 20 (reach counter, mutant caught) |
| R-7 | 42, 43, 46, 52, 53, 57 (held-data mismatch, resample variant, rule_error, detect verdict, fail-open verdict, forged `Checked`) |
| R-20 | 58, 59 (door refusal, door counter). The storage trigger still refuses the second lease (60 passes): that is the point of 2.7 |
| R-21 | 28, 29, 31, 32 |
| P-1 | 36, 37, 38 |
| P-2 | 25, 26 |

## Files and the guide sections they cover

| File | Covers |
|---|---|
| `invariants/__init__.py` | The loader and its validation: `targets` and `effects` (D3, 2.2), `providers` with the `@inv.provider` check of name, args and return type, and the load error for a `same_txn` provider on a target that is not transactional (D1), required `status`, the computed highest status and the refusal of anything higher (D2), per-enforcer `view` (D5), inferred `old_self` paths (D6), model policy (D4), artifact hash (2.7). The four-outcome evaluator with clause lists (D7, 2.9, 4.1); outcome actions with `Routed.review_id` and detect-never-blocks (D7); `@inv.guard` (sync, async, methods) with providers bound before pre-effect checks and cached per (provider, resolved args) per call; `@inv.transaction` boundaries, with rollback on a failed post-effect guard (D3); `@inv.model_call` (parse, abstain, `bad`, accept, bounded retries, `Checked` with a `verdict`); `@inv.edge` with type gate, verdict check and `bad` re-check (D9); `check_snapshot` for the plain-data test (D6); `verify` (an alias of `verify_coverage`, D8); `standing`, where only `same_txn` and `immutable` providers can be held without a storage enforcer (D1, D2, D8); `ablated`, `using` |
| `invariants/cel.py` | A safe CEL subset with a tokenizer, a recursive-descent parser, a tree-walking evaluator and a cost limit. It implements `inv.sum`, `inv.zip` and `inv.unique_by` (4.1), plus CEL's commutative `&&`/`\|\|` error absorption |
| `invariants.json` | The rule file: `targets` (six), `providers` (`approval` same_txn, `pool_leased` and `pool_size` snapshot, `job` immutable), and rules R-1, R-7 (held data, edge enforcer), R-20 (door view and rows view), R-21 (approval from a provider), P-1 (transition rule, routed and rolled back) and P-2 (`fail_open_and_page`) |
| `jobs.py` | `split_work` (R-1, effects none) with a seeded rounding-bug mutant, and `restart_job` (effects external) |
| `triage.py` | `CauseReply` (closed schema with an enum that holds the abstain value), `JobFacts` and the `job` provider (data code holds), `CheckedCause`, `propose_cause` (scripted model client, `inv_feedback`), and `remediate` (an `@inv.edge`) |
| `pool.py`, `pool_schema.sql` | The `acquire` lease door (R-20) and its `pool_leased`/`pool_size` providers, the `approvals` table and `approval` provider (R-21), `PoolService.txn` (an `@inv.transaction`), `PoolService.resize_door` (R-21, P-1) and the SQLite storage enforcer (2.7). `pool._AFTER_READ` is a test seam that forces the interleaving |
| `demo.py` | The checks, listed below |

## What the checks cover

| Checks | Covers |
|---|---|
| 1-10 | Load and verify: the file loads; `verify` is `verify_coverage` and passes; negatives for an unknown field, a missing target, the timing rule (now a D2 status refusal), an `args.<x>` rename, bare `has()`, a changed artifact hash, an unguarded target, and an edge target without `@inv.edge` (D8) |
| 11-12 | D2: `enforce` without `falsified_by` is refused (highest allowed: detect); the effective-mode table `min(mode, cap(status))` |
| 13-14 | D3: `effects` on a rule is a load error; a transactional target must name its transaction |
| 15-17 | D1 at load: a provider whose return annotation differs from its `type`; a function registered under another provider name; a `same_txn` provider read by a rule whose target is not `effects: transactional` |
| 18-24 | R-1: the property test, the reach counter, the mutant, ablation, rule_error on a `None` result, unequal `inv.zip`, the async guard |
| 25-27 | D7: P-2 `fail_open_and_page` and reject; clause-list combination (all fired clauses listed; an error with no true clause gives rule_error) |
| 28-33 | D1 and D3 on R-21: the approval comes from a same_txn provider read inside `PoolService.txn`; no row, PENDING or wrong size is refused and nothing is written; a call outside the boundary is a rule_error; a provider that raises or returns the wrong type is a rule_error; two rules that bind a provider's args differently on one target each get their own value |
| 34-35 | D6: P-1's snapshot is inferred as only `old_self.sizes` and passes the plain-data test on a real `PoolService`; `old_self.conn` fails that test, and bare `old_self` fails to load |
| 36-38 | D3 and D7: a shrink raises `Routed` with a review id and the transaction rolls back; a caller that swallows the exception still gets a rollback (`RolledBack`); with status `detect` the shrink is reported and commits |
| 39-46 | D4 runtime: verdict ok; parse errors fed back through `inv_feedback`; a made-up cause is a parse error (enum); a held-data mismatch escalates with no resample; without `compares_to` there is one fresh resample with no feedback; abstain escalates before `bad`; spent format retries go to `on_violation`; a rule_error is never resampled |
| 47-51 | D4 at load: the abstain value must be in the enum; `on_exhaust` is refused; `compares_to: held_data` with a resample is refused; `inv_feedback` is required for `parser_error`; an enum-restating clause loads and is reported as "held by the schema (rung 1)" |
| 52-57 | D4 and D9: a detect-mode call returns `Checked` with verdict violation and the enforcing edge refuses it; `fail_open_and_page` returns verdict rule_error; the type gate; no direct construction; an honest `Checked` passes the edge; a forged one (`object.__new__`, verdict ok, bad value) is refused by the re-check |
| 58-62 | D5 on R-20: the door reads `leased`/`size` from providers, so it refuses a sequential over-lease on its own; the forced interleaving with and without the trigger; the differential fills the door view and the rows view, checks they agree, and checks that storage refuses exactly when `bad` is true; the off-by-one trigger mutant |
| 63-65 | Standing: R-20 without its storage enforcer cannot count as held because its door reads snapshot providers (D1); R-1 is held only when `scan=True` is assumed (D8); R-7 is held with `scan=True` because its `job` provider is `immutable`, and is not held when `job` is `snapshot` |

## Binding

The default names are `args` (the bound arguments, without `self`), `result`, `reply` (the parsed model reply), `self`, and `old_self` (only the inferred paths). Paths may also be rooted at `provider.<name>`. `bind` maps names for the whole rule. An enforcer's `view` maps names for that enforcer only; `view: rows` marks a storage enforcer that computes the names itself. A provider name that `bad` reads with no binding is bound automatically. A provider's `args` paths are resolved in the referencing rule's environment, so R-21's `req.pool_id` uses R-21's `req`. Values are cached per (provider, resolved args) for one call, so two rules on one target that bind the args differently each get their own value (check 33). At load, every `args.<x>` a rule reads (in `bad`, `bind`, `view` or a provider's `args`) must be a parameter of the target the enforcer runs on, and that includes an edge's target.

## What it leaves out

- **`inv scan`** (the code-graph check of 2.4). It is out of scope. The prototype implements only `verify`. `standing()` therefore never reports held unless the caller passes `scan=True`, and the demo prints every rule as `enforce(known-callers)` with the reason "inv scan not run".
- **A real CEL engine.** No `cel-expr-python` or `celpy` is installed, so the prototype uses the subset in `cel.py`. It has no static type-checker: load checks syntax, names and argument paths only.
- **The tooling around the SDK:** YAML (the rule file is JSON), `contracts` resolution, the lockfile (which would pin each target's signature and effects, D3), `applies_to`, the `inv` CLI, OpenTelemetry export, gateways and Discover. The detect-to-enforce soak evidence (reach N times, zero rule errors) is not checked: the computed status uses only enforcers, `falsified_by` and the timing rule.
- **A real model.** The model client is scripted.
- **Role grants for R-20's mediation.** SQLite has none, so this is listed in `residual_paths`, and R-20 is not held. The R-21 storage trigger is not implemented either, so R-21 has no chokepoint.
- **Unforgeable `Checked` values.** `object.__new__` still builds one. The edge re-check (D9) is what refuses a forged value with a bad payload. A forged value whose payload passes `bad` is accepted, as it should be.
- **A real transaction manager.** `@inv.transaction` wraps a generator method (BEGIN / yield / COMMIT or ROLLBACK) and marks the boundary in a `contextvar`. It does not handle nested savepoints or other threads.

## Where the decisions were ambiguous or needed a choice

Each finding below was a gap or a choice. "Guide" means `Invariant_first.md` now says what the prototype does.

1. **Nullable provider types (D1). Guide.** R-21's `bad` tests `approval == null`. A provider's `type` may be `T | null`; the loader checks the return annotation is exactly `T | None`.
2. **Provider `args` roots and caching (D1). Guide.** `args: {pool_id: req.pool_id}` roots at `req`, a name from R-21's `bind`. Provider args resolve in the referencing rule's binding environment. Values are cached per (provider, resolved args) per call, not per provider name, so two rules that bind differently on one target never share a stale value (check 33).
3. **What "shared state" means (D1). Guide.** R-7's `job` provider reads finished-job facts that never change. The consistency value `immutable` says so, and counts like `same_txn` for standing, so R-7 can be held (check 65).
4. **`same_txn` on a target that is not transactional. Guide.** A load error: there is no transaction to read in (check 17). A transactional target called outside its boundary is still a runtime `rule_error` (check 31).
5. **"Complete chokepoint" (D2, held).** The prototype reads this as `mediation.chokepoint` set and `residual_paths` empty.
6. **D8 makes held unreachable in the prototype.** With no scan, held needs `scan=True`.
7. **The edge re-check needs independent held data (D9). Guide.** If the `Checked` value carried its own inputs, a forger would forge those too. So the edge fetches held data itself, through providers keyed on its own arguments: `job` is keyed on `args.job_id`, which `remediate` also takes, and the loader checks that path against the edge's signature. A schema-only re-check would miss check 57.
8. **The edge's record shape. Guide.** `point: edge` with its own `target`; only edge enforcers name one.
9. **`schema` in `model_policy`.** The D4 shape omits it. The prototype and the guide keep it, because `verify` checks that it matches the decorator.
10. **"Forces" for `compares_to: held_data` (D4).** If the author omits `semantic_resamples`, the effective value is 0. If the author declares 1, or a semantic feedback other than `none`, the loader refuses the file instead of silently overriding it.
11. **Detect mode for model calls. Guide.** In detect, semantic resamples are skipped and an exhausted parse returns `Checked` with verdict `parse_error`. Abstain escalates in every mode. Effective mode `off` behaves as detect for model calls, so a call never returns verdict ok unchecked.
12. **Swallowed guard exceptions in a transaction (D3). Guide.** The SDK marks the transaction rollback-only and raises `RolledBack` at exit (check 37).
13. **Plain-data check timing (D6). Guide.** The check needs a real instance, so `inv.check_snapshot(key, instance)` runs it at test time and raises `LoadError`. A non-plain path reached at runtime is a `rule_error`, and nothing is copied. Bare `old_self` is a load error.
14. **Unlisted targets (D3). Guide.** A target missing from `targets:` defaults to `external`.
15. **`status` is required (D2). Guide.** A missing status is a load error. A failing timing check caps the highest allowed status at detect.
16. **Forcing the 2.7 race with providers.** The door reads its own state, so a test cannot hand it stale values. `pool._AFTER_READ` runs the second lease inside the first door's provider read.
17. **Earlier findings.** R-21's `!has(approval)` does not compile (check 7; the guide now tests `approval == null`). `inv.zip` with unequal lengths is a `rule_error` (check 23, as 4.1 says). `inv.unique_by`'s signature is still unspecified.
