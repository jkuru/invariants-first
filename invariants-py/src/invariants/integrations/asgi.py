"""ASGI: answer invariant outcomes as HTTP application/problem+json responses.

The guards themselves are the core's (@inv.guard on the handler); this module only maps the
exception a guard raised to a status and a body. The body names the rule and the outcome,
never the error text, provider failures or evaluated values.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
from typing import Any, Awaitable, Callable, MutableMapping

from ..errors import Escalation, Halted, InvariantError, ReviewNotOpened, RolledBack, Routed
from ..registry import Registry, using

PROBLEM_JSON = "application/problem+json"
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
ASGIApp = Callable[[Scope, Callable[[], Awaitable[Message]], Callable[[Message], Awaitable[None]]], Awaitable[None]]

log = logging.getLogger("invariants.http")

_TITLES = {
    "rejected": "The request violates an invariant",
    "rule-error": "An invariant could not be checked",
    "halted": "An internal invariant failed",
    "routed": "The request was sent for review",
    "escalated": "The request was sent to a person",
    "rolled-back": "The request was rolled back",
    "review-unavailable": "The request needs review, but no review could be opened",
}


@dataclasses.dataclass(frozen=True)
class StatusMap:
    """HTTP status per outcome. reject is 422 by default; 409 is the usual alternative."""

    reject: int = 422
    rule_error: int = 503
    halt: int = 500
    route: int = 202
    escalate: int = 202
    rolled_back: int = 409
    storage: int = 409          # a storage enforcer's refusal (inv.storage_rejection)
    review_unavailable: int = 503   # route/escalate whose review store (and fallback) failed: ReviewNotOpened

    def __post_init__(self) -> None:
        if not 400 <= self.reject <= 499:
            raise ValueError(f"reject status must be a 4xx code, not {self.reject}")


def problem(exc: InvariantError, statuses: StatusMap | None = None,
            expose_messages: bool = False) -> tuple[int, dict[str, Any]]:
    """(status, problem+json body) for a guard's exception.

    Routed and Escalation -> 202 (with review_id); ReviewNotOpened -> 503; a rule_error under
    fail_closed -> 503; Halted -> 500; RolledBack -> 409; a storage rejection -> statuses.storage (409);
    Rejected -> statuses.reject.
    Clause messages are included only with expose_messages (they are rule-file text).
    """
    s = statuses or StatusMap()
    out = exc.outcome
    if isinstance(exc, ReviewNotOpened):
        status, slug = s.review_unavailable, "review-unavailable"
    elif isinstance(exc, Routed):
        status, slug = s.route, "routed"
    elif isinstance(exc, Escalation):
        status, slug = s.escalate, "escalated"
    elif out is not None and out.kind == "rule_error":
        status, slug = s.rule_error, "rule-error"
    elif isinstance(exc, Halted):
        status, slug = s.halt, "halted"
    elif isinstance(exc, RolledBack):
        status, slug = s.rolled_back, "rolled-back"
    elif exc.point == "storage":
        status, slug = s.storage, "rejected"
    else:
        status, slug = s.reject, "rejected"
    body: dict[str, Any] = {"type": f"urn:invariants:{slug}", "title": _TITLES[slug], "status": status}
    if exc.rule is not None:
        body["rule"] = exc.rule
    if out is not None:
        body["outcome"] = out.kind
    if exc.point == "storage":
        body["point"] = "storage"
    if exc.review_id:
        body["review_id"] = exc.review_id
    if expose_messages and out is not None and out.kind == "violation" and out.messages:
        body["violations"] = list(out.messages)
    return status, body


def encode(body: dict[str, Any]) -> bytes:
    """The JSON bytes of a problem body."""
    return json.dumps(body, separators=(",", ":")).encode()


class InvariantMiddleware:
    """Pure ASGI middleware: guard exceptions become problem+json responses.

    With registry=, every request runs against that registry (inv.using). An exception raised
    after the response has started is re-raised: the status can no longer change.
    For FastAPI, prefer integrations.fastapi.install(app): Starlette's error middleware sits
    outside user middleware, so this class must be added with app.add_middleware there.
    """

    def __init__(self, app: ASGIApp, *, registry: Registry | None = None, reject_status: int = 422,
                 statuses: StatusMap | None = None, expose_messages: bool = False) -> None:
        self.app, self.registry, self.expose_messages = app, registry, expose_messages
        self.statuses = statuses or StatusMap(reject=reject_status)

    async def __call__(self, scope: Scope, receive: Callable[[], Awaitable[Message]],
                       send: Callable[[Message], Awaitable[None]]) -> None:
        ctx = using(self.registry) if self.registry is not None else contextlib.nullcontext()
        if scope["type"] != "http":
            with ctx:
                await self.app(scope, receive, send)
            return
        started = False

        async def send_tracked(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            with ctx:
                await self.app(scope, receive, send_tracked)
        except InvariantError as exc:
            if started:
                raise
            status, body = problem(exc, self.statuses, self.expose_messages)
            log.warning("invariant http %s %s -> %s %s", scope.get("method"), scope.get("path"), status, body)
            data = encode(body)
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", PROBLEM_JSON.encode()),
                                    (b"content-length", str(len(data)).encode())]})
            await send({"type": "http.response.body", "body": data})


__all__ = ["PROBLEM_JSON", "StatusMap", "problem", "encode", "InvariantMiddleware"]
