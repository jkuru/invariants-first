"""@inv.provider and the runtime that binds provider values for one call."""
from __future__ import annotations

from typing import Any, Callable, TypeVar

from .cel import CelError
from .cel.errors import raised
from .model import Provider, Rule
from .registry import Registry

F = TypeVar("F", bound=Callable[..., Any])


def provider(name: str) -> Callable[[F], F]:
    """@inv.provider("approval"): register a read-only provider; the loader checks name, args and return type."""
    def deco(fn: F) -> F:
        fn.__inv_provider__ = name  # type: ignore[attr-defined]
        return fn
    return deco


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
    try:
        hash(v)
        return v
    except Exception:
        return ("id", id(v))            # unhashable: share only the very same object


def _raised(reg: Registry, name: str, e: BaseException) -> ProviderFailed:
    """A provider failure. The exception message (which may hold secrets) only with reg.debug."""
    return ProviderFailed(f"provider {name} raised {type(e).__name__}" + (f": {e}" if reg.debug else ""))


def _typed(prov: Provider, v: Any) -> Any:
    return v if prov.accepts(v) else ProviderFailed(
        f"provider {prov.name} returned {type(v).__name__}, declared {prov.raw['type']}")


Pending = list[tuple[Any, Provider, dict[str, Any]]]


def fetch_providers(reg: Registry, rule: Rule, m: dict[str, str], base: dict[str, Any],
                    cache: dict[Any, Any], pending: Pending | None = None,
                    providers: dict[str, Provider] | None = None) -> dict[str, Any]:
    """Call every provider the names map reads; return {name: value} for this rule.

    Provider args resolve in this rule's binding environment. Values are cached per
    (provider, resolved args) for the call. A provider that raises or returns the wrong
    type gives a value that turns into rule_error when read. pending: collect async provider calls there (prefetch_async awaits
    them) instead of failing them. providers: the provider table of the registry generation the
    call started under (a reload mid-call must not swap it); default reg.providers.
    Never raises: anything that goes wrong becomes a ProviderFailed for that provider.
    """
    table = reg.providers if providers is None else providers
    local, out = dict(base), {}
    for n, p in m.items():
        if not p.startswith("provider."):
            try:
                local[n] = resolve_path(base, p)
            except Exception:
                pass
    for p in m.values():
        if not p.startswith("provider."):
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
    """Fill cache for one names map, awaiting async providers. Sync providers run inline.

    Never raises (a provider's own exception is cached as a ProviderFailed); returns why, when the
    prefetch itself failed, so the caller can turn that rule's evaluation into a rule_error.
    """
    pending: Pending = []
    try:
        fetch_providers(reg, rule, m, base, cache, pending, providers)
    except Exception as e:
        return f"binding providers raised {type(e).__name__}"
    for key, prov, args in pending:
        if key in cache:
            continue
        try:
            cache[key] = _typed(prov, await prov.fn(**args))
        except Exception as e:
            cache[key] = _raised(reg, prov.name, e)
    return None
