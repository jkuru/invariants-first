"""Exceptions raised by the loader, verify, and the guards."""
from __future__ import annotations

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
    """Base for guard outcomes that stop a call."""

    def __init__(self, msg: str, outcome: Outcome | None = None, attempts: list[dict[str, Any]] | None = None,
                 review_id: str | None = None, rule: str | None = None, point: str | None = None) -> None:
        super().__init__(msg)
        self.outcome, self.attempts, self.review_id = outcome, attempts or [], review_id
        self.rule = rule if rule is not None else (outcome.rule if outcome is not None else None)
        self.point = point
        self.bound: dict[str, Any] = dict(outcome.bound or {}) if outcome is not None else {}


class Rejected(InvariantError):
    """on_violation: reject. The caller gets an error."""


class WiringError(Rejected):
    """A transactional target ran outside its declared boundary: a wiring bug, refused whatever on_violation says."""


class Halted(InvariantError):
    """on_violation: halt. An internal invariant broke: a bug."""


class Escalation(InvariantError):
    """on_violation: escalate (and abstain, always). Carries the attempt record."""


class Routed(InvariantError):
    """on_violation: route. A review was opened (review_id); the call does not proceed."""


class ReviewNotOpened(InvariantError):
    """route or escalate (or an abstain) could not open a review: the review store failed, and so did
    the review_fallback if one is configured. The call does not proceed, as with Routed/Escalation.

    The work is not lost: .handoff holds the Handoff that was to be stored, .action says route or
    escalate, and __cause__ is the store's exception. Catch it next to Routed and Escalation.
    """

    def __init__(self, msg: str, handoff: Any, **kw: Any) -> None:
        super().__init__(msg, **kw)
        self.handoff, self.action = handoff, getattr(handoff, "action", None)


class RolledBack(InvariantError):
    """A guard failed inside an @inv.transaction and the caller swallowed it; the SDK rolled back."""


RAISE: dict[str, type[InvariantError]] = {"reject": Rejected, "halt": Halted, "escalate": Escalation,
                                          "route": Routed}
