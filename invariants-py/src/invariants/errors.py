"""Exceptions raised by the loader, verify, and the guards."""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .model import Outcome


class LoadError(Exception):
    """The rule file is invalid: a build failure."""


class CoverageError(Exception):
    """A rule's target does not carry its guard, or a guard names no rule."""


class ParseError(Exception):
    """A model reply does not parse into the closed schema.

    str(e) names the field and the kind of error, never a reply value or a key the reply made up,
    so it is safe in exceptions, events and logs (a model may echo user data). .feedback is the
    full text, with the offending value: the core feeds it back to the model as inv_feedback, and
    outcomes, events and logs use it only with debug on (inv.configure(debug=True)).
    """

    def __init__(self, msg: str = "", feedback: str | None = None) -> None:
        super().__init__(msg)
        self.feedback: str = msg if feedback is None else feedback


class InvariantError(Exception):
    """Base for guard outcomes that stop a call.

    It never carries the values the rule read: .bound is the names they were bound to (sorted), and
    .outcome is the outcome without them. The values go to the review a route or escalate opens (the
    Handoff), never into an exception that a caller may log, serialize or send back.
    """

    def __init__(self, msg: str, outcome: Outcome | None = None, attempts: list[dict[str, Any]] | None = None,
                 review_id: str | None = None, rule: str | None = None, point: str | None = None) -> None:
        super().__init__(msg)
        self.bound: tuple[str, ...] = tuple(sorted(outcome.bound or ())) if outcome is not None else ()
        if outcome is not None and outcome.bound is not None:
            outcome = dataclasses.replace(outcome, bound=None)
        self.outcome, self.attempts, self.review_id = outcome, attempts or [], review_id
        self.rule = rule if rule is not None else (outcome.rule if outcome is not None else None)
        self.point = point


class Rejected(InvariantError):
    """on_violation: reject. The caller gets an error."""


class Halted(InvariantError):
    """on_violation: halt. An internal invariant broke: a bug."""


class Escalation(InvariantError):
    """on_violation: escalate, or an abstain at a model_call whose enforcer is in force (below enforce an
    abstain is recorded and the call goes on with verdict abstain). Carries the attempt record."""


class Routed(InvariantError):
    """on_violation: route. A review was opened (review_id); the call does not proceed."""


class BypassError(InvariantError):
    """inv.doors.install(mode="enforce") (and `pytest --inv-doors`): a path law's sink was reached at run time
    without passing one of its doors. The message names the rule, the sink and the doors; never a value."""


class ReviewNotOpened(InvariantError):
    """route or escalate (or an abstain) could not open a review: the review store failed, and so did
    the review_fallback if one is configured. The call does not proceed, as with Routed/Escalation.

    The work is not lost: .handoff holds the Handoff that was to be stored, .action says route or
    escalate, and __cause__ is the store's exception. Catch it next to Routed and Escalation.
    """

    def __init__(self, msg: str, handoff: Any, **kw: Any) -> None:
        super().__init__(msg, **kw)
        self.handoff, self.action = handoff, getattr(handoff, "action", None)


RAISE: dict[str, type[InvariantError]] = {"reject": Rejected, "halt": Halted, "escalate": Escalation,
                                          "route": Routed}
