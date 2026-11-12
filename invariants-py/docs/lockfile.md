# The lockfile

`invariants.lock` pins what each rule's meaning depends on: the rule's semantics, its targets'
signatures and types, its providers, and the contracts and schemas they name. `inv lock` writes it,
`inv lock --check` fails CI when it is stale, and neither will silently accept a change that weakens
a rule.

- [Why a lock](#why-a-lock)
- [Format (lock version 3)](#format-lock-version-3)
- [What `--check` reports](#what---check-reports)
- [Weakening](#weakening)
- [How each field is judged](#how-each-field-is-judged)
- [How `bad` is compared](#how-bad-is-compared)
- [Migration](#migration)
- [Where the CLI imports from](#where-the-cli-imports-from)
- [Limitations](#limitations)

## Why a lock

A rule can be defeated without touching the guard: relax `bad`, move a check to `post_effect`,
rebind a name, or change the type a rule reads. The lock turns each of those into a reviewed diff.
It pins only what a rule's meaning depends on, so an ordinary code change does not make it stale. A
body-only refactor, a new function, or a new endpoint in the OpenAPI file leaves the lock fresh.

```text
$ inv lock
invariants.lock: 1 change(s) against the stored lock
  rule PAY-1: bad [refund must be positive]: 'args.amount <= 0' -> 'args.amount < 0'  WEAKENED (the condition now matches fewer cases)
refused: 1 change(s) weaken a rule; the lock was not written. Review them and re-run `inv lock --accept-weakening` to accept them.
```

`inv lock` prints what changed against the stored lock, and writes the lock only when nothing is
weakened or when `--accept-weakening` is given, so a possible weakening is never re-pinned
silently. `--update-artifacts` first rewrites stale `artifact` digests in the rule file (and that
rewrite is itself a possible weakening).

## Format (lock version 3)

| Section | Pinned |
|---|---|
| `rules` | `rev`, `status`, the expanded targets, the rule's semantic content (`bad`, `enforcers` with `mode`, `timing`, `on_violation`, `on_error`, `view` and `artifact`, `bind`, `scope`, `subject`, `mediation` sink, door, tables and residual paths, `model_policy` with its defaults filled in, `compares_to`, `falsified_by`) and a digest per prose field (`statement`, `why`, `owner`, `class`, `mediation.chokepoint`). |
| `targets` | The signature as text (unwrapped through decorators) and as resolved `types`, `effects`, `transaction`, and a model-call target's reply schema. |
| `providers` | `type`, `from`, `args`, `consistency`, and the function's signature and resolved types. |
| `contracts` | A pydantic, dataclass, TypedDict or `{field: type}` schema; an `openapi` file as the parameters, request body and response schemas of the operations rules name; a `python` module by reference only (its targets carry the pins); any other file by digest. |
| `schemas` | Every schema the entries above use, keyed by the sha256 of its canonical JSON. A model used by a target and a contract, or a shared OpenAPI component (a 422 error body), is stored once. |

In detail:


`inv lock` writes `invariants.lock` beside the rule file: JSON with sorted keys, two-space indent,
any value that fits in 128 columns on one line, and a `lock_version` field (currently 3). It pins
what each rule's meaning depends on, entry by entry:

- `rules.<id>`: `rev`, `status`, `targets` (the subject target, edge targets and expanded
  `applies_to`), `semantic` and `doc`.
  - `semantic` holds every rule field except `id`, `rev`, `status`, `evidence` and the prose
    fields. The loader's defaults are filled in, so writing a default does not count as a change:
    each enforcer's `on_violation: reject` and `on_error: fail_closed`, and `model_policy`'s
    `format_retries: 2`, `semantic_resamples` (0 with `compares_to: held_data`, else 1) and
    `feedback: {format: parser_error, semantic: none}`.
  - `doc` holds a digest of each of `statement`, `why`, `owner`, `class` and
    `mediation.chokepoint`.
- `targets.<key>`: `signature` (text), `types`, `effects`, `transaction` and, for a model-call
  target, `reply_schema`.
- `providers.<name>`: `type`, `from`, `args`, `consistency`, and the `from` function's `signature`
  and `types`.
- `contracts.<name>`: `{type, ref}` plus, by type:
  - `schema` for a pydantic model, a dataclass, a TypedDict or a `{field: type}` mapping;
  - `signature` and `types` for a function;
  - `operations` for an `openapi` file: each operation a rule names through `subject.operation`,
    as its method, path, `parameters`, `requestBody` and `responses` schemas (a file no rule names
    an operation of is pinned by digest);
  - nothing more for a `python` module, whose targets and providers carry the pins;
  - `hash` for any other file.
- `schemas.<sha256>`: each schema the entries above reference as `{"$ref": "sha256:..."}`, keyed
  by the digest of its canonical JSON. The same model, or a shared OpenAPI component, is stored
  once.

`types` maps each annotated parameter, and `return`, to its resolved type (`typing.get_type_hints`).
Aliases (`Caps = list[int]`, `type Caps = ...`), `Literal`, unions, `Annotated` and `NewType` are
expanded. A dataclass, TypedDict or pydantic model is pinned by its fields in the schema table,
recursively: nested classes go into the entry's `$defs`, cycles are cut by name, and nesting stops
at 64 nested classes or 24 levels (marked `truncated`). Pinned schemas have no prose: `title`,
`description`, `summary`, `tags`, `examples` and `$comment` are stripped at every level. A property
named `title` is kept, because it is a name, not a keyword.

`inv lock --check` exits 1 when anything differs. It lists each change as
`<where>: <what>: old -> new`, one line per changed key of a dict value, with values compared
type-strictly (`1` and `true` differ). A schema change is reported once, as `schema <name>`, with
the entries that use it.

Types are resolved with `typing.get_type_hints`, not read as annotation text. An alias change
(`Caps = list[int]` to `list[float]`) changes the pin. So does a field type in a dataclass,
TypedDict or pydantic model that a signature or return type names, followed recursively. Prose is
stripped from every pinned schema, so a docstring edit does not re-pin. The lock is written with
sorted keys, and a value that fits in 128 columns stays on one line. The output is byte-identical
across hash seeds and Python versions.

## What `--check` reports

`inv lock --check` names each change as `old -> new`. A dict-valued field shows one line per
changed key, and a list whose members were replaced shows the removed and added members as one
`old -> new` line. Values compare type-strictly, so `1` to `true` is a change. `--check` also checks
the schemas table: each key must be the sha256 of its body, every `$ref` must name an entry, and no
entry may be unreferenced. A damaged table is reported as stale; a plain `inv lock` says so and
rewrites it.

## Weakening

These changes are marked **WEAKENED**:
- a lower status or enforcer mode, or a removed enforcer, clause or rule;
- an `on_violation` below reject, or `on_error: fail_open_and_page`;
- `timing` moved to `post_effect`;
- a provider moved to a staler consistency;
- a removed sink or table, or a narrower statement kind;
- a new door on a scanned sink;
- a target declared `effects: none`, or a target no longer covered;
- `compares_to` removed, `compensable` set to true, or more `format_retries` or
  `semantic_resamples`;
- a removed `mediation.residual_paths` entry (while one is listed `inv status` withholds `held`,
  so removing it can raise the rule's standing);
- a `bad` that now matches fewer cases;
- a pinned pydantic schema that now accepts more input (see below);
- a removed or changed constraint in a `sql` contract.

**Constraints the code already has.** A `pydantic` contract (or a pydantic model in a target's
signature) is pinned as its JSON Schema, so its constraints are pinned too.
`lockfile.constraint_weakening` marks a change **WEAKENED** when the model may now accept more input.
That covers:

- `maximum`, `exclusiveMaximum`, `maxLength`, `maxItems` or `maxProperties` raised or removed;
- the matching minimums lowered or removed;
- an `enum` value added, or the enum removed;
- a field that is no longer `required`, or a property removed;
- `pattern`, `format`, `multipleOf`, `const`, `type` or `anyOf`/`oneOf` changed or removed;
- `additionalProperties` relaxed.

Adding a constraint or tightening one is not a weakening. Other schema changes, such as a default
value or a new optional field, are reported as pin changes. Changing `le` to `lt` with the same
bound shows as a removed `maximum` and is treated as a weakening; the check is conservative.

A `{type: sql, ref: schema.sql}` contract is pinned as the sorted list of its constraints (CHECK,
NOT NULL, UNIQUE, PRIMARY KEY, from `CREATE TABLE` and `ALTER TABLE ... ADD`; see
`invariants.sql_constraints`). Removing or editing a constraint is a weakening; adding one is not.
Indexes, comments and the rest of the file are not pinned.

**Comparing with a base branch.** `inv lock --check` compares with the lock in the working tree, which
the change itself may have re-pinned with `--accept-weakening`. `inv check --base REF` compares the
rule file with the lock committed at `REF` instead, and fails on any weakening since then. It passes
only when `--allow-weakening` is given or `INV_ALLOW_WEAKENING=1` is set, which a reviewer sets (for
example from a pull-request label), not the change's author.

Any change to a field that changes what `bad` reads, or how a check runs, is a possible
weakening: `bind`, an enforcer's `view`, `artifact`, `conformance_test` or `generated`, a
provider's `args`, `from` or `type`, `subject` (contract, operation, target), `scope`, a target's
`transaction`, and `model_policy`'s `schema`, `feedback` and `abstain`. That includes an
`artifact` digest rewritten by `inv lock --update-artifacts`. Every field of the rule-file schema
is classified in `lockfile.FIELD_POLICY`, and a field that is not classified is a possible
weakening. A test walks the schema, so a new field cannot ship unclassified.

## How each field is judged

`lockfile.FIELD_POLICY` classifies every field of the rule-file schema, and a test
(`tests/test_fix_round13.py`) walks `schema/invariants.schema.json` and fails on a field it does not
list. A field that is not listed is a possible weakening. `inv lock` refuses to write a weakened
change unless `--accept-weakening` is given.

| Class | Fields | Marked `WEAKENED` when |
|---|---|---|
| possible weakening | `bind`; `subject` (`contract`, `operation`, `target`); `scope`; enforcer `view`, `artifact`, `conformance_test`, `generated`; provider `type`, `from`, `args`; target `transaction`; `model_policy.schema`, `feedback`, `abstain`; contract `type`, `ref` | any change. These change what `bad` reads or how a check runs. This covers an `artifact` digest rewritten by `--update-artifacts`. |
| ranked | `status` | it moves down `enforce > detect > candidate/contested > retired` |
| ranked | enforcer `mode` | it moves down `enforce > detect > off` |
| ranked | enforcer `on_violation` | it moves from `reject`/`halt` to `escalate`/`route` |
| ranked | enforcer `on_error` | it becomes `fail_open_and_page` |
| ranked | enforcer `timing` | it becomes `post_effect` |
| ranked | provider `consistency` | it moves down `same_txn/immutable > snapshot > stale_ok` |
| ranked | target `effects` | it becomes `none` |
| ranked | `compensable` | it becomes `true` |
| more is weaker | `model_policy.format_retries`, `semantic_resamples` | it goes up |
| presence | `compares_to` | it is removed |
| analysed | `bad` (`when`) | it now matches fewer cases, or the direction cannot be decided |
| analysed | enforcer `point`, `target` (identity), a `bad` clause, a rule (`id`) | it is removed |
| analysed | `mediation.sink`, `mediation.tables` | a sink or table, or a statement kind of a table, is removed |
| analysed | `mediation.door` | a door is added to a rule that already had a sink |
| analysed | rule `targets`, `applies_to` | a target is no longer covered |
| analysed | `mediation.residual_paths` | an entry is removed (it can raise the rule's standing to `held`) |
| neutral | `rev`, `spec`, clause `message`, `falsified_by` | never (recorded as a change) |
| prose | `statement`, `why`, `owner`, `class`, `mediation.chokepoint` | never (digest only) |
| pin | signatures, `types`, schemas, OpenAPI operations, file digests | never (a code change, shown old -> new) |
| computed | `evidence` | not pinned |

## How `bad` is compared

`bad` is compared as a canonical parse tree. Literals keep their type, so `true`, `1` and `1.0`
are different values (on a dyn field `x == true` and `x == 1` differ), and so are `0.0` and `-0.0`.
These are the same condition, not a weakening:
- whitespace;
- commuted operands of `||`, `&&`, `==`, `!=`, and of `+` and `*` on numbers (`+` counts as
  commutative only when an operand is plainly numeric, since it may be string or list
  concatenation);
- regrouped `||` and `&&` chains (not `+` or `*`: floating-point rounding and int overflow depend
  on the grouping);
- a mirrored comparison (`10000 < args.size` for `args.size > 10000`);
- `!` pushed inward: De Morgan, `!(a == b)` for `a != b`, and `!!e` for `e` when `e` is a bool;
- a repeated disjunct or conjunct, or a repeated clause, dropped;
- a renamed comprehension variable (`exists(x, x > 5)` for `exists(y, y > 5)`);
- a disjunction split into clauses or merged from them.

Two more hold only for integers, so they apply only when both operands are provably int or uint:
an int literal, `size()`, `int()`, `uint()`, or arithmetic on those. Then `> 10000` is `>= 10001`,
and `!(a >= 1)` is `a < 1` (for a double, 10000.5 or NaN makes these differ). A field declared `int`
does not count: Python does not enforce annotations, so a caller can pass 10000.5. To make such an
edit, write `int(args.n) > 10000`, or accept it with `--accept-weakening`.

Otherwise the direction is decided when the two trees differ only in numeric literals of the same
type, list-literal elements, the strictness of a comparison, or an added or dropped disjunct or
conjunct. `!(p in [...])` with a longer list is weaker. Scaling by `*` or `/` keeps its direction
only when the other operand is provably non-negative (a non-negative literal, `size()`, `uint()`
or a uint-typed field): `> 4 * size(x)` to `> 10 * size(x)` is weaker, while `> 4 * x` to
`> 10 * x` on an `int` x cannot be decided, since x may be negative. A threshold edit across types
(`> 5` to `> 6.0`) has no direction. Any other edit to `bad` is a possible weakening.

## Migration

`inv lock --check` refuses an older lock with a hint, and `inv lock` migrates it:
- **Version 2.** The migration compares rule semantics (with the loader's defaults filled in),
  effects, transactions, signature text, provider fields and contract references, so the weakening
  gate holds across the migration. Types and schemas are re-pinned in the version-3 form.
- **Version 1** (whole-module hashes). Each rule is compared against its hash. A rule that changed
  since that pin (version 1 cannot show how) or went away needs `--accept-weakening`.

A lock with a newer version is refused. So is a malformed lock: a `lock_version` that is not an
integer (`true` included), or a section or entry of the wrong shape, is a `LockError` that names
the file and the place.

## Where the CLI imports from

`validate`, `lock`, `verify` and `status` import the app from this checkout, ahead of
site-packages: every `--path DIR`, then the project's `src/`, its setuptools `packages.find`
`where` and `[tool.invariants.scan]` roots, the rule file's directory, and the project root (the
nearest directory holding a `pyproject.toml`). So the lock pins the source beside the rule file,
not an installed copy or another checkout that shares the venv. When a pinned module is still
imported from outside the project (it is not under any of those roots), `inv lock` and
`inv verify` print a warning naming the file it came from; pass `--path` to point at the source.

The full synopsis is `inv lock [file] [--check] [--accept-weakening] [--lock FILE]
[--update-artifacts] [--path DIR]...`.

## Limitations

- Signature text depends on how `inspect.signature` renders a signature, and a pydantic pin depends
  on `model_json_schema()`. So a pydantic upgrade that renders schemas differently makes the lock
  stale (not weakened).
- The direction of a `bad` edit is a syntactic analysis over canonical trees. Anything the analysis
  cannot decide is reported as a possible weakening, never as safe.
- "Matches more cases" also requires that the edit adds no sub-expression that can raise where the
  old condition could not: a new field or variable read (a missing key, a read on null), a new or
  changed divisor or modulus (other than a non-zero literal), new or changed arithmetic that may
  overflow int64 (bounded arithmetic over int literals and `size()` is exempt), a new index or a new
  function call. Such an edit is reported as a possible weakening: under
  `on_error: fail_open_and_page` an error lets the call through, so a rule that used to reject an
  input could let it pass. The check applies to every rule, whatever its `on_error`. An operand added
  to an existing `||` or `&&` is exempt, since wherever the old condition held its own operands
  decide the result and CEL's commutative `||` and `&&` absorb the new operand's error; so is a new
  `bad` clause, because any clause that fires is a violation even if another errors. So
  `args.n > 5` to `args.n > 4`, a literal added to an `in` list, and a clause or disjunct added over
  any fields are still accepted, while `args.region in ["xx", "yy"]` to
  `args.region in ["xx", "yy", args.cfg.extra]`, `size(s) / 2 > 1` to `size(s) / 0 > 1` and
  `args.n + 1 > 5` to `args.n + 2 > 5` are not.
- A class-level SQL constant (`self.SQL`) is not resolved to its uses, so the scan judges it where
  it is defined.
- A behaviour change inside a target's body, or in a provider's body, does not change the lock. The
  guards, the tests and `inv scan` check that.
