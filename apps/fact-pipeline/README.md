# fact-pipeline

A batch worker CLI that:
1. reads text documents;
2. extracts facts with a model;
3. resolves entities;
4. stores facts in SQLite;
5. promotes the facts that are corroborated;
6. answers questions with citations.

The knowledge rules of section 10.3 of the Invariant-first guide ([`Invariant_first.md`](../../Invariant_first.md)) are enforced by the
`invariants` library, which is installed as a package. The app uses only its public API.

By default the model is `fact_pipeline.fake_model.FakeModel`, a deterministic pattern reader that never
calls a real API; `APP_MODEL` swaps in Claude or a replayed trace (see below). The fake's fault switch (`--inject FAULT[:N]`) makes it return bad replies. The faults are:
- extraction: misquote, drop_qualifier, model_id, bad_predicate, garbage, inverted_interval, empty_quote, abstain
- answers: uncited_chunk, answer_garbage, no_citation

## Install

This is a standalone project. It sits in `apps/` of the monorepo and depends on
the library as an installed package:

```
python -m pip install -e ../../packages/invariants-cel -e ../../packages/invariants-scan -e ../../packages/invariants-schema-diff   # the libraries it depends on
python -m pip install -e '../../invariants-py[pydantic,schema]'   # the library from this checkout (or pip install invariants-first)
python -m pip install -e '.[dev]'
```

`./ci.sh` does this for you in a private `.venv` on its first run, unless you set `PYTHON` or
activate a venv.

## Use

Two entities share the alias "Beta Ltd", so a mention of it waits for a person:

```
$ fact-pipeline --db f.db add-entity "Beta Ltd (UK)" --alias "Beta Ltd" --xref companies-house:0123
ent_0ee82d6c0261
$ fact-pipeline --db f.db add-entity "Beta Ltd (Cayman)" --alias "Beta Ltd"
ent_405974b4095d
$ fact-pipeline --db f.db --inject drop_qualifier ingest examples/s1.txt --publisher newswire-a
{
  "claims": 1,
  "conflicts": [],
  "extracted": 3,
  "file": "examples/s1.txt",
  "rejected": [],
  "reviews": [
    1,
    2
  ],
  "source_id": "src_e30606b934a3",
  "stored": []
}
$ fact-pipeline --db f.db reviews        # each open review: its candidates, the mention and the fact
$ fact-pipeline --db f.db resolve-review 1 ent_0ee82d6c0261
{
  "claims": 0,
  "conflicts": [],
  "extracted": 2,
  "rejected": [],
  "reviews": [],
  "source_id": null,
  "stored": [
    "fact_9765c67dff9879bf",
    "fact_4841f266901a47c5"
  ]
}
```

Resolving review 1 settles the mention, so review 2 (the same mention in the same source) is closed
with it; `resolve-review 2 ...` now exits 2 with ``no open entity review 2; none is open (a mention
opens one only when it matches two or more entities); `reviews` lists them``. Then:

```
$ fact-pipeline --db f.db ingest examples/s2.txt --publisher wire-b --async
{
  "claims": 0,
  "conflicts": [
    1
  ],
  "extracted": 2,
  "file": "examples/s2.txt",
  "rejected": [],
  "reviews": [],
  "source_id": "src_dfa0e44486f9",
  "stored": [
    "fact_9765c67dff9879bf",
    "fact_10b099cffd1da672"
  ]
}
$ fact-pipeline --db f.db promote
{
  "audit_sample": [
    "fact_4841f266901a47c5"
  ],
  "promoted": [
    "fact_9765c67dff9879bf"
  ]
}
$ fact-pipeline --db f.db ask "What was the price of the acquisition of Beta Ltd?"
{
  "citations": [
    "src_dfa0e44486f9:8c27c940:0"
  ],
  "retrieved": [
    "src_dfa0e44486f9:8c27c940:0",
    "src_e30606b934a3:67c1d9f1:0",
    "src_e30606b934a3:67c1d9f1:1"
  ],
  "status": "ANSWERED",
  "text": "Acme Corp completed its acquisition of Beta Ltd for $2.0 billion."
}
$ fact-pipeline --db f.db delete-source src_e30606b934a3
{
  "demoted": [
    "fact_9765c67dff9879bf"
  ],
  "live_orphans": 0,
  "retracted": [
    "fact_4841f266901a47c5"
  ],
  "source_id": "src_e30606b934a3"
}
$ fact-pipeline --db f.db monitor        # exit 1 on any finding
[]
```

