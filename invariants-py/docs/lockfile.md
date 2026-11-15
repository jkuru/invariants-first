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
- [Lock versions](#lock-versions)
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
| `rules` | `rev`, `status`, the expanded targets, the rule's semantic content (`bad`, `enforcers` with `mode`, `timing`, `on_violation`, `on_error`, `view` and `artifact`, `bind`, `scope`, `subject`, `mediation` sink, door, allow, allow_unresolved and residual paths, `model_policy` with its defaults filled in, `compares_to`, `falsified_by`) and a digest per prose field (`statement`, `why`, `owner`, `class`) and of `mediation.chokepoint` (not prose: it decides `held`). |
| `targets` | The signature as text (unwrapped through decorators) and as resolved `types`, the JSON Schema of each parameter with an `Annotated` constraint (`param_schemas`), `effects`, and a model-call target's reply schema. |
| `providers` | `type`, `from`, `args`, `concurrent` (when set), and the function's signature, resolved types and `param_schemas`. |
| `contracts` | A pydantic model's JSON Schema; a dataclass or TypedDict as a JSON Schema too; a `{field: type}` schema; an Enum's values; an `openapi` file, or a live app's generated document (`app: module:app`), as the parameters, request body and response schemas of its operations (a file no rule names an operation of: its digest and every operation); a `python` module by reference only (its targets carry the pins); any other file by digest. |
| `schemas` | Every schema the entries above use, keyed by the sha256 of its canonical JSON. A model used by a target and a contract, or a shared OpenAPI component (a 422 error body), is stored once. |
| `scan` | When a rule has a sink: the source `roots` (relative to the rule file) and `exclude` globs `inv check` scans with, from `[tool.invariants.scan]`, the defaults (src/, setuptools `packages.find.where`) and any `--root`/`--exclude`, and the directories the scan always `skip`s (`build`, `dist`, `venv`, dot-directories, ...). With no `exclude` set, the pinned excludes are the 16 default globs: `tests`, `test`, `testing`, `test_*.py`, `*_test.py`, `conftest.py`, `migrations` and `alembic`, each also as `*/<name>`. |
| `spec`, `lock_version` | The rule file's `spec` (`invariant/v0.1`) and the lock version (3). |

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
    `mediation.chokepoint`. The first four are prose: an edit to one is reported but never makes
    the lock stale. `mediation.chokepoint` is not prose (`inv status` reads it to decide whether a
    rule is `held`), so an edit to it is stale and judged.
- `targets.<key>`: `signature` (text), `types`, `param_schemas` (when a parameter has an
  `Annotated` constraint), `effects` and, for a model-call target, `reply_schema`.
- `providers.<name>`: `type`, `from`, `args`, `concurrent` when the record sets it, and the
  `from` function's `signature`, `types` and `param_schemas`.
- `contracts.<name>`: `{type, ref}` (or `{type, app}`) plus, by type:
  - `schema` for a pydantic model (`{name, pydantic: <model_json_schema()>}`), a dataclass or
    TypedDict (`{name, jsonschema: ...}`: pydantic's `TypeAdapter(cls).json_schema()` when pydantic is
    installed, else a JSON Schema built from the resolved annotations, with the bounds an
    `Annotated` metadata object carries as `gt`/`ge`/`lt`/`le`/`min_length`/`max_length`), or a
    `{field: type}` mapping, or an Enum (`{name, type: {enum, values}}`);
  - `signature` and `types` for a function;
  - `operations` for an `openapi` file: each operation a rule names through `subject.operation`,
    as its method, path, `parameters`, `requestBody` and `responses` schemas (a file no rule names
    an operation of is pinned by digest, and every operation with an `operationId` is pinned
    beside it, so a looser schema in any of them is judged; a file that is not JSON or YAML, or
    has an unresolvable `$ref`, is pinned by digest alone). An OpenAPI 3.0 or Swagger 2.0
    document also pins `dialect: openapi-3.0` (`swagger-2.0`), on the contract and on each of its
    component schemas, so its schemas are judged with Draft 4's `exclusiveMaximum`. A Swagger
    2.0 parameter other than `in: body` carries its schema fields on itself (`type`, `format`,
    `maximum`, `enum`, `items`, ...): they are pinned as the `schema` they make (its
    `collectionFormat` beside them), and judged like any other. A Swagger 2.0 response carries
    its `schema` itself (not under `content`): it is pinned in the shape of an OpenAPI 3 response,
    under each media type the operation (or the document) `produces`, `application/json` when
    neither says, and judged like one;
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
- `scan`: `{roots, exclude, skip}`, present when a rule declares a sink. `inv check` builds the
  same pin from the roots and excludes it is about to scan with, so a `[tool.invariants.scan]` or
  `[tool.setuptools.packages.find] where` edit, a new `src/`, or an `inv check --root/--exclude`
  shows up as a lock change. **Any change to the set of roots is WEAKENED**, added as well as
  removed: the scan names a file from the deepest root that holds it and matches excludes against
  root-relative paths, so a new root renames the files under it (a relative import, a sink or door
  name may stop resolving, hiding a path) and makes an existing exclude glob match more files. The
  order of the roots does not matter and is not pinned. An added exclude is **WEAKENED**; a removed
  one is a change only. `skip` is the scan's built-in list of directories it never reads (each name
  in `invariants.scan.SKIP_DIRS`, and `.*` for dot-directories), pinned so that a library upgrade
  or change that skips more is **WEAKENED** (an entry added) rather than silent. A lock taken before the scan was pinned is compared with what that configuration scanned by
  default: `inv lock` reads `[tool.invariants.scan]` (else src/ and the rule file's directory) from
  the working tree, and `inv check --base REF` reads them from `REF`. So the first pin is not a way
  to slip in an exclude or narrow the roots in the same change. To scan with an extra `--root` or `--exclude`, pin it with
  `inv lock --root DIR --exclude GLOB`; prefer `[tool.invariants.scan]` in `pyproject.toml`.

`types` maps each annotated parameter, and `return`, to its resolved type (`typing.get_type_hints`).
Aliases (`Caps = list[int]`, `type Caps = ...`), `Literal`, unions, `Annotated` and `NewType` are
expanded. A dataclass, TypedDict or pydantic model is pinned by its fields in the schema table,
recursively: nested classes go into the entry's `$defs`, cycles are cut by name, and nesting stops
at 64 nested classes or 24 levels (marked `truncated`). Pinned schemas have no prose: `title`,
`description`, `summary`, `tags`, `examples`, `example`, `externalDocs` and `$comment` are stripped
at every level. A property
named `title` is kept, because it is a name, not a keyword.

`types` drop `Annotated` metadata, so a parameter whose annotation carries a constraint
(`Annotated[int, Field(gt=0)]`, `Annotated[str, MaxLen(10)]`, an `annotated_types` object, also
inside `Optional[...]` or `list[...]`) is pinned again under `param_schemas` as the annotation's
JSON Schema (pydantic's `TypeAdapter` when installed, else the stdlib reading of `gt`/`ge`/`lt`/`le`/
`min_length`/`max_length`/`pattern`/`multiple_of`). Other markers, such as FastAPI's `Depends(...)`,
are not constraints; they stay in the signature text.

`inv lock --check` exits 1 when anything that bears on a rule's meaning differs. Two kinds of change
are recorded in the lock but are informational: an edit to a prose field (`statement`, `why`,
`owner`, `class`), and a signature spelled differently with the same meaning. They are listed,
marked `(not stale)`, and `--check` (and `inv check`) passes; the next `inv lock` refreshes them. A
signature counts as the same when it has the same name and, in order, the same parameters with
the same kinds (positional-only, keyword-only, `*args`, `**kwargs`), defaults and annotations
(compared as written, ignoring quotes and spacing), and the entry pins resolved `types` on both sides
(which are compared on their own): `account: 'str'` for `account: str`, or re-wrapped lines. A renamed
or reordered parameter, a new or changed default, a parameter made keyword-only, and an annotation
added, removed or renamed are stale. A renamed annotation counts even when the resolved type is the
same, because an alias can stand for an `Annotated[...]` whose metadata (`Depends(get_admin)`, a
validator) no pin carries: swapping `AdminUser` for `AnyUser` is stale. Signature text that does not parse as Python (a repr such as `<object ...>`) is compared as
text. A resolved annotation that is an object of no recognised form (`x: SomeMarker()`) is pinned in
`types` by its type only, `{"object": "module.SomeMarker"}`, never by its repr, which can carry a
memory address (a different lock every run) or data the object holds; a typing special form
(`typing.Never`) and a `Callable`'s parameter list of types pin as their names. It lists each change as
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
rewrites it. A damaged entry is left out of the old side, so when the schema it held has changed
too, the change cannot be shown or judged: it is WEAKENED, and `inv lock` writes it only with
`--accept-weakening`.

## Weakening

These changes are marked **WEAKENED**:
- a lower status or enforcer mode, or a removed enforcer, clause or rule;
- an `on_violation` below reject, or `on_error: fail_open_and_page`;
- `timing` moved to `post_effect`;
- a removed sink;
- a new door on a scanned sink, or a new `allow` entry (a back door is now let through);
- with a sink mapping (`sink: {sink: [doors]}`) on either side, judged sink by sink for the sinks on
  both sides: a door that may now reach a sink it could not before, whether added to that sink's
  list or because the list form replaced the mapping (there every door reaches every sink); a door
  taken off a sink's list, or a move from the list form to a mapping, is a tightening, and the same
  doors per sink written another way (the other form, a reordered mapping, `door:` beside it) is a
  change that is not a weakening;
- with a sink mapping, `allow` (a map from each back door to the sinks it may reach without a door)
  judged entry by entry: a new entry, or a sink added to an entry, lets a back door reach a sink it
  could not before (`mediation.allow py:app.admin.fix: [...] -> [...]  WEAKENED`); removing a sink
  from an entry, or the entry, is a tightening. A plain list pinned before the map form counts as
  each entry reaching every sink;
- a `mediation.allow_unresolved` site (file, code, sha) named by more entries than before, compared
  as a multiset: a new site, or a second entry for a sha one entry named (each entry accepts one
  finding, and identical statements in one function share a sha), or more entries for it without a
  column (they match any column of the line); fewer entries for a site is a tightening, and a new
  line or `why` is neither. An entry of the file's top-level `allow_unresolved` (shared by the rules
  its `rules` lists) is pinned in each of those rules' `mediation.allow_unresolved` and judged there
  the same way: a new one, or a rule added to its `rules`, is a weakening for that rule; a rule taken
  out is a tightening; the same site moved between a rule's own list and the shared one is neither;
