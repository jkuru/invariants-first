# Friction with the invariants library (0.1.0)

> **Since this was written:** the library removed every feature that reached below the API
> (storage enforcers, transactional targets and rollback, provider consistency, `tables:`, `.sql`
> contracts). Entries about those features no longer apply; see the library's CHANGELOG.

Found while building pool-service as an outside team: public API only, library unmodified.

1. **`inv scan` has no exclude setting.**
   - What I tried: keep a deliberate bypass module in the repo and leave it out of the clean scan "by config".
   - What happened: a scan root is the only control, and the module name comes from the path relative to the root. The bypass file therefore lives in `bypass/pool_service/rogue.py`, a tree that mirrors the package with no `__init__.py`, so that `--root src --root bypass` names it `pool_service.rogue`. ci.sh chooses the roots.
   - What the library should do: accept `scan: {roots, exclude}` in the rule file or in `[tool.invariants]`.

2. **`mediation.tables` works per table, not per column.**
   - What I tried: R-23 is about `pools.size`, so I set `tables: [pools]`.
   - What happened: `INSERT INTO pools` in `store.insert_pool` is flagged too. I had to make `insert_pool` a second sink and `create_pool` a second door. A door applies to every sink, so a path `create_pool -> set_size` would now pass the scan. For R-20, `DELETE FROM leases` (release) would be flagged, so R-20 declares no tables.
   - What the library should do: accept column-level tables (`{pools: [size]}`), filter by statement kind (UPDATE/INSERT/DELETE), and pair each door with its own sinks.

3. **A scan-only rule has to be faked.**
   - What I tried: write R-23 as a structural rule.
   - What happened: it needs a CEL `bad` (I wrote `'false'`), an enforcer (`point: structure`), and a `falsified_by` for `status: enforce`. A pytest falsifier that runs `inv scan` in a subprocess still passes under `--inv-ablate`, which reports "guard not load-bearing" and fails the session. So the falsifier is `ci.sh::scan-bypass`, which the plugin reports as "not collected / not checked". `inv status` still calls a rule whose `bad` is `false` "held".
   - What the library should do: add a structural rule kind with no `bad`, falsifiers of type `scan:`, and ablation that skips them.

4. **Storage-enforcer rejections are invisible to the library.**
   - What I tried: let the R-20 triggers reject over-leases.
   - What happened: a trigger raises `sqlite3.IntegrityError`, not an `InvariantError`. The app matches the trigger message (`R-20:`) and builds the 409 problem body by hand, copying the private body shape. The reach report counts only door evaluations, so the storage enforcer's catches during the race test show nowhere.
   - What the library should do: provide something like `inv.storage_rejection(rule_id, exc)` that raises a `Rejected` carrying `point: storage`, counts it, and lets `install()` map it (with a configurable status, here 409).

5. **"held" needs `mediation.sink` even where no sink exists.**
   - What happened: R-1 (a pure function, `effects: none`) and the HTTP input rules H-1/H-2 can never be held. `inv status` asks to "add mediation.sink", which means nothing for a pure function.
   - What the library should do: skip the scan requirement for `effects: none` targets, or accept `sink: none` with a reason.

6. **Escalation opens no review.**
   - What happened: an R-7 abstain, violation or exhausted retries gives 202 with no `review_id`. Only `on_violation: route` opens a review, but triage escalation is exactly where a person needs a handle.
   - What the library should do: open a review on `escalate` and on abstain too.

7. **`inv.model_call` and `inv.edge` are sync only.**
   - What happened: the triage route had to be a sync handler, which runs on the threadpool. A real model client is usually async.
   - What the library should do: support `async def` the way `guard` does.

8. **The CEL subset has no `matches()`.**
   - What happened: H-2 (pool id shape) is written with `size()` and `contains()` instead of a regex.

9. **The openapi contract is pinned as a file, but routes are checked against the live app.**
   - What happened: `inv lock` hashes `ref` as a file beside the rule file, while `verify_routes` needs the app object. I export `openapi.json` with a script and test that it is current.
   - What the library should do: accept `ref: 'pool_service.main:app'` and render and hash its OpenAPI.

