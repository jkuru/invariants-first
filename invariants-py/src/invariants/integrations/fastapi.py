"""FastAPI: the core guard on route handlers, problem+json answers, and 'openapi' contract checks.

    @router.post("/orders/{region}", operation_id="createOrder")
    @inv_fastapi.guard("py:shop.create_order")
    def create_order(region: str, order: Order) -> Receipt: ...

The guard is @inv.guard, so args are the handler's parameters as FastAPI parsed them (the body
as its pydantic model, path and query params by name) and result is the response model.
pre_effect enforcers run before the handler; post_effect ones run on the response, and the
loader allows them to enforce only on targets declared effects: none.
"""
from __future__ import annotations

import dataclasses
import functools
import inspect
import pathlib
import typing
import weakref
from typing import Any, Callable

import fastapi as _fastapi
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from ..errors import CoverageError, InvariantError
from ..guard import guard as _core_guard
from ..loader import read_document
from ..registry import Registry, current
from .asgi import PROBLEM_JSON, StatusMap, problem

HTTP_METHODS = frozenset({"GET", "PUT", "POST", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"})


def _return_model(fn: Callable[..., Any]) -> type | None:
    try:
        ann = typing.get_type_hints(fn).get("return")
    except Exception:          # unresolvable annotation: leave the result as returned
        return None
    from .pydantic import is_model
    return ann if is_model(ann) else None


def _coerce(fn: Callable[..., Any], model: type) -> Callable[..., Any]:
    def to_model(v: Any) -> Any:
        return v if isinstance(v, model) else getattr(model, "model_validate")(v)

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def inner(*a: Any, **kw: Any) -> Any:
            return to_model(await fn(*a, **kw))
    else:
        @functools.wraps(fn)
        def inner(*a: Any, **kw: Any) -> Any:
            return to_model(fn(*a, **kw))
    return inner


def guard(*keys: str, response_model: type | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """@inv.guard for a route handler; put it under the route decorator.

    When the handler returns a dict and its return annotation (or response_model) is a pydantic
    model, the dict is validated into that model first, so post_effect rules read the response model.
    It evaluates both decorator and middleware enforcers.
    """
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        model = response_model or _return_model(fn)
        return _core_guard(*keys, points=("decorator", "middleware"))(fn if model is None else _coerce(fn, model))
    return deco


def install(app: _fastapi.FastAPI, *, reject_status: int = 422, statuses: StatusMap | None = None,
            expose_messages: bool = False, reg: Registry | None = None) -> None:
    """Answer every guard exception raised in app as application/problem+json (see asgi.problem).

    It also registers the app with reg (default: the current registry), so `inv verify` (and
    inv.verify, --inv-verify, `inv status`) runs verify_routes on it: every rule bound to an
    openapi operation finds exactly one route, guarded for that rule.
    """
    s = statuses or StatusMap(reject=reject_status)
    register(app, reg)

    async def handler(request: _fastapi.Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, InvariantError):        # registered for InvariantError only
            raise exc
        status, body = problem(exc, s, expose_messages)
        return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)

    app.add_exception_handler(InvariantError, handler)


def register(app: _fastapi.FastAPI, reg: Registry | None = None) -> None:
    """Have reg's verify() (inv verify) check app's routes with verify_routes (install does this)."""
    r = reg or current()
    ref = weakref.ref(app)

    def check(registry: Registry) -> None:
        live = ref()
        if live is not None:
            verify_routes(live, {"contracts": registry.contracts}, registry)
    check.__inv_app__ = ref  # type: ignore[attr-defined]
    if not any(getattr(c, "__inv_app__", None) == ref for c in r.verifiers):
        r.verifiers.append(check)


# --- openapi contracts --------------------------------------------------------------------------
def operation_of(rule_raw: dict[str, Any], contracts: dict[str, Any]) -> str | None:
    """The operation a rule binds to: subject.operation, else its openapi contract's operation."""
    subject = rule_raw.get("subject") or {}
    c = contracts.get(str(subject.get("contract")))
    if not (isinstance(c, dict) and c.get("type") == "openapi"):
        return None
    return subject.get("operation") or c.get("operation")


@dataclasses.dataclass(frozen=True)
class Operation:
    """One HTTP operation of an app: full path template, methods, operationId and handler."""

    path: str
    methods: frozenset[str]
    operation_id: str
    endpoint: Callable[..., Any]


def operations(app: _fastapi.FastAPI) -> list[Operation]:
    """Every APIRoute of app, including routes of included routers (with their prefixes)."""
    found: list[tuple[Any, Any]]
    try:
        from fastapi.routing import iter_route_contexts      # FastAPI that includes routers lazily
        found = [(c, c.original_route) for c in iter_route_contexts(app.routes)]
    except ImportError:
        found = [(r, r) for r in app.routes]
    out = []
    for ctx, route in found:
        if not isinstance(route, APIRoute):
            continue
        op_id = getattr(ctx, "operation_id", None) or getattr(ctx, "unique_id", None) or route.unique_id
        endpoint = getattr(ctx, "endpoint", None) or route.endpoint
        path = getattr(ctx, "path", None) or route.path
        if not isinstance(op_id, str) or endpoint is None or not isinstance(path, str):
            continue                                     # not an addressable operation
        out.append(Operation(path, frozenset(getattr(ctx, "methods", None) or ()), op_id, endpoint))
    return out


def find_route(app: _fastapi.FastAPI, operation: str) -> list[Operation]:
    """Operations matching an operationId, or 'METHOD /path' (the full path template as declared)."""
    method, _, path = operation.partition(" ")
    ops = operations(app)
    if method.upper() in HTTP_METHODS and path.strip():
        return [o for o in ops if method.upper() in o.methods and o.path == path.strip()]
    return [o for o in ops if o.operation_id == operation]


def verify_routes(app: _fastapi.FastAPI, source: str | pathlib.Path | dict[str, Any],
                  reg: Registry | None = None) -> int:
    """Every rule bound to an openapi operation finds exactly one route, guarded for that rule.

    When the rule names a target, the route's handler must be that target. Returns the number of
    bound rules; raises CoverageError listing every problem.
    """
    doc = source if isinstance(source, dict) else read_document(source)
    contracts = doc.get("contracts") or {}
    problems, bound = [], 0
    for rule in (reg or current()).rules.values():
        op = operation_of(rule.raw, contracts)
        if op is None or rule.status == "retired":
            continue
        routes = find_route(app, op)
        if len(routes) != 1:
            problems.append(f"{rule.id}: operation {op!r} matches {len(routes)} routes, not one")
            continue
        endpoint = routes[0].endpoint
        keys = getattr(endpoint, "__inv_rules__", ())
        if rule.id not in keys and (rule.target_key is None or rule.target_key not in keys):
            problems.append(f"{rule.id}: the handler of {op!r} is not wrapped by its guard")
        elif rule.target is not None and endpoint is not rule.target:
            problems.append(f"{rule.id}: the handler of {op!r} is not {rule.target_key}")
        else:
            bound += 1
    if problems:
        raise CoverageError("; ".join(problems))
    return bound


__all__ = ["guard", "install", "register", "verify_routes", "operations", "find_route", "operation_of", "Operation",
           "PROBLEM_JSON", "StatusMap", "problem"]