- a target declared `effects: none`, or a target no longer covered;
- `compares_to` removed, `compensable` set to true, or more `format_retries` or
  `semantic_resamples`;
- `mediation.strict` turned off or removed (turning it on is a tightening);
- a removed `mediation.residual_paths` entry (while one is listed `inv status` withholds `held`,
  so removing it can raise the rule's standing);
- a `bad` that now matches fewer cases;
- a pinned schema (pydantic, dataclass, TypedDict or OpenAPI) that now accepts more input (see
  below); also a `{field: type}` contract, an Enum contract, and a dataclass or TypedDict a
  signature names, judged as the JSON Schema the pin describes (a field type widened, `int` to
  `Any` or to `int | None`, a field removed, a `Literal` or Enum member added, `tuple[int]` (exactly
  one item) to `tuple[int, ...]`, a dict's key type widened, an opaque Python type such as `Decimal`
  changed to `str` or removed, a constraint loosened on a pydantic model a dataclass names inside
  `$defs`); and a changed pinned schema whose old entry the stored lock does not hold (a hand-edited
  table entry), since the change cannot be judged. Changing a generic's arguments, or `str` to
  `Decimal`, is flagged too: whether that narrows or widens cannot be told, so it is treated as a
  weakening;
- a whole schema removed: a pydantic schema taken off an entry, a contract's `schema` ref, an OpenAPI
  parameter's `schema`, a `requestBody` (or its content or a media type), a response, or a target's
  `reply_schema` (removing a schema that accepted anything is not a weakening, and adding one never
  is);
- a bound loosened across dialects: when either side is Draft 4, each side's bounds are read in its
  own dialect, so 2020-12 `exclusiveMaximum: 10` changed to Draft 4 `exclusiveMaximum: true` with no
  `maximum` is WEAKENED;
- a parameter constraint (`param_schemas`) removed or loosened: `Annotated[int, Field(gt=0)]` to
  `int`, `lt=10` to `lt=100`, `MaxLen(3)` to `MaxLen(9)` (adding or tightening one is not);
- a new or edited `mediation.chokepoint` (it can let the rule be `held`; removing it is a change
  only);
- a removed contract: a rule may name it, and a contract no rule names (the pydantic models
  `inv init` pins) is itself the pin of its constraints;
- an added scan exclude, an added always-skipped directory, or any added or removed scan root.

**Constraints the code already has.** A `pydantic` contract (or a pydantic model in a target's
signature) is pinned as its JSON Schema, so its constraints are pinned too; so is a dataclass or
TypedDict contract, and the schemas of an OpenAPI contract's operations.
`invariants.lock.constraint_weakening` (from the invariants-schema-diff library,
`invariants_schema_diff.schemas`) marks a change **WEAKENED** when the schema may now accept more
input. That covers:

- `maximum`, `exclusiveMaximum`, `maxLength`, `maxItems`, `maxProperties` or `maxContains` raised
  or removed;
- the matching minimums lowered or removed (`minContains` defaults to 1, so adding `minContains: 0`
  is a lowering);
- a Draft 4 / OpenAPI 3.0 boolean `exclusiveMaximum` or `exclusiveMinimum` turned off (`true` to
  `false` or absent): the bound itself now passes. In Draft 4 and OpenAPI 3.0 (and Swagger 2.0)
  `exclusiveMaximum` is only a flag on `maximum`, and a number there bounds nothing, so `maximum: 10`
  to `exclusiveMaximum: 10` is **WEAKENED** there, while in JSON Schema 2020-12 (OpenAPI 3.1,
  pydantic) it is a tightening. The dialect comes from the schema's `$schema`, a boolean
  `exclusiveMaximum`/`exclusiveMinimum` in it, or the OpenAPI version: the pin of an OpenAPI 3.0 or
  Swagger 2.0 document records `dialect: openapi-3.0` (or `swagger-2.0`) on the contract and its
  component schemas (an OpenAPI 3.1 pin records none). A changed `$schema` is **WEAKENED**;
- an `enum` value added, or the enum removed. `const` and `enum` are compared as values, whole:
  below them nothing is a schema, so `const: {"a": 1}` to `{"a": 2}` is a new value, not a
  changed subschema, and `const: null` removed lets any value through;
- a field that is no longer `required` (or an OpenAPI parameter or request body no longer
  `required: true`), or a property removed. An operation's parameters are matched by `in` and
  `name`, not by position (`operations.op.parameters[query limit].schema.maximum`): a parameter
  removed is **WEAKENED**, a required one because a request without it now passes and an optional
  one because the value a request sends is no longer checked, while an added parameter (required
  or not) only tightens; `allowEmptyValue` turned on is **WEAKENED**; the `required` list is compared as a set, so a field
  added to it is a tightening, and a reordered list (pydantic lists required fields in declaration
  order, so a field moved in the class reorders it) or a duplicate dropped is no change of meaning;
- `pattern`, `format`, `multipleOf`, `const` or `anyOf`/`oneOf` changed or removed, and a `type`
  widened or removed;
- OpenAPI 3.0 `nullable` (or Swagger's `x-nullable`) turned on: `false` to `true`, or `true` added;
- a `$ref` removed (the value is now `{}`, Any) or pointed at a schema with a different body, and a
  `$defs` entry removed while the old schema referred to it. References are resolved the way a
  validator does (`$id`, `$anchor`, `$dynamicAnchor`, Draft 7's `"$id": "#name"`, JSON pointers), so
  an `$anchor` or `$id` edit that makes an unchanged `$ref` name another schema is judged by the
  old and new targets' bodies: **WEAKENED** unless they are the same. In Draft 7 and earlier (and
  OpenAPI 3.0) a `$ref` added beside other keywords is **WEAKENED**: they are ignored next to it.
  The new side's own dialect decides this: an OpenAPI 3.0 contract moved to 3.1 (JSON Schema
  2020-12, where they still apply) that adds a `$ref` beside them is a tightening;
- `additionalProperties` relaxed: `false` to anything else (absent, `true`, `{}` or a schema), a
  schema to absent or `true`, or, on a model (the schema's root, a `$defs` entry, or an object with
  `properties`, where absent means extras are ignored), absent to `true` (or to a schema), so extra
  properties now pass. On a map (an object schema with no `properties`: `dict`, or `dict[str, Any]`
  read without pydantic, or a free-form OpenAPI object) absent already let any value pass, so
  `dict[str, Any]` to `dict[str, int]` is a tightening. A schema changed into another schema is
  judged inside it, like any other subschema;
- `items`, `prefixItems`, `contains`, `allOf`, `not`, `propertyNames`, `dependentRequired`,
  `dependentSchemas`, Draft 7's `dependencies`, `if`, `then`, `else`, `properties` or
  `unevaluatedProperties` removed (adding one is a tightening, except under `unevaluatedProperties`
  or `unevaluatedItems`: see below; a constraint removed inside `items` is judged where it was), a
  `dependentRequired` or `dependencies` entry dropped (or a name dropped from it), a
  `patternProperties` entry removed;
- a subschema replaced by `true` (or `{}`), or `false` replaced by a schema: an `allOf`, `anyOf`,
  `oneOf` or `prefixItems` entry, a property, a pattern, a `$defs` entry, `items`, `not`, ... A
  subschema replaced by `false` is a tightening;
- a change where a tighter subschema can let more input through. Each place in a schema has a
  polarity: under `not` it is reversed, so there a looser subschema is the weakening and a tighter
  one is not (`not: {maximum: 5}` to `not: {maximum: 3}` lets 4 through); under `oneOf`, `if` and a
  `contains` with `maxContains` both directions can let more through (a value that failed a `oneOf`
  because it matched two branches passes once one of them is tightened), so any change there that
  is not a no-op is **WEAKENED**. A `$ref` passes its place's polarity to the schema it names, so a
  `$defs` entry used under `not` is judged the other way round, one used both ways is judged both
  ways, and the same holds for a shared component of the schema table (an OpenAPI
  `components/schemas` entry named under `not` in an operation);
- a property added while `additionalProperties` (or `unevaluatedProperties`) was `false` or a
  schema, so an input carrying it, rejected before, now passes: an optional field added to a
  pydantic model with `extra="forbid"` is **WEAKENED**. It is not when the new property is also
  `required` and its schema constrains its value (no old input had it, so none passes now), or when
  extra properties were allowed anyway;
- `patternProperties` added, or a pattern added to it, while `additionalProperties` (or
  `unevaluatedProperties`) was `false` or a schema: a property matching the pattern is no longer
  checked by it. With extra properties allowed anyway (absent or `true`) it is a tightening;
- an applicator (`allOf`, `anyOf`, `oneOf`, `if`, `then`, `else`, `dependentSchemas` or an entry
  of it, `$ref`, `$dynamicRef`, `contains`, `prefixItems`, `items`, `properties` or a property,
  `patternProperties` or a pattern, `additionalProperties`, or a nested `unevaluatedProperties` /
  `unevaluatedItems`) added or changed while `unevaluatedProperties` or `unevaluatedItems` is
  `false` or a schema on that schema or on one it is an in-place subschema of (inside `allOf`,
  `anyOf`, `oneOf`, `not`, `if`, `then`, `else` or `dependentSchemas`; inside `$defs`, which a `$ref`
  reaches from anywhere, such a keyword anywhere in the schema counts). JSON Schema 2020-12 counts
  what the applicator evaluates as evaluated, so it is no longer refused or checked there:
  `{properties: {a: …}, unevaluatedProperties: false}` plus `allOf: [{properties: {b: {}}}]` accepts
  `{"a": 1, "b": "x"}`. A `false` subschema evaluates nothing, and a property that is also `required`
  with a constrained value is judged as under `additionalProperties: false`. The keyword of a
  property's own value (`properties.x.allOf`) is judged by that value's schema, not its parent's;
- `prefixItems` extended past its old length while the old schema had an `items` schema: those
  positions were checked by `items` and are now checked only by the new entries (extending it with
  entries equal to `items`, or with no `items`, is a tightening);
- an OpenAPI operation removed while a rule names it (`subject.operation`). One that no rule names
  is reported as a change. An operation renamed with the same method and path (FastAPI derives the
  operationId from the function name) is reported as renamed and compared field by field with the
  old one, so a rename cannot hide a looser body.

Adding a constraint or tightening one is not a weakening (under `not`, loosening one is not). These
count as tightenings, judged with the neighbouring keywords:

- a bound replaced by its exclusive or inclusive twin at least as tight: `le=10` to `lt=10`, and
  on an integer `ge=1` to `gt=0` or `le=9` to `lt=10` (for an integer `gt=k` is `ge=k+1`, when
  both bounds are integral: `ge=1.5` to `gt=0.6` lets 1 through and is **WEAKENED**);
- an `enum` replaced by a `const` that was one of its values (`Literal["a", "b"]` to `Literal["a"]`),
  or values dropped from it; an `anyOf` with branches dropped;
- a `type` narrowed: `number` to `integer`, or a type dropped from a list;
- a `pattern` given `^` or `$` anchors it did not have, with the rest unchanged (a pattern with a
  top-level `|` is not judged this way). Any other pattern change is still a possible weakening.

Other schema changes, such as a default value or a new optional field, are reported as pin changes.
A `$ref` pointed at a renamed but identical schema is a change only.

**Comparing with a base branch.** `inv lock --check` compares with the lock in the working tree, which
the change itself may have re-pinned with `--accept-weakening`. `inv check --base REF` compares the
rule file with the lock committed at `REF` instead, and fails on any weakening since then. It passes
only when `--accept-weakening` is given (`--allow-weakening` is the same switch) or `INV_ALLOW_WEAKENING=1` is set, which a reviewer sets (for
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
is classified in `invariants.lock.FIELD_POLICY`, and a field that is not classified is a possible
weakening. A test walks the schema, so a new field cannot ship unclassified.

## How each field is judged

`invariants.lock.FIELD_POLICY` (defined in `invariants.lock.contracts`) classifies every field of the rule-file schema, and a test
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
| analysed | `scan` (`roots`, `exclude`, `skip`) | a root is added or removed, an exclude or a skipped directory is added |
| analysed | `mediation.sink` | a sink is removed |
| analysed | `mediation.door` | a door is added to a rule that already had a sink |
| analysed | `mediation.allow` | an entry is added |
| analysed | `mediation.allow_unresolved` | a site (file, code, sha) is named by more entries than before, or by more entries without a column (a multiset: a duplicate entry counts) |
| analysed | rule `targets`, `applies_to` | a target is no longer covered |
| analysed | `mediation.residual_paths` | an entry is removed (it can raise the rule's standing to `held`) |
| analysed | `mediation.chokepoint` (by digest) | it is added or edited (it can raise the rule's standing to `held`); removing it is a change |
| neutral | `rev`, `spec`, clause `message`, `falsified_by`, provider `concurrent` | never (recorded as a change) |
| prose | `statement`, `why`, `owner`, `class` | never, and never stale (digest only; an edit is reported for information) |
| pin | signatures, `types`, `param_schemas`, schemas, OpenAPI operations, file digests | never (a code change, shown old -> new), except a pinned schema or parameter constraint that now accepts more input (above). A signature spelled differently with the same meaning is informational, not stale |
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

## Lock versions

This invariants reads and writes lock version 3 only. `inv lock` and `inv lock --check` refuse a
lock of an older version (delete it and re-run `inv lock`) and of a newer one (upgrade invariants).
A malformed lock is refused too: a `lock_version` that is not an integer (`true` included), or a
section or entry of the wrong shape, is a `LockError` that names the file and the place. Under
`inv mcp` (`INV_CONFINE_ROOT` set), a lock file that resolves outside the server's `--root` (a
symlinked `invariants.lock`) is a `LockError` too, raised before it is opened.

## Where the CLI imports from

`validate`, `lock`, `verify` and `status` import the app from this checkout, ahead of
site-packages: every `--path DIR`, then the project's `src/`, its setuptools `packages.find`
`where` and `[tool.invariants.scan]` roots, the rule file's directory, and the project root (the
nearest directory holding a `pyproject.toml`). So the lock pins the source beside the rule file,
not an installed copy or another checkout that shares the venv. When a pinned module is still
imported from outside the project (it is not under any of those roots), `inv lock` and
`inv verify` print a warning naming the file it came from; pass `--path` to point at the source.

## What the lock trusts

`inv validate`, `inv lock`, `inv check` and `inv init` import the app in a child process (one per
command, `python -I` with the same import path). The process that compares the pins, decides
WEAKENED, reads `INV_ALLOW_WEAKENING` and writes the lock never imports the project, so a module
cannot change the verdict by what it does at import time: setting `INV_ALLOW_WEAKENING`, replacing
`invariants.lock.changes`, or faking the whole lock.

The guards step is decided there too, from the source. The child runs `inv verify`, but code it
imports runs in the same process and could replace that check (`invariants.cli.child.cmd_verify`,
say) with one that prints "pass". So the deciding process also reads, without importing anything,
each rule's target and edge targets: it finds the module on the child's import path (the `--path`
entries, the checkout's source roots, the rule file's directory, then `sys.path` and `--site`), parses
the file, walks to the `def` the target names (through classes and `from M import name`
re-exports), and checks its decorators: `@inv.guard` naming the rule's id or the target key, with the
enforcers' points (`decorator`, `middleware`) among the literal `points=` (or the FastAPI guard's
two); `@inv.model_call` for a rule with `model_policy`; `@inv.edge` on each edge target. Any import
form of `invariants`, `invariants.guard`, `invariants.integrations.fastapi` or
`invariants.integrations.pydantic` counts. The guards step passes only when this check and the
child's `inv verify` both pass. It fails closed: a target with no `.py` source, a name bound more
than once or inside an `if`/`try` block, a decorator whose rule ids are not literal strings, a
decorator made some other way (`g = inv.guard("R-1")` then `@g`), or an import alias the module
rebinds is a FAIL that says so; write the decorator on the `def` with literal ids. The child, for
defence in depth, refuses to report when the code it imported replaced the check it runs or
invariants' own decorators (`invariants.guard`, `model_call`, `edge`).

What the source check does not decide, and so still trusts the imported code for: a guard naming a
rule the file does not have, a guard on a targetless rule (found only in what is imported), a
`model_call`'s schema, the bindings of a guard, FastAPI routes (`install`), and the object the
target resolves to at run time (code that rebinds it with `setattr` or through `sys.modules`, which
the source of its own module does not show, or a module that patches invariants' internals in a way
the child's identity check does not cover). Those come from the child's `inv verify`, as the pins
of signatures and schemas do. That process also reads everything the verdict
depends on before the child runs (the rule file, the stored lock, the lock at `--base`, the scan of
the sources), so a module that rewrites a file at import does not change what is compared:
`--base` compares with the scan settings read then too, not a second read of `pyproject.toml`, and
the child is refused any scan settings that were not read before it ran. It takes
from the child only what importing alone can tell: the targets' and providers' signatures, the
schemas of pydantic, dataclass and TypedDict contracts, a live app's OpenAPI document, and the
clause types read from those signatures. The rules, the scan settings, declared effects and
provider fields, and contracts read from files are pinned in the deciding process itself.

So the lock trusts the code it imports to describe itself. A module that lies about its own
signature or schema at import (a `__signature__`, a `model_json_schema` override, an `openapi()`
that returns something else) gets that pinned, and a module can do anything the CI user can (write
files, change the installed tools for a later step). The same goes for any of the pull request's
code that runs in the job before `inv check`: installing the project (`pip install -e .`) runs its
build backend, `setup.py` or a console script it declares, and that code can replace the `inv` a
later step runs or the files of its environment. The GitHub Action runs invariants from a venv of
its own, set up before anything is installed, as `python -I -m invariants.cli` with the
dependencies' site-packages passed as `--site` (their `.pth` files are not run), and `python -m
invariants.cli` drops the working directory from `sys.path`, so a project's own `yaml.py` or
`tomllib.py` is not imported by the deciding process; but no step order makes a job safe once the
pull request's build code has run in it (start.md, "Make the check unskippable": install
dependencies from wheels, and let `inv` import the project from the checkout). Treat the guarded,
contract and model modules as part of the rules: give them, the rule file, the lock, the files that
build the project (`pyproject.toml`, `setup.py`, `setup.cfg`, an in-tree build backend) and the CI
workflow a code owner. `inv verify` and `inv status` import the app in their own process and decide
no weakening.

The full synopsis is `inv lock [file] [--check] [--accept-weakening] [--lock FILE]
[--update-artifacts] [--root DIR]... [--exclude GLOB]... [--path DIR]... [--site DIR]...`. `--allow-weakening` is
another spelling of `--accept-weakening`.

## Limitations

- Signature text depends on how `inspect.signature` renders a signature, and a pydantic pin depends
  on `model_json_schema()`. Annotation spelling alone is not stale (see `--check` above), but an
  `Annotated[...]` repr that a pydantic upgrade renders differently is, and a pydantic upgrade that
  renders schemas differently makes the lock stale (not weakened).
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
