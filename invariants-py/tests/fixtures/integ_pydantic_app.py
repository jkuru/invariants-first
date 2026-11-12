"""Example app for the pydantic integration: R-7 with a pydantic CauseReply (guide 4.2, 4.4)."""
from __future__ import annotations

import dataclasses
import enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

import invariants as inv
from invariants.integrations import pydantic as inv_pd


class Cause(str, enum.Enum):
    TIMEOUT = "TIMEOUT"
    OUT_OF_MEMORY = "OUT_OF_MEMORY"
    BAD_INPUT = "BAD_INPUT"
    CANNOT_COMPLY = "CANNOT_COMPLY"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["scheduler", "kernel", "log"]
    line: int


class CauseReply(BaseModel):
    """Closed: extra fields forbidden; cause is a closed enum (rung 1)."""
    model_config = ConfigDict(extra="forbid")
    cause: Cause
    message: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence]


class OpenReply(BaseModel):
    """Not closed: allows extra fields."""
    cause: str


class LeakyReply(BaseModel):
    """Closed at the top, but a nested model allows extras."""
    model_config = ConfigDict(extra="forbid")
    inner: OpenReply


@dataclasses.dataclass(frozen=True)
class JobFacts:
    job_id: str
    oom_killed: bool
    elapsed_s: int
    timeout_s: int


JOBS = {"J-oom": JobFacts("J-oom", True, 40, 300), "J-slow": JobFacts("J-slow", False, 300, 300)}


@inv.provider("job")
def load_job(job_id: str) -> JobFacts:
    return JOBS[job_id]


class CheckedCause(inv.Checked):
    __slots__ = ()


class ScriptedClient:
    """Stands in for a model client: returns scripted raw replies and records the feedback."""

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.calls: list[str | None] = []

    def script(self, *replies: str) -> None:
        self.replies, self.calls = list(replies), []

    def create(self, prompt: str, feedback: str | None) -> str:
        self.calls.append(feedback)
        return self.replies.pop(0)


client = ScriptedClient()


@inv_pd.model_call("R-P7", schema=CauseReply)
def propose_cause(log_excerpt: str, job_id: str, inv_feedback: str | None = None) -> CheckedCause:
    return client.create(log_excerpt, inv_feedback)


@inv.edge("R-P7")
def remediate(cause: CheckedCause, job_id: str) -> str:
    return f"remediate {job_id}: {cause.cause}"


RULES = {
    "spec": "invariant/v0.1",
    "contracts": {"triage": {"type": "pydantic", "ref": "integ_pydantic_app:CauseReply"}},
    "targets": {"py:integ_pydantic_app.propose_cause": {"effects": "none"}},
    "providers": {"job": {"type": "py:integ_pydantic_app.JobFacts", "from": "py:integ_pydantic_app.load_job",
                          "args": {"job_id": "args.job_id"}, "consistency": "immutable"}},
    "rules": [{
        "id": "R-P7", "rev": 1, "status": "enforce", "class": "law", "owner": "team-sre",
        "statement": "A proposed cause never contradicts what the scheduler and the kernel recorded.",
        "subject": {"contract": "triage", "target": "py:integ_pydantic_app.propose_cause"},
        "bad": [
            {"when": 'reply.cause == "OUT_OF_MEMORY" && !job.oom_killed', "message": "OOM claimed, not OOM-killed"},
            {"when": 'reply.cause != "OUT_OF_MEMORY" && job.oom_killed', "message": "OOM-killed, cause not OOM"},
        ],
        "compares_to": "held_data",
        "model_policy": {"schema": "CauseReply", "format_retries": 2, "semantic_resamples": 0,
                         "feedback": {"format": "parser_error", "semantic": "none"},
                         "abstain": {"field": "cause", "value": "CANNOT_COMPLY"}},
        "enforcers": [
            {"point": "model_call", "timing": "post_effect", "mode": "enforce", "on_violation": "escalate"},
            {"point": "edge", "target": "py:integ_pydantic_app.remediate", "timing": "pre_effect",
             "mode": "enforce", "on_violation": "reject"},
        ],
        "falsified_by": ["tests/test_integrations_pydantic.py::test_held_data_mismatch_escalates"],
    }],
}
