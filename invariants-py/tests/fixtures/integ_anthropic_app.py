"""A one-rule app whose model_call body talks to an integrations.anthropic model (tests only)."""
from __future__ import annotations

import dataclasses
from typing import Any, Literal

import invariants as inv


@dataclasses.dataclass(frozen=True)
class CauseReply:
    cause: Literal["OUT_OF_MEMORY", "TIMEOUT", "CANNOT_COMPLY"]
    summary: str


class CheckedCause(inv.Checked):
    __slots__ = ()


llm: Any = None          # set by the tests: an AnthropicModel, ReplayModel or RecordingModel

SYSTEM = "You triage failed jobs. Answer with one JSON object {cause, summary} and nothing else."


def prompt_for(job_id: str, log: str) -> str:
    return f"Job {job_id} failed. Classify the cause.\n--- log ---\n{log}"


@inv.model_call("R-A1", schema=CauseReply, returns=CheckedCause)
def propose_cause(job_id: str, log: str, oom_killed: bool, inv_feedback: str | None = None) -> str:
    return llm.complete(prompt_for(job_id, log), inv_feedback)  # type: ignore[no-any-return]


@inv.model_call("R-A1", schema=CauseReply, returns=CheckedCause)
async def apropose_cause(job_id: str, log: str, oom_killed: bool, inv_feedback: str | None = None) -> str:
    return await llm.acomplete(prompt_for(job_id, log), inv_feedback)  # type: ignore[no-any-return]


RULES: dict[str, Any] = {
    "spec": "invariant/v0.1",
    "targets": {"py:integ_anthropic_app.propose_cause": {"effects": "none"},
                "py:integ_anthropic_app.apropose_cause": {"effects": "none"}},
    "rules": [{
        "id": "R-A1", "rev": 1, "status": "enforce", "class": "law", "owner": "team-sre",
        "statement": "A proposed cause never contradicts the kernel's OOM record.",
        "subject": {"target": "py:integ_anthropic_app.propose_cause"},
        "bad": [{"when": 'reply.cause == "TIMEOUT" && args.oom_killed', "message": "OOM-killed, cause not OOM"}],
        "model_policy": {"schema": "CauseReply", "format_retries": 2, "semantic_resamples": 1,
                         "feedback": {"format": "parser_error", "semantic": "none"},
                         "abstain": {"field": "cause", "value": "CANNOT_COMPLY"}},
        "enforcers": [{"point": "model_call", "timing": "post_effect", "mode": "enforce",
                       "on_violation": "escalate"}],
        "falsified_by": ["tests/test_integrations_anthropic.py::test_a_semantic_resample_is_a_fresh_conversation"],
    }],
}