10. **Where the rule file lives in an installed service.**
    - What happened: artifact paths (`src/pool_service/schema.sql#sha256:`) and the CLI resolve relative to the rule file, so it sits at the project root, outside the package. The service finds it through `parents[2]` (which works only for an editable install) or `POOL_SERVICE_RULES`.
    - What the library should do: support loading via `importlib.resources` and artifact paths relative to a package.

11. **The storage artifact hash is updated by hand.**
    - What happened: after editing `schema.sql`, the loader fails until someone pastes the new sha256 into `invariants.yaml`. `inv lock` does not offer to write it.
    - What the library should do: provide `inv lock --update-artifacts` or similar.

12. **Scan findings repeat and are sometimes misattributed.**
    - What happened: one `__wrapped__` site is reported under all five scanned rules, including R-7, which has nothing to do with resize. `DROP TRIGGER r20_lease` is reported only under R-23 (the rule with `tables`), not under R-20, whose storage enforcer it removes.
    - What the library should do: report one finding per site with the list of affected rules, and attribute a dropped trigger to the rule whose artifact defines it.

13. **Log volume.**
    - What happened: every violation logs a WARNING with the full event on the `invariants` logger. Under hypothesis this means hundreds of lines per failing test, so ci.sh runs pytest with `-p no:logging`.
    - What the library should do: provide a sampling setting or a quieter level for expected outcomes.

14. **No test helper for `Checked`.**
    - What happened: testing the edge against a forged or non-ok `Checked` means calling `object.__new__` and `object.__setattr__` by hand.
    - What the library should do: provide a public helper (for example `inv.testing.checked(value, verdict=...)`) that keeps forgery explicit.

15. **Guard overhead on small functions.**
    - What happened: R-1's four clauses add about 42 µs to a 1 µs function (p50). That is fine at HTTP scale (noise next to about 2.5 ms per request), but heavy for tight loops.
    - What the library should do: document the cost per clause and per comprehension, or offer a cheaper sampled mode for hot pure functions.


## Library fix round 1: what changed for this app

The library was changed (not this app's workarounds) for these entries:

- 3: R-23's falsifier is now a pytest test (`tests/test_scan.py`) that runs the bypass scan in a
  subprocess; ablation reaches it through `INV_ABLATE`. `ci.sh::scan-bypass` is gone. The rule still
  needs `bad: 'false'` and a `structure` enforcer (a first-class scan-only rule is not in 0.1).
- 4: `inv.storage_errors` / `inv.storage_rejection` replace the hand-built 409 body; the storage
  enforcer's refusals are counted.
- 5: an `effects: none` rule (R-1) no longer needs `mediation.sink` to be held.
- 6: `escalate` and abstain open a review; the 202 body has `review_id`.
- 7: `model_call` and `edge` accept `async def`. The triage route stays sync here because its
  SQLite connection comes from a sync dependency.
- 8: the subset has `matches()`. H-2 keeps its `size()`/`contains()` form.
- 11: `inv lock --update-artifacts` rewrites the `schema.sql` digest.
- 13: `ok` logs at DEBUG and the violation level is configurable; `ci.sh` no longer needs `-p no:logging`.
- 14: `invariants.testing.make_checked` replaces the hand-forged `Checked`.
- 1 (partly): `inv scan --exclude` and `[tool.invariants.scan]` exist. `bypass/` keeps its mirror
  layout because the module name must still resolve as `pool_service.rogue`.
- 10 (partly): `inv.load_resource(package)` loads a rule file shipped in a package; this app keeps
  the rule file at the project root, beside the artifacts it pins.

Still open in the library: 2 (column-level tables), 9 (openapi from a live app), 12 (one finding per
site), 15 (lazy providers; the env is now projected once per evaluation).


## Library fix round 3: what changed for this app

- `api.py` takes `PROBLEM_JSON` from `invariants.integrations.fastapi` (`inv_fastapi.PROBLEM_JSON`),
  which now re-exports it with `StatusMap` and `problem` and documents them; the import of the
  undocumented `invariants.integrations.asgi` constant is gone.
- 15 (documented): the library README has a Performance section (about 7 µs per clause on subset
  and cel-expr, about 85 µs per clause on celpy), with the advice to use cel-expr or subset on hot
  paths. A sampled mode is still not offered.
- New load-time type check: every R- and H-rule loads unchanged.
