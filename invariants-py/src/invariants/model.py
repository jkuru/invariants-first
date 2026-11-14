"""Rule record constants and the loaded-record dataclasses."""
from __future__ import annotations

import dataclasses
import inspect
from typing import Any, Callable

from invariants_cel import Program

DEFAULT_NAMES = frozenset({"args", "result", "reply", "self", "old_self"})
PATH_ROOTS = DEFAULT_NAMES | {"provider"}
FILE_FIELDS = frozenset({"spec", "contracts", "targets", "providers", "rules"})
SPECS = ("invariant/v0.1",)                      # the rule-file versions this library reads
TARGET_FIELDS = frozenset({"effects"})
PROVIDER_FIELDS = frozenset({"type", "from", "args"})
PROVIDER_OPTIONAL = frozenset({"concurrent"})          # may run alongside other opted-in async providers
RULE_FIELDS = frozenset({"id", "rev", "status", "class", "owner", "statement", "why", "subject", "scope", "bind",
                         "bad", "compensable", "enforcers", "mediation", "model_policy", "compares_to",
                         "falsified_by", "evidence", "applies_to"})
ENFORCER_FIELDS = frozenset({"point", "target", "timing", "mode", "on_violation", "on_error", "view", "artifact",
                             "generated", "conformance_test"})
MEDIATION_FIELDS = frozenset({"chokepoint", "residual_paths", "sink", "door", "allow", "allow_unresolved"})
POLICY_FIELDS = frozenset({"schema", "format_retries", "semantic_resamples", "feedback", "abstain"})
EVALUATED = frozenset({"decorator", "middleware", "model_call", "edge"})   # points the SDK evaluates in-process
VIEW_POINTS = EVALUATED | {"monitor"}                          # points that bind names (and may take a view)
GUARD_POINTS = frozenset({"decorator", "middleware"})          # points an @inv.guard can evaluate
ENUMS: dict[str, set[str]] = {
    "class": {"law", "policy"}, "effects": {"none", "external"},
    "status": {"candidate", "contested", "detect", "enforce", "retired"},
    "point": {"type", "gateway", "decorator", "middleware", "model_call", "edge", "build",
              "structure", "monitor"},
    "timing": {"pre_effect", "post_effect"}, "mode": {"off", "detect", "enforce"},
    "on_violation": {"reject", "halt", "escalate", "route"},
    "on_error": {"fail_closed", "fail_open_and_page"},
    "compares_to": {"held_data"},
}
MODES = ("off", "detect", "enforce")                           # off < detect < enforce
CAP = {"candidate": "detect", "contested": "detect", "detect": "detect", "enforce": "enforce", "retired": "off"}
STATUS_RANK = {"candidate": 0, "contested": 0, "detect": 1, "enforce": 2}


def effective_mode(mode: str, status: str) -> str:
    """Effective mode = min(enforcer.mode, cap(status))."""
    return min(mode, CAP[status], key=MODES.index)


@dataclasses.dataclass
class Outcome:
    """One evaluation result: ok | violation | rule_error | parse_error (and abstain: a model call's
    abstain, proceeding below enforce and escalated at it, or an edge given a verdict abstain)."""

    kind: str
    rule: str
    messages: list[str] = dataclasses.field(default_factory=list)
    error: str = ""
    bound: dict[str, Any] | None = dataclasses.field(default=None, repr=False, compare=False)  # names bad read


@dataclasses.dataclass
class Provider:
    """A declared, typed, read-only provider bound to its function."""

    name: str
    raw: dict[str, Any]
    fn: Callable[..., Any]
    type: type
    nullable: bool

    @property
    def is_async(self) -> bool:
        """True for an async def provider (read only by async guards, model calls and edges)."""
        return inspect.iscoroutinefunction(self.fn)

    @property
    def concurrent(self) -> bool:
        """True when this async provider may be awaited alongside other opted-in ones: the record says
        `concurrent: true`, or it says nothing and @inv.provider(..., concurrent=True) registered it."""
        declared = self.raw.get("concurrent")
        if declared is not None:
            return declared is True
        return self.name in getattr(self.fn, "__inv_concurrent__", ())

    def accepts(self, v: Any) -> bool:
        """True if v matches the declared type."""
        if v is None:
            return self.nullable
        return isinstance(v, self.type) and not (self.type in (int, float) and isinstance(v, bool))


@dataclasses.dataclass
class Rule:
    """A loaded rule: the raw record plus what the loader computed."""

    raw: dict[str, Any]
    clauses: list[tuple[Program, str]]                      # evaluated together
    names: set[str]
    target: Any = None
    effects: str = "external"
    snapshot: list[tuple[str, ...]] = dataclasses.field(default_factory=list)   # inferred old_self paths
    names_for: dict[Any, dict[str, str]] = dataclasses.field(default_factory=dict)  # enforcer index | "rule"
    notes: list[str] = dataclasses.field(default_factory=list)
    max_status: str = "candidate"
    semantic_resamples: int = 0
    # the provider and target records the rule reads, as loaded (registry.coverage.rule_digest)
    bound: dict[str, Any] = dataclasses.field(default_factory=dict)
    refs: frozenset[tuple[str, ...]] = frozenset()        # every path the clauses read
    reads: frozenset[tuple[str, ...]] = frozenset()       # what engines convert for them (cel.subset.reads)

    @property
    def id(self) -> str:
        return self.raw["id"]

    @property
    def status(self) -> str:
        return self.raw["status"]

    @property
    def policy(self) -> dict[str, Any] | None:
        return self.raw.get("model_policy")

    @property
    def enforcers(self) -> list[dict[str, Any]]:
        return self.raw.get("enforcers", [])

    @property
    def target_key(self) -> str | None:
        return (self.raw.get("subject") or {}).get("target")

    def mode(self, enf: dict[str, Any]) -> str:
        """Effective mode of one enforcer."""
        return effective_mode(enf["mode"], self.status)

    def enforcer(self, point: str) -> tuple[int | None, dict[str, Any] | None]:
        """The first enforcer at point, with its index."""
        return next(((i, e) for i, e in enumerate(self.enforcers) if e["point"] == point), (None, None))
