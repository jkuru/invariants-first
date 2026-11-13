# Friction with the invariants library (0.1.0)

Found while building pool-service as an outside team: public API only, library unmodified. Each
entry keeps its original number and ends with its current status.

**Status as of 2026-11-12.** The library removed every feature that reached below the API (storage
enforcers, transactional targets and rollback, provider consistency, `tables:`, `.sql` contracts):
rules live on API boundaries only. The entries about those features are moot and are listed at the
end, not kept. The schema's triggers stay as the store's own defence; their message is
`pool lease capacity exceeded (db constraint)`, not a rule id, so a database refusal is never read as
an API rule's. Of the rest, every entry is resolved except 12 and 15, which say why.

1. **`inv scan` has no exclude setting.**
   - What I tried: keep a deliberate bypass module in the repo and leave it out of the clean scan "by config".
   - What happened: a scan root was the only control, and the module name comes from the path relative to the root. The bypass file therefore lives in `bypass/pool_service/rogue.py`, a tree that mirrors the package with no `__init__.py`, so that `--root src --root bypass` names it `pool_service.rogue`.
   - **Status (2026-11-12): resolved.** `inv scan --exclude` and `[tool.invariants.scan]` (roots and excludes, pinned in the lock) exist. `bypass/` keeps its mirror layout because the module name must still resolve as `pool_service.rogue`; that is how modules are named, not friction.

3. **A scan-only rule has to be faked.**
   - What I tried: write R-23 as a structural rule.
   - What happened: it needed a CEL `bad` (`'false'`), an enforcer (`point: structure`) and a `falsified_by`, and a pytest falsifier that ran `inv scan` in a subprocess passed under `--inv-ablate`.
   - **Status (2026-11-12): resolved.** Ablation reaches a scan through `INV_ABLATE` (and the `inv_scan` fixture reads an ablated scan honestly), so `tests/test_scan.py` is R-23's falsifier. The rule file now has a door form (`door:`, `sink:`, `allow:`) that expands to the structure rule and may be `enforce` without `falsified_by`. R-23 keeps the full form because it carries its chokepoint text, owner and a falsifier test.

5. **"held" needs `mediation.sink` even where no sink exists.**
   - **Status (2026-11-12): resolved** (fix round 1): an `effects: none` rule (R-1) needs no sink to be held. H-1/H-2 are middleware rules on a route and are reported as such.

6. **Escalation opens no review.**
   - **Status (2026-11-12): resolved** (fix round 1): `escalate` and abstain open a review; the 202 body has `review_id`.

7. **`inv.model_call` and `inv.edge` are sync only.**
   - **Status (2026-11-12): resolved** (fix round 1): both accept `async def`. The triage route stays sync because its SQLite connection comes from a sync dependency.

8. **The CEL subset has no `matches()`.**
   - **Status (2026-11-12): resolved** (fix round 1): the subset has `matches()`. H-2 keeps its `size()`/`contains()` form.

9. **The openapi contract is pinned as a file, but routes are checked against the live app.**
   - What happened: `inv lock` hashed `ref` as a file beside the rule file, while `verify_routes` needs the app object. I exported `openapi.json` with a script and tested that it was current.
   - **Status (2026-11-12): resolved.** The contract is `pool_api: {type: openapi, app: "pool_service.openapi_app:app"}`: `inv lock` imports the app (the same routes as `pool_service.main:app`, on an in-memory database, rule file not loaded) and pins the operations H-1 and H-2 name from `app.openapi()`. `openapi.json` and its currency test are gone; `tests/test_api.py` checks that the contract app and the served app describe the same API, and `scripts/export_openapi.py` only prints the document for reading. Re-pinning showed the source change as WEAKENED (the document now comes from an app, not a file); the operation schemas pinned were unchanged (same digests), so it was accepted.

10. **Where the rule file lives in an installed service.**
    - What happened: the CLI resolves paths relative to the rule file, so it sits at the project root, outside the package. The service finds it through `parents[2]` (which works only for an editable install) or `POOL_SERVICE_RULES`.
    - **Status (2026-11-12): resolved in the library.** `inv.load_resource(package)` loads a rule file shipped in a package. This app keeps the rule file at the project root, beside its lock.

12. **Scan findings repeat and are sometimes misattributed.**
    - What happened: one `__wrapped__` site was reported under all five scanned rules, including R-7, which has nothing to do with resize.
    - **Status (2026-11-12): open, partly fixed.** The scan now reports one finding per call site per rule (S-6 in the CHANGELOG), so no site repeats inside a rule. But the `ResizeService.resize_door.__wrapped__` at `bypass/pool_service/rogue.py:15` is still listed once under each of R-7, R-20, R-21, R-22 and R-23 (checked with `inv scan --root src --root bypass` on 2026-11-12): a `forbidden/unwrap` finding goes to every scanned rule, not only to the rules whose door or sink it unwraps. The library should attribute it to the rules of the function unwrapped (here R-21, R-22, R-23), or print one line per site with the rules affected. (The `DROP TRIGGER` half of this entry was about table mediation and is moot.)

13. **Log volume.**
    - **Status (2026-11-12): resolved** (fix round 1): `ok` logs at DEBUG and the violation level is configurable; `ci.sh` no longer needs `-p no:logging`.

14. **No test helper for `Checked`.**
    - **Status (2026-11-12): resolved** (fix round 1): `invariants.testing.make_checked` replaces the hand-forged `Checked`.

15. **Guard overhead on small functions.**
    - What happened: R-1's four clauses add about 42 µs to a 1 µs function (p50). That is fine at HTTP scale (noise next to about 2.5 ms per request), but heavy for tight loops.
    - **Status (2026-11-12): open by design.** The cost per clause and per backend is documented (docs/cel.md, the README's Performance section), and providers are now called only when a clause reads their name. A sampled mode is not offered in v0.1 (docs/cel.md: "There is no sampled mode in v0.1"); R-1 stays a full check because a rounding bug is exactly what a sample would miss.

## Moot: features removed from the library

These entries were about features that reached below the API. The features are gone, so the entries
no longer apply; their text is in the git history.

- **2. `mediation.tables` works per table, not per column.** `tables:` is removed. R-23 names its sinks (`store.set_size`, `store.insert_pool`) and its doors.
- **4. Storage-enforcer rejections are invisible to the library.** Storage enforcers and `inv.storage_rejection` are removed. The trigger refusal is the store's own business: `api.store_conflicts` turns it into a plain 409 the app answers itself.
- **11. The storage artifact hash is updated by hand.** `.sql` artifacts are no longer pinned, so `schema.sql` has no digest in the rule file.

## Changes in this app since the original entries

- R-22 reads the pool's current size from the `current_size` provider (one `SELECT size FROM pools WHERE id = ?`), not from a `ResizeService.sizes` property that loaded every pool on each resize. `pool_size` serves both R-20 (`pool_size`) and R-22 (`current_size`) through `@inv.provider("pool_size", "current_size")`. The lock reported the rule change as WEAKENED (it cannot tell that `current` and `self.sizes[req.pool_id]` are the same value); it was reviewed and accepted. A missing pool is still a `rule_error` (fail closed), as the missing dict key was.
- `api.py` takes `PROBLEM_JSON` from `invariants.integrations.fastapi` (fix round 3).
