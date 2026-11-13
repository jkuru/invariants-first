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
- [Upgrading from a release with rules below the API](#upgrading-from-a-release-with-rules-below-the-api)
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
| `rules` | `rev`, `status`, the expanded targets, the rule's semantic content (`bad`, `enforcers` with `mode`, `timing`, `on_violation`, `on_error`, `view` and `artifact`, `bind`, `scope`, `subject`, `mediation` sink, door, allow and residual paths, `model_policy` with its defaults filled in, `compares_to`, `falsified_by`) and a digest per prose field (`statement`, `why`, `owner`, `class`, `mediation.chokepoint`). |
| `targets` | The signature as text (unwrapped through decorators) and as resolved `types`, `effects`, and a model-call target's reply schema. |
| `providers` | `type`, `from`, `args`, and the function's signature and resolved types. |
| `contracts` | A pydantic model's JSON Schema; a dataclass or TypedDict as a JSON Schema too; a `{field: type}` schema; an `openapi` file, or a live app's generated document (`app: module:app`), as the parameters, request body and response schemas of its operations; a `python` module by reference only (its targets carry the pins); any other file by digest. |
| `schemas` | Every schema the entries above use, keyed by the sha256 of its canonical JSON. A model used by a target and a contract, or a shared OpenAPI component (a 422 error body), is stored once. |
| `scan` | When a rule has a sink: the source `roots` (relative to the rule file) and `exclude` globs `inv check` scans with, from `[tool.invariants.scan]`, the defaults and any `--root`/`--exclude`. |

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
- `targets.<key>`: `signature` (text), `types`, `effects` and, for a model-call
  target, `reply_schema`.
- `providers.<name>`: `type`, `from`, `args`, and the `from` function's `signature`
  and `types`.
- `contracts.<name>`: `{type, ref}` (or `{type, app}`) plus, by type:
  - `schema` for a pydantic model (`{name, pydantic: <model_json_schema()>}`), a dataclass or
    TypedDict (`{name, jsonschema: ...}`: pydantic's `TypeAdapter(cls).json_schema()` when pydantic is
    installed, else a JSON Schema built from the resolved annotations, with the bounds an
    `Annotated` metadata object carries as `gt`/`ge`/`lt`/`le`/`min_length`/`max_length`), or a
    `{field: type}` mapping;
  - `signature` and `types` for a function;
  - `operations` for an `openapi` file: each operation a rule names through `subject.operation`,
    as its method, path, `parameters`, `requestBody` and `responses` schemas (a file no rule names
    an operation of is pinned by digest);
  - `operations` for an `openapi` contract read from a live app, `{type: openapi, app:
    "myapi.main:app"}`: `inv lock` imports the app and calls `app.openapi()` (a FastAPI app has
    it). The operations rules (or the contract's `operation`) name are pinned, or every operation
    when none is named, since a generated document has no file to digest. Give `ref` or `app`, not
    both; a changed `app` is a possible weakening;
  - nothing more for a `python` module, whose targets and providers carry the pins;
  - `hash` for any other file.
- `schemas.<sha256>`: each schema the entries above reference as `{"$ref": "sha256:..."}`, keyed
  by the digest of its canonical JSON. The same model, or a shared OpenAPI component, is stored
  once.
- `scan`: `{roots, exclude}`, present when a rule declares a sink. `inv check` builds the same
  pin from the roots and excludes it is about to scan with, so a `[tool.invariants.scan]` edit or
  an `inv check --exclude` shows up as a lock change. An added exclude or a removed root is
  **WEAKENED** (the scan reads less code); a removed exclude or an added root is a change only. A
  lock taken before the scan was pinned is compared with what that configuration scanned by
  default: `inv lock` reads `[tool.invariants.scan]` (else src/ and the rule file's directory) from
  the working tree, and `inv check --base REF` reads them from `REF`. So the first pin is not a way
  to slip in an exclude or narrow the roots in the same change. To scan with an extra `--root` or `--exclude`, pin it with
  `inv lock --root DIR --exclude GLOB`; prefer `[tool.invariants.scan]` in `pyproject.toml`.

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
- a removed sink;
- a new door on a scanned sink, or a new `allow` entry (a back door is now let through);
- a target declared `effects: none`, or a target no longer covered;
- `compares_to` removed, `compensable` set to true, or more `format_retries` or
  `semantic_resamples`;
- a removed `mediation.residual_paths` entry (while one is listed `inv status` withholds `held`,
  so removing it can raise the rule's standing);
- a `bad` that now matches fewer cases;
- a pinned schema (pydantic, dataclass, TypedDict or OpenAPI) that now accepts more input (see
  below);
- a removed contract: a rule may name it, and a contract no rule names (the pydantic models
  `inv init` pins) is itself the pin of its constraints;
- an added scan exclude or a removed scan root;
- a pin for a feature this release removed (see
  [Upgrading](#upgrading-from-a-release-with-rules-below-the-api)).

**Constraints the code already has.** A `pydantic` contract (or a pydantic model in a target's
signature) is pinned as its JSON Schema, so its constraints are pinned too; so is a dataclass or
TypedDict contract, and the schemas of an OpenAPI contract's operations.
`lockfile.constraint_weakening` marks a change **WEAKENED** when the schema may now accept more
input. That covers:

- `maximum`, `exclusiveMaximum`, `maxLength`, `maxItems` or `maxProperties` raised or removed;
- the matching minimums lowered or removed;
- an `enum` value added, or the enum removed;
- a field that is no longer `required` (or an OpenAPI parameter or request body no longer
  `required: true`), or a property removed;
- `pattern`, `format`, `multipleOf`, `const` or `anyOf`/`oneOf` changed or removed, and a `type`
  widened or removed;
- a `$ref` removed (the value is now `{}`, Any) or pointed at a schema with a different body, and a
  `$defs` entry removed while the old schema referred to it;
- `additionalProperties` relaxed: `false` to anything else (absent, `true`, `{}` or a schema), a
  schema to absent or `true`, or absent to `true` (or to a schema), so extra properties now pass. A
  schema changed into another schema is judged inside it, like any other subschema;
- `items`, `prefixItems`, `contains`, `allOf`, `not`, `propertyNames`, `dependentRequired`,
  `dependentSchemas`, `if`, `then`, `else`, `properties` or `unevaluatedProperties` removed (adding
  one is a tightening; a constraint removed inside `items` is judged where it was), a
  `dependentRequired` entry dropped, and any change inside `not` or `if`, where a tighter
  subschema can let more input through;
- an OpenAPI operation removed while a rule names it (`subject.operation`). One that no rule names
  is reported as a change. An operation renamed with the same method and path (FastAPI derives the
  operationId from the function name) is reported as renamed and compared field by field with the
  old one, so a rename cannot hide a looser body.

Adding a constraint or tightening one is not a weakening. These count as tightenings, judged with
the neighbouring keywords:

- a bound replaced by its exclusive or inclusive twin at least as tight: `le=10` to `lt=10`, and
  on an integer `ge=1` to `gt=0` or `le=9` to `lt=10` (for an integer `gt=k` is `ge=k+1`, when
  both bounds are integral: `ge=1.5` to `gt=0.6` lets 1 through and is **WEAKENED**);
- an `enum` replaced by a `const` that was one of its values (`Literal["a", "b"]` to `Literal["a"]`);
- a `type` narrowed: `number` to `integer`, or a type dropped from a list;
- a `pattern` given `^` or `$` anchors it did not have, with the rest unchanged (a pattern with a
  top-level `|` is not judged this way). Any other pattern change is still a possible weakening.

Other schema changes, such as a default value or a new optional field, are reported as pin changes.
A `$ref` pointed at a renamed but identical schema is a change only.

**Comparing with a base branch.** `inv lock --check` compares with the lock in the working tree, which
the change itself may have re-pinned with `--accept-weakening`. `inv check --base REF` compares the
rule file with the lock committed at `REF` instead, and fails on any weakening since then. It passes
only when `--allow-weakening` is given or `INV_ALLOW_WEAKENING=1` is set, which a reviewer sets (for
example from a pull-request label), not the change's author. It fails closed: a `REF` that does not
exist, a checkout that is not a git repository, and a lock at `REF` that cannot be read or compared
(damaged, or a lock version other than this invariants writes) all fail. A reviewer may accept the
last kind with the same switch; a missing ref cannot be accepted. The lock is looked up at its own
path in the repository: a lock that is a symlink (or sits under a symlinked directory) fails and
cannot be accepted, and when `REF` has an `invariants.lock` that the change moved or removed, the
check fails too (a reviewer may accept that). Only a `REF` with no `invariants.lock` at that path,
and none the change moved away, passes, reported as a first adoption, since the whole lock is then
new and reviewed in that change.

Any change to a field that changes what `bad` reads, or how a check runs, is a possible
weakening: `bind`, an enforcer's `view`, `artifact`, `conformance_test` or `generated`, a
provider's `args`, `from` or `type`, `subject` (contract, operation, target), `scope`, and `model_policy`'s `schema`, `feedback` and `abstain`. That includes an
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
| possible weakening | `bind`; `subject` (`contract`, `operation`, `target`); `scope`; enforcer `view`, `artifact`, `conformance_test`, `generated`; provider `type`, `from`, `args`; `model_policy.schema`, `feedback`, `abstain`; contract `type`, `ref`, `app` | any change. These change what `bad` reads or how a check runs. This covers an `artifact` digest rewritten by `--update-artifacts`. |
| ranked | `status` | it moves down `enforce > detect > candidate/contested > retired` |
| ranked | enforcer `mode` | it moves down `enforce > detect > off` |
| ranked | enforcer `on_violation` | it moves from `reject`/`halt` to `escalate`/`route` |
| ranked | enforcer `on_error` | it becomes `fail_open_and_page` |
| ranked | enforcer `timing` | it becomes `post_effect` |
| ranked | target `effects` | it becomes `none` |
| ranked | `compensable` | it becomes `true` |
| more is weaker | `model_policy.format_retries`, `semantic_resamples` | it goes up |
| presence | `compares_to` | it is removed |
| analysed | `bad` (`when`) | it now matches fewer cases, or the direction cannot be decided |
| analysed | enforcer `point`, `target` (identity), a `bad` clause, a rule (`id`), a contract | it is removed |
| analysed | `scan` (`roots`, `exclude`) | an exclude is added or a root removed |
| analysed | `mediation.sink` | a sink is removed |
| analysed | `mediation.door` | a door is added to a rule that already had a sink |
| analysed | `mediation.allow` | an entry is added |
| analysed | rule `targets`, `applies_to` | a target is no longer covered |
| analysed | `mediation.residual_paths` | an entry is removed (it can raise the rule's standing to `held`) |
| neutral | `rev`, `spec`, clause `message`, `falsified_by` | never (recorded as a change) |
| prose | `statement`, `why`, `owner`, `class`, `mediation.chokepoint` | never (digest only) |
| pin | signatures, `types`, schemas, OpenAPI operations, file digests | never (a code change, shown old -> new), except a pinned schema constraint that now accepts more input (above) |
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
  effects, signature text, provider fields and contract references, so the weakening
  gate holds across the migration. Types and schemas are re-pinned in the version-3 form.
- **Version 1** (whole-module hashes). Each rule is compared against its hash. A rule that changed
  since that pin (version 1 cannot show how) or went away needs `--accept-weakening`.

A lock with a newer version is refused. So is a malformed lock: a `lock_version` that is not an
integer (`true` included), or a section or entry of the wrong shape, is a `LockError` that names
the file and the place.

## Upgrading from a release with rules below the API

This release removed the features that reached below the API: provider `consistency`, target
`transaction`, `effects: transactional`, `mediation.tables` (and the `tables:` short form), storage
enforcers and `{type: sql}` contracts. First remove them from the rule file: each one is a load
error that says what to do. Then run `inv lock`. A lock taken before this release shows each
removed pin once as a **WEAKENED** change, with the note "removed in this version: it reached
below the API (see the CHANGELOG); accept it once with `inv lock --accept-weakening`". Review the
list, and have a person run `inv lock --accept-weakening` once.

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
- A behaviour change inside a target's body, or in a provider's body, does not change the lock. The
  guards, the tests and `inv scan` check that.
