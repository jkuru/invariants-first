# Public API

`import invariants as inv` exposes exactly the names below: the package's `__all__`, which a test
checks. The tables are generated from the installed package (the signatures and the first paragraph
of each docstring), so they match the code. The integrations' public surface is each module's
`__all__`.

- [`invariants`](#invariants)
- [Integrations](#integrations): [FastAPI](#invariantsintegrationsfastapi), [ASGI](#invariantsintegrationsasgi), [Pydantic](#invariantsintegrationspydantic), [OpenTelemetry](#invariantsintegrationsotel), [Buffered exporters](#invariantsintegrationsexporters), [Anthropic](#invariantsintegrationsanthropic)
- [`invariants.testing`](#invariantstesting)
- [The pytest plugin](#the-pytest-plugin)
- [The CLI](#the-cli)

## `invariants`

Key behaviours that the one-line docstrings leave out:

- `@inv.guard(*keys, points=("decorator",))`: a key is a rule id or a `py:` target. It works on
  sync and async functions, on methods, and above or below `@classmethod` or `@staticmethod`. A
  call raises `LoadError` when a rule-id key is not loaded, or when a `py:` key matches no loaded
  rule and no `targets:` entry, so a guard never runs open because nothing was loaded.
- `@inv.model_call(id, schema=..., returns=CheckedT)`: `schema` is a dataclass, a `{field: type}`
  map or a pydantic model. With `returns=`, annotate the body with what it really returns
  (`-> str`); the decorated function returns `CheckedT` to mypy (awaitable for an `async def`), with
  no `type: ignore`. Without it, the body's return annotation must be the `Checked` subclass (the
  older form). See [concepts.md](concepts.md#model-calls).
- `@inv.provider("a", "b")`: one function under several provider names. `inv.Dependency("store")`
  is a context-local value a provider reads (`with STORE.bound(store): ...`, `STORE.get()`).
- `inv.verify(reg=None, modules=None)`: by default it checks the guards in the packages of the
  loaded targets.
- `inv.standing(id, scan=False, ablation=None)` returns `(standing, reasons)`, where standing is
  `held`, `enforce(known-callers)`, `monitored`, `candidate` or `retired`. `ablation` is the pytest
  plugin's `--inv-report-json` data (`status_report.load_ablation(path)`); see
  [concepts.md](concepts.md#standing).
- `JsonlReviews.aopen(handoff)` runs the write and fsync in a worker thread; async guards, model
  calls and edges use it (`Registry.ahand_off`), so they never block the event loop. A review store
  may define `async def aopen(handoff) -> str`; one that defines only `open()` has it run in a worker
  thread (`asyncio.to_thread`) on the async paths (`MemoryReviews` opens on the loop: it is in memory).
- `@inv.edge(rule_id, at=None)` on agent-graph state: the `Checked` value may arrive in a field of a
  state parameter (a `TypedDict`, dataclass or pydantic model), as in `def act(state: State)` with
  `state["plan"]` or `state.plan` annotated `CheckedPlan` (or `CheckedPlan | None`). With one such field
  in the parameters' state types the edge finds it; with several, name it with
  `@inv.edge("R-7", at="state.plan")` (a dotted path from a parameter through annotated fields).
  None, several without `at=`, or an `at=` path that does not lead to a `Checked` annotation is a
  `TypeError` when the decorator runs. The checks are the direct case's: the value is read once,
  re-validated and re-checked, and the consumer gets a shallow copy of the state whose field holds
  the new `Checked` over the judged copy (the caller's state object is never changed).
- The value of a `Checked` over a `{field: type}` schema is a frozen dict (`guard.FrozenDict`, its
  lists `FrozenList`): equal to and read like the parsed dict, but `update`, `pop`, item assignment
  and the list methods raise `TypeError`, through `.value` or through attribute forwarding.
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
| `provider(name: str, *more: str, concurrent: bool = False) -> Callable[[F], F]` | function | @inv.provider("approval"): register a read-only provider; the loader checks name, args and return type. concurrent=True lets an async provider overlap other opted-in ones (the record's `concurrent:` overrides it). |
| `Dependency(name: str) -> None` | class | A context-local value providers read: a store, a client, a tenant (contextvars underneath). |
| `model_call(rule_id: str, schema: Any, *, returns: type[C]) -> ModelCallDecorator[C]`; `model_call(rule_id: str, schema: Any) -> Callable[[Callable[P, R]], Callable[P, R]]` | function | @inv.model_call("R-7", schema=CauseReply, returns=CheckedCause): parse -> abstain -> bad -> accept, with bounded retries. |
| `edge(rule_id: str, *, at: str \| None = None) -> Callable[[Callable[P, R]], Callable[P, R]]` | function | @inv.edge("R-7") on the consumer of a Checked value (sync or async); at="state.plan" names it inside a state parameter. |
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
| `standing(rule_id: str, reg: Registry \| None = None, scan: bool = False, ablation: Mapping[str, Any] \| None = None) -> tuple[str, list[str]]` | function | Computed standing of one rule, with the reasons it is not held (see Registry.standing). |
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
| `is_ablated(rule_id: str, reg: Registry \| None = None) -> bool` | function | True while the rule is ablated (inv.ablated, INV_ABLATE or Registry.ablate). Asking does not count as reaching the rule for `--inv-ablate`. |

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
| `rule_error` under `fail_closed` | 503, whatever `on_violation` is (a route or escalate enforcer still opens the review, and the body keeps its `review_id`) |
| no review could be opened (`ReviewNotOpened`) | 503 |
| halt | 500 |

The response body names the rule and the outcome, and never includes error text or values.
`inv_fastapi.verify_routes(app, doc)` checks that each openapi-bound rule matches exactly one
guarded route. The module re-exports `PROBLEM_JSON`, `StatusMap` and `problem` from `asgi`.

| Name | Kind | Purpose |
|---|---|---|
| `guard(*keys: str, response_model: type \| None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]` | function | @inv.guard for a route handler; put it under the route decorator. |
| `install(app: _fastapi.FastAPI, *, reject_status: int = 422, statuses: StatusMap \| None = None, expose_messages: bool = False, reg: Registry \| None = None) -> None` | function | Answer every guard exception raised in app as application/problem+json (see asgi.problem); also registers the app so `inv verify` runs verify_routes on it. |
| `register(app: _fastapi.FastAPI, reg: Registry \| None = None) -> None` | function | Have reg's verify() (inv verify) check app's routes with verify_routes (install does this). |
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
| `model_of(reply: Any) -> _pd.BaseModel` | function | A fresh copy of a pydantic reply (the reply, or a Checked holding one), validated again. |
| `parse(raw: Any, schema: Any) -> Any` | function | inv.parse, also accepting a pydantic model as the schema. |
| `model_call(rule_id: str, schema: Any, *, returns: type[C] \| None = None)` | function | inv.model_call, also accepting a pydantic model as the schema. |
| `resolve_ref(ref: Any) -> Any` | function | 'package.module:Class' -> the object: import the module, then walk attributes. No eval. |
| `resolve_contract(spec: Any, name: str = '?') -> type[_pd.BaseModel]` | function | A {type: pydantic, ref: 'module:Class'} contract -> the closed model class, or LoadError. |
| `check_contracts(source: str \| pathlib.Path \| dict[str, Any], reg: Registry \| None = None) -> dict[str, type[_pd.BaseModel]]` | function | Resolve every pydantic contract in a rule file and check the rules that name one. |
### `invariants.integrations.otel`

One span per evaluation, named `invariant.<point>`, with `invariant.*` attributes (`rule_id`,
`rule_rev`, `outcome`, `mode`, `point`, `timing`, `action`, `attempts`). Clause messages and error
text are not exported, since they can carry values. Needs the `otel` extra. A span runs from when
the check began (the guard's check phase with its providers, a model call's first attempt, an edge
call) to when the outcome was recorded (events carry `start_ns` and `end_ns`); a violation,
rule_error or parse_error sets status ERROR.

```python
from invariants.integrations import otel
inv.configure(exporter=otel.exporter())
```

| Name | Kind | Purpose |
|---|---|---|
| `exporter(tracer: Any = None) -> Exporter` | function | An exporter for inv.configure(exporter=...). tracer: an OpenTelemetry tracer (default: the global one). |
| `attributes(event: dict[str, Any]) -> dict[str, Any]` | function | The span attributes for one event (None values left out). |

### `invariants.integrations.exporters`

A registry calls its exporters inline, on the thread of the guarded call. `BufferedExporter` wraps
any exporter (`otel.exporter()`, your own) so that recording only appends the event to a bounded
queue; a background thread calls the wrapped exporter. The host owns that thread: it exists only
between `start()` (or `with`) and `close()`, and the library never starts it. Events recorded while
it is stopped wait in the queue until `start()`, `drain()` or `close()`. A full queue drops the
oldest event by default (`on_full="drop_oldest"`: the guarded call never waits, and the newest
events are kept, as in `Registry.sink`); `"drop_new"` drops the new one; `"block"` makes the
guarded call wait for room while the flusher runs. Drops are counted in `.dropped`, events the
wrapped exporter raised on in `.failed` (the first failure is logged, and the flusher goes on).
After `os.fork` the child's exporter is stopped with an empty queue; start it again there. Review
stores never go through it: a hand-off is durable on its own path.

```python
from invariants.integrations import exporters, otel
buffered = exporters.BufferedExporter(otel.exporter(), maxsize=10_000)
inv.configure(exporter=buffered)
with buffered:                       # or app = FastAPI(lifespan=exporters.lifespan(buffered))
    serve()
```

| Name | Kind | Purpose |
|---|---|---|
| `BufferedExporter(target: Exporter, *, maxsize: int = 10000, on_full: str = 'drop_oldest') -> None` | class | An exporter that queues events (bounded) for a host-started thread to pass to target. |
| `BufferedExporter.start() -> BufferedExporter` | method | Start the flusher thread (a no-op while it runs). Also `with exporter:`. |
| `BufferedExporter.drain(timeout: float \| None = None) -> bool` | method | Export everything queued now; True when nothing is left. Without a flusher it exports in the calling thread. |
| `BufferedExporter.close(timeout: float \| None = None) -> bool` | method | Flush the queue, then stop the flusher; idempotent. True when it stopped within timeout. |
| `BufferedExporter.dropped`, `.failed`, `.exported`, `.pending`, `.running` | attributes | Counts of dropped, failed and exported events; events queued now; whether the flusher runs. |
| `lifespan(*exporters: BufferedExporter) -> Callable[[Any], AbstractAsyncContextManager[None]]` | function | An ASGI lifespan that runs the exporters' flushers while the app runs (close in a worker thread). |

### `invariants.integrations.anthropic`

A real model for a `model_call` body, with deterministic replay and recording; see
[models.md](models.md). `AnthropicModel` needs the `anthropic` extra, imported on its first request;
the rest needs nothing. One model_call invocation, with its format retries, is one conversation: a
`feedback` text is sent as a follow-up user turn after the model's previous reply.

```python
from invariants.integrations.anthropic import AnthropicModel, ReplayModel
llm = AnthropicModel(system="Answer with one JSON object.")      # or ReplayModel("trace.json", system=...)
...
    return llm.complete(prompt, inv_feedback)                    # in the model_call body
```

| Name | Kind | Purpose |
|---|---|---|
| `AnthropicModel(model: str = 'claude-haiku-5-5', *, system: str \| None = None, max_tokens: int = 16000, client: Any = None, async_client: Any = None, feedback_template: str \| None = None, tool: dict \| None = None, tool_choice: dict \| None = None, **params: Any)` | class | A Claude model through the Messages API; `params` go to `messages.create`. With `tool=`, the reply is the model's call to that tool (see [agent-frameworks.md](agent-frameworks.md)). |
| `reply_tool(name: str, schema: Any = None, *, description: str \| None = None, input_schema: dict \| None = None, strict: bool = False) -> dict` | function | A tool definition whose input is the structured reply, built from the model call's schema. |
| `ReplayModel(path, *, system: str \| None = None, model: str = DEFAULT_MODEL, max_tokens: int = 16000, ignore=(), feedback_template: str \| None = None, tool=None, tool_choice=None, **params)` | class | Replies from a trace file, matched on the request; `ReplayMismatch` with a diff otherwise. `assert_done()`, `remaining`. |
| `RecordingModel(inner: AnthropicModel, path)` | class | Sends through inner and writes the trace, marked `"fixture": "recorded"`. |
| `ChatModel.complete(prompt: str \| Sequence[Message], feedback: str \| None = None) -> str` | method | The reply text (with a reply tool, the tool call's input as JSON text). feedback None opens a new conversation; a text continues the current one (as a `tool_result` with `is_error: true` after a tool call). `acomplete` for an async body. |
| `model_for(mode: str, trace=None, *, system: str \| None = None, model: str = DEFAULT_MODEL, **kw: Any) -> ChatModel \| None` | function | The model for `replay`, `record`, `anthropic`; `None` for `fake`. |
| `ReplayMismatch`, `ConversationError`, `ModelRefusal` | class | A replayed request no recorded call matches (an AssertionError); feedback with no conversation to continue; a `refusal` stop reason. |

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
| `inv status` | `inv status [file] [--root DIR]... [--exclude GLOB]... [--import MOD]... [--json] [--ablation JSON] [--path DIR]...`: the standing of each rule, combining verify, scan and (with `--ablation`, the pytest `--inv-ablate --inv-report-json` file) ablation. Without `--ablation` a `held` row is labelled unproven: `"ablation_proven": false` and a reason in JSON, `held (unproven: no --ablation)` in text; `ablation_proven` is true only for a `held` backed by the report. |
| `inv mcp` | `inv mcp [rulefile] [--root DIR]`: a read-only MCP server over stdio for coding agents (tools: scan, check, status, explain, propose_rules); it never writes the rule file, the lock or `allow`. The same server is installed as `inv-mcp [--root DIR] [--rules FILE]`. See [agents.md](agents.md). |

Where `validate`, `lock`, `verify` and `status` import the app from is described in
[lockfile.md](lockfile.md#where-the-cli-imports-from).

Exit codes:
- 0: ok
- 1: a check failed
- 2: usage error
- 3: the scan found nothing failing but some calls are unresolved. This is not a pass.
- 4: the scan skipped an `enforce` rule because it is ablated (`INV_ABLATE`). This is not a pass.
