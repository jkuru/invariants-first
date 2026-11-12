# The mediation scan

A guard only protects the paths that go through it. `inv scan` is a static, read-only call-graph
check that every path to a protected effect passes its door. The source is parsed with `ast`; it is
never imported or run.

- [Mediation](#mediation)
- [Findings](#findings)
- [Tables and raw SQL](#tables-and-raw-sql)
- [Reachability: what counts as door code](#reachability-what-counts-as-door-code)
- [Warnings that withhold `held`](#warnings-that-withhold-held)
- [Configuration and output](#configuration-and-output)
- [Limitations](#limitations)

## Mediation

Each rule declares, under `mediation`:

```yaml
mediation:
  chokepoint: pools.size is written only by resize_door    # the positive argument, in words
  sink:   "py:pkg.store.set_size"           # the protected effect: a string or a list
  door:   "py:pkg.service.resize"           # the chokepoint function(s); defaults to subject.target
  tables: [pools, {leases: [insert, update]}]  # raw SQL writes to these are forbidden outside sink and door
  residual_paths: []                        # known gaps, in words; while any is listed, the rule is not held
```

`inv scan` looks for any call path from an entrypoint to the sink that avoids every door. An
entrypoint is a function with no extracted caller, or a module body (and anything those cannot
reach). A rule on an `effects: none` target needs no sink: the guard wraps the function itself, and
the scan reports it as "not scanned (mediation declares no sink)".

```text
$ inv scan
bypass     PAY-1    ops.py:5:5  ops.goodwill_credit -> payments.send_to_gateway reaches the sink without passing payments.refund
PAY-1: fail (1 bypass)
scanned 3 file(s), 5 function(s)
```

## Findings

Findings have four kinds:

| Kind | Meaning | Codes |
|---|---|---|
| `bypass` | a path of extracted call edges from an entrypoint to the sink that avoids every door | `bypass` |
| `forbidden` | a bypass idiom | `forbidden/getattr-sink` (and `getattr` on a door), `forbidden/unwrap` (`.__wrapped__`), `forbidden/monkeypatch`, `forbidden/raw-sql` |
| `unresolved` | something the scan cannot follow; never counted as passed | `unresolved/unknown-receiver`, `unresolved/reflection`, `unresolved/computed-attribute`, `unresolved/computed-sql`, `unresolved/inferred-path`, `unresolved/sink-escapes`, `unresolved/parse-error` |
| `missing` | a declared sink or door that does not exist in the scanned code | |

A call such as `store.sink.__call__(c)` (or `getattr(store.sink, "__call__")(c)`) is a call of the
sink. A lookup in a scanned module's or class's namespace is a `getattr` on it:
`store.__dict__["sink"]`, `store.__dict__.get("sink")`, `store.__getattribute__("sink")` and
`object.__getattribute__(store, "sink")` are `forbidden/getattr-sink`, and any other use of
`store.__dict__` (a computed key, `.items()`, the dict as a value) or `inspect.getmembers(store)` is
`unresolved/reflection`, like `vars(store)`. A function's `__dict__` is its metadata and stays
harmless. When a method call cannot be resolved, the scan reports it as unresolved, never as a bypass.
The exit code is 1 on any bypass, forbidden or missing finding, and 3 when nothing failed but some
calls are unresolved (not a pass). It is 4 when the scan skipped an `enforce` rule because it is
ablated (`INV_ABLATE`), which is not a pass either.

## Tables and raw SQL

`mediation.tables` items are table names, which forbid every raw SQL write (INSERT, UPDATE,
DELETE and DDL) outside the sink and door, or one-key mappings that forbid only some statement
kinds: `tables: [pools, {leases: [insert, update]}]` lets a release `DELETE FROM leases` through
and still flags an `INSERT INTO leases` in a new endpoint. The scan reads concatenated literals,
schema-qualified and quoted names (`main.leases`, `"leases"`), SQL comments between keywords,
`MERGE INTO` (insert, update and delete), `INSERT` without `INTO`, `UPDATE t AS x SET`,
`UPDATE ONLY`, `DELETE FROM ONLY` and `ALTER TABLE IF EXISTS`. A write to a computed table name
(`f"INSERT INTO {t}"`, `"... {table}".format(...)`, `{0}`, `%s`, `%(t)s`) is `unresolved`.

## Reachability: what counts as door code

What counts as door code is decided by the call graph, not by where the text is. SQL in a helper
that only the door or sink reaches (it is not reachable from any entrypoint without them, and it is
never used as a value) is door code, and so is a module-level constant (`INSERT_LEASE = "..."`)
that only door or sink code uses. The same constant used anywhere else, or never used in the
scanned code, is flagged where it is defined.

## Warnings that withhold `held`

Two cases make `inv scan` print a warning, and then `inv status` does not report the rule `held`:
- The sink's own SQL writes a table, or a statement kind, that `mediation.tables` does not cover.
  This applies whatever the sink's `effects` are, `external` included. A raw write to that table
  elsewhere would go around the door unseen. The JSON report lists `sink_writes` and
  `uncovered_tables` per rule.
- A rule guards stored data in process (a `transactional` door or sink, or a storage enforcer)
  and declares no `mediation.tables` at all.

## Configuration and output

```text
inv scan [file] [--root DIR]... [--exclude GLOB]... [--format text|json|sarif] [--output FILE]
inv status [file] [--root DIR]... [--exclude GLOB]... [--import MOD]... [--json] [--path DIR]...
```

With no `--root` and no configured roots, `inv scan` and `inv status` scan the rule file's directory
and, when a `pyproject.toml` sits beside the rule file, its source directories (`src/` and the
setuptools `packages.find.where` directories) as roots of their own, so a src-layout project is
scanned under the module names `inv lock` imports it by. A file is named from the deepest root that
holds it. Scan roots and excludes can also live in the `pyproject.toml` beside the rule file:

```toml
[tool.invariants.scan]
roots = ["src"]
exclude = ["*/legacy/*"]
```

`--format json` gives one record per rule (with `sink_writes` and `uncovered_tables`), and
`--format sarif` gives SARIF 2.1.0 for code-scanning tools. A rule ablated through `INV_ABLATE` is
not scanned, so a scan inside a falsifier is ablated too. A scan-only rule still needs a `bad` and a
`structure` enforcer.

## Limitations

- `inv scan` builds an approximate call graph. When a method call cannot be resolved, the scan
  reports it as unresolved, never as a bypass.
- It does not follow calls made through variables.
- Callers outside the scanned roots are invisible to it.
- Tables are matched per table and statement kind, not per column. `archive.leases` counts as
  `leases`, because the schema qualifier is ignored.
- SQL text in a log message is read like any other string.
- A class-level SQL constant (`self.SQL`) is not resolved to its uses, so it is judged where it is
  defined.
- Each finding is reported under every scanned rule it affects. A dropped trigger is attributed to
  the rule with `tables`.
