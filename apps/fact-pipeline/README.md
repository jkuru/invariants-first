# fact-pipeline

A batch worker CLI that:
1. reads text documents;
2. extracts facts with a model;
3. resolves entities;
4. stores facts in SQLite;
5. promotes the facts that are corroborated;
6. answers questions with citations.

The knowledge rules from section 7.3 of the Invariant-first guide are enforced by the
`invariants` library, which is installed as a package. The app uses only its public API.

The model is `fact_pipeline.fake_model.FakeModel`, a deterministic pattern reader. It never calls a real API.
Its fault switch (`--inject FAULT[:N]`) makes it return bad replies. The faults are:
- extraction: misquote, drop_qualifier, model_id, bad_predicate, garbage, inverted_interval, empty_quote, abstain
- answers: uncited_chunk, answer_garbage, no_citation

## Install

This is a standalone project. It sits in `apps/` of the monorepo and depends on
the library as an installed package:

```
python -m pip install -e '../../invariants-py[pydantic,schema]'   # the library from this checkout (or pip install invariants-first)
python -m pip install -e '.[dev]'
```

`./ci.sh` does this for you in a private `.venv` on its first run, unless you set `PYTHON` or
activate a venv.

## Use

```
fact-pipeline --db f.db add-entity "Beta Ltd (UK)" --alias "Beta Ltd" --xref companies-house:0123
fact-pipeline --db f.db --inject drop_qualifier ingest examples/s1.txt --publisher newswire-a
fact-pipeline --db f.db reviews                         # an ambiguous mention waits for a person
fact-pipeline --db f.db resolve-review 1 ent_...
fact-pipeline --db f.db ingest examples/s2.txt --publisher wire-b --async
fact-pipeline --db f.db promote
fact-pipeline --db f.db ask "What was the price of the acquisition of Beta Ltd?"
fact-pipeline --db f.db delete-source src_...
fact-pipeline --db f.db monitor                         # exit 1 on any finding
```

## Rules (`invariants.yaml`) and where each one runs

| Rule | Never exists | Enforcers |
|---|---|---|
| K-1 | a fact without an evidence span | ingest-door guard; trigger `k1_fact_needs_evidence`; `inv scan` (sink `insert_fact_row`) |
| K-2 | a quote not at its offsets, or a stale source hash | `@inv.model_call` on `propose_fact` (1 fresh resample, 2 format retries); `@inv.edge` on `accept_fact` |
| K-4 | a predicate outside the vocabulary | `Literal` in `FactReply` (rung 1); ingest-door re-check |
| K-7 | `valid_to` before `valid_from` | ingest-door guard; `CHECK` on `facts` |
| K-8 | an updated or deleted fact, evidence or status row | `BEFORE UPDATE/DELETE` triggers |
| K-9 | one external record with two canonical ids | `UNIQUE (source_system, source_key)` |
| K-11 | a contradiction on a functional property with no Conflict record | ingest-door guard, `on_violation: route`; the pipeline opens an owned Conflict |
| K-12 | a BELIEVED fact without two independent trusted publishers | promotion door; monitor; `inv scan` (sink `write_status`) |
| K-15 | a live fact resting only on a deleted source | post-effect guard on the transactional deletion door (rolls back); monitor reconciler |
| K-16 | a BELIEVED fact citing a stale source version | **detect mode**: promotion door plus monitor |
| K-18 | a model-supplied id | no id field, `extra="forbid"` (rung 1); ingest door accepts only resolver-minted ids |
| K-19 | an answer citing a chunk that was not retrieved | `@inv.model_call` on `compose_answer`; `@inv.edge` on `render_answer` |

## Layout

- `src/fact_pipeline/`: the app.
  - `store.py`: the doors and the storage enforcers.
  - `extract.py`, `answer.py`: the model calls and edges.
  - `pipeline.py`: the stages.
  - `monitor.py`: the nightly job.
  - `cli.py`: the command line.
- `bypass/fact_pipeline_backfill/`: a deliberate bypass of the promotion door. It is never imported by the app and is left out of the clean scan.
- `bench/bench_guards.py`: guard overhead (p50/p99).
- `tests/`:
  - functional tests;
  - Hypothesis properties (`test_properties.py`);
  - the guide's section 7.6 worked flow (`test_worked_flow.py`);
  - the async path;
  - the CLI;
  - the scan.

## CI

`./ci.sh` (it uses `$PYTHON`, else an activated venv, else a private `.venv` it creates) runs, in order:
1. `inv validate`
2. `inv lock --check`
3. `inv verify`
4. `inv scan` over the clean roots from `[tool.invariants.scan]`
5. `pytest --inv-verify --inv-ablate --inv-report`

After changing the rules or the code they pin, run `inv lock invariants.yaml` and review the diff.

See `FRICTION.md` for where the library was hard to use.
