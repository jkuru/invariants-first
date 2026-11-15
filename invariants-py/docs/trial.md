# Measuring a trial: the two alarm rates

The question a trial answers is whether a team trusts the gate or clicks "approve" out of fatigue.
The two kinds of law raise alarms at different rates, so measure them apart:

1. **Value and schema laws.** A weakening is judged by JSON Schema and CEL, so a benign change
   should rarely be flagged.
2. **Path laws.** Split again into the blocking tier (a bypass or forbidden idiom: a call that skips a
   door) and the advisory tier (unresolved sites the scan cannot follow).

## Collect

Add `--metrics FILE` to the `inv check` your CI runs:

```bash
inv check --base main --metrics build/inv-metrics.jsonl
```

Each run appends one JSON line (an unwritable path is a usage error before any check runs):

```json
{"accepted": 0, "base": "main", "scan_blocking": 1, "scan_legacy_unresolved": 12,
 "scan_new_unresolved": 0, "scan_unresolved": 12, "time": "2026-11-18T10:02:11+00:00",
 "value_weakenings": {"rule": 0, "schema": 1}}
```

| Field | What it counts |
|---|---|
| `value_weakenings.schema` | weakened changes to a pinned schema, contract, target or provider (from the `--base` diff, else from the stored lock) |
| `value_weakenings.rule` | weakened changes to a rule (`bad`, enforcers, `mediation`, `strict` ...) |
| `scan_blocking` | bypass, forbidden and missing findings |
| `scan_new_unresolved` | unresolved sites the change adds since the base (`null` without `--base`) |
| `scan_legacy_unresolved` | unresolved sites already on the base (`null` without `--base`) |
| `scan_unresolved` | all unresolved sites not accepted |
| `accepted` | sites accepted in `allow_unresolved` |

Upload the file as a CI artifact, or append to one file per branch.

## Tally

For each alarm (a run where a count above is not zero), record by hand whether it was **real** (the
change did loosen a law or skip a door) or **benign** (a refactor, a false alarm), and whether the
reviewer accepted it after reading it or by reflex. Then, per tier:

```bash
python - build/inv-metrics.jsonl <<'PY'
import json, sys
runs = [json.loads(line) for line in open(sys.argv[1])]
for key in ("schema", "rule"):
    print(f"value {key:6}", sum(r["value_weakenings"][key] > 0 for r in runs), "of", len(runs), "runs")
print("scan blocking", sum(r["scan_blocking"] > 0 for r in runs), "of", len(runs), "runs")
print("scan new unresolved", sum((r["scan_new_unresolved"] or 0) > 0 for r in runs), "of", len(runs), "runs")
PY
```

If the noise sits in the advisory tier while the blocking tier and the value laws are precise, that
is the case for making value laws and the blocking scan tier the default surface, and for keeping
`strict` to the few sinks that need it.
