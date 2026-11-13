# Results

## Published (2026-11-10, repeated 2026-11-11 with identical results)

These are the result files in [published/](published), from the re-run after the lock redesign that
[docs/evaluation.md](../docs/evaluation.md#current-results-after-the-lock-redesign) reports. "CI
stopped at" is the step of `ci.sh` that failed; "re-pin" is `ci.sh` after the reflex `inv lock`;
B1 is the number of tests that fail with every rule ablated.

| ID | Cat | Change | CI stopped at | Failing tests | Re-pin | B1 |
|---|---|---|---|---|---|---|
| P01 | C | `_allocate` hands out one remainder too many (R-1) | pytest | 9 | | 8 |
| P02 | C | bulk-lease endpoint calls the store directly (R-20 bypass) | `inv scan` | 0 | | 0 |
| P03 | C | approval records the proposer as approver (R-21) | pytest | 6 | | 0 |
| P04 | C | R-22's cap raised 4x to 10x, falsifier edited to match | `inv lock --check` | 0 | refused (still `inv lock --check`) | 1 (stale lock, not a catch) |
| P05 | U | `leased` counts leases instead of slots | pytest | 1 | | 1 |
| P06 | U | approval drops its PENDING check | passes | 0 | | 0 |
| P07 | U | split labels off by one | passes | 0 | | 0 |
| P08 | U | job timing arguments swapped | passes | 0 | | 0 |
| P09 | B | store refactor | passes | 0 | | 0 |
| P10 | B | new list endpoint with a test | passes | 0 | | 0 |
| P11 | B | `heapq.nsmallest` instead of a sort | passes | 0 | | 0 |
| P12 | B | a log line | passes | 0 | | 0 |
| F01 | C | stored evidence offset one short (K-2) | pytest | 1 | | 1 |
| F02 | C | bulk import writes rows around the ingest door | `inv scan` | 0 | | 0 |
| F03 | C | support count drops the trusted filter (K-12) | pytest | 2 | | 0 |
| F04 | C | `promote_door` signature change breaks K-12's binding | `inv validate` | 0 | | 0 |
| F05 | U | audit sampling off by one | pytest | 2 | | 0 |
| F06 | U | retrieval returns k-1 chunks | passes | 0 | | 0 |
| F07 | U | review payload drops diagnostics | passes | 0 | | 0 |
| F08 | U | alias match drops `lower()` | pytest | 2 | | 1 |
| F09 | B | store refactor | passes | 0 | | 0 |
| F10 | B | new `stats` subcommand with a test | passes | 0 | | 0 |
| F11 | B | `list.extend` in extract | passes | 0 | | 0 |
| F12 | B | new predicate `founded_in`, K-4's list widened | `inv lock --check` | 0 | refused (still `inv lock --check`) | 0 |

From these, docs/evaluation.md counts: `inv lock --check` failed on 2 of 24 (P04, F12); 1 of 8
benign changes red (F12); invariants CI caught 8 of 8 covered defects and 3 of 8 uncovered ones; 6
of 16 defects were caught by an invariants mechanism (guard: P01, P03; scan: P02, F02; lock: P04;
load check: F04).

## Re-run against the current apps (2026-11-12)

`PYTHONPATH=$PWD/src python evaluation/run.py` with this checkout's library, Python 3.14, on the apps
as they are in this repository (after the release that removed everything below the API). Both
apps' `ci.sh` passed on the unchanged copies first (pool-service 70 tests, fact-pipeline 86, every
`enforce` rule load-bearing under `--inv-ablate`). The full run took about 5 minutes.

