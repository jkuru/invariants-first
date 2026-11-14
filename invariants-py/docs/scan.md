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
function are two findings, and two entrypoints through the same helper call are one. A call the scan
name-matches on a receiver it cannot type (`repo.save(x)`, `repo` unannotated) is one finding too, the
`unresolved/inferred-path` with its path. A rule on an `effects: none` target needs no sink: the guard wraps the function itself, and
the scan reports it as "not scanned (mediation declares no sink)".

```text
$ inv scan
bypass     PAY-1    ops.py:5:5  ops.goodwill_credit -> payments.send_to_gateway reaches the sink without passing payments.refund
PAY-1: fail (1 bypass)
scanned 3 file(s), 5 function(s)
```

### Functions used as values

A function used as a value is an entrypoint: whoever holds the value can call it from anywhere. So
a function that, called on its own, reaches the sink without passing a door, and that is taken as a
value outside the doors, the sinks and the allowed functions, is reported where it escapes, as
`unresolved/sink-escapes` with the path it would take. That includes a door's own helper: with
`refund -> _send -> send_to_gateway`, `_send` is not an entrypoint while only `refund` calls it, but
`threading.Thread(target=_send)`, `atexit.register(_send)`, `{"x": _send}`, `map(_send, ...)`,
`functools.partial(_send, ...)`, `x = _send`, a default argument `cb=_send`, `return _send`,
`staticmethod(_send)` in a class body, `Safe.go = _send`, `builtins.len = payments._send`, a bound
method `f = Service()._persist` and any rebinding of a name to it (walrus, `for`, `with`, `match`, a
comprehension, a lambda default, `nonlocal`, `global`) all are. A value made where the code has
already passed a door (inside the door, or in a helper only a door reaches) is not reported, nor is a
door or an unrelated function taken as a value. An allowed back door taken as a value outside
`allow` is a new caller, so a `bypass` ([Allowed back doors](#allowed-back-doors)). The sink itself
taken as a value is `unresolved/sink-escapes` at that site, as before.

Names resolve by the object they are bound to, not by their spelling. A module-level name that its
imports, top-level definitions and star imports bind to more than one object (`if ...: from payments
import _send as refund` / `else: from payments import refund`, the same under try/except
ImportError, or `from shadow import *` where `shadow` re-exports `_send` as `refund`) may be any of
them: a call through it is an edge to each scanned one, so the `_send` binding is a `bypass`.

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
parameter annotated with a scanned class (`repo: Repo`, `Optional[Repo]`, `Union[Repo, None]`,
`Repo | None`, `"Repo"`), `cls()` in a `@classmethod` (an instance of the class or a subclass, also
through `x = cls()`), or `self.x` when every assignment to it in the methods of the class and of its
scanned bases is one of those (tuple assignment `self.a, self.b = a, b` included; an inherited
`__init__` or `@property` counts, and a subclass that assigns a different class makes it untyped). Through an annotation or `self.x`, a subclass instance may arrive,
so the call also reaches every scanned subclass's override. Anything else (an unannotated parameter,
a rebinding, unpacking it cannot pair) is a name-matched edge, which the scan reports as unresolved
rather than trusting.

Annotations are trusted: a parameter annotated `repo: Repo` is taken to hold a `Repo` (or a
subclass), whatever a caller passes, because the scan cannot see every caller; a type checker is what
catches `h(OtherRepo())` against `def h(repo: Repo)`. A return annotation is trusted the same way,
except when the function's own body visibly returns an instance of another scanned class
(`def get() -> Safe: return Repo()`), or returns the result of an unannotated scanned function that
visibly does (`def get() -> Safe: return mk()` with `def mk(): return Repo()`, followed up to three
calls deep; a type checker reads `mk()` as `Any` and would not flag it): then the call gives no type
and its method calls are name-matched. What stays trusted, and so needs a type checker in CI: a
parameter annotation (`def go(s: Safe)` called with a `Repo`), a dataclass field or `__init__`
parameter given another class (`H(r=Repo())`, `dataclasses.replace(h, r=Repo())`), a return
annotation over an expression the scan does not type (`-> Safe: return [Repo()][0]`), and a class
annotation (`r: Safe`) over a value set in the class's own methods with a plain assignment.

A name is typed by the bindings of the scope it is looked up in. A binding in a nested function,
lambda or class types that scope only: `repo = Safe()` inside `def inner()` does not type a `repo`
the enclosing function reads (a global, say), nor one a sibling function reads; a nested function
that reads a name it does not bind sees the enclosing function's bindings; one that rebinds it
(`nonlocal repo`) makes it untyped there.

A class whose construction or identity the code can alter is not trusted at all: calls on its
instances, on its name and through `self` in its methods are name-matched (unresolved), never an
extracted edge to its own methods. That is a class (or a subclass of one) with a custom `__new__`
(other than one whose every return is `super().__new__(cls, ...)` or `object.__new__(cls, ...)` on its
first parameter, which it never rebinds: `def __new__(klass, *a, cls=Repo): return
super().__new__(cls)` and `cls = Repo; return object.__new__(cls)` are not trusted); with a
scanned metaclass that defines `__call__`, `__new__` (beyond handing on to `super()`/`type`),
`__getattr__` or `__getattribute__`, or a metaclass the scan cannot see into; with its own
`__getattribute__` (every attribute of an instance comes from it; a `__getattr__` only answers for
names the class lacks, which are name-matched anyway); a `typing.Protocol` (any class with the right
methods satisfies it, and a type checker accepts any of them); one given a virtual subclass
(`Safe.register(Repo)`, ABCMeta's `register`); with a class decorator that may return another class:
only `@dataclasses.dataclass` (bare or called), `@functools.total_ordering`, `@typing.final`,
`@typing.runtime_checkable`, `@typing.type_check_only`, `@enum.unique` and `@enum.verify(...)` are
trusted from outside the scan, and a scanned decorator must return its first parameter and never
rebind it (`def tidy(c): c = Repo; return c` is not trusted), `@register("x")` a nested function that
does; any other decorator (third-party, a lambda, `functools.partial(...)`, any other call) may
return another class; whose module-level name is bound more than once (`Safe = Repo` after the class, an import
over it, `global Safe` in a function: the name then resolves to nothing, as a runtime value); with a
dunder or a computed name written on it (`Leaf.__bases__ = (...)`, `setattr(Safe, name, f)`) or the
class itself rebound from outside (`ops.Safe = Repo`); or whose methods assign `self.__class__`. A
method replaced or deleted on a class (`Safe.save = len`, `del Safe.save`, `setattr(Safe, "save", v)`,
`self.save = v`) distrusts that method only, on the class and the subclasses that inherit it:
`Safe().save()` is name-matched, `Safe().other()` stays typed; a replacement that is a scanned
function or a lambda is read where it is (an entrypoint, or the sink as a value). A
`x.__class__ = Other` on a local makes that local untyped; on `obj.attr`, that attribute; on anything
the scan cannot place (or a `__bases__` write it cannot place), every class is distrusted. A
reflective write of an attribute (`object.__setattr__(self, "r", v)`, `self.__dict__["r"] = v`,
`self.__dict__.update(r=v)` or `.update({"r": v})`) counts as setting it from outside, so `self.r` is
never typed. A write through a method's own `self.__dict__` whose names the scan cannot read
(`self.__dict__.update(kw)`, `self.__dict__[k] = v`, `self.__dict__ |= m`, or `d = self.__dict__` /
`d = vars(self)` and then `d[...] = v`) leaves no attribute of that class (its bases and subclasses
included) typed; the same write on another object (`obj.__dict__.update(m)`) is
`unresolved/computed-attribute`, like `setattr(obj, name, v)`. A data
attribute of a scanned module or class (`other.repo.save()` where `other.repo = Repo()`, or
`Holder.r.save()`) is a value whose type the scan does not follow: its calls are name-matched.

A call to a scanned function annotated to return a scanned class types its result too:
`get_repo().add(...)`, or `r = get_repo()` then `r.add(...)`, where `def get_repo() -> Repo`
(also `Optional[Repo]`, `Repo | None` or a string annotation), and `await get_repo()` on an
`async def`. A method call on a typed receiver types its result the same way, so fluent chains
resolve: `Repo().with_x().save()`, or `repo.with_x().save()` on a local or parameter, where
`with_x` is annotated `-> Repo`, `-> "Repo"` or `-> Self` (the receiver's class); when the method and
the overrides a subclass may bring disagree, the call stays unresolved. That covers factories and
FastAPI dependency helpers: a parameter annotated
`Annotated[Repo, Depends(get_repo)]` is a `Repo`, and so is one annotated with a module-level alias
of it (`RepoDep = Annotated[Repo, Depends(get_repo)]`, bound once, imported or not). The annotation is read in the module that defines the function, and the result
may be a subclass, so overrides are reached as for an annotated parameter. A function with no return
annotation, or one naming more than one class, gives no type.

`with X() as r` (and `async with`) types `r` by the scanned class's `__enter__` (`__aenter__`)
return annotation (`-> Self` is the class itself), and `with session() as r` by a `@contextmanager`
(`@asynccontextmanager`) factory annotated `-> Iterator[Repo]` (`Generator[Repo, ...]`,
`AsyncIterator[Repo]`), a function or a method on a receiver the scan can type (`svc = Service()`
then `async with svc.cm() as r`, or a parameter `svc: Service`). A with target the scan cannot type
stays unresolved.

Generic classes resolve like plain ones: `class ConcreteRepo(BaseRepo[T])` is a subclass of
`BaseRepo` (`Generic[T]` bases are ignored; a `Protocol` class is not trusted, see above), so a call through a `BaseRepo` (or
`BaseRepo[int]`) receiver also reaches `ConcreteRepo.save`. `super().save(x)` (or
`super(Cls, self)`) in a method of class `C` is an extracted edge to the next `save` after `C` in the
method resolution order, through the scanned classes, of `C` and of every scanned subclass of `C`:
`self` may be any of them, so in `class Leaf(Mixin, SinkRepo)` a `super().save(x)` in `Mixin` reaches
`SinkRepo.save`. When no scanned class has it, a subclass has no consistent order, a class in one of
these orders has a base the scan cannot see (a call such as `pick()`, a `getattr()`, `*bases`, an
unresolved name, a third-party or excluded class; builtin and standard-library bases are known), or a
class in an excluded module subclasses `C` or one of its subclasses, the call is name-matched
(unresolved). A module-level alias bound once (`B = SinkRepo`) is resolved to its class, as a base.

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
a class namespace too, as are `C.__mro__[i].__dict__`, `C.__bases__[i].__dict__` and
`C.__base__.__dict__`; any other read of `C.__mro__` is `unresolved/reflection` (its classes are
values). `f.__globals__` on a scanned function is the namespace of its module
(`door.__globals__["sink"]` is `forbidden/getattr-sink`); on a function the scan cannot name it is
`unresolved/reflection`. `builtins.__dict__` (and the `__dict__` of any module outside the scan) is
reflection when the key is computed or names a reflective builtin, and so is `__builtins__`. A
function's `__dict__` is its metadata, except for `__wrapped__`: `door.__dict__["__wrapped__"]`,
`door.__dict__.get("__wrapped__")`, or any other use of a scanned function's `__dict__` (a computed
key, `.values()`, the dict itself) is `forbidden/unwrap`, as are `inspect.unwrap(door)` and
`door.__closure__` (its cells hold the function the guard wraps); `obj.__dict__["__wrapped__"]` on an
object the scan cannot name is an unwrap too. Reading another object's instance `__dict__` stays
harmless (writes through it are described above); a literal key in it that names a sink or door is
`forbidden/getattr-sink` (`-door`), as `getattr(o, "save")` is.

Names looked up from strings are lookups too. `pydoc.locate("pkg.mod.f")` and
`pkgutil.resolve_name("pkg.mod:f")` with a literal are a getattr of that name (naming the sink is
`forbidden/getattr-sink`), `inspect.getattr_static(obj, "name")` is `getattr(obj, "name")`, and
`getattr(builtins, "getattr")` is reflection. `logging.config.dictConfig(config)` imports and calls
the dotted names in its strings (`"()"` factories, `"class"` handlers, `ext://` objects): on a config
the scan can read (a literal, or a module-level name bound once to one) each scanned function or
class it names is a call edge (so a `"()": "pkg.store.sink"` is a bypass); any other config, and
`fileConfig`, `listen`, `pickle.loads` / `load` / `Unpickler` (a pickle names the functions it calls),
`shelve.open`, `runpy.run_path` / `run_module` and `importlib.util.spec_from_file_location`, are
`unresolved/reflection`. So are the other ways to run code from a string or to find objects without
naming them: `string.Formatter` (its `get_field` / `vformat` hand back `"{0.send_to_gateway}"`),
`code.InteractiveInterpreter`, `code.InteractiveConsole` and `code.interact`, `timeit.timeit` /
`repeat` / `Timer` with a `stmt` or `setup` that is not a function (a string is code), `timeit.main`,
`doctest` runners (`testmod`, `testfile`, `run_docstring_examples`, `DocTestRunner`, the suites and
`debug` helpers: docstrings are code the scan does not read), `trace.Trace`, `gc.get_objects` /
`get_referrers` / `get_referents`, `inspect.getmodule`, the frame getters (`sys._getframe`,
`inspect.currentframe`, `inspect.stack`, ...) and any read of a frame's `f_globals`, `f_locals` or
`f_builtins` (so `from payments import send_to_gateway as s` then `sys._getframe().f_globals["s"]` is
reported). A subprocess that runs Python (`subprocess.run`, `Popen`, `call`, `check_output`, ...,
`os.system`, `os.popen`, `os.exec*`, `os.spawn*`, `asyncio.create_subprocess_*` with `sys.executable`,
`sys.argv`, a string containing `python` or a `.py` path in its command) is `unresolved/reflection`:
the child process runs code the scan does not follow from that call; other commands are not reported. `copy.copy` and `copy.deepcopy` are not reported themselves: what a
`__reduce__` returns is (`pickle.loads` or the sink as a value, `getattr`, ...). Assigning a
dunder of a sink or door (`door.__code__ = other.__code__`) is `forbidden/monkeypatch`. Reflective
builtins taken as a value (`g = getattr`, then `g(obj, name)()`; also `setattr`, `eval`, `vars`,
`importlib.import_module`, `operator.methodcaller`, `pickle.loads`, `pydoc.locate`, `inspect.unwrap`)
are `unresolved/reflection`, and an import alias
(`from builtins import getattr as g`) is checked as `getattr` itself. `getattr(repo, "save")` on a
receiver the scan can type is an edge to the methods `repo.save` may name (on an unknown receiver,
name-matched, so unresolved). `operator.attrgetter` and `operator.methodcaller` called with literal
names are getattrs of those names (each part of a dotted `attrgetter("repo.save")`, each of several
names), so naming the sink is `forbidden/getattr-sink`; called with any name that is not a literal
(`attrgetter(name)`, `methodcaller(name, x)`, `attrgetter(*names)`) they are
`unresolved/computed-attribute`, like `getattr(obj, name)`. Any use of `sys.modules`
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

- a new caller of an allowed function (a new script that calls `admin_goodwill`), including one that
  takes it as a value (`g = admin_goodwill; g()`, `Thread(target=admin_goodwill)`): reported where
  the value is taken;
- new code that an allowed function calls and that reaches the sink.

Inside an allowed function, the ways it reaches the sink are excused with it: a direct call, a
`getattr` of the sink name, or the sink used as a value. They are excused only for the frozen chain:
a `getattr(repo, "save")(x)` (or `getattr(Repo, "save")`) in an allowed function is a call edge like
any other, so a new caller that routes through the allowed function is a `bypass` at that site (on a
receiver the scan cannot type, `unresolved/inferred-path`). `forbidden/unwrap`, `forbidden/monkeypatch`,
`unresolved/reflection` and a `getattr` with a computed name (`unresolved/computed-attribute`) in it
are still reported.

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
inv status [file] [--root DIR]... [--exclude GLOB]... [--import MOD]... [--json] [--ablation JSON] [--path DIR]...
```

With no `--root` and no configured roots, `inv scan` and `inv status` scan the rule file's directory
and, when a `pyproject.toml` sits beside the rule file, its source directories (`src/` and the
setuptools `packages.find.where` directories) as roots of their own, so a src-layout project is
scanned under the module names `inv lock` imports it by. A file is named from the deepest root that
holds it. Test code and migrations are skipped by default, at any depth (`tests/`, `test/`,
`testing/`, `test_*.py`, `*_test.py`, `conftest.py`, `migrations/`, `alembic/`): a unit test that calls
a sink directly is not a back door. Scan roots and excludes can
also live in the `pyproject.toml` beside the rule file: `extend_exclude` adds globs to the default
list, and `exclude` replaces it (so `exclude = []` scans the tests too; give both to replace the
defaults and add more). Excluded files are not read, but their module names are
noted: any reference from scanned code into an excluded module (production code calling
`app/testing/helpers.py` or `app/test_fastpath.py`, or using a function or class from one as a value,
a callback, a decorator, a base class or a getattr() target, a star import of one, a router from one
mounted with `include_router`) is `unresolved/excluded-callee`, naming the file and the glob, since the
scan cannot see where it goes. A reference inside a door, a sink or code reached only through them is
not reported, nor is an annotation. A module-level import of an excluded module (`import
app.admin_test`, `from app import admin_test`) is reported even with no later use when that module
runs code at import time ("imports ..., which runs code at import time that the scan does not read"):
excluded modules are read lightly for a decorated function or class (`@router.post`, `@app.get`) or a
top-level call other than constants, dataclass, enum and typing definitions and logging setup. An
import of an excluded module with no such effects stays quiet. Narrow the exclude or move the code.
Directories the scan always skips (`build`, `dist`, `venv`, `node_modules`, `site-packages`,
`__pycache__`, dot-directories) count as excluded the same way when their modules are importable under
the project's own package names (`app/build/jobs.py` is `app.build.jobs`); a virtualenv or a
`build/lib` copy inside a root is third-party code and stays quiet. Tests importing the app are
excluded code calling scanned code, and stay quiet:

```toml
[tool.invariants.scan]
roots = ["src"]
extend_exclude = ["*/legacy/*"]     # the default excludes, and this
```

The lock pins the roots and excludes `inv check` scans with (the `scan` section of
[lockfile.md](lockfile.md)), so adding an exclude, here or with `inv check --exclude`, or dropping a
root is a weakening that a person accepts, like any other. The agent hook `inv hook pretool` also
blocks an agent's edit of `[tool.invariants]`.

`--format json` gives one record per rule (its result, sinks, doors, `allow` and counts), and
`--format sarif` gives SARIF 2.1.0 for code-scanning tools. A rule ablated through `INV_ABLATE` is
not scanned, so a scan inside a falsifier is ablated too. A scan-only rule still needs a `bad` and a
`structure` enforcer.

### Files and roots

- **One name per file, every root's name resolves.** A file several roots hold (the default roots
  `src` and `.` both hold `src/app/x.py`) is indexed once, named from the deepest root that holds it
  (`app.x`, the name `inv lock` imports it by). The name each other root gives it (`src.app.x`) is an
  alias of the same module: an import, a call or a rule target through it resolves to the same code.
  Adding a root may change the name a file is shown under; it never loses a call into it.
- **Relative imports resolve by directory.** `from ..payments import send` in `src/app/sub/ops.py`
  is `app.payments.send` whichever root names the file. A relative import that names no module under
  the roots (a missing file, or one that climbs above every root) is `unresolved/relative-import`
  under every rule, never dropped.
- **Excludes are matched from every root.** A file is left out only when, from every root that holds
  it, an exclude glob matches its path or it lies in an always-skipped directory; so adding a root
  never excludes more (and dropping one, a weakening the lock reports, may). A glob that matches a
  file from some of its roots only leaves it scanned, and `inv scan` prints a note naming the glob and
  the file. Write globs that match from every root (`*/legacy/*`) when the roots overlap.
- **What Python imports wins.** In one directory a package (`ops/__init__.py`) wins over an extension
  module (`ops.*.so`, `ops.pyd`), which wins over `ops.py`, which wins over a sourceless `ops.pyc`;
  across roots, the first root wins. The scan reads the winner; any other scanned file of the same
  module name is `unresolved/shadowed-module` under every rule, since someone may think it runs. A
  stale `.pyc` beside its source is not reported.
- **Code with no source is not read as empty.** An importable module in a root with no Python source
  (an extension module or a sourceless `.pyc`) is `unresolved/no-source` under every rule, and any
  reference into it from scanned code is `unresolved/excluded-callee` (an import of it counts as
  running code). Inside an excluded or always-skipped directory it is treated like the `.py` files
  there.
- **The always-skipped directories are counted.** `inv scan` prints how many files each
  always-skipped directory left out (`skipped (directories the scan always skips): build/ 3 file(s)`;
  `skipped` in `--format json`). The list is `invariants.scan.SKIPPED` (dot-directories, then
  `__pycache__`, `build`, `dist`, `node_modules`, `site-packages`, `venv`).
- **A file in an always-skipped directory that reaches scanned code is reported.** A file there that
  imports any scanned module (`from facade import send`, also through a module that re-exports the
  sink), statically or by a literal dynamic import (`importlib.import_module("payments")`,
  `__import__("payments")`), or that runs or imports code it names at run time (a computed
  `import_module`, `exec`, `runpy`), is `unresolved/skipped-importer` under every rule: run as a
  script, or put on `sys.path`, its calls are not checked. Three kinds of file stay quiet: one that is a
  scanned module itself (reached through a symlink), an exact copy of one (`build/lib/app/x.py` with the
  same bytes as `app/x.py`), and code in a recognised third-party tree (a virtualenv, found by its
  `pyvenv.cfg`; a `site-packages` holding `*.dist-info` or `*.egg-info`; a node package
  `node_modules/<pkg>/` with its `package.json`). Even there, an import of a project module that the
  tree does not itself install is reported, and so is any import of one from a file that writes
  `sys.path`. A hand-made `site-packages/` or `node_modules/` directory is not third-party.
- **Symlinked directories are followed.** A symlink to a directory inside a root is read the way
  Python imports it: `pkg/admin -> ../.vendor/admin` makes `.vendor/admin/tasks.py` the scanned module
  `pkg.admin.tasks`. A link back into its own ancestry is not followed again, each target is entered
  through a link once per root, and a link inside an always-skipped directory is not followed. A
  target outside every root is still scanned under the package that holds the link, and `inv scan`
  prints a note naming the link.
- **An unknown submodule of a scanned package is not third-party.** An import of a module under a
  package the roots hold that no file under the roots is (`import pkg.admin.tasks` with no
  `pkg/admin`, a dangling symlink, `from pkg import admin` where `pkg/__init__.py` binds no `admin`,
  any missing module of a namespace package there) is `unresolved/unknown-module` under every rule. A
  module made at build time (a `_version.py` written by the build) counts: commit it or exclude the
  importer. A package whose `__init__.py` star-imports, defines a module `__getattr__` or writes
  `globals()` may bind any name, so `from pkg import x` from it stays quiet.
- **Archives and import-path writes are reported.** A zip archive in a root (`.zip`, `.egg`, `.whl`,
  `.pyz`) holding Python modules is `unresolved/no-source` under every rule, wherever it lies,
  unless an exclude glob leaves it out or it belongs to a recognised third-party tree; an archive of
  data only stays quiet. Scanned code that changes where Python finds modules is
  `unresolved/reflection` under every rule: a call that changes `sys.path`, `sys.meta_path`,
  `sys.path_hooks` or `sys.path_importer_cache` (any method but a read such as `index`), an
  assignment or deletion of them or of an item, `importlib.invalidate_caches()`, `site.addsitedir()`
  and `zipimport.zipimporter()`. Reading `sys.path` stays quiet.

## Limitations

- `inv scan` builds an approximate call graph. When a method call cannot be resolved, the scan
  reports it as unresolved, never as a bypass.
- It does not follow calls through dependency injection containers, event buses, callbacks or
  values it cannot type (see "Method calls" above). A function that can reach a sink without a door
  and is used as a value is reported where it escapes, not where the value is later called.
- It trusts annotations a type checker would check (see "Method calls" above): run one in CI.
- A child process or code given as a string is reported where it is started, never followed.
- Callers outside the scanned roots are invisible to it.
- It checks which function calls which. It does not read SQL or any other query text: what a
  repository does below its API is the implementation's business.
- Each finding is reported under every scanned rule it affects.
