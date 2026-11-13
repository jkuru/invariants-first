# Public API

`import invariants as inv` exposes exactly the names below: the package's `__all__`, which a test
checks. The tables are generated from the installed package (the signatures and the first paragraph
of each docstring), so they match the code. The integrations' public surface is each module's
`__all__`.

- [`invariants`](#invariants)
- [Integrations](#integrations): [FastAPI](#invariantsintegrationsfastapi), [ASGI](#invariantsintegrationsasgi), [Pydantic](#invariantsintegrationspydantic), [OpenTelemetry](#invariantsintegrationsotel)
- [`invariants.testing`](#invariantstesting)
- [The pytest plugin](#the-pytest-plugin)
- [The CLI](#the-cli)

## `invariants`

Key behaviours that the one-line docstrings leave out:

- `@inv.guard(*keys, points=("decorator",))`: a key is a rule id or a `py:` target. It works on
  sync and async functions, on methods, and above or below `@classmethod` or `@staticmethod`. A
  call raises `LoadError` when a rule-id key is not loaded, or when a `py:` key matches no loaded
  rule and no `targets:` entry, so a guard never runs open because nothing was loaded.
- `@inv.model_call(id, schema=...)`: `schema` is a dataclass, a `{field: type}` map or a pydantic
  model. See [concepts.md](concepts.md#model-calls).
- `inv.verify(reg=None, modules=None)`: by default it checks the guards in the packages of the
  loaded targets.
- `inv.standing(id)` returns `(standing, reasons)`, where standing is `held`,
  `enforce(known-callers)`, `monitored`, `candidate` or `retired`.
- Registries are context-local. `inv.ablated(id, scope="registry")` turns a rule off for every
  thread and task that uses the registry; see [concepts.md](concepts.md#ablation).
- `inv.__version__` is read from the installed metadata.
- Outside `__all__`: `invariants.registry.ablated_everywhere(rule_id)` turns a rule off in every
  registry and every thread (the pytest plugin's `--inv-ablate` uses it, with `INV_ABLATE` for
  subprocesses).

### Decorators

| Name | Kind | Purpose |
|---|---|---|
| `guard(*keys: str, points: tuple[str, ...] = ('decorator',)) -> Callable[[Callable[P, R]], Callable[P, R]]` | function | @inv.guard("R-1", ...) or @inv.guard("py:jobs.split_work"): pre- and post-effect checks. |
| `provider(name: str) -> Callable[[F], F]` | function | @inv.provider("approval"): register a read-only provider; the loader checks name, args and return type. |
| `model_call(rule_id: str, schema: Any) -> Callable[[Callable[P, R]], Callable[P, R]]` | function | @inv.model_call("R-7", schema=CauseReply): parse -> abstain -> bad -> accept, with bounded retries. |
| `edge(rule_id: str) -> Callable[[Callable[P, R]], Callable[P, R]]` | function | @inv.edge("R-7") on the consumer of a Checked value (sync or async). |
| `Checked(*a: Any, **k: Any) -> None` | class | Base for refined types built by inv.model_call; carries value, attempts, rule and verdict. |
| `parse(raw: Any, schema: Any, *, max_size: int \| None = None) -> Any` | function | Parse a reply into a closed schema (dataclass, {field: type} or pydantic model); extra or missing fields fail. |

### Loading and coverage

| Name | Kind | Purpose |
|---|---|---|
| `load(source: str \| pathlib.Path \| dict[str, Any], install: bool = True, base_dir: str \| pathlib.Path \| None = None, backend: str \| cel.Backend \| None = None) -> Registry` | function | Load and validate a rule file. With install=True the current registry takes the rules. |
| `load_resource(package: str, name: str = 'invariants.yaml', **kw: Any) -> Registry` | function | inv.load a rule file shipped inside a package (importlib.resources); artifacts resolve beside it. |
| `read_document(source: str \| pathlib.Path) -> dict[str, Any]` | function | Read a rule file from disk: YAML for .yaml/.yml, JSON otherwise. |
| `schema_path() -> pathlib.Path` | function | Path to the bundled JSON Schema for rule files. |
| `verify(reg: Registry \| None = None, modules: Iterable[str] \| None = None) -> int` | function | `inv verify`: every target guarded, every guard names a rule. Returns the number of targeted rules. |
| `verify_coverage(reg: Registry \| None = None, modules: Iterable[str] \| None = None) -> int` | function | `inv verify`: every target guarded, every guard names a rule. Returns the number of targeted rules. |
| `standing(rule_id: str, reg: Registry \| None = None, scan: bool = False) -> tuple[str, list[str]]` | function | Computed standing of one rule, with the reasons it is not held. |
| `check_snapshot(key: str, instance: Any, reg: Registry \| None = None) -> list[str]` | function | Test-time check with a real instance: every old_self path is plain data, else LoadError. |

### Evaluating directly

| Name | Kind | Purpose |
|---|---|---|
| `evaluate(rule: Rule, base: dict[str, Any], enf_index: Any = 'rule', reg: Registry \| None = None, cache: dict[Any, Any] \| None = None, providers: dict[str, Any] \| None = None) -> Outcome` | function | One evaluation, one of the four outcomes. A rule defect is rule_error, never an exception. |
| `evaluate_env(rule: Rule, env: dict[str, Any], reg: Registry \| None = None) -> Outcome` | function | Run every clause. Any true -> violation (all fired); else any error -> rule_error; else ok. |
| `fill_view(rule: Rule, enf_index: Any, base: dict[str, Any], reg: Registry \| None = None, cache: dict[Any, Any] \| None = None, providers: dict[str, Any] \| None = None) -> View` | function | Fill one enforcer's view as the SDK does (providers called now, or from the call's cache). |
| `effective_mode(mode: str, status: str) -> str` | function | Effective mode = min(enforcer.mode, cap(status)). |

### Registries and ablation

| Name | Kind | Purpose |
|---|---|---|
| `Registry(sink_size: int \| None = None) -> None` | class | Loaded rules, providers and targets, plus the event sink, reviews and reach counters. |
| `current() -> Registry` | function | The registry guards use now: the one set by using()/isolated(), else the default. |
| `default_registry() -> Registry` | function | The process-wide default registry. |
| `using(reg: Registry) -> Iterator[Registry]` | function | Run guards against reg inside the block (context-local), then restore. |
| `isolated() -> Iterator[Registry]` | function | A fresh, empty registry for the block; inv.load() inside it installs there. |
| `ablated(rule_id: str, reg: Registry \| None = None, *, scope: str = 'context') -> contextlib.AbstractContextManager[None]` | function | Disable one rule's guards (what `pytest --inv-ablate` does per rule); its falsifiers must then fail. |
| `is_ablated(rule_id: str, reg: Registry \| None = None) -> bool` | function | True while the rule is ablated (inv.ablated, INV_ABLATE or Registry.ablate). |

### Operations

| Name | Kind | Purpose |
|---|---|---|
| `configure(reg: Registry \| None = None, *, sink_size: int \| None = ..., review_store: ReviewStore \| None = ..., pager: Pager \| None = ..., exporter: Exporter \| None = ..., error_spike: ErrorSpike \| None = ..., violation_log_level: int = ..., debug: bool = ..., review_fallback: ReviewStore \| None = ..., max_reply_size: int = ..., allow_non_finite: bool = ...) -> Registry` | function | Set the operational hooks of reg (default: the current registry). Unnamed settings are kept. |
| `observe(fn: Observer) -> Callable[[], None]` | function | Call fn(rule_id, kind, event) on every evaluation in any registry; returns an unsubscribe. |
| `capture(reg: Registry \| None = None) -> Iterator[list[dict[str, Any]]]` | function | Collect every event recorded in the block (in reg only, when given) into a list. For tests. |
| `monitor(rule_id: str, rows: Iterable[tuple[Any, dict[str, Any]]], reg: Registry \| None = None) -> list[tuple[Any, Outcome]]` | function | Evaluate a rule's monitor enforcer over (key, base) rows; record each outcome. Never blocks. |
| `Handoff(rule: str, action: str, outcome: Any = None, bound: dict[str, Any] = <factory>, attempts: list[dict[str, Any]] = <factory>, event: dict[str, Any] = <factory>, reason: str = '') -> None` | class | What route, escalate and page hand to the review store or the pager. |
| `ReviewStore(*args, **kwargs)` | class(Protocol) | Where route and escalate open a review. open() returns the review id. |
| `MemoryReviews(maxlen: int = 1000) -> None` | class | The default review store: in memory and bounded (the oldest are dropped). Not durable. |
| `JsonlReviews(path: str \| os.PathLike[str]) -> None` | class | A durable review store: each hand-off is appended as one JSON line to path and fsynced. |
| `ErrorSpike(errors: int = 20, window: float = 60.0) -> None` | class | errors rule_errors within window seconds demote a rule's effective mode to detect and page. |

### Records

| Name | Kind | Purpose |
|---|---|---|
| `Outcome(kind: str, rule: str, messages: list[str] = <factory>, error: str = '', bound: dict[str, Any] \| None = None) -> None` | class | One evaluation result: ok \| violation \| rule_error \| parse_error. |
| `Rule(raw: dict[str, Any], clauses: list[tuple[Program, str]], names: set[str], target: Any = None, effects: str = 'external', snapshot: list[tuple[str, ...]] = <factory>, names_for: dict[Any, dict[str, str]] = <factory>, notes: list[str] = <factory>, max_status: str = 'candidate', semantic_resamples: int = 0, refs: frozenset[tuple[str, ...]] = frozenset(), reads: frozenset[tuple[str, ...]] = frozenset()) -> None` | class | A loaded rule: the raw record plus what the loader computed. |
| `Provider(name: str, raw: dict[str, Any], fn: Callable[..., Any], type: type, nullable: bool) -> None` | class | A declared, typed, read-only provider bound to its function. |

### Errors

| Name | Kind | Purpose |
|---|---|---|
| `LoadError` | class(Exception) | The rule file is invalid: a build failure. |
| `CoverageError` | class(Exception) | A rule's target does not carry its guard, or a guard names no rule. |
| `ParseError` | class(Exception) | A model reply does not parse into the closed schema. |
| `InvariantError` | class(Exception) | Base for guard outcomes that stop a call. |
| `Rejected` | class(InvariantError) | on_violation: reject. The caller gets an error. |
| `Halted` | class(InvariantError) | on_violation: halt. An internal invariant broke: a bug. |
| `Escalation` | class(InvariantError) | on_violation: escalate (and abstain, always). Carries the attempt record. |
| `Routed` | class(InvariantError) | on_violation: route. A review was opened (review_id); the call does not proceed. |
| `ReviewNotOpened` | class(InvariantError) | route or escalate (or an abstain) could not open a review: the review store failed, and so did the review_fallback if one is configured. The call does not proceed, as with Routed/Escalation. |

### Module attributes

| Name | Kind | Purpose |
|---|---|---|
| `cel` | module | CEL adapter: the Backend protocol and backend selection. |
| `CEL_BACKEND` | `str` | currently `'subset'` |
| `__version__` | `str` | currently `'0.1.0'` |

## Integrations

### `invariants.integrations.fastapi`

```python
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict
import invariants as inv
from invariants.integrations import fastapi as inv_fastapi

class Order(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str
    quantity: int

app = FastAPI()

@app.post("/orders/{region}", operation_id="createOrder")
@inv_fastapi.guard("py:shop.create_order")        # under the route decorator
def create_order(region: str, order: Order) -> dict: ...

inv_fastapi.install(app, reject_status=422)        # guard exceptions become problem+json responses
```

Rules bind to the handler's parameters as FastAPI parsed them, for example `args.order.quantity`
or `args.region`. They bind to handler parameter names, not to OpenAPI JSON paths. A
`post_effect` rule reads the response model as `result`. The HTTP status depends on the outcome:

| Outcome | Status |
|---|---|
| reject | 422, configurable within 4xx |
| route or escalate | 202, with `review_id` |
| `rule_error` under `fail_closed` | 503 |
| no review could be opened (`ReviewNotOpened`) | 503 |
| halt | 500 |

The response body names the rule and the outcome, and never includes error text or values.
`inv_fastapi.verify_routes(app, doc)` checks that each openapi-bound rule matches exactly one
guarded route. The module re-exports `PROBLEM_JSON`, `StatusMap` and `problem` from `asgi`.

| Name | Kind | Purpose |
|---|---|---|
| `guard(*keys: str, response_model: type \| None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]` | function | @inv.guard for a route handler; put it under the route decorator. |
| `install(app: _fastapi.FastAPI, *, reject_status: int = 422, statuses: StatusMap \| None = None, expose_messages: bool = False) -> None` | function | Answer every guard exception raised in app as application/problem+json (see asgi.problem). |
| `verify_routes(app: _fastapi.FastAPI, source: str \| pathlib.Path \| dict[str, Any], reg: Registry \| None = None) -> int` | function | Every rule bound to an openapi operation finds exactly one route, guarded for that rule. |
| `operations(app: _fastapi.FastAPI) -> list[Operation]` | function | Every APIRoute of app, including routes of included routers (with their prefixes). |
| `find_route(app: _fastapi.FastAPI, operation: str) -> list[Operation]` | function | Operations matching an operationId, or 'METHOD /path' (the full path template as declared). |
| `operation_of(rule_raw: dict[str, Any], contracts: dict[str, Any]) -> str \| None` | function | The operation a rule binds to: subject.operation, else its openapi contract's operation. |
| `Operation(path: str, methods: frozenset[str], operation_id: str, endpoint: Callable[..., Any]) -> None` | class | One HTTP operation of an app: full path template, methods, operationId and handler. |
| `PROBLEM_JSON` | `str` | currently `'application/problem+json'` |
| `StatusMap(reject: int = 422, rule_error: int = 503, halt: int = 500, route: int = 202, escalate: int = 202, review_unavailable: int = 503) -> None` | class | HTTP status per outcome. reject is 422 by default; 409 is the usual alternative. |
| `problem(exc: InvariantError, statuses: StatusMap \| None = None, expose_messages: bool = False) -> tuple[int, dict[str, Any]]` | function | (status, problem+json body) for a guard's exception. |

### `invariants.integrations.asgi`

`InvariantMiddleware` does the same outcome-to-status mapping for any ASGI app.

| Name | Kind | Purpose |
|---|---|---|
| `PROBLEM_JSON` | `str` | currently `'application/problem+json'` |
| `StatusMap(reject: int = 422, rule_error: int = 503, halt: int = 500, route: int = 202, escalate: int = 202, review_unavailable: int = 503) -> None` | class | HTTP status per outcome. reject is 422 by default; 409 is the usual alternative. |
| `problem(exc: InvariantError, statuses: StatusMap \| None = None, expose_messages: bool = False) -> tuple[int, dict[str, Any]]` | function | (status, problem+json body) for a guard's exception. |
| `encode(body: dict[str, Any]) -> bytes` | function | The JSON bytes of a problem body. |
| `InvariantMiddleware(app: ASGIApp, *, registry: Registry \| None = None, reject_status: int = 422, statuses: StatusMap \| None = None, expose_messages: bool = False) -> None` | class | Pure ASGI middleware: guard exceptions become problem+json responses. |

### `invariants.integrations.pydantic`

Pydantic v2 models as closed schemas for model calls, and the `pydantic` contract type. The module
has no `__all__`; these are its public functions. `closed(model)` (cached) returns the closed-schema
dataclass for a pydantic model, so the core parse, retry, abstain and edge logic run unchanged.

| Name | Kind | Purpose |
|---|---|---|
| `is_model(obj: Any) -> bool` | function | True for a pydantic.BaseModel subclass. |
| `check_closed(model: type[_pd.BaseModel], _seen: set[type] \| None = None) -> None` | function | LoadError unless model and every model nested in its fields forbid extra fields and use no aliases. |
| `enum_values(ann: Any) -> list[Any] \| None` | function | The closed set of a Literal or Enum field (None included when optional), else None. |
| `field_names(model: type[_pd.BaseModel], _seen: set[type] \| None = None) -> frozenset[str]` | function | Every field name declared by model and the models nested in it. |
| `safe_feedback(err: _pd.ValidationError, names: frozenset[str]) -> str` | function | 'loc: error type; ...' with no input value and no key the reply made up (a loc part that is not a declared field name or a list index is '*'): the text for events, logs and exceptions. |
| `feedback(err: _pd.ValidationError) -> str` | function | A short, model-facing text for a ValidationError: 'loc: message; ...' (no inputs, no URLs). |
| `is_lax(model: type[_pd.BaseModel]) -> bool` | function | True when the model opts out of strict reply validation: model_config sets strict=False explicitly. |
| `validate(model: type[_pd.BaseModel], data: dict[str, Any]) -> _pd.BaseModel` | function | data validated into model: strict over its JSON form unless the model is lax; ParseError on failure. |
| `schema_for(schema: Any) -> Any` | function | closed(schema) for a pydantic model; any other schema unchanged. |
| `model_of(reply: Any) -> _pd.BaseModel` | function | A new pydantic instance validated from a reply's JSON attributes (the reply, or a Checked holding one). |
| `parse(raw: Any, schema: Any) -> Any` | function | inv.parse, also accepting a pydantic model as the schema. |
| `model_call(rule_id: str, schema: Any) -> Callable[[Callable[P, R]], Callable[P, R]]` | function | inv.model_call, also accepting a pydantic model as the schema. |
| `resolve_ref(ref: Any) -> Any` | function | 'package.module:Class' -> the object: import the module, then walk attributes. No eval. |
| `resolve_contract(spec: Any, name: str = '?') -> type[_pd.BaseModel]` | function | A {type: pydantic, ref: 'module:Class'} contract -> the closed model class, or LoadError. |
| `check_contracts(source: str \| pathlib.Path \| dict[str, Any], reg: Registry \| None = None) -> dict[str, type[_pd.BaseModel]]` | function | Resolve every pydantic contract in a rule file and check the rules that name one. |
### `invariants.integrations.otel`

One span per evaluation, named `invariant.<point>`, with `invariant.*` attributes (`rule_id`,
`rule_rev`, `outcome`, `mode`, `point`, `timing`, `action`, `attempts`). Clause messages and error
text are not exported, since they can carry values. Needs the `otel` extra.

```python
from invariants.integrations import otel
inv.configure(exporter=otel.exporter())
```

| Name | Kind | Purpose |
|---|---|---|
| `exporter(tracer: Any = None) -> Exporter` | function | An exporter for inv.configure(exporter=...). tracer: an OpenTelemetry tracer (default: the global one). |
| `attributes(event: dict[str, Any]) -> dict[str, Any]` | function | The span attributes for one event (None values left out). |

## `invariants.testing`

| Name | Kind | Purpose |
|---|---|---|
| `make_checked(cls: type[C], value: Any, verdict: str = 'ok', attempts: int = 1, rule: str \| None = None) -> C` | function | A cls instance carrying value and verdict, as inv.model_call would build it. A deliberate forgery for tests. |
| `make_unbuilt(cls: type[C]) -> C` | function | A cls instance with no verdict or value at all (what object.__new__ gives). For edge tests. |
| `capture(reg: Registry \| None = None) -> Iterator[list[dict[str, Any]]]` | function | Collect every event recorded in the block (in reg only, when given) into a list. For tests. |

## The pytest plugin

The plugin is installed with the package (entry point `invariants`). Without options it only adds
its fixtures.

```text
pytest --inv-rules invariants.yaml --inv-verify --inv-ablate --inv-report
```

| Option | What it does |
|---|---|
| `--inv-rules PATH` | Loads the rule file after collection. You can also set it with the ini option `inv_rules` (relative to the ini file). |
| `--inv-verify` | Fails the session when a target lacks its guard. |
| `--inv-ablate` | Re-runs each rule's `falsified_by` tests with the rule turned off in every registry and in `INV_ABLATE` (so subprocesses see it). A falsifier that still passes fails the session with "guard not load-bearing". What counts as caught, and what is `inconclusive`, is in [concepts.md](concepts.md#ablation). |
| `--inv-ablate-lenient` | With `--inv-ablate`, fail only on a falsifier that still passes. |
| `--inv-report` | Prints per-rule reach, ok, violation, rule_error and parse_error counts. |
| `--inv-report-json PATH` | Writes the same counts as JSON. |

Fixtures:
- `inv_registry` is a fresh registry that holds the session rules.
- `inv_ablated(rule_id)` returns a context manager that turns the rule off in this thread or task.
- `inv_events` is the list of every event recorded during the test.
- `inv_scan(rule_file="invariants.yaml", *roots, exclude=(), cwd=None)` runs `inv scan --format json`
  in a subprocess and returns a `ScanRun`: `failed(rule)`, `findings(rule=None, kind=None)`,
  `passed(rule)`, `code`, `ablated` and the raw `exit_code` and `report`. Under `--inv-ablate`,
  `code` and `passed(rule)` have no answer for the ablated rule (the scan exits 4), so a falsifier
  asserts `failed(rule)` or its findings. See [concepts.md](concepts.md#ablation).

A falsifier's re-runs share every fixture broader than the test function with the rest of the
session. Under pytest-xdist each worker ablates the falsifiers it runs and the controller reports
the merged results.

## The CLI

| Command | Synopsis |
|---|---|
| `inv init` | `inv init [file] [--root DIR]... [--exclude GLOB]... [--no-lock] [--no-doors] [--graph|--graphify GRAPH] [--no-graphify] [--max-doors N] [--path DIR]...`: drafts the rule file from pydantic constraints, service-to-repository doors and function preconditions, then runs `inv lock`; reads `graphify-out/graph.json` for door hints when it exists; never overwrites. See [start.md](start.md). |
| `inv check` | `inv check [file] [--relock [--fail-on-repin]] [--base REF] [--allow-weakening] [--root DIR]... [--exclude GLOB]... [--import MOD]... [--path DIR]...`: `validate`, `lock --check`, `verify` and `scan` with one exit code; `--fail-on-repin` exits 1 after `--relock` re-pins, so the new lock is staged; `--base` also fails on a weakening since the lock at REF unless `--allow-weakening` or `INV_ALLOW_WEAKENING=1`, and fails closed when REF is missing, the checkout is not git, or the lock at REF cannot be compared (only a REF with no lock passes, as a first adoption). The `--root` and `--exclude` it scans with are compared with the ones the lock pins. |
| `inv hook` | `inv hook pretool\|stop`: Claude Code hooks reading the hook JSON on stdin; exit 2 blocks. `pretool` refuses a shell command that accepts a weakening, any direct edit of `invariants.lock` (symlinks resolved; a shell command naming it must be clearly read-only), and an edit of `[tool.invariants]` in `pyproject.toml`; `stop` runs `inv check --relock` once per stop. An early warning; `inv check --base` in CI is the gate. |
| `inv validate` | `inv validate [file] [--path DIR]`: runs the JSON Schema check and every load-time check, and lists every error. |
| `inv lock` | `inv lock [file] [--check] [--accept-weakening] [--lock FILE] [--update-artifacts] [--root DIR]... [--exclude GLOB]... [--path DIR]...`: see [lockfile.md](lockfile.md). `--root` and `--exclude` pin extra scan roots and excludes, as `inv check` is then run with them. |
| `inv verify` | `inv verify [file] [--import MOD]... [--json] [--path DIR]...`: imports the app, then checks that every target is guarded and every guard names a rule. |
| `inv scan` | `inv scan [file] [--root DIR]... [--exclude GLOB]... [--format text\|json\|sarif] [--output FILE]`: see [scan.md](scan.md). |
| `inv status` | `inv status [file] [--root DIR]... [--exclude GLOB]... [--import MOD]... [--json] [--path DIR]...`: the standing of each rule, combining verify and scan. |

Where `validate`, `lock`, `verify` and `status` import the app from is described in
[lockfile.md](lockfile.md#where-the-cli-imports-from).

Exit codes:
- 0: ok
- 1: a check failed
- 2: usage error
- 3: the scan found nothing failing but some calls are unresolved. This is not a pass.
- 4: the scan skipped an `enforce` rule because it is ablated (`INV_ABLATE`). This is not a pass.