```text
id   cat applied by now                                                         published                                                   same
P01  C   patch      pytest; 9 failing, B1 8                                     pytest; 9 failing, B1 8                                     yes
P02  C   patch      inv scan; 0 failing, B1 0                                   inv scan; 0 failing, B1 0                                   yes
P03  C   patch      pytest; 7 failing, B1 0                                     pytest; 6 failing, B1 0                                     NO
P04  C   rebased    inv lock --check, re-pin: inv lock --check; 0 failing, B1 1 inv lock --check, re-pin: inv lock --check; 0 failing, B1 1 yes
P05  U   patch      pytest; 1 failing, B1 1                                     pytest; 1 failing, B1 1                                     yes
P06  U   patch      ok; 0 failing, B1 0                                         ok; 0 failing, B1 0                                         yes
P07  U   patch      ok; 0 failing, B1 0                                         ok; 0 failing, B1 0                                         yes
P08  U   patch      ok; 0 failing, B1 0                                         ok; 0 failing, B1 0                                         yes
P09  B   patch      ok; 0 failing, B1 0                                         ok; 0 failing, B1 0                                         yes
P10  B   patch      ok; 0 failing, B1 0                                         ok; 0 failing, B1 0                                         yes
P11  B   patch      ok; 0 failing, B1 0                                         ok; 0 failing, B1 0                                         yes
P12  B   edits      ok; 0 failing, B1 0                                         ok; 0 failing, B1 0                                         yes
F01  C   patch      pytest; 1 failing, B1 1                                     pytest; 1 failing, B1 1                                     yes
F02  C   patch      inv scan; 0 failing, B1 0                                   inv scan; 0 failing, B1 0                                   yes
F03  C   patch      pytest; 2 failing, B1 0                                     pytest; 2 failing, B1 0                                     yes
F04  C   patch      inv validate; 0 failing, B1 0                               inv validate; 0 failing, B1 0                               yes
F05  U   patch      pytest; 2 failing, B1 0                                     pytest; 2 failing, B1 0                                     yes
F06  U   patch      ok; 0 failing, B1 0                                         ok; 0 failing, B1 0                                         yes
F07  U   patch      ok; 0 failing, B1 0                                         ok; 0 failing, B1 0                                         yes
F08  U   patch      pytest; 2 failing, B1 1                                     pytest; 2 failing, B1 1                                     yes
F09  B   patch      ok; 0 failing, B1 0                                         ok; 0 failing, B1 0                                         yes
F10  B   patch      ok; 0 failing, B1 0                                         ok; 0 failing, B1 0                                         yes
F11  B   patch      ok; 0 failing, B1 0                                         ok; 0 failing, B1 0                                         yes
F12  B   patch      inv lock --check, re-pin: inv lock --check; 0 failing, B1 0 inv lock --check, re-pin: inv lock --check; 0 failing, B1 0 yes
23 of 24 match the published results (where CI stopped, the number of failing tests, the re-pin and B1)
```

Every headline number in docs/evaluation.md is reproduced: 2 of 24 lock failures (P04, F12, both
refused on the reflex re-pin with the weakening named), 1 of 8 benign changes red (F12), 8 of 8
covered and 3 of 8 uncovered defects caught by CI, and the same 6 caught by an invariants mechanism.

The one difference, P03, is in the app, not the catch: R-21's guard rejects every approval in both
runs, and the current pool-service has one more happy-path test that goes through an approval
(`test_resize_door_needs_no_transaction`, which the published run's copy did not have), so 7 tests
fail instead of 6. Two of the other failing tests have new names
(`test_r22_growth_above_4x_is_rolled_back` is now `..._is_rejected`, and
`test_shrink_below_leased_is_a_storage_conflict` is now `..._store_conflict`).

How the inputs differ from the published run: P04 was applied from `REBASED` (R-22 now refuses at
the door, so the original text is gone; the weakening is the same, 4x to 10x), P12 from its text
edits (the patch's context moved), and B1 drops 29 and 37 tests instead of 31 and 43.

One runner artefact repeated the published run's: on 14 changes `patch` applied a hunk at an offset
and left a `.orig` backup, so `reversed_clean` was false although `patch -R` had undone the change.
The runner restores every copy from its pristine copy anyway, so no later change was affected, and
it now deletes those backups; a re-run of P02, P04, P12 and F12 after that fix gave the same results
with every copy clean.
