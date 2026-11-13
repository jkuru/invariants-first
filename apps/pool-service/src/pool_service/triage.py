"""LLM triage of failed jobs (R-7).

propose_cause asks the model for a cause. inv.model_call parses the reply into CauseReply,
retries format errors with the parser error fed back, escalates on the abstain value, and
checks the cause against job facts the scheduler recorded (held data, never from the model).
remediate is the only consumer, an @inv.edge that re-checks what it receives.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

import invariants as inv

from . import store
from .fake_model import FakeModelClient

ACTIONS = {"OUT_OF_MEMORY": "raise_memory_limit", "TIMEOUT": "extend_timeout", "BAD_INPUT": "quarantine_input"}


class CauseReply(BaseModel):
    """The closed schema the model must answer in."""

    model_config = ConfigDict(extra="forbid")
    cause: Literal["OUT_OF_MEMORY", "TIMEOUT", "BAD_INPUT", "CANNOT_COMPLY"]
    summary: str = Field(min_length=1, max_length=200)


@dataclasses.dataclass(frozen=True)
class JobFacts:
    """What the scheduler and kernel recorded for a finished job."""

    job_id: str
    pool_id: str
    oom_killed: bool
    elapsed_s: int
    timeout_s: int
    exit_code: int


@inv.provider("job")
def load_job(conn: sqlite3.Connection, job_id: str) -> JobFacts:
    """Job facts from the jobs table; they never change once a job has finished."""
    row = store.get_job(conn, job_id)
    if row is None:
        raise LookupError(f"job {job_id} is not recorded")
    return JobFacts(*row)


class CheckedCause(inv.Checked):
    """A CauseReply that went through R-7."""

    __slots__ = ()


_client = FakeModelClient()


def client() -> FakeModelClient:
    return _client


def set_client(c: FakeModelClient) -> None:
    """Swap the model client (tests, demos)."""
    global _client
    _client = c


def prompt_for(job_id: str, log_excerpt: str) -> str:
    return (f"Job {job_id} failed. Classify the cause as OUT_OF_MEMORY, TIMEOUT, BAD_INPUT or CANNOT_COMPLY "
            f"and answer as JSON {{cause, summary}}.\n--- log ---\n{log_excerpt}")


@inv.model_call("R-7", schema=CauseReply, returns=CheckedCause)
def propose_cause(conn: sqlite3.Connection, job_id: str, log_excerpt: str,
                  inv_feedback: str | None = None) -> str:
    """One model call; returns the raw reply text (the decorator parses and checks it)."""
    return _client.complete(prompt_for(job_id, log_excerpt), feedback=inv_feedback)


@dataclasses.dataclass(frozen=True)
class Remediation:
    job_id: str
    cause: str
    action: str


@inv.edge("R-7")
def remediate(conn: sqlite3.Connection, cause: CheckedCause, job_id: str) -> Remediation:
    """Record the remediation for a checked cause."""
    action = ACTIONS[cause.cause]
    store.insert_remediation(conn, job_id, cause.cause, action)
    return Remediation(job_id, cause.cause, action)
