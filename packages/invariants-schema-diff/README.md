# invariants-schema-diff

**Is an edit a weakening?** Two static judges, for the review of a change to a contract or a rule.
Import name `invariants_schema_diff`.

- **JSON Schema.** `schema_changes(old, new)` lists every leaf difference between two schemas and
  marks the ones that let the new schema accept input the old one refused: a bound raised or removed,
  an enum widened, a field no longer required, a closed object opened (`additionalProperties`,
  `unevaluatedProperties`), a pattern or a format dropped, a type widened, a `$ref` re-pointed, a
  subschema moved under `not` or `if` (judged the other way round). Drafts 4, 7 and 2020-12
  (pydantic's) and OpenAPI 3.0 and Swagger 2.0 dialects. `constraint_weakening(path, old, new, ...)`
  judges one difference.
- **CEL conditions.** `compare_bad(old, new, parse)` says whether a CEL condition that flags bad
  input (`args.amount > 100`) flags the same cases after an edit, more (`"stronger"`), fewer
  (`"weaker"`), or cannot be decided (`"unknown"`). Both are compared as canonical trees (commuted
  operands, mirrored comparisons, De Morgan, repeated disjuncts and renamed macro variables are the
  same), and an edit that adds a sub-expression that can raise is `"unknown"`, never `"stronger"`.

Standard library only; the CEL judge parses with [invariants-cel](../invariants-cel), its one
dependency. It is the weakening gate of [invariants-first](../../invariants-py) (`inv lock`,
`inv check`), which judges every pinned schema and every rule's `bad` with it. It does not need
invariants-first.

## Install

```bash
pip install invariants-schema-diff      # pulls invariants-cel
```

From this repository: `pip install ./packages/invariants-cel ./packages/invariants-schema-diff`.
Python 3.11 or later.

## Five minutes

```python
from invariants_cel import subset
from invariants_schema_diff import compare_bad, schema_changes

old = {"type": "object", "additionalProperties": False, "required": ["amount", "currency"],
       "properties": {"amount": {"type": "integer", "minimum": 1, "maximum": 10000},
                      "currency": {"enum": ["USD", "EUR"]}}}
new = {"type": "object", "additionalProperties": False, "required": ["amount"],
       "properties": {"amount": {"type": "integer", "minimum": 1, "maximum": 50000},
                      "currency": {"enum": ["USD", "EUR"]}}}
for change in schema_changes(old, new, "schema Refund"):
    print(change)
# schema Refund: jsonschema.properties.amount.maximum: 10000 -> 50000  WEAKENED (maximum raised: larger values pass)
# schema Refund: jsonschema.required removed: ["currency"] -> (none)  WEAKENED (currency no longer required)

tighter = {**old, "properties": {**old["properties"], "amount": {"type": "integer", "minimum": 1, "maximum": 500}}}
print([c.weakened for c in schema_changes(old, tighter)])        # [False]: a tightening

# a rule's `bad` condition: true means the input is refused
print(compare_bad("args.amount > 10000", "args.amount > 50000", subset.parse))   # weaker
print(compare_bad("args.amount > 10000", "args.amount >= 10000", subset.parse))  # stronger
print(compare_bad("a > 1 || b", "b || 1 < a", subset.parse))                     # same
print(compare_bad("args.n > 5", "args.n / args.d > 5", subset.parse))            # unknown
```

Each change is a `Change` (`where`, `what`, `old`, `new`, `weakened`, `note`); `canonical` and
`same` are its type-strict JSON comparison (`1`, `1.0` and `true` differ).

## Tests

```bash
pip install './packages/invariants-schema-diff[test]'
python -m pytest packages/invariants-schema-diff/tests -q
```

How invariants-first uses the judges, and what it treats as a weakening, is in its
[docs/lockfile.md](../../invariants-py/docs/lockfile.md). Licensed under Apache-2.0.