## Rules (`invariants.yaml`) and where each one runs

| Rule | Never exists | Enforcers |
|---|---|---|
| K-1 | a fact without an evidence span | ingest-door guard; `inv scan` (sink `insert_fact_row`) |
| K-2 | a quote not at its offsets, or a stale source hash | `@inv.model_call` on `propose_fact` (1 fresh resample, 2 format retries); `@inv.edge` on `accept_fact` |
| K-4 | a predicate outside the vocabulary | `Literal` in `FactReply` (rung 1); ingest-door re-check |
| K-7 | `valid_to` before `valid_from` | ingest-door guard |
| K-11 | a contradiction on a functional property with no Conflict record | ingest-door guard, `on_violation: route`; the pipeline opens an owned Conflict |
| K-12 | a BELIEVED fact without two independent trusted publishers | promotion door; monitor; `inv scan` (sink `write_status`) |
| K-15 | a live fact resting only on a deleted source | **detect mode**: post-effect check on the deletion door (an external effect, so it reports); monitor reconciler |
| K-16 | a BELIEVED fact citing a stale source version | **detect mode**: promotion door plus monitor |
| K-18 | a model-supplied id | no id field, `extra="forbid"` (rung 1); ingest door accepts only resolver-minted ids |
| K-19 | an answer citing a chunk that was not retrieved | `@inv.model_call` on `compose_answer`; `@inv.edge` on `render_answer` |

## Layout

- `src/fact_pipeline/`: the app.
  - `store.py`: the doors. Its schema also keeps its own constraints (evidence before a fact,
    append-only history, one canonical id per external record); they are below the API and no rule
    names them. (Rules K-8 and K-9 used to declare them; they were removed with the library's
    storage enforcers.)
  - `extract.py`, `answer.py`: the model calls and edges.
  - `pipeline.py`: the stages.
  - `monitor.py`: the nightly job.
  - `cli.py`: the command line.
  - `llm.py`: the prompts and the `APP_MODEL` choice of model.
- `bypass/fact_pipeline_backfill/`: a deliberate bypass of the promotion door. It is never imported by the app and is left out of the clean scan.
- `bench/bench_guards.py`: guard overhead (p50/p99).
- `tests/`:
  - functional tests;
  - Hypothesis properties (`test_properties.py`);
  - a worked flow, step by step: Acme acquires Beta (`test_worked_flow.py`);
  - the async path;
  - the CLI;
  - the scan;
  - the happy-path flows on replay (`test_replay.py`, traces in `tests/fixtures/traces/`).

## Models: fake, replay, Claude

`APP_MODEL` picks the model (`src/fact_pipeline/llm.py`):

| `APP_MODEL` | model |
|---|---|
| `fake` | the deterministic fake above; the app's default, and what the seeded-defect tests use, since it can be told to misbehave |
| `replay` | answers from a trace file (`APP_TRACE`), failing with a diff on a request the trace does not hold; the default of `tests/test_replay.py` |
| `anthropic` | Claude (`claude-haiku-5-5`, or `APP_MODEL_ID`) through `invariants.integrations.anthropic`; needs `pip install 'invariants-first[anthropic]'` and `ANTHROPIC_API_KEY` |
| `record` | Claude, writing every call to the trace file |

The CLI reads it too: `APP_MODEL=anthropic fact-pipeline ingest ...` (`--inject` works only with the fake).

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

`./ci.sh` (it uses `$PYTHON`, else an activated venv, else a private `.venv` it creates) runs, in order:
1. `inv validate`
2. `inv lock --check`
3. `inv verify`
4. `inv scan` over the clean roots from `[tool.invariants.scan]`
5. `pytest --inv-verify --inv-ablate --inv-report --inv-report-json build/inv-report.json`

After changing the rules or the code they pin, run `inv lock invariants.yaml` and review the diff.

See `FRICTION.md` for where the library was hard to use.
