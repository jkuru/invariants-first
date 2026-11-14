# pool-service

A FastAPI + SQLite service that leases worker-pool slots, resizes pools through an approval flow, splits job
batches across leases, and triages failed jobs with a model. Its invariants live in `invariants.yaml` and are
enforced by the [`invariants`](../../invariants-py/README.md) library, which is used only as an installed package
through its public API (`import invariants as inv`, `invariants.integrations.*`).

## Install

This is a standalone project. It sits in `apps/` of the monorepo and depends on
the library as an installed package:

```
pip install -e ../../packages/invariants-cel -e ../../packages/invariants-scan -e ../../packages/invariants-schema-diff   # the libraries it depends on
pip install -e '../../invariants-py[fastapi,pydantic,schema]'   # the library from this checkout (or pip install invariants-first)
pip install -e '.[dev]'
```

`./ci.sh` does this for you in a private `.venv` on its first run, unless you set `PYTHON` or
activate a venv.

Run it: `POOL_SERVICE_DB=pool.db uvicorn pool_service.main:app` (uvicorn is not a dependency).
`POOL_SERVICE_RULES` overrides the rule file path.

## Endpoints

| Method and path | What it does | Rules |
|---|---|---|
| `POST /pools` | create a pool | H-2 (route), R-24 (door) |
| `GET /pools/{id}` | size and leased slots | |
| `POST /pools/{id}/leases` | lease slots | H-1 (route), R-20 (door) |
| `DELETE /leases/{id}` | release a lease | |
| `POST /pools/{id}/resizes` | propose a resize | |
| `POST /resizes/{id}/approve` | approve it | |
| `POST /resizes/{id}/apply` | apply it | R-21, R-22 (door) |
| `POST /pools/{id}/split` | split `total_jobs` across the pool's leases | R-1 |
| `POST /jobs` | record job facts (from the scheduler) | |
| `POST /jobs/{id}/triage` | model proposes a cause, then remediation | R-7 |

Guard outcomes come back as `application/problem+json`: 422 rejected, 202 escalated. The body names
the rule, never the values. The store's own triggers also refuse an over-lease (two concurrent leases
can both pass R-20's check); the app answers that with a plain 409 that names no rule, because it is
below the API.

## Rules

| Id | Statement | How it is enforced |
|---|---|---|
| R-1 | split_work shares are non-negative, within capacity, sum to total | post-effect guard on a pure function |
| R-7 | a model's cause never contradicts the recorded job facts | `@inv.model_call` with the pydantic `CauseReply`, the `job` provider, `compares_to: held_data` (a contradicting reply is not resampled), abstain `CANNOT_COMPLY`, `@inv.edge` re-check on `remediate` |
| R-20 | leases never exceed pool size | pre-effect guard on `lease_slots` with two providers; `inv scan` (sink `insert_lease`) |
| R-21 | a resize needs an APPROVED proposal by someone else | pre-effect guard on `resize_door`, approval provider |
| R-22 | one resize never grows a pool more than 4x | pre-effect guard: the requested size against the current size |
| R-23 | pools.size is written only through the doors | `inv scan` (sink/door) |
| R-24 | a new pool has 1..10000 slots | pre-effect guard on `create_pool` |
| H-1, H-2 | lease size 1..64; pool id shape | FastAPI route guards (`point: middleware`) |

## Layout

```
src/pool_service/    the package: store (raw SQL), db, service (doors), work, triage, fake_model, llm, config,
                     api, main, openapi_app (the app the pool_api contract reads)
bypass/              deliberately bad code that the scan must flag; excluded from the clean scan
bench/bench_guards.py  guard overhead, p50/p99
scripts/export_openapi.py  prints the OpenAPI document (the pool_api contract reads the live app)
tests/               functional, hypothesis property, concurrency, CLI, scan and replay tests
```

## The fake model

`pool_service.fake_model.FakeModelClient` is deterministic and never touches a network. It classifies the log
by keywords. `inject("malformed", "contradict", ...)` makes the next replies bad, and `always(fault)` makes
every reply bad. The faults are `malformed`, `extra_field`, `wrong_enum`, `missing_field`, `contradict` and
`abstain`.

## Models: fake, replay, Claude

`APP_MODEL` picks the model (`src/pool_service/llm.py`):

| `APP_MODEL` | model |
|---|---|
| `fake` | the deterministic fake above; the app's default, and what the seeded-defect tests use, since it can be told to misbehave |
| `replay` | answers from a trace file (`APP_TRACE`), failing with a diff on a request the trace does not hold; the default of `tests/test_replay.py` |
| `anthropic` | Claude (`claude-haiku-5-5`, or `APP_MODEL_ID`) through `invariants.integrations.anthropic`; needs `pip install 'invariants-first[anthropic]'` and `ANTHROPIC_API_KEY` |
| `record` | Claude, writing every call to the trace file |

`main.py` applies it to the served app: `APP_MODEL=anthropic uvicorn pool_service.main:app`.

`tests/test_replay.py` runs the happy-path flows on replay, from `tests/fixtures/traces/`. **The
committed traces are hand-written fixtures, not recordings**: the requests come from the app's own
prompt code, the replies were written by hand in the shape the API returns, and each file says
`"fixture": "hand-written"`. One trace scripts a first reply that fails the closed schema, so the
parser error goes back as a second user turn in the same conversation and the second reply fixes it.

To record real traces (this calls the API and costs money; it rewrites the files, marked
`"fixture": "recorded"`; the scripted format-failure flow is skipped):

```
pip install 'invariants-first[anthropic]'
export ANTHROPIC_API_KEY=...
python -m pytest tests/test_replay.py --record        # or APP_MODEL=record python -m pytest tests/test_replay.py
```

`APP_MODEL=anthropic python -m pytest tests/test_replay.py` runs the same flows on Claude without
writing anything.

## CI

`./ci.sh` (it uses `$PYTHON`, else an activated venv, else a private `.venv` it creates) runs `inv validate`,
`inv lock --check`, `inv verify`, the clean `inv scan --root src`, `inv status`, then
`pytest --inv-verify --inv-ablate --inv-report --inv-report-json build/inv-report.json`. R-23's falsifier, `tests/test_scan.py`, runs the bypass
scan (`--root src --root bypass`) in a subprocess; under `--inv-ablate` the plugin sets `INV_ABLATE=R-23`,
so the scan skips the rule and the falsifier fails, as it must. After a rule, target signature or API
change, run `inv lock`: the pool_api contract reads the live app (`pool_service.openapi_app:app`), so
there is no exported file to keep current. `schema.sql` is the store's own business and is not pinned.

## Benchmark

`python bench/bench_guards.py --n 3000` prints p50/p99 for each case with and without the guards.

Library friction found while building this lives in [FRICTION.md](FRICTION.md).
