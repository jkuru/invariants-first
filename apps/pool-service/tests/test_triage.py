"""R-7: model triage with a pydantic schema, held-data job facts, abstain, and the edge re-check."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

import invariants as inv
from invariants.testing import make_checked
from pool_service import db, service, store, triage
from pool_service.fake_model import FAULTS, FakeModelClient
from conftest import make_pool

OOM_LOG = "kernel: Out of memory: Killed process 4242 (trainer)"
SLOW_LOG = "worker: deadline exceeded after 600s"
BAD_LOG = "loader: invalid input at record 17"
VAGUE_LOG = "process exited"


def record_job(client: TestClient, job_id: str, *, oom: bool = False, elapsed: int = 10, timeout: int = 600,
               exit_code: int = 1) -> None:
    r = client.post("/jobs", json={"job_id": job_id, "pool_id": "gpu-a", "oom_killed": oom, "elapsed_s": elapsed,
                                   "timeout_s": timeout, "exit_code": exit_code})
    assert r.status_code == 201, r.text


@pytest.fixture
def jobs(client: TestClient) -> TestClient:
    make_pool(client, "gpu-a", 10)
    record_job(client, "J-oom", oom=True, exit_code=137)
    record_job(client, "J-slow", elapsed=600, timeout=600)
    record_job(client, "J-bad", exit_code=65)
    return client


def triage_job(client: TestClient, job_id: str, log: str):
    return client.post(f"/jobs/{job_id}/triage", json={"log_excerpt": log})


@pytest.mark.parametrize("job, log, cause, action", [
    ("J-oom", OOM_LOG, "OUT_OF_MEMORY", "raise_memory_limit"),
    ("J-slow", SLOW_LOG, "TIMEOUT", "extend_timeout"),
    ("J-bad", BAD_LOG, "BAD_INPUT", "quarantine_input"),
])
def test_honest_replies_are_remediated(jobs: TestClient, model: FakeModelClient, job: str, log: str, cause: str,
                                       action: str) -> None:
    r = triage_job(jobs, job, log)
    assert r.status_code == 200, r.text
    assert r.json() == {"job_id": job, "cause": cause, "action": action, "attempts": 1}
    assert len(model.calls) == 1 and model.calls[0]["feedback"] is None


def test_format_errors_are_retried_with_the_parser_error(jobs: TestClient, model: FakeModelClient) -> None:
    model.inject("malformed", "extra_field")
    r = triage_job(jobs, "J-oom", OOM_LOG)
    assert r.status_code == 200 and r.json()["attempts"] == 3
    fb = [c["feedback"] for c in model.calls]
    assert fb[0] is None and "not JSON" in fb[1] and "extra fields forbidden" in fb[2]


def test_pydantic_validation_error_is_fed_back(jobs: TestClient, model: FakeModelClient) -> None:
    model.inject("wrong_enum")
    assert triage_job(jobs, "J-oom", OOM_LOG).status_code == 200
    assert "DISK_FULL" in model.calls[1]["feedback"]


def test_format_retries_exhausted_escalates(jobs: TestClient, model: FakeModelClient, conn: sqlite3.Connection) -> None:
    model.always("missing_field")
    r = triage_job(jobs, "J-oom", OOM_LOG)
    assert r.status_code == 202
    assert r.json()["rule"] == "R-7" and r.json()["outcome"] == "parse_error"
    assert len(model.calls) == 3                     # 1 + format_retries 2
    assert store.remediations(conn, "J-oom") == []


def test_r7_contradicting_reply_escalates(jobs: TestClient, model: FakeModelClient, conn: sqlite3.Connection) -> None:
    """Falsifier: the model says OUT_OF_MEMORY for a job the kernel never OOM-killed. No resample (held data)."""
    model.inject("contradict")
    r = triage_job(jobs, "J-slow", SLOW_LOG)
    body = r.json()
    assert r.status_code == 202 and body.pop("review_id").startswith("review-")    # escalation opens a review
    assert body == {"type": "urn:invariants:escalated", "status": 202, "rule": "R-7", "outcome": "violation",
                    "title": "The request was sent to a person"}
    assert len(model.calls) == 1
    assert store.remediations(conn, "J-slow") == []


def test_abstain_escalates_at_once(jobs: TestClient, model: FakeModelClient) -> None:
    model.inject("abstain")
    r = triage_job(jobs, "J-oom", OOM_LOG)
    assert r.status_code == 202 and r.json()["rule"] == "R-7"
    assert len(model.calls) == 1


def test_vague_log_abstains(jobs: TestClient) -> None:
    assert triage_job(jobs, "J-bad", VAGUE_LOG).status_code == 202


def test_unknown_job_is_404(jobs: TestClient) -> None:
    assert triage_job(jobs, "J-none", OOM_LOG).status_code == 404


def test_missing_job_facts_fail_closed(conn: sqlite3.Connection) -> None:
    with pytest.raises(inv.Escalation) as e:
        triage.propose_cause(conn, "J-none", OOM_LOG)
    assert e.value.outcome.kind == "rule_error"


def _forge(reply_cause: str, verdict: str = "ok") -> triage.CheckedCause:
    """What a careless or hostile caller can do: build a CheckedCause without the model call."""
    reply = inv.parse({"cause": reply_cause, "summary": "forged"}, triage.CauseReply)
    return make_checked(triage.CheckedCause, reply, verdict=verdict, attempts=0, rule="R-7")


def test_r7_forged_checked_refused_at_edge(jobs: TestClient, conn: sqlite3.Connection) -> None:
    """Falsifier: a forged 'ok' CheckedCause that contradicts the job facts is refused by the edge re-check."""
    with pytest.raises(inv.Rejected) as e:
        triage.remediate(conn, _forge("OUT_OF_MEMORY"), "J-slow")
    assert e.value.rule == "R-7"
    assert store.remediations(conn, "J-slow") == []


def test_edge_refuses_a_non_ok_verdict(jobs: TestClient, conn: sqlite3.Connection) -> None:
    with pytest.raises(inv.Rejected):
        triage.remediate(conn, _forge("OUT_OF_MEMORY", verdict="violation"), "J-oom")


def test_edge_accepts_only_checked(jobs: TestClient, conn: sqlite3.Connection) -> None:
    with pytest.raises(TypeError):
        triage.remediate(conn, inv.parse({"cause": "OUT_OF_MEMORY", "summary": "x"}, triage.CauseReply), "J-oom")


def test_fake_model_is_deterministic() -> None:
    a, b = FakeModelClient(), FakeModelClient()
    assert a.complete(OOM_LOG) == b.complete(OOM_LOG)
    with pytest.raises(ValueError):
        a.inject("nonsense")


jobs_st = st.builds(triage.JobFacts, job_id=st.just("J"), pool_id=st.just("p"), oom_killed=st.booleans(),
                    elapsed_s=st.integers(0, 100), timeout_s=st.integers(1, 100),
                    exit_code=st.sampled_from([0, 1, 65, 137]))


@settings(max_examples=120, deadline=None)
@given(facts=jobs_st, log=st.sampled_from([OOM_LOG, SLOW_LOG, BAD_LOG, VAGUE_LOG]),
       faults=st.lists(st.sampled_from(FAULTS), max_size=3))
def test_r7_property_remediation_never_contradicts_facts(facts: triage.JobFacts, log: str,
                                                         faults: list[str]) -> None:
    """Whatever the model says, a remediation is only ever recorded for a cause the facts allow."""
    c = db.memory()
    service.create_pool(c, "p", "P", 1)
    store.insert_job(c, "J", "p", facts.oom_killed, facts.elapsed_s, facts.timeout_s, facts.exit_code)
    m = FakeModelClient()
    m.inject(*faults)
    triage.set_client(m)
    try:
        cause = triage.propose_cause(c, "J", log)
        done = triage.remediate(c, cause, "J")
    except (inv.Escalation, inv.Rejected):
        assert store.remediations(c, "J") == []
        return
    assert len(m.calls) <= 3
    if done.cause == "OUT_OF_MEMORY":
        assert facts.oom_killed
    else:
        assert not facts.oom_killed
    if done.cause == "TIMEOUT":
        assert facts.elapsed_s >= facts.timeout_s
    if done.cause == "BAD_INPUT":
        assert facts.exit_code == 65
