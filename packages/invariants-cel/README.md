# invariants-cel

A CEL ([Common Expression Language](https://cel.dev)) engine for Python, and a static type check of
CEL conditions against the types your Python code declares. Import name `invariants_cel`.

- **Two engines behind one parser.** `subset` is the default: standard library only, a
  recursive-descent parser, a linear-time regex for `matches()`, a cost budget per evaluation.
  `cel-expr` (Google's C++ runtime, through `cel-expr-python`) is optional: `pip install
  'invariants-cel[cel]'`. `INV_CEL_BACKEND=subset|cel-expr|auto` picks one; both pass the same
  conformance suite (`tests/conformance/cel/`).
- **Python values in, strict CEL semantics.** Dataclasses, pydantic models, TypedDicts, mappings,
  `datetime` (a timestamp), `timedelta` (a duration), `UUID` and Enums map to CEL values by one table
  (`values`); a NaN or an infinity is an error, not a silent `false`; ints, uints and doubles keep
  their CEL types.
- **Fixed extensions** `inv.sum(list)`, `inv.zip(a, b)` and `inv.unique_by(list, x, key)`.
- **A load-time type check** (`typecheck`): a clause that compares a string with a number, reads a
  field a dataclass does not have, or reads a `Decimal` is refused before it ever runs.

It is the engine of [invariants-first](../../invariants-py), which uses it to evaluate the `bad`
condition of each rule. It does not need invariants-first.

## Install

```bash
pip install invariants-cel            # the subset engine: no dependencies
pip install 'invariants-cel[cel]'     # also the cel-expr engine (cel-expr-python, protobuf)
```

From this repository: `pip install ./packages/invariants-cel`. Python 3.11 or later.

## Five minutes

```python
import dataclasses

import invariants_cel as cel
from invariants_cel import typecheck as tc

# compile once, evaluate many times
prog = cel.get_backend().compile('args.amount_cents > 10000 && !(args.currency in ["USD", "EUR"])')
print(prog.evaluate({"args": {"amount_cents": 25000, "currency": "GBP"}}))   # True
print(prog.evaluate({"args": {"amount_cents": 25000, "currency": "USD"}}))   # False
print(sorted(prog.references()))      # [('args', 'amount_cents'), ('args', 'currency')]

# an error is a CelError, never a wrong answer: here a missing field
try:
    prog.evaluate({"args": {"currency": "GBP"}})
except cel.CelError as e:
    print("error:", e)

# the extensions
print(cel.get_backend().compile("inv.sum(xs) > 10").evaluate({"xs": [4, 5, 6]}))   # True


# the type check reads the types from your code's annotations
@dataclasses.dataclass
class Refund:
    amount_cents: int
    currency: str


def refund(req: Refund) -> bool: ...


params, _ = tc.signature_types(refund)
env = {"args": tc.T({"map"}, fields=params)}
tc.check_clause("args.req.amount_cents > 10000", env)           # fine
for bad in ('args.req.amount_cents > "10000"', "args.req.amount > 1"):
    try:
        tc.check_clause(bad, env)
    except tc.TypeMismatch as e:
        print("refused:", e)
```

`cel.available_backends()` lists the engines installed; `cel.get_backend("cel-expr")` asks for one by
name. Your own engine registers with `cel.register_backend(name, factory)`: it compiles text into a
`Program` with `evaluate(env)` and `references()`.

## Tests

```bash
pip install './packages/invariants-cel[test]'      # add ,cel to run every case on cel-expr too
python -m pytest packages/invariants-cel/tests -q
```

The language the engines read, the value mapping and the differences between the engines are in
invariants-first's [docs/cel.md](../../invariants-py/docs/cel.md). Licensed under Apache-2.0.
