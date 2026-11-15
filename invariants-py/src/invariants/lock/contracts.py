"""What every stage of the lock shares: the lock's name and version and the tables that say how each
rule-file field is judged (FIELD_POLICY, RANKS, NOTES). Canonical JSON, its digests, Change (one
explained difference) and the leaf-level JSON diff the stages report through are the schema-diff
library's (invariants_schema_diff.changes). The stages import these from here and there, never from one
another, so pinning, the schema and CEL judgements and the orchestrator stay apart. Pure: it imports
nothing (the scan's default exclude is in rule_diff)."""
from __future__ import annotations



LOCK_NAME = "invariants.lock"
LOCK_VERSION = 3
PY_CONTRACTS = frozenset({"python", "pydantic"})
SCHEMA_CONTRACTS = frozenset({"pydantic", "dataclass", "schema"})

# rule fields that are prose, pinned by digest only; everything else is semantic
DOC_FIELDS = ("statement", "why", "owner", "class")
MEDIATION_DOC = ("chokepoint",)
STATUS_RANK = {"retired": 0, "candidate": 1, "contested": 1, "detect": 2, "enforce": 3}
MODE_RANK = {"off": 0, "detect": 1, "enforce": 2}
VIOLATION_RANK = {"route": 1, "escalate": 1, "reject": 2, "halt": 2}
ERROR_RANK = {"fail_open_and_page": 0, "fail_closed": 1}
EFFECTS_RANK = {"none": 0, "external": 1}
TIMING_RANK = {"post_effect": 0, "pre_effect": 1}
COMPENSABLE_RANK = {"true": 0, "false": 1}
ENFORCER_DEFAULTS = {"on_violation": "reject", "on_error": "fail_closed"}
FEEDBACK_DEFAULT = {"format": "parser_error", "semantic": "none"}

# --- how each field is judged (R10-1) ------------------------------------------------------------
# Every property of schema/invariants.schema.json is listed here, by its place in the schema
# ("rule.bind", "enforcer.view", "modelPolicy.abstain.value", ...); tests/test_fix_round13.py walks the
# schema and fails when one is missing. A field that is not listed is treated as "weakening".
#   weakening  any change may relax the rule (it changes what `bad` reads or how a check runs):
#              refused without --accept-weakening
#   ranked     ordered values (RANKS): a lowering is WEAKENED, a raising is a strengthening
#   more_is_weaker  a number (retries, resamples): going up is WEAKENED, going down is not
#   presence   removing it is WEAKENED, adding it is a strengthening
#   analysed   a dedicated analysis decides (bad polarity, enforcer and clause matching, mediation,
#              residual paths (a removal is WEAKENED: it can raise standing), target coverage,
#              rule identity)
#   neutral    recorded as a change, never a weakening (rev, falsified_by, messages)
#   prose      pinned by digest only, and never stale (statement, why, owner, class): a change is
#              reported for information; mediation.chokepoint is digested too, but analysed
#   computed   not pinned (evidence: computed from the tests)
#   pin        a code pin taken by `inv lock` (signatures, types, schemas): a change, not a weakening,
#              except a pinned schema (or a parameter's Annotated constraints) that now accepts more input
WEAKENING, RANKED, MORE_IS_WEAKER, PRESENCE, ANALYSED, NEUTRAL, PROSE, COMPUTED, PIN = (
    "weakening", "ranked", "more_is_weaker", "presence", "analysed", "neutral", "prose", "computed", "pin")
