"""@inv.provider, inv.Dependency, and the runtime that binds provider values for one call."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
from typing import Any, Callable, Generic, Iterator, TypeVar

from .cel import CelError
from .cel.errors import raised
from .model import Provider, Rule
from .registry import Registry

F = TypeVar("F", bound=Callable[..., Any])
T = TypeVar("T")


def provider(name: str, *more: str, concurrent: bool = False) -> Callable[[F], F]:
    """@inv.provider("approval"): register a read-only provider; the loader checks name, args and return type.

    One function may serve several provider names (@inv.provider("subject_known", "object_known"),
    or stacked decorators): each name is declared under providers: with its own args.

    concurrent=True: this async provider may be awaited alongside other async providers that opted
    in too (asyncio.gather). The default runs async providers one at a time, in the order the rule
    reads them, so providers that share a resource that is not safe for concurrent use (one
    SQLAlchemy AsyncSession, one connection) never overlap. The record's `concurrent:` overrides it.
    """
    names = (name, *more)
    if not all(isinstance(n, str) and n for n in names):
        raise TypeError(f"@inv.provider names must be non-empty strings, got {list(names)!r}")
    if not isinstance(concurrent, bool):
        raise TypeError(f"@inv.provider concurrent must be True or False, got {concurrent!r}")

    def deco(fn: F) -> F:
        had: tuple[str, ...] = tuple(getattr(fn, "__inv_providers__", ()))
        every = tuple(dict.fromkeys((*had, *names)))
        fn.__inv_providers__ = every  # type: ignore[attr-defined]
        fn.__inv_provider__ = every[0]  # type: ignore[attr-defined]
        if concurrent:
            fn.__inv_concurrent__ = frozenset(getattr(fn, "__inv_concurrent__", ())) | set(names)  # type: ignore[attr-defined]
        return fn
    return deco


def provider_names(fn: Any) -> tuple[str, ...]:
    """The provider names fn is registered under (empty when it is not a provider)."""
    names = getattr(fn, "__inv_providers__", None)
    if isinstance(names, tuple):
        return names
    one = getattr(fn, "__inv_provider__", None)
    return (one,) if isinstance(one, str) else ()


class Dependency(Generic[T]):
    """A context-local value providers read: a store, a client, a tenant (contextvars underneath).

        STORE: inv.Dependency[Store] = inv.Dependency("store")

        @inv.provider("subject_known")
        def subject_known(subject: str) -> bool:
            return STORE.get().knows(subject)

        with STORE.bound(store):           # around the guarded call, model call or edge
            propose_fact(chunk)

    A provider's arguments are binding paths only; a dependency carries what no path reaches (a
    module-level model call has no self). Bindings are context-local: tasks started inside the
    block and threads started with asyncio.to_thread see them, other requests do not. get()
    outside any binding raises LookupError, which a provider turns into a rule_error.
    """

    __slots__ = ("name", "_var")

    def __init__(self, name: str) -> None:
        self.name = name
        self._var: contextvars.ContextVar[T] = contextvars.ContextVar(f"inv_dependency_{name}")

    def get(self) -> T:
        """The value bound in this context; LookupError when none is."""
        try:
            return self._var.get()
        except LookupError:
            raise LookupError(f"inv.Dependency {self.name!r} is not bound in this context: run the call "
                              f"inside `with <dependency>.bound(value):`") from None

    def set(self, value: T) -> contextvars.Token[T]:
        """Bind value for the rest of this context; returns the token reset() takes."""
        return self._var.set(value)

    def reset(self, token: contextvars.Token[T]) -> None:
        """Undo a set()."""
        self._var.reset(token)

    @contextlib.contextmanager
    def bound(self, value: T) -> Iterator[T]:
        """Bind value for the block, then restore what was bound before."""
        token = self._var.set(value)
        try:
            yield value
        finally:
            self._var.reset(token)

    def __repr__(self) -> str:
        return f"inv.Dependency({self.name!r})"


class ProviderFailed:
    """A provider value that failed; reading it is a rule_error."""

    __slots__ = ("msg",)

    def __init__(self, msg: str) -> None:
        self.msg = msg


def walk(v: Any, parts: Any) -> Any:
    """Follow a path through dict keys or public attributes. Every error is CelError (rule_error)."""
    for p in parts:
        if isinstance(v, ProviderFailed):
            raise CelError(v.msg)
        if isinstance(v, dict):
            try:
                if p not in v:
                    raise CelError(f"no such key {p!r}")
                v = v[p]
            except CelError:
                raise
            except Exception as e:      # a mapping whose lookup raises is a rule_error, never an escape
                raise raised(f"reading key {p!r} on {type(v).__name__}", e) from e
            continue
        if p.startswith("_"):
            raise CelError(f"no such field {p!r} on {type(v).__name__}")
        try:
            v = getattr(v, p)
        except AttributeError:
            raise CelError(f"no such field {p!r} on {type(v).__name__}") from None
        except Exception as e:          # a property that raises is a rule_error, never an escape
            raise raised(f"reading {p!r} on {type(v).__name__}", e) from e
    if isinstance(v, ProviderFailed):
        raise CelError(v.msg)
    return v


def resolve_path(base: dict[str, Any], path: str) -> Any:
    """Resolve 'root.a.b' against a dict of roots."""
    head, *rest = path.split(".")
    if head not in base:
        raise CelError(f"{head} not available here")
    return walk(base[head], rest)


def _arg_key(v: Any) -> Any:
    """A cache key part for one argument value, tagged with its type: 1, True and 1.0 are equal (and
    hash alike) in Python, but they are different arguments to a provider."""
    if type(v) is tuple:
        return (tuple, tuple(_arg_key(x) for x in v))
    if type(v) is frozenset:
        return (frozenset, frozenset(_arg_key(x) for x in v))
    try:
        hash(v)
        return (type(v), v)
    except Exception:
        return ("id", id(v))            # unhashable: share only the very same object


def _raised(reg: Registry, name: str, e: BaseException) -> ProviderFailed:
    """A provider failure. The exception message (which may hold secrets) only with reg.debug."""
    return ProviderFailed(f"provider {name} raised {type(e).__name__}" + (f": {e}" if reg.debug else ""))


def _typed(prov: Provider, v: Any) -> Any:
    return v if prov.accepts(v) else ProviderFailed(
        f"provider {prov.name} returned {type(v).__name__}, declared {prov.raw['type']}")


Pending = list[tuple[Any, Provider, dict[str, Any]]]


def read_by_clauses(rule: Rule, n: str) -> bool:
    """True if a clause of rule reads the name n (a provider bound to a name no clause reads is never called)."""
    names = getattr(rule, "names", None)
    return names is None or n in names


def fetch_providers(reg: Registry, rule: Rule, m: dict[str, str], base: dict[str, Any],
                    cache: dict[Any, Any], pending: Pending | None = None,
                    providers: dict[str, Provider] | None = None) -> dict[str, Any]:
    """Call every provider bound to a name a clause reads; return {name: value} for this rule.

    A provider bound (in bind or a view) to a name no clause reads is not called. Every clause of
    an enforcer is evaluated (there is no short circuit across clauses, and CEL's && and || are
    commutative over errors), so which providers run is fixed by the rule, never by the data.
    Provider args resolve in this rule's binding environment. Values are cached per (provider,
    resolved args, with their types) for the call. A provider that raises or returns the wrong
    type gives a value that turns into rule_error when read. pending: collect async provider calls
    there (prefetch_async awaits them) instead of failing them. providers: the provider table of
    the registry generation the call started under (a reload mid-call must not swap it); default
    reg.providers. Never raises: anything that goes wrong becomes a ProviderFailed for that provider.
    """
    table = reg.providers if providers is None else providers
    local, out = dict(base), {}
    for n, p in m.items():
        if not p.startswith("provider."):
            try:
                local[n] = resolve_path(base, p)
            except Exception:
                pass
    for n, p in m.items():
        if not p.startswith("provider.") or not read_by_clauses(rule, n):
            continue
        name = p.split(".")[1]
        if name in out:
            continue
        prov = table.get(name)
        if prov is None:
            out[name] = ProviderFailed(f"provider {name} is not declared in the rules this call started under")
            continue
        try:
            args = {a: resolve_path(local, ap) for a, ap in prov.raw["args"].items()}
        except CelError as e:
            out[name] = ProviderFailed(f"provider {name}: an argument does not resolve: {e}")
            continue
        except Exception as e:
            out[name] = ProviderFailed(f"provider {name}: resolving an argument raised {type(e).__name__}")
            continue
        key = (name, tuple(sorted((a, _arg_key(v)) for a, v in args.items())))   # names unique: no value compare
        if key not in cache:
            if prov.is_async:
                if pending is not None:
                    pending.append((key, prov, args))
                    continue
                cache[key] = ProviderFailed(f"provider {name} is async; only an async guard, model call or edge "
                                            f"can read it")
            else:
                try:
                    cache[key] = _typed(prov, prov.fn(**args))
                except Exception as e:
                    cache[key] = _raised(reg, name, e)
        out[name] = cache[key]
    return out


async def prefetch_async(reg: Registry, rule: Rule, m: dict[str, str], base: dict[str, Any],
                         cache: dict[Any, Any], providers: dict[str, Provider] | None = None) -> str | None:
    """Fill cache for one names map, awaiting async providers (see prefetch_many). Sync providers run inline.

    Never raises (a provider's own exception is cached as a ProviderFailed); returns why, when the
    prefetch itself failed, so the caller can turn that rule's evaluation into a rule_error.
    """
    return (await prefetch_many(reg, [(rule, m, base)], cache, providers))[0]


async def prefetch_many(reg: Registry, items: list[tuple[Rule, dict[str, str], dict[str, Any]]],
                        cache: dict[Any, Any], providers: dict[str, Provider] | None = None) -> list[str | None]:
    """prefetch_async for several (rule, names map, base) at once: every async provider call they
    need, each distinct one once, in the order the rule file declares the providers. One `why` per item.

    A provider that did not opt in to concurrency is awaited alone; consecutive calls of providers
    that all opted in (`concurrent: true`, or @inv.provider(..., concurrent=True)) are awaited
    together with asyncio.gather. So two providers sharing one AsyncSession never overlap unless
    both say they may."""
    pending: Pending = []
    whys: list[str | None] = []
    for rule, m, base in items:
        try:
            fetch_providers(reg, rule, m, base, cache, pending, providers)
            whys.append(None)
        except Exception as e:
            whys.append(f"binding providers raised {type(e).__name__}")
    todo: dict[Any, tuple[Provider, dict[str, Any]]] = {}
    for key, prov, args in pending:
        if key not in cache and key not in todo:
            todo[key] = (prov, args)
    declared = {n: i for i, n in enumerate(reg.providers if providers is None else providers)}
    todo = dict(sorted(todo.items(), key=lambda kv: declared.get(kv[1][0].name, len(declared))))  # file order

    async def one(prov: Provider, args: dict[str, Any]) -> Any:
        try:
            return _typed(prov, await prov.fn(**args))
        except Exception as e:
            return _raised(reg, prov.name, e)
    batch: list[Any] = []                  # consecutive opted-in calls, awaited together

    async def flush() -> None:
        if batch:
            got = await asyncio.gather(*(one(*todo[k]) for k in batch))
            for k, v in zip(batch, got):
                cache[k] = v
            batch.clear()
    for key, (prov, args) in todo.items():
        if prov.concurrent:
            batch.append(key)
            continue
        await flush()
        cache[key] = await one(prov, args)
    await flush()
    return whys
