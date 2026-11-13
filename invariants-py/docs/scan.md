# The mediation scan

A guard only protects the paths that go through it. `inv scan` is a static, read-only call-graph
check of path laws: every call path to a protected API (the *sink*, usually a repository method)
passes an upper API (the *door*, usually a service method). The source is parsed with `ast`; it is
never imported or run.

- [Mediation](#mediation)
- [Findings](#findings)
- [Allowed back doors](#allowed-back-doors)
- [Configuration and output](#configuration-and-output)
- [Limitations](#limitations)

## Mediation

Each rule declares, under `mediation`:

```yaml
mediation:
  chokepoint: pools.size is written only by resize_door    # the positive argument, in words
  sink:   "py:pkg.store.set_size"           # the protected effect: a string or a list
  door:   "py:pkg.service.resize"           # the chokepoint function(s); defaults to subject.target
  allow:  ["py:pkg.batch.refund_all"]       # known back doors: the call chain, frozen
  residual_paths: []                        # known gaps, in words; while any is listed, the rule is not held
```

`inv scan` looks for any call path from an entrypoint to the sink that avoids every door. An
entrypoint is a function with no extracted caller, or a module body (and anything those cannot
reach). Each call site that reaches the sink this way (each `file:line:col`) is one finding, shown
with the shortest path from the first entrypoint that reaches it: two calls of the sink in one
function are two findings, and two entrypoints through the same helper call are one. A rule on an `effects: none` target needs no sink: the guard wraps the function itself, and
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
| `forbidden` | a bypass idiom | `forbidden/getattr-sink` (and `getattr` on a door), `forbidden/unwrap` (`.__wrapped__`), `forbidden/monkeypatch` |
| `unresolved` | something the scan cannot follow; never counted as passed | `unresolved/unknown-receiver`, `unresolved/reflection`, `unresolved/computed-attribute`, `unresolved/inferred-path`, `unresolved/sink-escapes`, `unresolved/excluded-callee`, `unresolved/parse-error` |
| `missing` | a declared sink, door or `allow` entry that does not exist in the scanned code, or an `allow` entry that no longer reaches a sink without a door | `missing/sink`, `missing/door`, `missing/allow`, `missing/allow-unused` |

Each finding carries the function it is in (`owner`, in `--format json`), which is what `allow`
names to freeze it.

A `forbidden/unwrap` on a function the scan can name (`ResizeService.resize_door.__wrapped__`) is
listed under the rules where that function is a sink, door or allowed back door, or reaches a sink
without passing a door; an unwrap of something it cannot name (`fn.__wrapped__` on a parameter) is
listed under every rule.

### Method calls

A method call is an extracted edge only when the scan is sure of the receiver's class: a constructor
call (`Repo(session).add(...)`), a local variable whose every assignment is the same constructor, a
parameter annotated with a scanned class (`repo: Repo`, `Optional[Repo]`, `Repo | None`, `"Repo"`),
or `self.x` when every assignment to it in the class's methods is one of those (tuple assignment
`self.a, self.b = a, b` included). Through an annotation or `self.x`, a subclass instance may arrive,
so the call also reaches every scanned subclass's override. Anything else (an unannotated parameter,
a rebinding, unpacking it cannot pair) is a name-matched edge, which the scan reports as unresolved
rather than trusting.

A call to a scanned function annotated to return a scanned class types its result too:
`get_repo().add(...)`, or `r = get_repo()` then `r.add(...)`, where `def get_repo() -> Repo`
(also `Optional[Repo]`, `Repo | None` or a string annotation), and `await get_repo()` on an
`async def`. That covers factories and FastAPI dependency helpers: a parameter annotated
`Annotated[Repo, Depends(get_repo)]` is a `Repo`, and so is one annotated with a module-level alias
of it (`RepoDep = Annotated[Repo, Depends(get_repo)]`, bound once, imported or not). The annotation is read in the module that defines the function, and the result
may be a subclass, so overrides are reached as for an annotated parameter. A function with no return
annotation, or one naming more than one class, gives no type.

`with X() as r` (and `async with`) types `r` by the scanned class's `__enter__` (`__aenter__`)
return annotation (`-> Self` is the class itself), and `with session() as r` by a `@contextmanager`
(`@asynccontextmanager`) factory annotated `-> Iterator[Repo]` (`Generator[Repo, ...]`,
`AsyncIterator[Repo]`). A with target the scan cannot type stays unresolved.

Generic classes resolve like plain ones: `class ConcreteRepo(BaseRepo[T])` is a subclass of
`BaseRepo` (`Generic[T]` and `Protocol` bases are ignored), so a call through a `BaseRepo` (or
`BaseRepo[int]`) receiver also reaches `ConcreteRepo.save`. `super().save(x)` (or
`super(Cls, self)`) in a method of class `C` is an extracted edge to the next `save` after `C` in the
method resolution order, through the scanned classes, of `C` and of every scanned subclass of `C`:
`self` may be any of them, so in `class Leaf(Mixin, SinkRepo)` a `super().save(x)` in `Mixin` reaches
`SinkRepo.save`. When no scanned class has it, or a subclass has no consistent order, the call is
name-matched (unresolved).

Receiver types also follow attribute chains. In `ctx.billing.refund()`, where `ctx: AgentContext`
and `AgentContext` sets `self.billing = BillingService(...)` in its methods (or declares
`billing: BillingService` at class level, as a dataclass does), the call is an extracted edge to
`BillingService.refund`. A `@property` (or `functools.cached_property`) getter with a return
annotation types `self.repo` the same way. An attribute is resolved in the module that defines the class. An
attribute that any code sets from outside its object (`obj.x = ...` in a function that is not one
of that object's methods, or `setattr(obj, "x", ...)`) is never typed: calls through it stay
name-matched, so they are unresolved.

A receiver known to be a builtin container (a dict, set or list literal or comprehension, `dict()`,
`set()`, `list()`, or a parameter annotated `dict[...]`, `set[...]`, `list[...]`) calls the
container's own method, so `d.update(x)` or `s.add(x)` is not `unresolved/unknown-receiver` when a
sink happens to be called `update` or `add`. When a scanned class subclasses that builtin and
overrides the method, a call through an annotation (which may hold the subclass) stays unresolved.

A sink taken as a value through a typed receiver (`cb = repo.save`, `add_task(self.repo.save)`,
`functools.partial(repo.save, x)`) is `unresolved/sink-escapes`, as for a sink named directly;
calling it (`repo.save(x)`) is an edge.

A call such as `store.sink.__call__(c)` (or `getattr(store.sink, "__call__")(c)`) is a call of the
sink. A lookup in a scanned module's or class's namespace is a `getattr` on it:
`store.__dict__["sink"]`, `store.__dict__.get("sink")`, `store.__getattribute__("sink")` and
`object.__getattribute__(store, "sink")` are `forbidden/getattr-sink`, and any other use of
`store.__dict__` (a computed key, `.items()`, the dict as a value) or `inspect.getmembers(store)` is
`unresolved/reflection`, like `vars(store)`. `type(obj).__dict__` and `obj.__class__.__dict__` are
a class namespace too. A function's `__dict__` is its metadata and stays harmless. Reflective
builtins taken as a value (`g = getattr`, then `g(obj, name)()`; also `setattr`, `eval`, `vars`,
`importlib.import_module`, `operator.methodcaller`) are `unresolved/reflection`, and an import alias
(`from builtins import getattr as g`) is checked as `getattr` itself. Any use of `sys.modules`
(`sys.modules[name].Repo`, `sys.modules.get(name)`, assigning into it) is `unresolved/reflection`;
`name in sys.modules` only asks and stays harmless. Rebinding a scanned module or class that holds a
sink or door from outside (`pkg.repo.Repo = Fake`, `pkg.repo = fake`, `setattr(pkg.repo, "Repo",
Fake)`) is `forbidden/monkeypatch`, like assigning over the sink itself (`Repo.save = other`). When a method call cannot be resolved, the scan reports it as unresolved, never as a bypass.
The exit code is 1 on any bypass, forbidden or missing finding, and 3 when nothing failed but some
calls are unresolved (not a pass). It is 4 when the scan skipped an `enforce` rule because it is
ablated (`INV_ABLATE`), which is not a pass either, and not a fail: a falsifier that runs the scan
asserts the rule's findings (or exit code 1 exactly), never `returncode != 0`. The pytest plugin's
`inv_scan` fixture reads it that way ([concepts.md](concepts.md#ablation)).

## Allowed back doors

`mediation.allow` freezes the back doors that exist when it is written: each function on the call
chain from an entrypoint to the sink. An allowed function is not a door. A path to the sink is let
through only when every function on it is in `allow`. So these are a `bypass`:

- a new caller of an allowed function (a new script that calls `admin_goodwill`);
- new code that an allowed function calls and that reaches the sink.

Inside an allowed function, the ways it reaches the sink are excused with it: a direct call, a
`getattr` of the sink name, or the sink used as a value. `forbidden/unwrap`, `forbidden/monkeypatch`
and `unresolved/reflection` in it are still reported.

`inv init` freezes the whole chain of each back door it finds (both `admin_goodwill` and the
`goodwill_refund` it calls, say), from every entrypoint that reaches it (not only the path a finding
shows), re-scanning up to five rounds until nothing new is found. When the scan still fails, the rule
is written commented out with the reason, including when every finding is inside a door or sink
(which `allow` cannot freeze). An
entry that no longer reaches the sink without a door is `missing/allow-unused` and fails the check
until it is deleted. Deleting it is a tightening; adding one is a weakening the lock reports.

## Configuration and output

```text
inv scan [file] [--root DIR]... [--exclude GLOB]... [--format text|json|sarif] [--output FILE]
inv status [file] [--root DIR]... [--exclude GLOB]... [--import MOD]... [--json] [--path DIR]...
```

With no `--root` and no configured roots, `inv scan` and `inv status` scan the rule file's directory
and, when a `pyproject.toml` sits beside the rule file, its source directories (`src/` and the
setuptools `packages.find.where` directories) as roots of their own, so a src-layout project is
scanned under the module names `inv lock` imports it by. A file is named from the deepest root that
holds it. Test code and migrations are skipped by default, at any depth (`tests/`, `test/`,
`testing/`, `test_*.py`, `*_test.py`, `conftest.py`, `migrations/`, `alembic/`): a unit test that calls
a sink directly is not a back door. Scan roots and excludes can
also live in the `pyproject.toml` beside the rule file (an `exclude` there replaces the default list,
so `exclude = []` scans the tests too). Excluded files are not read, but their module names are
noted: a call from scanned code into an excluded module (production code calling
`app/testing/helpers.py` or `app/test_fastpath.py`) is `unresolved/excluded-callee`, naming the file
and the glob, since the scan cannot see where it goes. Narrow the exclude or move the code. Tests
importing the app are excluded code calling scanned code, and stay quiet:

```toml
[tool.invariants.scan]
roots = ["src"]
exclude = ["*/legacy/*"]
```

The lock pins the roots and excludes `inv check` scans with (the `scan` section of
[lockfile.md](lockfile.md)), so adding an exclude, here or with `inv check --exclude`, or dropping a
root is a weakening that a person accepts, like any other. The agent hook `inv hook pretool` also
blocks an agent's edit of `[tool.invariants]`.

`--format json` gives one record per rule (its result, sinks, doors, `allow` and counts), and
`--format sarif` gives SARIF 2.1.0 for code-scanning tools. A rule ablated through `INV_ABLATE` is
not scanned, so a scan inside a falsifier is ablated too. A scan-only rule still needs a `bad` and a
`structure` enforcer.

## Limitations

- `inv scan` builds an approximate call graph. When a method call cannot be resolved, the scan
  reports it as unresolved, never as a bypass.
- It does not follow calls through dependency injection containers, event buses, callbacks or
  values it cannot type (see "Method calls" above).
- Callers outside the scanned roots are invisible to it.
- It checks which function calls which. It does not read SQL or any other query text: what a
  repository does below its API is the implementation's business.
- Each finding is reported under every scanned rule it affects.
