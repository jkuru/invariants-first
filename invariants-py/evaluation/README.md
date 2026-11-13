# The 24-change evaluation harness

This directory holds the experiment behind [docs/evaluation.md](../docs/evaluation.md): 24 changes to
the two example apps (8 defects a rule covers, 8 no rule covers, 8 benign), the ground truth written
before any run, and the runner that applies each change, runs the app's own `ci.sh`, and compares
the outcome with the published results.

| File | What it is |
|---|---|
| [GROUND_TRUTH.md](GROUND_TRUTH.md) | the category and predicted result of each change, written before any change was applied |
| [changes.py](changes.py) | each change as exact text edits; `REBASED` holds edits re-based on the current apps where the originals no longer apply |
| [patches/](patches) | the unified diffs the published runs applied (`patch -p2`), made from `changes.py` |
| [run.py](run.py) | the runner |
| [make_patches.py](make_patches.py) | rebuilds the patches from `changes.py` against the current apps (into a temporary directory by default) |
| [published/](published) | the per-change result files of the published re-run (2026-11-10/11) and its B1 deselect lists |
| [RESULTS.md](RESULTS.md) | the published results and a re-run against the current apps, side by side |

## Running it

From the `invariants-py` directory, with a virtual environment that has the library and the apps'
test dependencies installed (`python -m pip install -e '.[dev,cel]'`):

```bash
python evaluation/run.py                       # all 24 changes, about 5 minutes
python evaluation/run.py P02 P04 F12           # a subset
python evaluation/run.py --work /tmp/inv-eval --python .venv/bin/python
```

- `--apps DIR` is where `pool-service` and `fact-pipeline` live (default: `../apps` beside
  `invariants-py`). They are copied into a working directory (`--work`, default a new temporary
  directory) twice, as a working copy and a pristine copy; the repository is never modified.
- `--python` is the interpreter `ci.sh` runs (it is passed as `PYTHON`); `inv` must sit next to it.
  A `PYTHONPATH` in the environment is kept after each app's `src/`, so
  `PYTHONPATH=$PWD/src python evaluation/run.py` tests this checkout's library without installing it.
- The apps' replay tests (`tests/test_replay.py`) are left out by default: the published runs
  predate them. They match every request to the model exactly, so a change to what reaches the
  model fails them whatever it is: with `--with-replay`, F06 (one chunk fewer retrieved, a defect no
  rule covers) is caught by two of them, and F12 (a new predicate, benign) fails two of them in B1.
- Results go to `WORK/results` (or `--results DIR`): one `<ID>.json` and the logs of every CI run,
  re-pinned run and baseline run per change, and `summary.txt` with the comparison table.

What the runner does, per change (the method in [docs/evaluation.md](../docs/evaluation.md#method)):

1. Before any change: run `ci.sh` on the unchanged copy of each app (it must pass), and find B1's
   deselect list: the tests that already fail on the unchanged app with every rule ablated
   (`INV_ABLATE=<all ids>`, `INV_ABLATE_TESTING=1`).
2. Apply the change: the published patch if it applies, else its text edits from `changes.py`, else
   the re-based edits. `applied_by` in each result says which.
3. Run `ci.sh`. If it stopped at `inv lock --check`, run the reflex re-pin `inv lock` and `ci.sh`
   again; both are recorded.
4. Run B1: the app's tests with every rule ablated, minus the deselect list.
5. Undo the change (`patch -R` for a patch), check the copy matches the pristine copy
   (`reversed_clean`), and restore it from the pristine copy either way.

The comparison with the published results is on where CI stopped, the number of failing tests, the
re-pin outcome and the number of B1 failures. Elapsed times are recorded but not compared.

## What has changed since the published run

The apps have changed since 2026-11-10 (the release that removed everything below the API, see
[CHANGELOG.md](../CHANGELOG.md)), so a few inputs differ, and [RESULTS.md](RESULTS.md) says how:

- P04's patch no longer applies: R-22 now refuses at the door before the write. `REBASED["P04"]`
  makes the same weakening (4x to 10x, with the falsifier edited to match) on the current rule.
- P12's patch fails on context, so its text edits from `changes.py` are applied instead.
- pool-service's `pool_api` contract now reads the live app (`{type: openapi, app: ...}`), so
  `openapi.json` is gone and the P02 and P10 patches (which edit it) give way to their text edits.
  R-22 reads the pool's size from the `current_size` provider, which `REBASED["P04"]` follows.
- fact-pipeline's `models.PREDICATES` is derived from the `Literal`, so F12's edit of that line no
  longer applies; `REBASED["F12"]` widens the `Literal` and K-4's list only. A re-run of P02, P04,
  P10 and F12 on 2026-11-12 matched the published results.
- B1 drops 36 of the 73 pool-service tests and 48 of the 90 fact-pipeline tests on the current apps
  (re-run of the deselect step on 2026-11-12; the published run dropped 31 of 72 and 43 of 86). The
  count moves as the apps gain tests: it was 29 and 37 at the earlier re-run of P02, P04, P10 and
  F12, and 34 and 46 before each app's `tests/test_replay.py` (the replayed model traces) added two
  tests that also fail with every rule ablated.