FIELD_POLICY: dict[str, str] = {
    # the file
    "file.spec": NEUTRAL, "file.contracts": ANALYSED, "file.targets": ANALYSED, "file.providers": ANALYSED,
    "file.rules": ANALYSED,
    # the top-level allow_unresolved is given to each rule it lists before the lock reads the file: the
    # lock judges each rule's mediation.allow_unresolved (fix round 30)
    "file.allow_unresolved": ANALYSED,
    "contract.type": WEAKENING, "contract.ref": WEAKENING, "contract.app": WEAKENING,
    "contract.schema": PIN, "contract.operations": PIN, "contract.hash": PIN, "contract.signature": PIN,
    "contract.types": PIN, "contract.param_schemas": PIN,
    "contract.dialect": PIN,            # an OpenAPI 3.0 / Swagger 2.0 document: its schemas read as Draft 4
    # targets and providers
    "target.effects": RANKED,
    "target.signature": PIN, "target.types": PIN, "target.reply_schema": PIN, "target.param_schemas": PIN,
    "provider.type": WEAKENING, "provider.from": WEAKENING, "provider.args": WEAKENING,
    "provider.signature": PIN, "provider.types": PIN, "provider.param_schemas": PIN,
    "provider.concurrent": NEUTRAL,     # how async providers are awaited, never what bad reads
    # rules
    # the short form (guard, tests) is expanded by loader.expand_short_forms before the lock reads the file:
    # the lock judges the subject.target, enforcer and falsified_by it becomes
    "rule.guard": ANALYSED, "rule.tests": NEUTRAL,
    "rule.door": ANALYSED, "rule.sink": ANALYSED, "rule.allow": ANALYSED,   # short door form
    "rule.allow_unresolved": ANALYSED, "rule.strict": ANALYSED,
    "rule.id": ANALYSED, "rule.rev": NEUTRAL, "rule.status": RANKED,
    "rule.class": PROSE, "rule.owner": PROSE, "rule.statement": PROSE, "rule.why": PROSE,
    "rule.subject": WEAKENING, "rule.subject.contract": WEAKENING, "rule.subject.target": WEAKENING,
    "rule.subject.operation": WEAKENING,
    "rule.scope": WEAKENING, "rule.bind": WEAKENING, "rule.bad": ANALYSED, "rule.compensable": RANKED,
    "rule.enforcers": ANALYSED, "rule.mediation": ANALYSED, "rule.model_policy": ANALYSED,
    "rule.compares_to": PRESENCE, "rule.falsified_by": NEUTRAL, "rule.evidence": COMPUTED,
    "rule.applies_to": ANALYSED,
    "rule.mediation.chokepoint": ANALYSED, "rule.mediation.residual_paths": ANALYSED,
    "rule.mediation.sink": ANALYSED, "rule.mediation.door": ANALYSED,
    "rule.mediation.allow": ANALYSED, "rule.mediation.allow_unresolved": ANALYSED,
    "rule.mediation.strict": ANALYSED,
    "clause.when": ANALYSED, "clause.message": NEUTRAL,
    # enforcers (point and target identify the enforcer: a change is a removal plus an addition)
    "enforcer.point": ANALYSED, "enforcer.target": ANALYSED, "enforcer.timing": RANKED, "enforcer.mode": RANKED,
    "enforcer.on_violation": RANKED, "enforcer.on_error": RANKED, "enforcer.view": WEAKENING,
    "enforcer.artifact": WEAKENING, "enforcer.generated": WEAKENING, "enforcer.conformance_test": WEAKENING,
    # model_policy
    "modelPolicy.schema": WEAKENING, "modelPolicy.format_retries": MORE_IS_WEAKER,
    "modelPolicy.semantic_resamples": MORE_IS_WEAKER, "modelPolicy.feedback": WEAKENING,
    "modelPolicy.feedback.format": WEAKENING, "modelPolicy.feedback.semantic": WEAKENING,
    "modelPolicy.abstain": WEAKENING, "modelPolicy.abstain.field": WEAKENING,
    "modelPolicy.abstain.value": WEAKENING,
}
RANKS: dict[str, dict[str, int]] = {
    "rule.status": STATUS_RANK, "enforcer.mode": MODE_RANK, "enforcer.on_violation": VIOLATION_RANK,
    "enforcer.on_error": ERROR_RANK, "enforcer.timing": TIMING_RANK,
    "target.effects": EFFECTS_RANK, "rule.compensable": COMPENSABLE_RANK,
}
NOTES = {
    "rule.bind": "a binding changes what bad reads",
    "rule.subject": "the rule now checks a different contract, operation or target",
    "rule.scope": "the scope changes which calls or rows bad is evaluated over",
    "enforcer.view": "a view changes what bad reads at this enforcer",
    "enforcer.artifact": "the artifact that enforces the rule changed",
    "enforcer.conformance_test": "a different test now shows the artifact agrees with bad",
    "enforcer.generated": "whether the artifact is generated from bad changed",
    "enforcer.timing": "a post_effect check runs after the effect",
    "provider.args": "the provider now reads different arguments",
    "provider.from": "a different function now supplies the value bad reads",
    "provider.type": "the declared type of the value bad reads changed",
    "modelPolicy.format_retries": "more retries give the model more chances to pass",
    "modelPolicy.semantic_resamples": "more resamples give the model more chances to pass",
    "modelPolicy.schema": "the reply is parsed with a different schema",
    "modelPolicy.feedback": "what the model is told after a failure changed",
    "modelPolicy.abstain": "what counts as an abstention changed",
    "rule.compares_to": "the reply is no longer compared with held data",
    "contract.type": "the contract the rule names is read differently",
    "contract.ref": "the contract the rule names points elsewhere",
    "contract.app": "the OpenAPI document is read from a different app",
}


def policy(where: str) -> str:
    """How a change at a schema location is judged; an unlisted location is a possible weakening."""
    return FIELD_POLICY.get(where, WEAKENING)


MAX_DEFS = 64          # schema classes expanded in one closure; beyond it a $ref is marked truncated
